#!/usr/bin/env python3
"""JARVIS — local dashboard + voice for the Brightspace task tracker.

Binds to 127.0.0.1 only. Nothing here reaches the internet except edge-tts,
which sends the text to be spoken to Microsoft's voice service.

  python server/app.py            # http://127.0.0.1:8765
  JARVIS_PORT=8799 python server/app.py
"""
import asyncio
import difflib
import hashlib
import json
import mimetypes
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import paths  # noqa: E402

# Inside the packaged app: if a newer version is out, install it and restart NOW, before
# anything listens — so an update lands on the next start, not the one after. Does
# nothing anywhere else (tests, JARVIS.bat). See tools/updater.py.
try:
    import updater  # noqa: E402
    updater.quick_update_and_restart()
except Exception:                                        # noqa: BLE001 - never block a start
    pass

# ROOT is one user's data (config, tasks, work, reports). The program itself is paths.APP.
# They are the same folder in the original checkout and different in a handed-out copy.
ROOT = paths.HOME

import brief as brief_mod  # noqa: E402
import speak as speak_mod  # noqa: E402
import tasks as tasks_mod  # noqa: E402

# Windows has no registry entry for .webmanifest; without this StaticFiles serves it
# as octet-stream and Chrome refuses to install the app.
mimetypes.add_type("application/manifest+json", ".webmanifest")

STATIC = paths.APP / "server" / "static"
AUDIO = ROOT / "reports" / "voice"
AUDIO.mkdir(parents=True, exist_ok=True)
# AVANS_WORK lets the test suite keep its files out of the real work/ folder
WORK = Path(os.environ.get("AVANS_WORK") or ROOT / "work")

app = FastAPI(title="Brightspace assistant")

# What a config with nothing in it still has to answer. A handed-out copy starts from
# config.default.json, but a half-written or hand-edited file must not take the page down.
FALLBACK_LANGS = {
    "en": {"label": "English", "voice": "en-US-AndrewMultilingualNeural",
           "recognition": "en-US", "address": ""},
}


def cfg():
    c = paths.read_config()
    c.setdefault("report", {})
    if not c.get("languages"):
        c["languages"] = FALLBACK_LANGS
    return c


# ---------------------------------------------------------------- who this copy is for

def assistant_name() -> str:
    """What this person called their assistant. JARVIS is only the default."""
    return (cfg().get("assistant", {}).get("name") or "JARVIS").strip()[:24] or "JARVIS"


def ai_on() -> bool:
    """Is there a Claude session behind this copy?

    The original checkout has one — questions, Solve and Submit are queued for it. An
    early-access copy does not, and a button that queues work nobody will ever pick up
    is worse than no button: it looks busy for ever. So those features switch off as a
    group, and the page hides them instead of letting them fail.
    """
    return bool(cfg().get("features", {}).get("ai", True))


def teams_on() -> bool:
    """Is the Teams timetable switched on for this copy? Off until the user connects it."""
    return bool(cfg().get("teams", {}).get("enabled", False))


def followed_courses() -> list:
    import cdp                                        # noqa: PLC0415
    return cdp.courses()


def onboarded() -> bool:
    a = cfg().get("assistant", {})
    if "onboarded" in a:
        return bool(a["onboarded"])
    return bool(followed_courses())                   # the original checkout predates setup


def org_label() -> str:
    """AVANS, from brightspace.avans.nl — the small word next to the assistant's name."""
    import cdp                                        # noqa: PLC0415
    parts = cdp.bs_host().split(":")[0].split(".")
    return (parts[-2] if len(parts) >= 2 else parts[0]).upper()


def is_local(request: Request) -> bool:
    return bool(request.client) and request.client.host in ("127.0.0.1", "::1")


def ui_flags() -> dict:
    """Layout switches for the beta (config `ui`). The owner's config has none, so the
    personal dashboard keeps its own layout."""
    u = cfg().get("ui") or {}
    return {k: bool(u.get(k)) for k in ("english_only", "overdue_tab", "split_sources",
                                        "group_undated")}


def resolve_lang(lang=None):
    c = cfg()
    if ui_flags()["english_only"]:
        return "en"
    known = c.get("languages", {})
    if lang in known:
        return lang
    default = c.get("report", {}).get("language", "en")
    return default if default in known else "en"


# ---------------------------------------------------------------- phrasebook

# Spoken replies to voice commands. Task titles are never translated.
SAY = {
    "en": {
        "none": "You have no open tasks.",
        "most_urgent": "The most urgent is {title}, {when}.",
        "worth": " It is worth {w}.",
        "which": "I did not catch which task you mean. Say more of the title.",
        "closed": "Closed: {title}.",
        "started": "Marked as in progress: {title}.",
        "due_is": "{title} is due {when}{date}.",
        "no_due": "{title} has no announced deadline.",
        "which_when": "I did not catch which task you are asking about.",
        "queued": "Noted. That is a question for Claude — I have queued it, "
                  "and the answer will appear here.",
        "unknown": "I can do: report, most urgent, when is a task due, done with a task, "
                   "and starting a task.",
    },
    "bg": {
        "none": "Нямаш отворени задачи.",
        "most_urgent": "Най-спешното е {title}, {when}.",
        "worth": " Тежи {w}.",
        "which": "Не разбрах коя задача имаш предвид. Кажи повече от заглавието.",
        "closed": "Затворих: {title}.",
        "started": "Отбелязах като започната: {title}.",
        "due_is": "{title} е {when}{date}.",
        "no_due": "{title} няма обявен срок.",
        "which_when": "Не разбрах за коя задача питаш.",
        "queued": "Записах. Това е въпрос за Claude — пуснах го, "
                  "отговорът ще излезе тук.",
        "unknown": "Мога: репорт, най-спешното, кога е дадена задача, готово с задача "
                   "и започвам задача.",
    },
}

# Order matters — first match wins.
INTENTS = {
    "en": {
        "stop": r"\b(stop|quiet|silence|shut up|enough)\b",
        "done": r"\b(done|finished|completed|submitted|handed in)\b",
        "start": r"\b(start|starting|begin|beginning|working on)\b",
        "when": r"\b(when|how long|deadline|due date|due for)\b",
        "next": r"\b(next|most urgent|urgent|burning|top priority|priority)\b",
        "brief": r"\b(report|brief|briefing|status|update me|what do i have|what.s up|what.s waiting|read me)\b",
    },
    "bg": {
        "stop": r"\b(спри|стоп|тихо|млъкни|достатъчно)\b",
        "done": r"\b(готов\w*|приключи\w*|завърши\w*|предадох|направих)\b",
        "start": r"\b(започвам|почвам|хващам се|работя по)\b",
        "when": r"\b(кога|колко време|какъв е срок\w*|до кога)\b",
        "next": r"\b(следващ\w*|най.?спешн\w*|кое гори|какво е спешно)\b",
        "brief": r"\b(репорт|брифинг|доклад|обстановк\w*|какво ме чака|какво предстои|давай|обнови ме|слушам)\b",
    },
}


# ---------------------------------------------------------------- state

def task_view(t, lang, course_names=None):
    loc = tasks_mod.localize(t, lang)
    return {
        "id": t["id"],
        "title": t.get("title", ""),          # never translated — must match Brightspace
        # which course it came from, so several courses do not blur into one list
        "course": t.get("course") or (course_names or {}).get(str(t.get("source_unit", "")), ""),
        "type": t.get("type", ""),
        "status": t.get("status", "todo"),
        "priority": t.get("priority", 3),
        "due": t.get("due"),
        "days_left": tasks_mod.days_left(t),
        "weight": loc.get("weight"),
        "score": tasks_mod.score(t),
        "description": loc.get("description", ""),
        "instructions": loc.get("instructions", ""),
        "instructions_source": t.get("instructions_source", ""),
        "deliverables": loc.get("deliverables", []),
        "requirements": loc.get("requirements", []),
        "resources": t.get("resources", []),
        "source_url": t.get("source_url", ""),
        "submitted_at": t.get("submitted_at"),
        "resit_covered": t.get("resit_covered"),
        "notes": t.get("notes", ""),
        "estimate_h": t.get("estimate_h"),
        "flags": t.get("flags", []),
        "translated": bool((t.get("i18n") or {}).get(lang)),
        "started": t.get("started"),
        "completed": t.get("completed"),
        "submissions": t.get("submissions", []),
    }


