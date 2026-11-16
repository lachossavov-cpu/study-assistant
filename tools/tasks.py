#!/usr/bin/env python3
"""Task store for Brightspace work: data/tasks.json.

  python tools/tasks.py upsert incoming.json     # merge scraped tasks, keep my status/notes
  python tools/tasks.py agenda                   # what to do next, ranked
  python tools/tasks.py list --status todo
  python tools/tasks.py show <id>
  python tools/tasks.py set <id> status=done priority=1
  python tools/tasks.py stats
  python tools/tasks.py changes                  # what moved since the last sync
"""
import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import HOME as ROOT  # noqa: E402 - one user's data, not the program folder
# AVANS_DB lets the test suite run against a throwaway database
DB = Path(os.environ.get("AVANS_DB") or ROOT / "data" / "tasks.json")
CONFIG = ROOT / "config.json"

# fields the scraper owns; everything else is mine and survives a re-sync
SCRAPED = {
    "title", "type", "due", "weight", "deliverables", "requirements",
    "resources", "source_url", "source_unit", "description", "i18n",
    # the actual brief: what to make, how long, what must be in it. Lives in the
    # Brightspace documents, not in the dropbox folder, which only says how to hand in.
    "instructions", "instructions_source",
}
MINE = {"status", "priority", "notes", "estimate_h", "started", "completed", "plan_file"}
STATUSES = ("todo", "in_progress", "blocked", "done", "dropped")


# fields that have translations; `title` deliberately does NOT — it has to keep matching
# what Brightspace shows, or you cannot find the task again
TRANSLATABLE = ("description", "deliverables", "requirements", "weight",
                "instructions")


def now():
    return datetime.now().isoformat(timespec="seconds")


def localize(t, lang="en"):
    """Task with translated fields swapped in. English original is the fallback."""
    tr = (t.get("i18n") or {}).get(lang)
    if not tr:
        return t
    out = dict(t)
    for k in TRANSLATABLE:
        if tr.get(k):
            out[k] = tr[k]
    return out


def languages():
    if not CONFIG.exists():
        return {}
    return json.loads(CONFIG.read_text(encoding="utf-8")).get("languages", {})


def load():
    if not DB.exists():
        return {"tasks": [], "last_sync": None, "history": []}
    return json.loads(DB.read_text(encoding="utf-8"))


def save(db):
    DB.parent.mkdir(parents=True, exist_ok=True)
    DB.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_due(t):
    d = t.get("due")
    if not d:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(d.strip()[:len(fmt) + 2].strip(), fmt).date()
        except ValueError:
            continue
    return None


def days_left(t):
    d = parse_due(t)
    return None if d is None else (d - date.today()).days


def rules():
    if CONFIG.exists():
        return json.loads(CONFIG.read_text(encoding="utf-8")).get("priority_rules", {})
    return {}


def score(t):
    """Higher = do it sooner."""
    if t.get("status") in ("done", "dropped"):
        return -100
    r = rules()
    s = 6 - float(t.get("priority", 3))
    dl = days_left(t)
    window = max(int(r.get("due_within_days", 7)), 1)
    if dl is not None:
        if dl < 0:
            s += float(r.get("overdue_boost", 3)) + 5
        elif dl <= window:
            s += 5 - (dl * 5 / window)
    if t.get("weight"):
        s += float(r.get("graded_boost", 2))
    if t.get("status") == "in_progress":
        s += 1.5
    if t.get("status") == "blocked":
        s -= 1
    return round(s, 2)


def due_phrase(t):
    dl = days_left(t)
    if dl is None:
        return "без срок"
    if dl < 0:
        return "ПРОСРОЧЕНА с {} дни".format(-dl)
    if dl == 0:
        return "ДНЕС"
    if dl == 1:
        return "утре"
    return "след {} дни".format(dl)


def label(t):
    status = t.get("status", "todo")
    title = (t.get("title") or "")[:60]
    return "[{:<11}] {:<28} {:<22} {}".format(status, t["id"], due_phrase(t), title)


