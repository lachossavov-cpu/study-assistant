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
  const skipped = [];
  for (const OU of OUS) {
    const f = await g(`/d2l/api/le/${V}/${OU}/dropbox/folders/`);
    if (f && (f.__err === 401 || f.__err === 403)) { denied = f; break; }
    if (Array.isArray(f)) folders = folders.concat(f.map(x => ({
      ou: OU, id: x.Id, name: x.Name, due: x.DueDate,
      pts: x.Assessment && x.Assessment.ScoreDenominator,
      instructions: strip(x.CustomInstructions && x.CustomInstructions.Html).slice(0, 700)
    }))); else skipped.push({ou: OU, what: 'dropbox', err: f && f.__err});
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
    folders: denied || folders, grades, quizzes, news, skipped
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
                "description": (f.get("instructions") or "")[:400] or
                               "Нова папка за предаване в Brightspace. Подробностите не са прочетени.",
                "deliverables": [f"Предава се в Brightspace dropbox #{f['id']} '{f['name']}'"],
                "requirements": [
                    "Автоматично засечена папка — описанието не е минало през преценка",
                ],
                "resources": [],
                "source_url": cdp.cfg("course", "lessons_url", default="") or cdp.home_url(),
                "source_unit": str(f.get("ou") or fallback_ou),
                "course": names.get(str(f.get("ou") or fallback_ou), ""),
                "dropbox_ids": [int(f["id"])],
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
        "dry_run": dry_run,
    }
    if dry_run:
        return result

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

    # Announcements are kept as they are. Nothing here interprets them — a person reads
    # them in the News tab. Newest first, capped, and never added twice.
    known = {str(a.get("id")) for a in db.get("announcements", [])}
    fresh_rows = [dict(n, seen=False) for n in news if str(n.get("id")) not in known]
    db["announcements"] = (fresh_rows + db.get("announcements", []))[:60]

    db["last_sync"] = tasks_mod.now()
    entry = {"at": tasks_mod.now(), "added": len(new_tasks), "updated": len(moved),
             "changes": ([{"id": t["id"], "change": "new", "title": t["title"]} for t in new_tasks]
                         + [{"id": m["id"], "change": "due", "from": m["from"], "to": m["to"]}
                            for m in moved]),
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