@app.get("/api/languages")
def languages():
    c = cfg()
    return {
        "active": resolve_lang(),
        "available": [
            {"code": k, "label": v.get("label", k), "voice": v.get("voice"),
             "recognition": v.get("recognition", k), "note": v.get("note", "")}
            for k, v in c.get("languages", {}).items()
            if not ui_flags()["english_only"] or k == "en"
        ],
    }


@app.get("/api/state")
def state(lang: str | None = Query(None)):
    lang = resolve_lang(lang)
    db = tasks_mod.load()
    n = brief_mod.numbers(db)
    live = sorted((t for t in db["tasks"] if t.get("status") not in ("done", "dropped")),
                  key=tasks_mod.score, reverse=True)
    # The big ring shows the most pressing DATED task, picked with the same score the
    # agenda uses — so the ring and the list never disagree. An overdue task therefore
    # wins over a nearer future one; the front end renders it red as overdue.
    dated = [t for t in live if t.get("due") and tasks_mod.days_left(t) is not None]
    nxt = max(dated, key=tasks_mod.score) if dated else None
    courses = followed_courses()
    names = {str(c["org_unit_id"]): c.get("name", "") for c in courses}
    return {
        "now": datetime.now().isoformat(timespec="seconds"),
        "lang": lang,
        "course": " · ".join(c["name"] for c in courses if c.get("name")),
        "courses_count": len(courses),
        "ui": ui_flags(),
        "assistant": {"name": assistant_name(), "onboarded": onboarded(),
                      "address": brief_mod.address(lang)},
        "features": {"ai": ai_on(), "teams": teams_on()},
        "org": org_label(),
        "schedule": (db.get("schedule") or [])[:40],
        "schedule_sync": db.get("schedule_sync"),
        "announcements": (db.get("announcements") or [])[:30],
        "last_sync": db.get("last_sync"),
        "freshness": freshness(db),
        "counts": {
            "open": n["open"], "done": n["done"],
            "overdue": len(n["overdue"]), "urgent": len(n["urgent"]),
            "week": len(n["week"]), "undated": len(n["undated"]),
        },
        "next_deadline": {"id": nxt["id"], "title": nxt["title"], "due": nxt["due"]} if nxt else None,
        "tasks": [task_view(t, lang, names) for t in live],
        "done": [task_view(t, lang, names) for t in db["tasks"] if t.get("status") == "done"],
    }


