#!/usr/bin/env python3
"""Check Brightspace without a Claude session, through the JARVIS Chrome profile.

  python tools/autosync.py           # check, apply what is mechanical, report
  python tools/autosync.py --json    # same, machine readable (the server uses this)
  python tools/autosync.py --dry-run # look, change nothing

What it decides for itself, because there is no judgement in it:
  * a dropbox folder that is new, or whose deadline moved
  * `last_sync`

What it hands to Claude instead of guessing:
  * announcements. They are prose, and they carry moved rooms, extra sessions and
    deadline changes that exist nowhere else in the API. Turning those into tasks is
    reading comprehension, so a `kind: "sync"` request is queued with the new ones in it.

Never types a password. If the tab lands on the Microsoft login page it stops and says
the session needs renewing — `python tools/cdp.py login` opens a window for that.
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cdp                      # noqa: E402
import paths                    # noqa: E402
import tasks as tasks_mod       # noqa: E402

ROOT = paths.HOME
RAW = ROOT / "data" / "raw"

# One round trip. Every endpoint the sync needs, returned as one compact JSON string —
# a separate evaluate() per endpoint would mean a fresh websocket each time.
FETCH_JS = r"""
(async () => {
  const V = '%(V)s', OUS = %(OUS)s;
  const g = async p => {
    try { const r = await fetch(p, {credentials:'include'});
          if (!r.ok) return {__err: r.status};
          return await r.json(); }
    catch (e) { return {__err: String(e).slice(0,120)}; }
  };
  const strip = h => (h||'')
    .replace(/<br\s*\/?>/gi,'\n').replace(/<\/p>/gi,'\n')
    .replace(/<[^>]+>/g,' ')
    .replace(/&nbsp;|&#160;/g,' ').replace(/&amp;/g,'&')
    .replace(/&#39;|&rsquo;/g,"'").replace(/&quot;|&ldquo;|&rdquo;/g,'"')
    .replace(/[ \t]+/g,' ').replace(/\n\s*\n+/g,'\n').trim();

  // Every followed course, flattened into one list per kind with the course id on each
  // row. A 401/403 on any of them means the session is gone and is reported as such; any
  // other error (a course with no dropbox tool answers 404) just skips that course.
  let folders = [], grades = [], quizzes = [], news = [], denied = null;
  const skipped = [], toc = {};
  // The latest moment I handed something in to this folder, or null. A group folder
  // answers with the group's entry, which is what counts for a group assignment.
  const mySubmission = async (OU, id) => {
    const s = await g(`/d2l/api/le/${V}/${OU}/dropbox/folders/${id}/submissions/mysubmissions/`);
    if (!Array.isArray(s)) return null;
    const dates = s.flatMap(e => (e.Submissions || []).map(q => q.SubmissionDate)).filter(Boolean);
    return dates.length ? dates.sort().pop() : null;
  };
  for (const OU of OUS) {
    const f = await g(`/d2l/api/le/${V}/${OU}/dropbox/folders/`);
    if (f && (f.__err === 401 || f.__err === 403)) { denied = f; break; }
    if (Array.isArray(f)) {
      const subs = await Promise.all(f.map(x => mySubmission(OU, x.Id)));
      folders = folders.concat(f.map((x, i) => ({
        ou: OU, id: x.Id, name: x.Name, due: x.DueDate,
        pts: x.Assessment && x.Assessment.ScoreDenominator,
        // the teacher's whole note, not a teaser — it is often the only text there is
        instructions: strip(x.CustomInstructions && x.CustomInstructions.Html).slice(0, 4000),
        attachments: (x.Attachments || []).map(a => ({id: a.FileId, name: a.FileName})),
        links: (x.LinkAttachments || []).map(a => ({name: a.LinkName, url: a.Href})),
        submitted: subs[i]
      })));
    } else skipped.push({ou: OU, what: 'dropbox', err: f && f.__err});
    // The brief is not in the folder: it is a document in the course content, next to the
    // link to the folder. Keep just enough of the content tree to find it from Python.
    const t = await g(`/d2l/api/le/${V}/${OU}/content/toc`);
    if (t && Array.isArray(t.Modules)) {
      const rows = [];
      const walk = (m, path) => {
        const here = path.concat([m.ModuleId]);
        for (const x of (m.Topics || [])) rows.push({id: x.TopicId, title: x.Title,
          type: x.TypeIdentifier, item: x.ToolItemId, hidden: !!x.IsHidden, path: here});
        for (const s of (m.Modules || [])) walk(s, here);
      };
      t.Modules.forEach(m => walk(m, []));
      toc[OU] = rows;
    }
    const gr = await g(`/d2l/api/le/${V}/${OU}/grades/`);
    if (Array.isArray(gr)) grades = grades.concat(gr.map(i => ({
      ou: OU, name: i.Name, max: i.MaxPoints, weight: i.Weight })));
    const q = await g(`/d2l/api/le/${V}/${OU}/quizzes/`);
    if (q && q.Objects) quizzes = quizzes.concat(q.Objects.map(x => ({
      ou: OU, name: x.Name, due: x.DueDate })));
    const n = await g(`/d2l/api/le/${V}/${OU}/news/`);
    if (Array.isArray(n)) news = news.concat(n.map(x => ({
      ou: OU, id: x.Id, title: strip(x.Title),
      start: x.StartDate, created: x.CreatedDate,
      body: strip(x.Body && (x.Body.Html || x.Body.Text)).slice(0, 1200)
    })));
  }
  return JSON.stringify({
    host: location.host,
    fetched: new Date().toISOString(),
    folders: denied || folders, grades, quizzes, news, skipped, toc
  });
})()
"""

# Which courses this person is in. Asked once, during setup. The API versions are read
# from the server rather than assumed: another school's Brightspace may be on a different
# release, and a wrong version is a 404 that looks like "you have no courses".
COURSES_JS = r"""
(async () => {
  const g = async p => {
    try { const r = await fetch(p, {credentials:'include'});
          if (!r.ok) return {__err: r.status};
          return await r.json(); }
    catch (e) { return {__err: String(e).slice(0,120)}; }
  };
  const vers = await g('/d2l/api/versions/');
  const latest = code => {
    const row = Array.isArray(vers) ? vers.find(v => v.ProductCode === code) : null;
    return row && row.LatestVersion;
  };
  const lp = latest('lp') || '1.43', le = latest('le') || '%(V)s';
  const who = await g(`/d2l/api/lp/${lp}/users/whoami`);
  let items = [], bookmark = '', err = null;
  for (let page = 0; page < 20; page++) {
    const r = await g(`/d2l/api/lp/${lp}/enrollments/myenrollments/?orgUnitTypeId=3`
                      + (bookmark ? `&bookmark=${encodeURIComponent(bookmark)}` : ''));
    if (!r || r.__err) { err = r ? r.__err : 'empty'; break; }
    items = items.concat(r.Items || []);
    if (!r.PagingInfo || !r.PagingInfo.HasMoreItems) break;
    bookmark = r.PagingInfo.Bookmark;
  }
  return JSON.stringify({
    host: location.host, le, lp, err,
    first_name: who && !who.__err ? (who.FirstName || '') : '',
    courses: items.filter(i => i.OrgUnit && (!i.Access || i.Access.CanAccess !== false))
      .map(i => ({
        org_unit_id: String(i.OrgUnit.Id), name: i.OrgUnit.Name, code: i.OrgUnit.Code,
        active: !i.Access || i.Access.IsActive !== false,
        ends: i.Access && i.Access.EndDate
      }))
  });
})()
"""


NEW_FOLDER_DESC = "New submission folder in Brightspace. The details have not been read yet."
NEW_FOLDER_REQ = "Detected automatically — the description has not been reviewed."
# what 0.1.0 wrote instead, still sitting in testers' task lists
_OLD_TEXT = {
    "Нова папка за предаване в Brightspace. Подробностите не са прочетени.": NEW_FOLDER_DESC,
    "Автоматично засечена папка — описанието не е минало през преценка": NEW_FOLDER_REQ,
}
_OLD_DELIVERABLE = re.compile(r"^Предава се в Brightspace dropbox #(\d+) '(.*)'$", re.S)


def english_placeholders(db) -> int:
    """Rewrite the Bulgarian placeholder text earlier versions stored. Returns how many
    tasks changed. Only exact known strings are touched — never anything a person wrote."""
    changed = 0
    for t in db.get("tasks", []):
        before = json.dumps([t.get("description"), t.get("deliverables"), t.get("requirements")],
                            ensure_ascii=False)
        if t.get("description") in _OLD_TEXT:
            t["description"] = _OLD_TEXT[t["description"]]
        if isinstance(t.get("requirements"), list):
            t["requirements"] = [_OLD_TEXT.get(r, r) for r in t["requirements"]]
        if isinstance(t.get("deliverables"), list):
            t["deliverables"] = [_OLD_DELIVERABLE.sub(r"Hand in through Brightspace dropbox #\1 '\2'", d)
                                 if isinstance(d, str) else d for d in t["deliverables"]]
        after = json.dumps([t.get("description"), t.get("deliverables"), t.get("requirements")],
                           ensure_ascii=False)
        changed += before != after
    return changed


def program_version() -> str:
    try:
        return str(json.loads((paths.APP / "release.json").read_text(encoding="utf-8"))
                   .get("version", ""))
    except (OSError, json.JSONDecodeError):
        return ""


def iso_local(dt_utc: str):
    """D2L hands out UTC with a Z. Store local wall-clock, which is what tasks.py sorts."""
    if not dt_utc:
        return None
    try:
        dt = datetime.fromisoformat(dt_utc.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone().replace(tzinfo=None).isoformat(timespec="minutes")


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:48] or "task"


def fetch():
    """Everything Brightspace will tell us, or a reason why it will not."""
    # Visible, not headless: a headless profile cannot complete the silent SSO refresh
    # and starts answering 403 after about a day. It is minimised, so it stays out of
    # the way without being invisible to Microsoft's sign-in.
    cdp.ensure_session_tab()
    if not cdp.is_up():
        return {"ok": False, "reason": "chrome",
                "message": "Could not start the browser profile."}
    ous = [c["org_unit_id"] for c in cdp.courses()]
    if not ous:
        return {"ok": False, "reason": "setup",
                "message": "No course is selected yet. Finish the setup first."}
    tgt, why = _brightspace_tab()
    if tgt is None:
        return why
    js = FETCH_JS % {"V": cdp.cfg("course", "api_version", default="1.87"),
                     "OUS": json.dumps(ous)}
    raw, err = _evaluate_settled(tgt, js)
    if raw is None:
        return {"ok": False, "reason": "evaluate", "message": str(err)[:200]}
    if not raw:
        return {"ok": False, "reason": "empty", "message": "Brightspace returned nothing."}
    data = json.loads(raw)
    if any(h in (data.get("host") or "") for h in cdp.LOGIN_HOSTS):
        return {"ok": False, "reason": "login",
                "message": "The Brightspace session has expired. Press Sign in."}
    if isinstance(data.get("folders"), dict) and data["folders"].get("__err") in (403, 401):
        return {"ok": False, "reason": "login",
                "message": "Brightspace refused access — a fresh sign-in is needed."}
    data["ok"] = True
    return data


def _brightspace_tab():
    """A tab that is on Brightspace, or (None, the reason there is not one)."""
    host = cdp.bs_host()
    course = cdp.home_url()
    tgt = cdp.find(host)
    if not tgt:
        tgt = cdp.find("login.microsoftonline.com") or (cdp.targets() or [None])[0]
        if tgt is None:
            tgt = cdp.open_tab(course)
        if tgt is None:
            return None, {"ok": False, "reason": "chrome",
                          "message": "The browser is up but gives no tab."}
    if host not in (tgt.get("url") or ""):
        try:
            cdp.navigate(tgt, course, timeout=40)
        except Exception as e:                                   # noqa: BLE001
            return None, {"ok": False, "reason": "navigate", "message": str(e)[:200]}
        tgt = cdp.find(host) or cdp.find("login.") or tgt
    return tgt, None


def _evaluate_settled(tgt, js):
    """Run JS in the Brightspace tab, riding out the SSO redirects.

    A cold profile lands on Brightspace through a redirect chain, and a redirect that
    fires while the call is in flight kills the execution context ("Inspected target
    navigated or closed"). Re-find the tab and try again rather than reporting a failure
    that is really just the page still settling.
    """
    raw, err = None, None
    for attempt in range(3):
        live = cdp.find(cdp.bs_host()) or cdp.find("login.") or tgt
        try:
            raw = cdp.evaluate(live, js, timeout=60)
            break
        except Exception as e:                                   # noqa: BLE001
            err = e
            time.sleep(2 + attempt)
    return raw, err


def my_courses():
    """The signed-in person's courses, for the setup screen. Changes nothing."""
    cdp.ensure_session_tab(minimise=False)
    if not cdp.is_up():
        return {"ok": False, "reason": "chrome", "message": "Could not start the browser."}
    tgt, why = _brightspace_tab()
    if tgt is None:
        return why
    live = cdp.find(cdp.bs_host())
    if not live or any(h in (live.get("url") or "") for h in cdp.LOGIN_HOSTS):
        return {"ok": False, "reason": "login",
                "message": "Not signed in to Brightspace yet."}
    raw, err = _evaluate_settled(
        tgt, COURSES_JS % {"V": cdp.cfg("course", "api_version", default="1.87")})
    if not raw:
        return {"ok": False, "reason": "evaluate", "message": str(err)[:200]}
    data = json.loads(raw)
    if (any(h in (data.get("host") or "") for h in cdp.LOGIN_HOSTS)
            or data.get("err") in (401, 403)):
        return {"ok": False, "reason": "login",
                "message": "Not signed in to Brightspace yet."}
    if data.get("err"):
        return {"ok": False, "reason": "api",
                "message": f"Brightspace would not list the courses ({data['err']})."}
    data["ok"] = True
    return data


# What marks a document as part of an assignment's brief, by its title.
BRIEF_RE = re.compile(r"assess|instruction|assignment|rubric|deliverable|brief|criteria|"
                      r"grading|opdracht|beoordel|toets", re.I)


def _norm(s) -> str:
    return re.sub(r"\W+", " ", str(s or "").lower()).strip()


def brief_documents(folder, topics) -> list:
    """The course documents that hold this assignment's brief, or [] if unsure.

    Brightspace keeps the brief out of the submission folder: teachers put the folder as
    a link in a "Hand in" module and the instruction documents in a sibling module of
    the same block (EFM: "Block I / Hand-in assignments" next to "Block I / Assessment").
    So: find the link to THIS folder, step up to the block it sits in, and take the
    documents in that block whose titles read like a brief.

    Without the link there is no block to look in, and a course-wide guess would put
    another assignment's instructions on this card — so nothing is returned instead.
    """
    topics = topics or []
    link = next((t for t in topics if t.get("type") != "File"
                 and str(t.get("item") or "") == str(folder.get("id"))), None) \
        or next((t for t in topics if t.get("type") != "File"
                 and _norm(t.get("title")) == _norm(folder.get("name"))), None)
    if not link:
        return []
    path = link.get("path") or []
    block = path[:-1] if len(path) > 1 else path
    return [t for t in topics
            if t.get("type") == "File" and not t.get("hidden")
            and BRIEF_RE.search(t.get("title") or "")
            and (t.get("path") or [])[:len(block)] == block][:6]


def folder_url(f) -> str:
    return (f"{cdp.base_url()}/d2l/lms/dropbox/user/folder_submit_files.d2l"
            f"?db={f['id']}&ou={f.get('ou')}")


def auto_fields(f, data, names, fallback_ou) -> dict:
    """Everything an automatically found task shows, built only from what Brightspace says.

    No placeholder text: an empty description is honest, a line saying "not reviewed"
    on every card is noise. The documents and the link are what a person needs to go
    and read the brief themselves.
    """
    ou = str(f.get("ou") or fallback_ou)
    v = cdp.cfg("course", "api_version", default="1.87")
    docs = brief_documents(f, (data.get("toc") or {}).get(ou))
    resources = [{"title": d["title"],
                  "url": f"{cdp.base_url()}/d2l/le/content/{ou}/viewContent/{d['id']}/View"}
                 for d in docs]
    resources += [{"title": a["name"],
                   "url": f"{cdp.base_url()}/d2l/api/le/{v}/{ou}/dropbox/folders/{f['id']}/attachments/{a['id']}"}
                  for a in (f.get("attachments") or []) if a.get("id")]
    resources += [{"title": a.get("name") or a.get("url"), "url": a["url"]}
                  for a in (f.get("links") or []) if str(a.get("url") or "").startswith("http")]
    return {
        "auto": True,
        "description": (f.get("instructions") or "").strip(),
        "requirements": [],
        "resources": resources,
        "source_url": folder_url(dict(f, ou=ou)),
        "source_unit": ou,
        "course": names.get(ou, ""),
    }


def is_auto(t) -> bool:
    """Made by this script rather than written by a person (or by Claude in the chat)."""
    return bool(t.get("auto") or NEW_FOLDER_REQ in (t.get("requirements") or [])
                or t.get("description") == NEW_FOLDER_DESC)


def refresh_auto(data, db) -> list:
    """Bring every automatically found task up to date with its folder, and close the
    ones already handed in. Returns the titles it marked as submitted.

    Tasks a person or Claude wrote are never touched — only `is_auto()` ones. Closing on
    submission is a switch (`sync.close_on_submission`, on in the beta): the owner's own
    list is curated by hand and must not start closing itself.
    """
    names = {c["org_unit_id"]: c["name"] for c in cdp.courses()}
    fallback_ou = str(cdp.cfg("course", "org_unit_id", default="") or "")
    close = bool(cdp.cfg("sync", "close_on_submission", default=False))
    by_id = {int(f["id"]): f for f in (data.get("folders") or [])
             if isinstance(f, dict) and f.get("id") is not None}
    closed = []
    for t in db.get("tasks", []):
        ids = [int(x) for x in (t.get("dropbox_ids") or [])]
        f = by_id.get(ids[0]) if ids else None
        if f is None:
            continue
        if is_auto(t):
            t.update(auto_fields(f, data, names, fallback_ou))
        sub = iso_local(f.get("submitted"))
        if sub:
            t["submitted_at"] = sub
        if not (close and sub):
            continue
        # once per submission: if the person reopens it, a later sync must not undo that
        if t.get("status") in ("done", "dropped") or t.get("closed_for_submission") == sub:
            continue
        t["status"] = "done"
        t["completed"] = sub
        t["closed_for_submission"] = sub
        t.setdefault("submissions", []).append(
            {"at": sub, "what": "Handed in on Brightspace", "status": "submitted"})
        closed.append(t.get("title", ""))
    mark_covered_resits(db)
    return closed


RESIT_RE = re.compile(r"\bresit\b|second opportunity|2nd opportunity|herkansing", re.I)
_SIT_WORDS = re.compile(r"\b(sit|resit|first|second|1st|2nd|opportunity|herkansing|"
                        r"submission|box)\b", re.I)


def _assignment_key(title) -> str:
    """'PPD Mind Movie RESIT (second opportunity)' and 'PPD Mind Movie SIT (first
    opportunity)' are the same assignment: drop the sit/resit words and compare the rest."""
    return _norm(_SIT_WORDS.sub(" ", str(title or "")))


def mark_covered_resits(db):
    """Flag each resit whose first attempt was handed in, as `resit_covered`.

    Such a resit is usually not needed, and leaving it in Overdue made Overdue mean
    nothing. It is NOT closed or dropped: a failed first attempt is exactly when the
    resit matters, and grades are not read here — so the page moves it to its own group
    instead of hiding it.
    """
    handed_in = {}
    for t in db.get("tasks", []):
        if t.get("submitted_at") and not RESIT_RE.search(t.get("title") or ""):
            handed_in[(str(t.get("source_unit") or ""), _assignment_key(t.get("title")))] = t["title"]
    for t in db.get("tasks", []):
        key = (str(t.get("source_unit") or ""), _assignment_key(t.get("title")))
        if RESIT_RE.search(t.get("title") or "") and key in handed_in and not t.get("submitted_at"):
            t["resit_covered"] = handed_in[key]
        else:
            t.pop("resit_covered", None)


def diff(data, db):
    """What changed. Mechanical changes only — announcements are handled separately."""
    # A task claims every folder it owns, so no duplicate task is invented for the resit
    # — but only the FIRST id drives its deadline. Without that split, a task holding
    # both "Group Report - Sit" and "Group Report - Resit" had its due date overwritten
    # by whichever folder was read last, quietly moving the real deadline 11 days out.
    by_folder, primary = {}, {}
    for t in db["tasks"]:
        ids = [int(x) for x in (t.get("dropbox_ids") or [])]
        for fid in ids:
            by_folder[fid] = t
        if ids:
            primary[t["id"]] = ids[0]

    weights = {g["name"]: g for g in (data.get("grades") or [])
               if isinstance(g, dict) and g.get("name")}
    names = {c["org_unit_id"]: c["name"] for c in cdp.courses()}
    fallback_ou = str(cdp.cfg("course", "org_unit_id", default="") or "")

    new_tasks, moved = [], []
    for f in (data.get("folders") or []):
        if not isinstance(f, dict):
            continue
        due = iso_local(f.get("due"))
        t = by_folder.get(int(f["id"]))
        if t is None:
            g = weights.get(f["name"])
            new_tasks.append({
                "id": slug(f["name"]),
                "title": f["name"],
                "type": "assignment",
                "due": due,
                "weight": (f"{g['weight']}% of the grade, {g['max']} points"
                           if g and g.get("weight") else
                           (f"{f['pts']} points" if f.get("pts") else None)),
                "deliverables": [f"Hand in through Brightspace dropbox #{f['id']} '{f['name']}'"],
                "dropbox_ids": [int(f["id"])],
                **auto_fields(f, data, names, fallback_ou),
            })
        elif (due and t.get("due") != due
              and primary.get(t["id"]) == int(f["id"])):
            moved.append({"id": t["id"], "title": t["title"],
                          "from": t.get("due"), "to": due,
                          "folder": f["id"], "folder_name": f["name"]})
    return new_tasks, moved


def fresh_news(data, db):
    """Announcements published since the last check."""
    last = db.get("last_sync")
    try:
        cut = datetime.fromisoformat(last) if last else None
    except (ValueError, TypeError):
        cut = None
    out = []
    for n in (data.get("news") or []):
        if not isinstance(n, dict):
            continue
        start = iso_local(n.get("start"))
        if cut and start and datetime.fromisoformat(start) <= cut:
            continue
        out.append({"id": n.get("id"), "title": n.get("title"),
                    "when": start, "body": n.get("body") or "",
                    "ou": str(n.get("ou") or "")})
    return out


def queue_for_claude(news, moved, lang="en"):
    """Ask a Claude session to read the prose. Returns the request id, or None."""
    import ask as ask_mod
    if ask_mod.ASK.exists():
        for p in ask_mod.ASK.glob("*.json"):
            if p.name.startswith("."):
                continue
            try:
                r = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if r.get("kind") == "sync" and r.get("status") == "pending":
                return r["id"]          # one is enough; do not pile them up
    lines = [
        "SYNC — announcements that autosync could not interpret.",
        "",
        "The mechanical part is already done: deadlines and new submission folders are "
        "merged into data/tasks.json and last_sync is current. What is left is prose.",
        "",
        f"{len(news)} new announcement(s) since the last check:",
    ]
    for n in news:
        lines += ["", f"--- {n['when']}  {n['title']}", n["body"][:900]]
    if moved:
        lines += ["", "Deadlines that moved (already applied):"]
        lines += [f"  {m['title']}: {m['from']} -> {m['to']}" for m in moved]
    lines += [
        "",
        "Turn anything with a date, a time or a room into a task with "
        "`tasks.py upsert --partial`. Ignore the purely motivational ones. Then answer "
        "this request with what actually matters — short, it gets read aloud.",
    ]
    ASKIN = {"text": "\n".join(lines), "lang": lang, "kind": "sync"}
    ask_mod.ASK.mkdir(parents=True, exist_ok=True)
    import hashlib
    qid = (f"{datetime.now():%Y%m%d-%H%M%S}-"
           f"{hashlib.sha1(ASKIN['text'].encode()).hexdigest()[:4]}")
    rec = {"id": qid, "question": ASKIN["text"], "kind": "sync", "lang": lang,
           "about": None, "asked_at": datetime.now().isoformat(timespec="seconds"),
           "status": "pending", "answer": None}
    ask_mod.save(rec)
    return qid


def run(dry_run=False, lang="en"):
    data = fetch()
    if not data.get("ok"):
        return data

    RAW.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    (RAW / f"autosync-{stamp}.json").write_text(
        f"// source: D2L API via the dedicated browser profile\n"
        f"// fetched: {datetime.now().isoformat(timespec='seconds')}\n"
        + json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    db = tasks_mod.load()
    new_tasks, moved = diff(data, db)
    news = fresh_news(data, db)

    result = {
        "ok": True,
        "new_folders": [{"id": t["id"], "title": t["title"], "due": t["due"]} for t in new_tasks],
        "moved": moved,
        "news": [{"title": n["title"], "when": n["when"]} for n in news],
        "queued": None,
        "submitted": [],
        "dry_run": dry_run,
    }
    if dry_run:
        return result

    english_placeholders(db)
    by_id = {t["id"]: t for t in db["tasks"]}
    for m in moved:
        by_id[m["id"]]["due"] = m["to"]
    for t in new_tasks:
        if t["id"] in by_id:                    # slug collision with something known
            t["id"] = f"{t['id']}-{t['dropbox_ids'][0]}"
        t.setdefault("status", "todo")
        t.setdefault("priority", 3)
        t.setdefault("notes", "")
        t["first_seen"] = t["last_seen"] = tasks_mod.now()
        db["tasks"].append(t)
    # after the new ones are in, so a folder found already handed in is closed straight away
    result["submitted"] = refresh_auto(data, db)

    # Announcements are kept as they are. Nothing here interprets them — a person reads
    # them in the News tab. Newest first, capped, and never added twice.
    known = {str(a.get("id")) for a in db.get("announcements", [])}
    fresh_rows = [dict(n, seen=False) for n in news if str(n.get("id")) not in known]
    db["announcements"] = (fresh_rows + db.get("announcements", []))[:60]

    db["last_sync"] = tasks_mod.now()
    # which version of the program did this check — an update changes what a check
    # produces, so the first start after one must not skip it as "checked just now"
    db["sync_version"] = program_version()
    entry = {"at": tasks_mod.now(), "added": len(new_tasks), "updated": len(moved),
             "changes": ([{"id": t["id"], "change": "new", "title": t["title"]} for t in new_tasks]
                         + [{"id": m["id"], "change": "due", "from": m["from"], "to": m["to"]}
                            for m in moved]
                         + [{"change": "submitted", "title": s} for s in result["submitted"]]),
             "note": "autosync (no Claude session)"}
    db["history"] = ([entry] + db.get("history", []))[:30]
    tasks_mod.save(db)

    # Reading prose into tasks needs a model. A copy with no AI behind it does not
    # queue a request nobody will ever answer — the announcement is on screen instead.
    if news and cdp.cfg("features", "ai", default=True):
        result["queued"] = queue_for_claude(news, moved, lang)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--lang", default="en")
    a = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    r = run(dry_run=a.dry_run, lang=a.lang)
    if a.json:
        print(json.dumps(r, ensure_ascii=False))
        sys.exit(0 if r.get("ok") else 1)
    if not r.get("ok"):
        print(f"НЕ мина ({r['reason']}): {r['message']}")
        sys.exit(1)
    if not (r["new_folders"] or r["moved"] or r["news"]):
        print("Нищо ново.")
        return
    for f in r["new_folders"]:
        print(f"нова папка : {f['title']}  ({f['due']})")
    for m in r["moved"]:
        print(f"сменен срок: {m['title']}  {m['from']} -> {m['to']}")
    for n in r["news"]:
        print(f"съобщение  : {n['when']}  {n['title'][:70]}")
    if r["queued"]:
        print(f"\nСъобщенията чакат Claude: {r['queued']}")


if __name__ == "__main__":
    main()