def cmd_upsert(a):
    db = load()
    incoming = json.loads(Path(a.file).read_text(encoding="utf-8"))
    if isinstance(incoming, dict):
        incoming = incoming.get("tasks", [])
    by_id = {t["id"]: t for t in db["tasks"]}
    added, updated, changes = [], [], []
    for new in incoming:
        tid = new["id"]
        if tid not in by_id:
            new.setdefault("status", "todo")
            new.setdefault("priority", 3)
            new.setdefault("notes", "")
            new["first_seen"] = now()
            new["last_seen"] = now()
            db["tasks"].append(new)
            by_id[tid] = new
            added.append(tid)
            changes.append({"id": tid, "change": "new", "title": new.get("title")})
            continue
        cur = by_id[tid]
        diffs = []
        for k, v in new.items():
            if k in MINE or k in ("first_seen", "last_seen"):
                continue
            if k in SCRAPED and cur.get(k) != v:
                diffs.append({"field": k, "from": cur.get(k), "to": v})
                cur[k] = v
        cur["last_seen"] = now()
        if diffs:
            updated.append(tid)
            changes.append({"id": tid, "change": "updated",
                            "title": cur.get("title"), "diffs": diffs})
    # Only a full sync can conclude that a task has disappeared from Brightspace. A
    # partial merge — a couple of tasks picked out of an announcement — says nothing
    # about the ones it does not mention.
    if not getattr(a, "partial", False):
        seen = {t["id"] for t in incoming}
        for t in db["tasks"]:
            if t["id"] in seen or t.get("status") in ("done", "dropped"):
                continue
            flags = t.setdefault("flags", [])
            if "missing_from_last_sync" not in flags:
                flags.append("missing_from_last_sync")
    db["last_sync"] = now()
    entry = {"at": now(), "added": len(added), "updated": len(updated), "changes": changes}
    db["history"] = ([entry] + db.get("history", []))[:30]
    save(db)
    print(json.dumps({"added": added, "updated": updated, "total": len(db["tasks"])},
                     ensure_ascii=False, indent=2))


def cmd_agenda(a):
    db = load()
    live = [t for t in db["tasks"] if t.get("status") not in ("done", "dropped")]
    live.sort(key=score, reverse=True)
    live = live[:a.limit]
    if a.json:
        print(json.dumps(live, ensure_ascii=False, indent=2))
        return
    if not live:
        print("Няма отворени задачи.")
        return
    for t in live:
        print("{:>6}  {}".format(score(t), label(t)))


def cmd_list(a):
    db = load()
    ts = list(db["tasks"])
    if a.status:
        ts = [t for t in ts if t.get("status") == a.status]
    if a.due_within is not None:
        ts = [t for t in ts if days_left(t) is not None and days_left(t) <= a.due_within]
    ts.sort(key=score, reverse=True)
    if a.json:
        print(json.dumps(ts, ensure_ascii=False, indent=2))
        return
    for t in ts:
        print(label(t))
    print("\n{} задачи".format(len(ts)))


def cmd_show(a):
    db = load()
    for t in db["tasks"]:
        if t["id"] == a.id:
            print(json.dumps(t, ensure_ascii=False, indent=2))
            return
    sys.exit("няма задача с id {}".format(a.id))


def cmd_set(a):
    db = load()
    for t in db["tasks"]:
        if t["id"] != a.id:
            continue
        for pair in a.pairs:
            k, _, v = pair.partition("=")
            if k == "status" and v not in STATUSES:
                sys.exit("status трябва да е едно от {}".format(STATUSES))
            if k in ("priority", "estimate_h"):
                v = float(v) if "." in v else int(v)
            t[k] = v
            if k == "status" and v == "done":
                t["completed"] = now()
            if k == "status" and v == "in_progress" and not t.get("started"):
                t["started"] = now()
        save(db)
        print(json.dumps(t, ensure_ascii=False, indent=2))
        return
    sys.exit("няма задача с id {}".format(a.id))


def cmd_stats(a):
    db = load()
    ts = db["tasks"]
    by = {}
    for t in ts:
        st = t.get("status", "todo")
        by[st] = by.get(st, 0) + 1
    live = [t for t in ts if t.get("status") not in ("done", "dropped")]
    overdue = [t for t in live if days_left(t) is not None and days_left(t) < 0]
    week = [t for t in live if days_left(t) is not None and 0 <= days_left(t) <= 7]
    print(json.dumps({
        "total": len(ts),
        "by_status": by,
        "overdue": [t["id"] for t in overdue],
        "due_this_week": [t["id"] for t in week],
        "last_sync": db.get("last_sync"),
    }, ensure_ascii=False, indent=2))


def cmd_changes(a):
    db = load()
    hist = db.get("history", [])
    if not hist:
        print("Няма история — още няма синхронизация.")
        return
    print(json.dumps(hist[:a.n], ensure_ascii=False, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("upsert")
    s.add_argument("file")
    s.add_argument("--partial", action="store_true",
                   help="merging a few tasks, not a full sync — do not flag the rest as missing")
    s.set_defaults(fn=cmd_upsert)

    s = sub.add_parser("agenda")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_agenda)

    s = sub.add_parser("list")
    s.add_argument("--status")
    s.add_argument("--due-within", type=int)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("show")
    s.add_argument("id")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("set")
    s.add_argument("id")
    s.add_argument("pairs", nargs="+")
    s.set_defaults(fn=cmd_set)

    s = sub.add_parser("stats")
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("changes")
    s.add_argument("-n", type=int, default=1)
    s.set_defaults(fn=cmd_changes)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
