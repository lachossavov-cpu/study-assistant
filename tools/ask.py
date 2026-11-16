#!/usr/bin/env python3
"""Questions asked from the dashboard, waiting for an answer.

  python tools/ask.py                          # what is pending
  python tools/ask.py show <id>
  python tools/ask.py answer <id> --text "..."
  python tools/ask.py answer <id> --file reply.md
  python tools/ask.py all --limit 10
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# The Windows console is cp1252 by default, and this script prints back whatever was
# asked. A question with Cyrillic in it — or just an em dash — killed the watcher on the
# print, so nobody was listening and the queue silently filled up for eighteen days.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import HOME as ROOT  # noqa: E402 - one user's data, not the program folder
# AVANS_ASK lets the test suite point the queue at a throwaway directory
ASK = Path(os.environ.get("AVANS_ASK") or ROOT / "work" / "ask")
# `wait` refreshes this while it is blocking, so the dashboard can tell the difference
# between "checking Brightspace now" and "queued, but nobody is listening". Without it a
# check requested with no Claude session open just sits in the queue looking busy.
BEAT = ASK / ".watcher"
BEAT_STALE_SECONDS = 10.0


def load_all():
    if not ASK.exists():
        return []
    out = []
    for p in sorted(ASK.glob("*.json"), reverse=True):
        if p.name.startswith("."):          # .watcher and friends are not questions
            continue
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def save(rec):
    (ASK / f"{rec['id']}.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")


def find(qid):
    p = ASK / f"{qid}.json"
    if not p.is_file():
        sys.exit(f"няма въпрос {qid}")
    return json.loads(p.read_text(encoding="utf-8"))


def cmd_pending(a):
    rows = [r for r in load_all() if r.get("status") == "pending"]
    if not rows:
        print("Няма чакащи въпроси.")
        return
    for r in rows:
        print(f"{r['id']}  [{r['lang']}]  {r['asked_at']}")
        print(f"    {r['question']}")
        if r.get("about"):
            print(f"    (за задача: {r['about']})")
        print()
    print(f"{len(rows)} чакащи. Отговаря се с:  python tools/ask.py answer <id> --text \"...\"")


def cmd_all(a):
    rows = load_all()[:a.limit]
    for r in rows:
        mark = "✓" if r["status"] == "answered" else "…"
        print(f"{mark} {r['id']}  [{r['lang']}]  {r['question'][:70]}")
    print(f"\n{len(rows)} показани")


def cmd_show(a):
    print(json.dumps(find(a.id), ensure_ascii=False, indent=2))


def cmd_answer(a):
    rec = find(a.id)
    if a.file:
        text = Path(a.file).read_text(encoding="utf-8")
    elif a.text:
        text = a.text
    else:
        text = sys.stdin.read()
    text = text.strip()
    if not text:
        sys.exit("празен отговор")
    rec["answer"] = text
    rec["status"] = "answered"
    rec["answered_at"] = datetime.now().isoformat(timespec="seconds")
    save(rec)
    print(f"отговорен: {rec['id']}  ({len(text)} знака)")
    print("Таблото ще го покаже и прочете при следващата си проверка (до 4 секунди).")


def cmd_wait(a):
    """Block until someone asks something, then print it and exit.

    Run this in the background from a Claude session: the session is woken when it
    exits, answers the question, and starts another wait. That is what makes the
    dashboard feel live without the user having to come to the chat and nudge.
    Exit 0 = a question is waiting, 2 = nothing came within the timeout.
    """
    deadline = time.time() + a.timeout
    try:
        return _wait_loop(a, deadline)
    finally:
        # stop claiming to be listening the moment this exits, whichever way it exits
        try:
            BEAT.unlink(missing_ok=True)
        except OSError:
            pass


def beat():
    """Say 'a watcher is alive' on disk. Never fatal — a missed beat only costs a label."""
    try:
        ASK.mkdir(parents=True, exist_ok=True)
        BEAT.write_text(json.dumps({
            "pid": os.getpid(),
            "beat": datetime.now().isoformat(timespec="seconds"),
        }), encoding="utf-8")
    except OSError:
        pass


def _wait_loop(a, deadline):
    while time.time() < deadline:
        beat()
        pending = sorted((r for r in load_all() if r.get("status") == "pending"),
                         key=lambda r: r.get("asked_at", ""))
        if pending:
            print(f"{len(pending)} question(s) waiting:\n")
            for r in pending:
                # Stamp it as taken. The heartbeat dies with this process — which is
                # one second from now — so without this the dashboard would say
                # "nobody is listening" for the whole time Claude is actually working.
                if not r.get("picked_up_at"):
                    r["picked_up_at"] = datetime.now().isoformat(timespec="seconds")
                    try:
                        save(r)
                    except OSError:
                        pass
                kind = r.get("kind", "question")
                print(f"id:       {r['id']}")
                print(f"kind:     {kind}"
                      + ("   <-- prepare the submission, do NOT press Submit"
                         if kind == "submit" else ""))
                print(f"lang:     {r['lang']}")
                print(f"asked:    {r['asked_at']}")
                if r.get("about"):
                    print(f"about:    {r['about']}")
                print("text:")
                for line in r["question"].splitlines():
                    print(f"  {line}")
                print()
            print('Answer with:  python tools/ask.py answer <id> --text "..."')
            return 0
        time.sleep(a.interval)
    print(f"nothing asked within {a.timeout}s")
    return 2


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("wait", help="block until a question arrives")
    s.add_argument("--timeout", type=int, default=3600)
    s.add_argument("--interval", type=float, default=0.5)
    s.set_defaults(fn=cmd_wait)

    s = sub.add_parser("pending")
    s.set_defaults(fn=cmd_pending)

    s = sub.add_parser("all")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_all)

    s = sub.add_parser("show")
    s.add_argument("id")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("answer")
    s.add_argument("id")
    s.add_argument("--text")
    s.add_argument("--file")
    s.set_defaults(fn=cmd_answer)

    a = p.parse_args()
    if not a.cmd:
        cmd_pending(a)
        return
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()