def watcher_alive() -> bool:
    """Is a Claude session actually listening for queued requests right now?

    `tools/ask.py wait` refreshes work/ask/.watcher while it blocks. Without this the
    dashboard cannot tell "checking Brightspace" from "queued, and nobody will ever
    pick it up" — and the second one looks exactly like the first while doing nothing.
    """
    beat = ASK / ".watcher"
    try:
        rec = json.loads(beat.read_text(encoding="utf-8"))
        last = datetime.fromisoformat(rec["beat"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        return False
    return (datetime.now() - last).total_seconds() < 10.0


def freshness(db):
    age = brief_mod.sync_age_hours(db)
    return {
        "last_sync": db.get("last_sync"),
        "age_hours": None if age is None else round(age, 2),
        "stale": age is None or age >= brief_mod.stale_after_hours(),
        "stale_after_hours": brief_mod.stale_after_hours(),
        "watcher": watcher_alive(),
    }


@app.get("/api/brief")
def get_brief(lang: str | None = Query(None)):
    lang = resolve_lang(lang)
    return {"lang": lang, "text": brief_mod.build(lang=lang),
            "freshness": freshness(tasks_mod.load())}


def pending_of_kind(kind: str):
    if not ASK.exists():
        return None
    for p in sorted(ASK.glob("*.json"), reverse=True):
        if p.name.startswith("."):          # .watcher heartbeat, not a question
            continue
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if r.get("kind") == kind and r.get("status") == "pending":
            return r
    return None


# ---------------------------------------------------------------- unattended sync
#
# The dashboard has no Brightspace session, but the JARVIS Chrome profile does. This
# runs the D2L calls through it over DevTools, so opening the dashboard actually
# refreshes the data instead of only asking a Claude session to. Claude is still the
# fallback, and still the one that reads the announcements.

AUTOSYNC = {"state": "idle", "at": None, "result": None}
AUTOSYNC_LOCK = threading.Lock()


def autosync_enabled() -> bool:
    """Off switch. Set AVANS_NO_AUTOSYNC=1, or `sync.autosync: false` in config.json.

    The test suite sets the variable: without it every /api/sync in the tests would
    start a real Chrome. It is also the honest answer for anyone who would rather keep
    one browser profile and let a Claude session do the checking.
    """
    if os.environ.get("AVANS_NO_AUTOSYNC", "").strip() not in ("", "0", "false"):
        return False
    try:
        return bool(cfg().get("sync", {}).get("autosync", True))
    except (OSError, json.JSONDecodeError, AttributeError):
        return True


# Brightspace, Teams and bringing the window up all drive the same browser profile. Two
# at once would each try to launch it, and the second launch fails on the profile lock —
# so the ↻ button, which now starts both, makes them take turns.
BROWSER_LOCK = threading.Lock()


def _autosync_worker(lang: str):
    import autosync                                   # noqa: PLC0415 - worker-only
    try:
        with BROWSER_LOCK:
            res = autosync.run(lang=lang)
    except Exception as e:                            # noqa: BLE001 - never kill the thread
        res = {"ok": False, "reason": "crash", "message": str(e)[:300]}
    # Brightspace unreachable from this profile? Then a Claude session driving the
    # user's own Chrome is the remaining way in — queue it rather than going quiet.
    if ai_on() and not res.get("ok") and res.get("reason") in (
            "login", "chrome", "navigate", "evaluate", "empty", "crash"):
        try:
            res["queued"] = queue_sync_for_claude(lang, why=res.get("reason"))
        except Exception:                             # noqa: BLE001
            pass
    with AUTOSYNC_LOCK:
        AUTOSYNC.update(state="done" if res.get("ok") else "failed",
                        at=datetime.now().isoformat(timespec="seconds"), result=res)


TEAMS = {"state": "idle", "at": None, "result": None}
TEAMS_LOCK = threading.Lock()


def _teams_worker():
    import teams as teams_mod                           # noqa: PLC0415
    try:
        with BROWSER_LOCK:
            res = teams_mod.run()
    except Exception as e:                               # noqa: BLE001
        res = {"ok": False, "reason": "crash", "message": str(e)[:300]}
    with TEAMS_LOCK:
        TEAMS.update(state="done" if res.get("ok") else "failed",
                     at=datetime.now().isoformat(timespec="seconds"),
                     result={"ok": res.get("ok"), "reason": res.get("reason"),
                             "message": res.get("message"),
                             "count": len(res.get("events", []))})


def start_teams() -> bool:
    with TEAMS_LOCK:
        if TEAMS["state"] == "running":
            return False
        TEAMS.update(state="running", at=datetime.now().isoformat(timespec="seconds"),
                     result=None)
    threading.Thread(target=_teams_worker, daemon=True).start()
    return True


@app.get("/api/teams")
def teams_status():
    with TEAMS_LOCK:
        return dict(TEAMS)


@app.post("/api/teams/sync", status_code=202)
def teams_sync():
    """Re-read the Teams timetable. Only ever reads the calendar — see tools/teams.py."""
    if not teams_on():
        return {"started": False, "reason": "off"}
    if not autosync_enabled():
        return {"started": False, "reason": "no browser profile"}
    return {"started": start_teams()}


def maybe_start_teams(force: bool, on_start: bool) -> str | None:
    """Check Teams alongside Brightspace. None when Teams is off or was just checked."""
    if not (teams_on() and autosync_enabled()):
        return None
    if on_start and not force:
        last = tasks_mod.load().get("schedule_sync")
        try:
            age_min = (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 60
        except (TypeError, ValueError):
            age_min = None
        if age_min is not None and age_min < brief_mod.sync_on_start_after_minutes():
            return None
    return "started" if start_teams() else "running"


BROWSER_READY = {"at": None, "result": None}


def ensure_browser_async():
    """Bring up the Brightspace tab the checks run through, in the background.

    Called when the dashboard is opened, and deliberately NOT behind the
    `sync_on_start_after_minutes` throttle: the point is that the window exists, which
    is what keeps the Brightspace session alive. Skipping it because a check ran twenty
    minutes ago is how the session quietly dies overnight again.
    """
    if not autosync_enabled():
        return False

    def worker():
        try:
            import cdp
            with BROWSER_LOCK:
                res = cdp.ensure_session_tab()
        except Exception as e:                        # noqa: BLE001
            res = {"error": str(e)[:200]}
        BROWSER_READY.update(at=datetime.now().isoformat(timespec="seconds"), result=res)
    threading.Thread(target=worker, daemon=True).start()
    return True


def start_autosync(lang: str) -> bool:
    """Kick off a check in the background. False if one is already running."""
    with AUTOSYNC_LOCK:
        if AUTOSYNC["state"] == "running":
            return False
        AUTOSYNC.update(state="running",
                        at=datetime.now().isoformat(timespec="seconds"), result=None)
    threading.Thread(target=_autosync_worker, args=(lang,), daemon=True).start()
    return True


@app.get("/api/autosync")
def autosync_status():
    with AUTOSYNC_LOCK:
        return dict(AUTOSYNC)


@app.post("/api/cdp/login", status_code=202)
def cdp_login():
    """Open the JARVIS Chrome profile in a visible window so the user can sign in.

    The Brightspace session in that profile expires in about a day, so this is not a
    one-off — it is a thing that needs doing again, and hunting for a terminal command
    every morning is how a feature quietly stops being used. Opens a window and nothing
    more: no credential ever passes through here.
    """
    if not autosync_enabled():
        # No JARVIS profile in play, so there is nothing to sign into — and without this
        # the test suite would kill the real profile and pop a window on every run.
        return {"opening": False, "reason": "autosync is off"}

    def worker():
        try:
            import cdp
            cdp.login()
        except Exception:                             # noqa: BLE001
            pass
    threading.Thread(target=worker, daemon=True).start()
    return {"opening": True}


@app.post("/api/sync", status_code=201)
def request_sync(lang: str | None = Query(None), force: bool = Query(False),
                 on_start: bool = Query(False)):
    """Ask Claude to check Brightspace for anything new.

    The dashboard cannot do this itself — it has no Brightspace session, only Chrome
    does. So a report on stale data queues this, the watcher wakes Claude, and the
    answer comes back with whatever changed. One pending request is enough; pressing
    Report twice must not queue two syncs.

    Three ways in, and they judge "is this worth a check" differently:
      force=true     the ↻ button — always, even on fresh data
      on_start=true  the dashboard was just opened — a much shorter threshold, because
                     opening it is the moment you start trusting what it says
      neither        a report built on stale data, using `stale_after_hours`
    """
    lang = resolve_lang(lang)
    db = tasks_mod.load()
    fresh = freshness(db)
    if on_start:
        # Before any throttling: opening the dashboard always brings the browser up.
        ensure_browser_async()
    # Teams is checked by the same button. It waits its turn for the browser (see
    # BROWSER_LOCK) and has its own "checked just now" clock.
    teams = maybe_start_teams(force, on_start) if (force or on_start) else None
    if not force:
        age_h = fresh["age_hours"]
        if on_start:
            floor_h = brief_mod.sync_on_start_after_minutes() / 60.0
            if age_h is not None and age_h < floor_h:
                return {"teams": teams, "queued": None, "reason": "checked just now",
                        "browser": "opening", "freshness": fresh}
        elif not fresh["stale"]:
            return {"teams": teams, "queued": None, "reason": "already fresh", "freshness": fresh}
    # Autosync runs FIRST, and runs even when a request is already queued for Claude.
    # Ordering these the other way round reproduced the bug this exists to kill: a
    # request nobody ever picked up sat in the queue for two weeks, and because one was
    # "already waiting" the check kept being skipped while the data went stale.
    trigger = ("the dashboard being opened — the user is sitting down to work right now"
               if on_start else "the ↻ button" if force else
               "a report built on stale data")
    if not autosync_enabled() and not ai_on():
        return {"teams": teams, "queued": None, "reason": "nothing can check: autosync is off and "
                "this copy has no Claude session", "freshness": fresh}
    if not autosync_enabled():
        # No unattended path — a Claude session is the only way in, so ask for one now.
        waiting = pending_of_kind("sync")
        if waiting:
            return {"teams": teams, "queued": waiting["id"], "already": True,
                    "picked_up": bool(waiting.get("picked_up_at")),
                    "autosync": "off", "freshness": fresh}
        return {"teams": teams, "queued": queue_sync_for_claude(lang, why="disabled", trigger=trigger),
                "already": False, "picked_up": False,
                "autosync": "off", "freshness": fresh}
    started = start_autosync(lang)
    waiting = pending_of_kind("sync") if ai_on() else None
    return {"teams": teams, 
        "queued": waiting["id"] if waiting else None,
        "already": bool(waiting),
        # Picked up = a watcher handed it to Claude and exited, so the heartbeat is gone
        # even though the work is under way. Without this an in-progress check looks
        # abandoned.
        "picked_up": bool(waiting and waiting.get("picked_up_at")),
        "autosync": "started" if started else "running",
        "freshness": fresh,
    }


def queue_sync_for_claude(lang: str, why: str | None = None,
                          trigger: str | None = None) -> str | None:
    """Ask a Claude session to do the check, when autosync could not.

    Returns the request id, or the id of the one already waiting.
    """
    waiting = pending_of_kind("sync")
    if waiting:
        return waiting["id"]
    fresh = freshness(tasks_mod.load())
    age = fresh["age_hours"]
    reason = {
        "login": "the JARVIS Chrome profile is signed out, so autosync could not read "
                 "anything. Use the user's own Chrome, which is signed in.",
        "chrome": "the JARVIS Chrome profile would not start.",
        "disabled": "the unattended check is switched off (AVANS_NO_AUTOSYNC or "
                    "sync.autosync in config.json).",
    }.get(why or "", f"autosync failed ({why}).")
    lines = [
        "SYNC REQUEST — check Brightspace for anything new.",
        "",
        f"Last checked: {fresh['last_sync'] or 'never'}"
        + (f" ({age:.0f} hours ago)" if age else ""),
        f"Triggered by: {trigger or 'the dashboard'}",
        f"Why you and not autosync: {reason}",
        "",
        "Run the avans-sync skill against the live Chrome session. Cover all four, not "
        "just the first:",
        "  1. content/toc — new or changed materials",
        "  2. dropbox/folders — assignments and their deadlines",
        "  3. calendar events and quizzes",
        "  4. news/ — ANNOUNCEMENTS. These carry sessions, room changes and deadline "
        "changes that exist nowhere else in the API.",
        "",
        "Merge anything new with `tasks.py upsert --partial` so untouched tasks are not "
        "flagged as missing. Then answer this request with what actually changed — or "
        "with 'nothing new' if nothing did. Do not pad it.",
    ]
    if why == "login":
        lines += ["",
                  "Also tell the user to run `python tools/cdp.py login` and sign in "
                  "once, so the unattended check works again."]
    rec = ask(AskIn(text="\n".join(lines), lang=lang, kind="sync"))
    return rec["id"]


# ---------------------------------------------------------------- voice

class SpeakIn(BaseModel):
    text: str | None = None
    lang: str | None = None
    voice: str | None = None
    rate: str | None = None


# text waiting to be streamed, by cache key — filled by /api/speak, drained by /api/speech
SPEECH_JOBS: dict[str, tuple[str, str, str]] = {}

# last synthesis failure, so the dashboard can say WHY instead of "Voice failed"
VOICE_TROUBLE: dict | None = None


def describe_voice_error(exc) -> dict:
    """Turn a synthesis failure into something a person can act on.

    The voice went silent for a month because Microsoft started answering edge-tts with
    403, and all the dashboard said was "Voice failed: NotSupportedError". The raw
    exception is useless to the person staring at a silent dashboard; the fix is one
    command, so say the command.
    """
    text = f"{type(exc).__name__}: {exc}"
    if "403" in text and "speech.platform" in text:
        return {
            "why": "The voice service refused the connection (403).",
            "hint": "Microsoft changed its signing scheme. Update the voice library: "
                    "python -m pip install --upgrade edge-tts, then restart the server.",
            "detail": text[:300],
        }
    if isinstance(exc, (ConnectionError, TimeoutError)) or "Cannot connect" in text:
        return {
            "why": "Could not reach the voice service.",
            "hint": "Check the internet connection — the voice is the only part that needs one.",
            "detail": text[:300],
        }
    return {"why": "Speech synthesis failed.",
            "hint": "The full trace is in reports/server.log.", "detail": text[:300]}


@app.get("/api/voice/health")
async def voice_health():
    """Try to synthesise two words and say plainly what happened.

    The dashboard calls this only when playback has already failed, so the cost of the
    probe is paid only when something is actually wrong.
    """
    c = cfg()
    lang = resolve_lang()
    voice = c["languages"][lang]["voice"]
    agen = speak_mod.stream_synth("Test.", voice, "+0%")
    got = 0
    try:
        async for data in agen:
            got += len(data)
            if got > 2000:
                break
        return {"ok": True, "voice": voice, "bytes": got}
    except Exception as exc:
        return {"ok": False, "voice": voice, **describe_voice_error(exc)}
    finally:
        await agen.aclose()


@app.post("/api/speak")
async def speak(body: SpeakIn):
    """Hand back a URL immediately; the audio is produced while it plays.

    Synthesis runs at about the speed of speech, so a fifty-second answer used to mean
    fifty seconds of silence before anything happened. Now the response returns at once
    and the bytes stream out of /api/speech as they are made.
    """
    c = cfg()
    lang = resolve_lang(body.lang)
    text = (body.text or brief_mod.build(lang=lang)).strip()
    if not text:
        raise HTTPException(400, "nothing to speak")
    voice = body.voice or c["languages"][lang]["voice"]
    rate = body.rate or c["report"].get("rate", "+0%")
    clean = speak_mod.strip_markdown(text)
    key = hashlib.sha1(f"{voice}|{rate}|{clean}".encode("utf-8")).hexdigest()[:16]

    out = AUDIO / f"{key}.mp3"
    if out.exists():
        return {"url": f"/audio/{out.name}", "text": text, "voice": voice,
                "lang": lang, "cached": True}

    SPEECH_JOBS[key] = (clean, voice, rate)
    return {"url": f"/api/speech/{key}.mp3", "text": text, "voice": voice,
            "lang": lang, "cached": False}


@app.get("/api/speech/{name}")
async def speech_stream(name: str):
    key = name[:-4] if name.endswith(".mp3") else name
    job = SPEECH_JOBS.get(key)
    if not job:
        # already streamed once and cached, or an id we never issued
        done = AUDIO / f"{key}.mp3"
        if done.is_file():
            return FileResponse(done, media_type="audio/mpeg")
        raise HTTPException(404, "no such speech job")
    text, voice, rate = job

    async def gen():
        global VOICE_TROUBLE
        buf = bytearray()
        complete = False
        try:
            async for data in speak_mod.stream_synth(text, voice, rate):
                buf.extend(data)
                yield data
            complete = True
            VOICE_TROUBLE = None
        except (asyncio.CancelledError, ConnectionResetError):
            raise                      # the browser stopped listening; nothing is wrong
        except Exception as exc:
            VOICE_TROUBLE = {**describe_voice_error(exc),
                             "at": datetime.now().isoformat(timespec="seconds")}
            print(f"[voice] {VOICE_TROUBLE['why']} {VOICE_TROUBLE['hint']}")
            raise
        finally:
            # Only a finished stream may be cached. Writing a cut-off one meant the next
            # play served the stub for ever, and the voice looked like it cut itself off.
            if complete and buf:
                try:
                    (AUDIO / f"{key}.mp3").write_bytes(bytes(buf))
                except OSError:
                    pass
            SPEECH_JOBS.pop(key, None)

    return StreamingResponse(gen(), media_type="audio/mpeg",
                             headers={"Cache-Control": "no-store"})


@app.get("/audio/{name}")
def audio(name: str):
    # resolve before trusting: "../../reports/x.mp3" would otherwise escape the folder
    root = AUDIO.resolve()
    p = (root / name).resolve()
    if not p.is_relative_to(root) or p.suffix != ".mp3" or not p.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(p, media_type="audio/mpeg")


# ---------------------------------------------------------------- mutate

class TaskPatch(BaseModel):
    status: str | None = None
    priority: int | None = None
    notes: str | None = None
    estimate_h: float | None = None


@app.patch("/api/task/{task_id}")
def patch_task(task_id: str, body: TaskPatch, lang: str | None = Query(None)):
    lang = resolve_lang(lang)
    db = tasks_mod.load()
    for t in db["tasks"]:
        if t["id"] != task_id:
            continue
        if body.status:
            if body.status not in tasks_mod.STATUSES:
                raise HTTPException(400, f"status must be one of {tasks_mod.STATUSES}")
            t["status"] = body.status
            if body.status == "done":
                t["completed"] = tasks_mod.now()
            if body.status == "in_progress" and not t.get("started"):
                t["started"] = tasks_mod.now()
        if body.priority is not None:
            t["priority"] = body.priority
        if body.notes is not None:
            t["notes"] = body.notes
        if body.estimate_h is not None:
            t["estimate_h"] = body.estimate_h
        tasks_mod.save(db)
        return task_view(t, lang)
    raise HTTPException(404, f"no task {task_id}")


# ---------------------------------------------------------------- voice commands

class Command(BaseModel):
    text: str
    lang: str | None = None


# Words too common to identify anything. Without this, "done with X" matched any title
# containing "with", and a fuzzy ratio alone pushed it over the line — a voice command
# could close a completely unrelated task.
STOPWORDS = {
    "the", "and", "with", "for", "from", "your", "that", "this", "you", "are", "was",
    "have", "has", "had", "into", "onto", "about", "like", "task", "assignment", "all",
    "и", "на", "за", "от", "със", "по", "да", "се", "но", "или", "това", "тази", "този",
    "задача", "задачата",
}


def _tokens(s):
    return [w for w in re.findall(r"\w{3,}", (s or "").lower()) if w not in STOPWORDS]


def match_task(phrase: str, live_only=True):
    """Find the task a spoken phrase refers to, or None.

    A title only counts as "the one you meant" when its own distinctive words actually
    turn up in what was said. Fuzzy similarity alone is not enough — unrelated strings
    routinely score 0.4-0.5, which is how "absolutely nothing like that" once closed
    a task called "Reading with no deadline".
    """
    db = tasks_mod.load()
    pool = [t for t in db["tasks"] if not live_only or t.get("status") not in ("done", "dropped")]
    said = set(_tokens(phrase))
    if not pool or not said:
        return None
    low = phrase.lower()
    best_score, best = 0.0, None
    for t in pool:
        title = (t.get("title") or "")
        toks = _tokens(title)
        if not toks:
            continue
        hits = sum(1 for w in toks if w in said)
        coverage = hits / len(toks)
        ratio = difflib.SequenceMatcher(None, low, title.lower()).ratio()
        if not (hits >= 2 or coverage >= 0.5 or ratio >= 0.75):
            continue
        score = coverage + ratio * 0.3
        if score > best_score:
            best_score, best = score, t
    return best


@app.post("/api/command")
async def command(body: Command):
    lang = resolve_lang(body.lang)
    say, intents = SAY.get(lang, SAY["en"]), INTENTS.get(lang, INTENTS["en"])
    raw = body.text.strip()
    low = raw.lower()
    intent = next((name for name, pat in intents.items() if re.search(pat, low)), None)

    if intent == "stop":
        return {"intent": "stop", "lang": lang, "say": None, "action": "stop"}

    if intent == "brief":
        return {"intent": "brief", "lang": lang, "say": brief_mod.build(lang=lang)}

    if intent == "next":
        db = tasks_mod.load()
        live = sorted((t for t in db["tasks"] if t.get("status") not in ("done", "dropped")),
                      key=tasks_mod.score, reverse=True)
        if not live:
            return {"intent": "next", "lang": lang, "say": say["none"]}
        t = live[0]
        out = say["most_urgent"].format(title=t["title"], when=brief_mod.when(t, lang))
        w = brief_mod.weight_phrase(t, lang)
        if w:
            out += say["worth"].format(w=w)
        return {"intent": "next", "lang": lang, "say": out, "focus": t["id"]}

    if intent in ("done", "start"):
        t = match_task(re.sub(intents[intent], " ", low))
        if not t:
            return {"intent": intent, "lang": lang, "say": say["which"]}
        new = "done" if intent == "done" else "in_progress"
        db = tasks_mod.load()
        for row in db["tasks"]:
            if row["id"] == t["id"]:
                row["status"] = new
                row["completed" if new == "done" else "started"] = tasks_mod.now()
        tasks_mod.save(db)
        key = "closed" if new == "done" else "started"
        return {"intent": intent, "lang": lang, "say": say[key].format(title=t["title"]),
                "refresh": True, "focus": t["id"]}

    if intent == "when":
        t = match_task(low)
        if not t:
            return {"intent": "when", "lang": lang, "say": say["which_when"]}
        if not t.get("due"):
            return {"intent": "when", "lang": lang,
                    "say": say["no_due"].format(title=t["title"]), "focus": t["id"]}
        d = brief_mod.date_phrase(t, lang)
        return {"intent": "when", "lang": lang, "focus": t["id"],
                "say": say["due_is"].format(title=t["title"], when=brief_mod.when(t, lang),
                                            date=f", {d}" if d else "")}

    # Not a dashboard command, so it is a question. Queue it rather than telling the
    # user to go somewhere else — asking right after hearing the report is the point.
    if not ai_on():
        return {"intent": "unknown", "lang": lang,
                "say": say.get("unknown") or SAY["en"]["unknown"]}
    rec = ask(AskIn(text=raw, lang=lang))
    return {"intent": "question", "lang": lang, "say": say["queued"],
            "passthrough": raw, "question_id": rec["id"]}


# ---------------------------------------------------------------- hand off to Claude

HANDOFF = WORK / "handoff"


class HandoffIn(BaseModel):
    task_id: str
    lang: str | None = None
    note: str | None = None


def build_handoff_prompt(t, lang, note=None):
    """A prompt that stands on its own — Claude gets a fresh session, not this one."""
    dl = tasks_mod.days_left(t)
    when = ("no deadline" if dl is None
            else f"{-dl} days OVERDUE" if dl < 0
            else "TODAY" if dl == 0 else f"in {dl} days")
    en = tasks_mod.localize(t, "en")
    lines = [
        f"/avans-solve {t['id']}",
        "",
        f"# {t.get('title', '')}",
        "",
        f"- **Deadline:** {t.get('due') or 'not published in Brightspace'} ({when})",
        f"- **Weight:** {en.get('weight') or 'not stated'}",
        f"- **Type:** {t.get('type', '?')}   **Status:** {t.get('status', 'todo')}"
        f"   **Priority:** {t.get('priority', 3)}",
        f"- **Source:** {t.get('source_url', '')}",
    ]
    if en.get("description"):
        lines += ["", "## What it is", "", en["description"]]
    if en.get("deliverables"):
        lines += ["", "## What is handed in", ""] + [f"- {d}" for d in en["deliverables"]]
    if en.get("requirements"):
        lines += ["", "## Requirements and grading", ""] + [f"- {r}" for r in en["requirements"]]
    if t.get("resources"):
        lines += ["", "## Material in Brightspace", ""] + [
            f"- [{r.get('title', 'link')}]({r.get('url', '')})" for r in t["resources"]]
    if t.get("notes"):
        lines += ["", "## My notes", "", t["notes"]]
    if note:
        lines += ["", "## What I want from you", "", note]

    tid = t["id"]
    lines += [
        "",
        "---",
        "",
        "SOLVE THIS. Not a plan about solving it — the actual worked solution.",
        "",
        f"Write it to `work/{tid}/solution/`. Whatever the task really produces: the "
        "draft, the outline with the arguments written out, the script, the code, the "
        "spreadsheet structure. One file the user can open and use. Name it for what it "
        "is, not `solution.md`.",
        "",
        "**Anything I cannot do for them gets its own section, written out in full.** "
        "Filming, recording, being somewhere, interviewing someone, signing something, "
        "group work needing other people, anything physical. Do not skip it and do not "
        "wave at it — give the shot list, the word-for-word script, the checklist, the "
        "order to do it in, and how long each part takes. That section is the most "
        "useful part of the file, because it is the part they actually have to perform.",
        "",
        "Read the grading criteria first and say what is genuinely being assessed. If "
        "the criteria are not in what we have collected, say so plainly — do not invent "
        "them. Do not submit anything to Brightspace.",
        "",
        f"_Sent from the {assistant_name()} dashboard, {datetime.now().isoformat(timespec='minutes')}, "
        f"interface language {lang}._",
    ]
    return "\n".join(lines)


def need_ai():
    if not ai_on():
        raise HTTPException(409, "this copy has no Claude session behind it")


@app.post("/api/handoff")
def handoff(body: HandoffIn):
    need_ai()
    lang = resolve_lang(body.lang)
    db = tasks_mod.load()
    t = next((x for x in db["tasks"] if x["id"] == body.task_id), None)
    if not t:
        raise HTTPException(404, f"no task {body.task_id}")
    prompt = build_handoff_prompt(t, lang, body.note)
    HANDOFF.mkdir(parents=True, exist_ok=True)
    path = HANDOFF / f"{datetime.now():%Y%m%d-%H%M%S}-{t['id']}.md"
    path.write_text(prompt, encoding="utf-8")
    # queue it the same way a question is queued, so the watcher wakes Claude and the
    # solution turns up on its own — the user no longer has to carry the prompt anywhere
    rec = ask(AskIn(text=prompt, lang=lang, about=t["id"], kind="solve"))
    return {"task_id": t["id"], "title": t.get("title", ""), "queued": rec["id"],
            "prompt": prompt, "file": shown_path(path)}


@app.get("/api/handoff")
def handoff_list():
    """Requests waiting for Claude. Answered ones move to work/handoff/done/."""
    if not HANDOFF.exists():
        return {"pending": []}
    rows = []
    for p in sorted(HANDOFF.glob("*.md")):
        rows.append({
            "file": shown_path(p),
            "task_id": p.stem.split("-", 2)[-1],
            "at": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
        })
    return {"pending": rows}


# ---------------------------------------------------------------- ask a question

# AVANS_ASK lets the test suite point the queue at a throwaway directory
ASK = Path(os.environ.get("AVANS_ASK") or ROOT / "work" / "ask")


class AskIn(BaseModel):
    text: str
    lang: str | None = None
    about: str | None = None      # optional task id the question is about
    kind: str = "question"        # "question" or "submit"


class AnswerIn(BaseModel):
    text: str


def _ask_path(qid):
    p = (ASK / f"{qid}.json").resolve()
    if not p.is_relative_to(ASK.resolve()):
        raise HTTPException(400, "bad id")
    return p


@app.post("/api/ask", status_code=201)
def ask(body: AskIn):
    """Queue a question asked from the dashboard. Claude answers it from the chat."""
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "empty question")
    ASK.mkdir(parents=True, exist_ok=True)
    qid = f"{datetime.now():%Y%m%d-%H%M%S}-{hashlib.sha1(text.encode()).hexdigest()[:4]}"
    rec = {
        "id": qid,
        "question": text,
        "kind": body.kind if body.kind in ("question", "submit", "solve", "sync")
                else "question",
        "lang": resolve_lang(body.lang),
        "about": body.about,
        "asked_at": datetime.now().isoformat(timespec="seconds"),
        "status": "pending",
        "answer": None,
        "answered_at": None,
    }
    _ask_path(qid).write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec


@app.get("/api/ask")
def ask_list(status: str | None = Query(None), limit: int = Query(20, ge=1, le=100)):
    if not ASK.exists():
        return {"questions": []}
    rows = []
    for p in sorted(ASK.glob("*.json"), reverse=True):
        if p.name.startswith("."):          # .watcher heartbeat, not a question
            continue
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if status and r.get("status") != status:
            continue
        rows.append(r)
        if len(rows) >= limit:
            break
    return {"questions": rows}


@app.get("/api/ask/{qid}")
def ask_one(qid: str):
    p = _ask_path(qid)
    if not p.is_file():
        raise HTTPException(404, f"no question {qid}")
    return json.loads(p.read_text(encoding="utf-8"))


@app.delete("/api/ask/{qid}")
def ask_delete(qid: str):
    p = _ask_path(qid)
    if not p.is_file():
        raise HTTPException(404, f"no question {qid}")
    p.unlink()
    return {"deleted": qid}


@app.get("/api/ask/{qid}/wait")
async def ask_wait(qid: str, timeout: float = Query(25.0, ge=1, le=60)):
    """Hold the connection until the answer lands, instead of the page asking every
    few seconds. The answer then shows up as fast as it is written, not up to a poll
    interval later."""
    p = _ask_path(qid)
    if not p.is_file():
        raise HTTPException(404, f"no question {qid}")
    deadline = time.monotonic() + timeout
    while True:
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            rec = None                       # mid-write; look again in a moment
        if rec and rec.get("status") == "answered":
            return rec
        if time.monotonic() >= deadline:
            return rec or {"id": qid, "status": "pending"}
        await asyncio.sleep(0.2)


@app.post("/api/ask/{qid}/answer")
def ask_answer(qid: str, body: AnswerIn):
    p = _ask_path(qid)
    if not p.is_file():
        raise HTTPException(404, f"no question {qid}")
    rec = json.loads(p.read_text(encoding="utf-8"))
    rec["answer"] = body.text
    rec["status"] = "answered"
    rec["answered_at"] = datetime.now().isoformat(timespec="seconds")
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec


# ---------------------------------------------------------------- files on a task

# what the browser is allowed to put on disk — no scripts, no archives
UPLOAD_OK = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".txt", ".md",
             ".csv", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mov", ".mp3",
             ".wav", ".m4a", ".zip"}
MAX_UPLOAD = 64 * 1024 * 1024


def shown_path(p: Path) -> str:
    """A path to show the user. Relative to the project when it is inside it — but
    AVANS_WORK can point anywhere (the tests send it to a temp folder), and a bare
    relative_to() raised ValueError and turned the whole request into a 500."""
    try:
        return str(p.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


def safe_name(name: str) -> str:
    """Keep a recognisable filename but never let it point anywhere but this folder."""
    base = Path(name or "").name.replace("\\", "_")
    base = re.sub(r"[^\w.\- ()\[\]]+", "_", base, flags=re.UNICODE).strip(" .") or "file"
    return base[:120]


def task_dir(task_id: str, kind: str) -> Path:
    if kind not in ("solution", "attachments"):
        raise HTTPException(400, "kind must be solution or attachments")
    tid = safe_name(task_id)
    p = (WORK / tid / kind).resolve()
    if not p.is_relative_to(WORK.resolve()):
        raise HTTPException(400, "bad task id")
    return p


def file_rows(task_id: str, kind: str):
    d = task_dir(task_id, kind)
    if not d.is_dir():
        return []
    rows = []
    for f in sorted(d.iterdir()):
        if not f.is_file():
            continue
        st = f.stat()
        rows.append({
            "name": f.name,
            "kind": kind,
            "size": st.st_size,
            "at": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "url": f"/api/files/{task_id}/{kind}/{f.name}",
        })
    return rows


@app.get("/api/files/{task_id}")
def list_files(task_id: str):
    return {"task_id": task_id,
            "solution": file_rows(task_id, "solution"),
            "attachments": file_rows(task_id, "attachments")}


@app.get("/api/files/{task_id}/{kind}/{name}")
def get_file(task_id: str, kind: str, name: str):
    p = (task_dir(task_id, kind) / safe_name(name)).resolve()
    if not p.is_relative_to(task_dir(task_id, kind)) or not p.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(p, filename=p.name, media_type="application/octet-stream")


@app.post("/api/files/{task_id}/attachments", status_code=201)
async def upload_files(task_id: str, files: list[UploadFile] = File(...)):
    db = tasks_mod.load()
    if not any(t["id"] == task_id for t in db["tasks"]):
        raise HTTPException(404, f"no task {task_id}")
    d = task_dir(task_id, "attachments")
    d.mkdir(parents=True, exist_ok=True)
    saved, skipped = [], []
    for up in files:
        name = safe_name(up.filename)
        if Path(name).suffix.lower() not in UPLOAD_OK:
            skipped.append({"name": name, "why": "file type not allowed"})
            continue
        data = await up.read()
        if len(data) > MAX_UPLOAD:
            skipped.append({"name": name, "why": f"over {MAX_UPLOAD // (1024*1024)} MB"})
            continue
        (d / name).write_bytes(data)
        saved.append(name)
    return {"saved": saved, "skipped": skipped, "attachments": file_rows(task_id, "attachments")}


@app.delete("/api/files/{task_id}/attachments/{name}")
def delete_attachment(task_id: str, name: str):
    d = task_dir(task_id, "attachments")
    p = (d / safe_name(name)).resolve()
    if not p.is_relative_to(d) or not p.is_file():
        raise HTTPException(404, "not found")
    p.unlink()
    return {"removed": p.name, "attachments": file_rows(task_id, "attachments")}


# ---------------------------------------------------------------- submit to Brightspace

class SubmitIn(BaseModel):
    task_id: str
    what: str                     # the link, or a path to the file to hand in
    lang: str | None = None
    note: str | None = None


@app.post("/api/submit", status_code=201)
def submit(body: SubmitIn):
    """Queue a submission for Claude to PREPARE in Brightspace.

    Deliberately not an automatic hand-in. A university dropbox submission cannot be
    undone, and on this module a wrong or unopenable submission is a fail — so Claude
    fills everything in and stops at the Submit button. The last click stays yours.
    """
    need_ai()
    what = body.what.strip()
    if not what:
        raise HTTPException(400, "nothing to submit")
    db = tasks_mod.load()
    t = next((x for x in db["tasks"] if x["id"] == body.task_id), None)
    if not t:
        raise HTTPException(404, f"no task {body.task_id}")

    dropbox = next((r.get("url") for r in (t.get("resources") or [])
                    if "dropbox" in (r.get("url") or "").lower()), None)
    attached = file_rows(body.task_id, "attachments")
    lines = [
        f"SUBMISSION REQUEST — {t.get('title', '')}",
        "",
        f"What to hand in: {what}",
        f"Dropbox: {dropbox or 'not recorded on this task — find it in the course'}",
        f"Deadline: {t.get('due') or 'not published in Brightspace'}",
    ]
    if attached:
        lines += ["", "Files attached in the dashboard, upload exactly these:"]
        for f in attached:
            lines.append(f"  - work/{body.task_id}/attachments/{f['name']}  "
                         f"({f['size'] // 1024} KB)")
    for d in (tasks_mod.localize(t, 'en').get("deliverables") or []):
        lines.append(f"Required: {d}")
    if body.note:
        lines += ["", f"Note from me: {body.note}"]
    lines += [
        "",
        "Open the dropbox in my Chrome and upload the attached files into it. Check "
        "what is staged against what the task requires — if the task asks for two "
        "things and only one is here, say so instead of submitting half.",
        "",
        "Then STOP. Do not press Submit. Screenshot what is about to go in, show it to "
        "me, and leave the window open on the dropbox so I press Submit myself.",
    ]
    rec = ask(AskIn(text="\n".join(lines), lang=body.lang, about=t["id"], kind="submit"))

    t.setdefault("submissions", []).append({
        "at": datetime.now().isoformat(timespec="seconds"),
        "what": what,
        "status": "requested",
        "ask_id": rec["id"],
    })
    tasks_mod.save(db)
    return {"queued": rec["id"], "task_id": t["id"], "what": what, "dropbox": dropbox}


# ---------------------------------------------------------------- updates
#
# The launcher fetches newer versions of the program by itself and writes what happened
# to <home>/app/status.json. This only reports it — the page says "an update is ready,
# reopen the app" rather than anything changing under a running session.

def running_version() -> str:
    try:
        return str(json.loads((paths.APP / "release.json").read_text(encoding="utf-8"))
                   .get("version", "?"))
    except (OSError, json.JSONDecodeError):
        return "?"


@app.post("/api/update/check")
def update_check(request: Request):
    """Called when the dashboard opens. The server may have been running for days — the
    window closes, the server does not — so this is the moment to look again."""
    if not is_local(request):
        raise HTTPException(403, "only from the computer")
    return updater.check_now()


@app.post("/api/update/apply", status_code=202)
def update_apply(request: Request):
    """Restart into the newer version that is already downloaded. The page reloads."""
    if not is_local(request):
        raise HTTPException(403, "only from the computer")
    shell = updater._shell()
    if shell is None or not os.environ.get("ASSISTANT_PAYLOAD"):
        raise HTTPException(409, "not running inside the packaged app")
    best = shell.pick_payload()[1]
    if shell.vtuple(best.get("version")) <= shell.vtuple(running_version()):
        raise HTTPException(409, "nothing newer is downloaded")

    def later():
        time.sleep(0.5)                                  # let this answer reach the page
        updater.restart_detached(best["version"])
    threading.Thread(target=later, daemon=True).start()
    return {"restarting": True, "version": best["version"], "from": running_version()}


@app.get("/api/update")
def update_status():
    out = {"running": running_version(), "state": "unknown",
           "shell": os.environ.get("ASSISTANT_SHELL_VERSION")}
    try:
        st = json.loads((ROOT / "app" / "status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        st = {}
    # "ready" for the version already running means it was just put to use — say what
    # changed, once (the page remembers which versions it has announced)
    if (st.get("state") == "ready" and st.get("version") == out["running"]) \
            or os.environ.get("ASSISTANT_RESTARTED") == out["running"]:
        notes = st.get("notes") or ""
        if not notes:                       # the shell's later check wrote a status without them
            try:
                notes = (ROOT / "app" / f"notes-{out['running']}.txt").read_text(encoding="utf-8")
            except OSError:
                notes = ""
        return {**out, "state": "applied", "version": out["running"], "notes": notes[:600]}
    return {**out, **st}


# ---------------------------------------------------------------- setup
#
# What a person answers once: what the assistant is called, how it addresses them, which
# Brightspace, which courses. No password is asked for or stored anywhere — signing in
# happens in the browser window, with the school's own page.

class SetupIn(BaseModel):
    name: str | None = None
    address: str | None = None
    lang: str | None = None
    base_url: str | None = None
    courses: list[dict] | None = None
    api_version: str | None = None
    teams: bool | None = None


@app.get("/api/setup")
def setup_status():
    c = cfg()
    import cdp                                        # noqa: PLC0415
    lang = resolve_lang()
    return {
        "onboarded": onboarded(),
        "name": assistant_name(),
        "address": brief_mod.address(lang),
        "lang": lang,
        "base_url": cdp.base_url(),
        "courses": followed_courses(),
        "ai": ai_on(),
        "teams": teams_on(),
        "browser": {"found": bool(cdp.chrome_binary()),
                    "up": cdp.is_up() if autosync_enabled() else False},
        "feedback": {"email": bool(c.get("feedback", {}).get("email")),
                     "url": c.get("feedback", {}).get("url") or ""},
    }


@app.get("/api/setup/courses")
def setup_courses():
    """The signed-in person's Brightspace courses. Reads, changes nothing."""
    if not autosync_enabled():
        return {"ok": False, "reason": "disabled", "message": "autosync is off"}
    import autosync                                   # noqa: PLC0415
    try:
        with BROWSER_LOCK:
            return autosync.my_courses()
    except Exception as e:                            # noqa: BLE001
        return {"ok": False, "reason": "crash", "message": str(e)[:300]}


@app.post("/api/setup")
def setup_save(body: SetupIn, request: Request):
    if not is_local(request):
        raise HTTPException(403, "setup is done on the computer, not from the phone")
    c = paths.read_user_config()
    a = c.setdefault("assistant", {})
    if body.name is not None:
        name = re.sub(r"[\x00-\x1f<>]", "", body.name).strip()[:24]
        if not name:
            raise HTTPException(400, "the assistant needs a name")
        a["name"] = name
    if body.lang and body.lang in (c.get("languages") or FALLBACK_LANGS):
        c.setdefault("report", {})["language"] = body.lang
    if body.address is not None:
        lang = body.lang or c.get("report", {}).get("language", "en")
        langs = c.setdefault("languages", dict(FALLBACK_LANGS))
        langs.setdefault(lang, {})["address"] = re.sub(
            r"[\x00-\x1f<>]", "", body.address).strip()[:24]
    if body.base_url:
        url = body.base_url.strip().rstrip("/")
        if not re.match(r"^https://[A-Za-z0-9.-]+(:\d+)?$", url):
            raise HTTPException(400, "the Brightspace address must look like "
                                     "https://brightspace.school.nl")
        c.setdefault("brightspace", {})["base_url"] = url
        if c.get("course", {}).get("base_url") not in (None, url):
            c["course"].pop("lessons_url", None)      # it pointed at the old school
            c["course"]["base_url"] = url
    if body.courses is not None:
        rows = [{"org_unit_id": str(x.get("org_unit_id")), "name": str(x.get("name", ""))[:160]}
                for x in body.courses if str(x.get("org_unit_id", "")).isdigit()]
        c["courses"] = rows
        a["onboarded"] = bool(rows)
    if body.api_version and re.match(r"^\d+\.\d+$", body.api_version):
        c.setdefault("course", {})["api_version"] = body.api_version
    if body.teams is not None:
        c.setdefault("teams", {})["enabled"] = bool(body.teams)
    paths.write_config(c)
    return setup_status()


# ---------------------------------------------------------------- announcements

@app.post("/api/news/seen")
def news_seen():
    db = tasks_mod.load()
    for n in db.get("announcements") or []:
        n["seen"] = True
    tasks_mod.save(db)
    return {"ok": True}


# ---------------------------------------------------------------- feedback
#
# Early-access testers say what they think here. It is written to a file on THEIR
# machine and nothing is sent anywhere by this server: the page then offers to open
# their own mail program with the text filled in, and they press Send themselves.

FEEDBACK = ROOT / "feedback"


class FeedbackIn(BaseModel):
    rating: int | None = None
    liked: str | None = None
    annoyed: str | None = None
    quote: str | None = None
    name: str | None = None
    may_quote: bool = False


@app.post("/api/feedback", status_code=201)
def feedback(body: FeedbackIn):
    import urllib.parse                               # noqa: PLC0415
    clean = lambda v: (v or "").strip()[:2000]        # noqa: E731
    rec = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "assistant": assistant_name(),
        "rating": body.rating if body.rating in (1, 2, 3, 4, 5) else None,
        "liked": clean(body.liked), "annoyed": clean(body.annoyed),
        "quote": clean(body.quote), "name": clean(body.name)[:80],
        "may_quote": bool(body.may_quote),
    }
    if not (rec["rating"] or rec["liked"] or rec["annoyed"] or rec["quote"]):
        raise HTTPException(400, "nothing was written")
    FEEDBACK.mkdir(parents=True, exist_ok=True)
    path = FEEDBACK / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    text = "\n".join([
        f"Early access feedback — {rec['at']}",
        f"Rating: {rec['rating'] or '-'} / 5",
        "", "What worked:", rec["liked"] or "-",
        "", "What got in the way:", rec["annoyed"] or "-",
        "", "In one sentence (testimonial):", rec["quote"] or "-",
        "", f"Name: {rec['name'] or '-'}",
        f"May be quoted publicly: {'yes' if rec['may_quote'] else 'no'}",
    ])
    fb = cfg().get("feedback", {})
    mailto = None
    if fb.get("email"):
        mailto = (f"mailto:{fb['email']}?subject="
                  + urllib.parse.quote("Early access feedback")
                  + "&body=" + urllib.parse.quote(text))
    return {"saved": shown_path(path), "text": text, "mailto": mailto,
            "url": fb.get("url") or None}


# ---------------------------------------------------------------- phone access
#
# The dashboard on a phone is the same page, served by this computer over the home
# network. Off unless switched on. A device that is not this computer has to enter a
# six-digit code shown on the computer once; after that it holds a long random cookie.
# Five wrong codes and the code changes, so it cannot be guessed by trying them all.

PAIR_FAILS = {"n": 0}
PAIR_PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pair this device</title>
<body style="margin:0;background:#04070d;color:#cfe6ef;font-family:ui-monospace,Consolas,monospace;
display:grid;place-items:center;min-height:100vh">
<form method="post" action="/pair" style="display:grid;gap:14px;padding:24px;max-width:320px;text-align:center">
<div style="letter-spacing:.3em;color:#4fd8e8">PAIR THIS DEVICE</div>
<div style="font-size:13px;color:#6b8798;line-height:1.5">Enter the six-digit code shown on the
computer, under Settings.</div>
<input name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" autofocus
style="font:inherit;font-size:28px;letter-spacing:.3em;text-align:center;padding:12px;
background:#060d18;color:#4fd8e8;border:1px solid rgba(79,216,232,.4)">
<button style="font:inherit;padding:12px;background:transparent;color:#4fd8e8;
border:1px solid rgba(79,216,232,.4);letter-spacing:.2em">CONNECT</button>
<div style="color:#ff5f6d;font-size:12px">%s</div></form></body>"""


def mobile_cfg() -> dict:
    return cfg().get("mobile", {}) or {}


def lan_ip() -> str | None:
    """This computer's address on the local network. Opens no connection: a UDP socket
    'connected' to a public address only asks the OS which interface it would use."""
    import socket                                     # noqa: PLC0415
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("192.0.2.1", 9))
            return sk.getsockname()[0]
    except OSError:
        return None


def _new_code() -> str:
    import secrets                                    # noqa: PLC0415
    return f"{secrets.randbelow(10**6):06d}"


class LanGuard:
    """Lets this computer through untouched and makes any other device pair first.

    Plain ASGI on purpose, not `@app.middleware("http")`. That decorator wraps every
    response body in its own task, and it broke exactly the thing the voice depends on:
    a stream the browser stops listening to was no longer cancelled cleanly, and the
    request hung. For the local machine this adds one comparison and nothing else.
    """

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        client = scope.get("client")
        if client and client[0] in ("127.0.0.1", "::1"):
            return await self.inner(scope, receive, send)
        import secrets                                # noqa: PLC0415
        request = Request(scope)
        m = mobile_cfg()
        path = scope.get("path", "")
        if not m.get("enabled"):
            deny = HTMLResponse("Phone access is switched off on the computer.",
                                status_code=403)
        elif path == "/pair":
            return await self.inner(scope, receive, send)
        else:
            given = request.cookies.get("pair") or ""
            if given and any(secrets.compare_digest(given, t)
                             for t in m.get("sessions", [])):
                return await self.inner(scope, receive, send)
            deny = (HTMLResponse("not paired", status_code=401)
                    if path.startswith("/api/")
                    else HTMLResponse(PAIR_PAGE % "", status_code=401))
        await deny(scope, receive, send)


app.add_middleware(LanGuard)


@app.post("/pair")
async def pair(request: Request):
    import secrets                                    # noqa: PLC0415
    import urllib.parse                               # noqa: PLC0415
    form = urllib.parse.parse_qs((await request.body()).decode("utf-8", "replace"))
    code = (form.get("code") or [""])[0].strip()
    c = paths.read_user_config()
    m = c.setdefault("mobile", {})
    if not m.get("enabled") or not m.get("code"):
        raise HTTPException(403, "phone access is off")
    if not secrets.compare_digest(code, m["code"]):
        PAIR_FAILS["n"] += 1
        if PAIR_FAILS["n"] >= 5:                      # stop a walk through all 10^6
            m["code"] = _new_code()
            PAIR_FAILS["n"] = 0
            paths.write_config(c)
        return HTMLResponse(PAIR_PAGE % "Wrong code. Check the computer.", status_code=401)
    PAIR_FAILS["n"] = 0
    token = secrets.token_urlsafe(32)
    m["sessions"] = (m.get("sessions", []) + [token])[-5:]
    m["code"] = _new_code()                           # one code pairs one device
    paths.write_config(c)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("pair", token, max_age=180 * 86400, httponly=True, samesite="lax")
    return resp


class MobileIn(BaseModel):
    enabled: bool


@app.get("/api/mobile")
def mobile_status(request: Request):
    if not is_local(request):
        raise HTTPException(403, "only from the computer")
    m = mobile_cfg()
    ip = lan_ip()
    port = os.environ.get("JARVIS_PORT", "8765")
    return {"enabled": bool(m.get("enabled")),
            # the server binds once, at start — switching this on needs a restart
            "listening": BOUND["host"] == "0.0.0.0",
            "url": f"http://{ip}:{port}" if ip else None,
            "code": m.get("code") if m.get("enabled") else None,
            "devices": len(m.get("sessions", []))}


@app.post("/api/mobile")
def mobile_set(body: MobileIn, request: Request):
    if not is_local(request):
        raise HTTPException(403, "only from the computer")
    c = paths.read_user_config()
    m = c.setdefault("mobile", {})
    m["enabled"] = bool(body.enabled)
    if body.enabled and not m.get("code"):
        m["code"] = _new_code()
    if not body.enabled:
        m["sessions"] = []                            # switching off un-pairs everything
    paths.write_config(c)
    return mobile_status(request)


BOUND = {"host": "127.0.0.1"}


def bind_host() -> str:
    """127.0.0.1 unless phone access is on. Never `localhost` — see CLAUDE.md."""
    BOUND["host"] = "0.0.0.0" if mobile_cfg().get("enabled") else "127.0.0.1"
    return BOUND["host"]


# ---------------------------------------------------------------- static

@app.get("/app.webmanifest")
def manifest():
    """The install manifest, carrying the name this person chose — "Add to home screen"
    on a phone should say what they called it, not what the original was called."""
    from fastapi.responses import JSONResponse          # noqa: PLC0415
    m = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    m["name"] = m["short_name"] = assistant_name()
    m["lang"] = resolve_lang()
    return JSONResponse(m, media_type="application/manifest+json",
                        headers={"Cache-Control": "no-store"})


@app.get("/")
def index():
    # No caching for the page itself. The dashboard is edited while it is running, and a
    # cached copy meant "I restarted and nothing changed" — the browser was still showing
    # the old HUD. The icons and audio under /static and /audio still cache normally.
    return FileResponse(STATIC / "index.html", headers={
        "Cache-Control": "no-store, must-revalidate",
        "Pragma": "no-cache",
    })


app.mount("/static", StaticFiles(directory=STATIC), name="static")


class _Tee:
    """Write to the console when there is one, and always to the log file."""

    def __init__(self, *streams):
        self.streams = [s for s in streams if s is not None]

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
            except Exception:
                pass
        return len(data)

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self):
        return False


def _setup_logging():
    """Make the server's death diagnosable.

    Two problems this solves. Under pythonw.exe there is no console, and the first
    write to sys.stdout raises — uvicorn logs on startup, so the process died with
    exit code 1 and no traceback at all. And when there IS a console, everything went
    to a minimised window that vanishes with the process: a crash left no trace
    anywhere. Now every run appends to reports/server.log, tracebacks included, and a
    clean shutdown is recorded — so a "start" with no matching "stop" means it was
    killed or it crashed.
    """
    import atexit
    import traceback as tb

    def usable(s):
        try:
            s.write("")
            s.flush()
            return True
        except Exception:
            return False

    log = ROOT / "reports" / "server.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "a", buffering=1, encoding="utf-8", errors="replace")

    console = bool(sys.stdout and sys.stderr and usable(sys.stdout) and usable(sys.stderr))
    sys.stdout = _Tee(sys.stdout if console else None, fh)
    sys.stderr = _Tee(sys.stderr if console else None, fh)

    def stamp(kind, extra=""):
        fh.write(f"--- {kind} {datetime.now().isoformat(timespec='seconds')} "
                 f"pid={os.getpid()} {extra}---\n")

    fh.write("\n")
    stamp("start", f"console={'yes' if console else 'no'} "
                   f"port={os.environ.get('JARVIS_PORT', '8765')} ")

    def on_crash(exc_type, exc, trace):
        stamp("CRASH")
        tb.print_exception(exc_type, exc, trace, file=fh)
        fh.flush()
        if console:
            tb.print_exception(exc_type, exc, trace)

    sys.excepthook = on_crash
    atexit.register(lambda: stamp("stop", "clean "))
    return log


if __name__ == "__main__":
    import uvicorn
    _setup_logging()
    uvicorn.run(app, host=bind_host(),
                port=int(os.environ.get("JARVIS_PORT", "8765")), log_level="warning")
