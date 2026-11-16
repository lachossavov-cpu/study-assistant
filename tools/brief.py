#!/usr/bin/env python3
"""Build the spoken brief from data/tasks.json — deterministic, no model involved.

  python tools/brief.py                # English (the default in config.json)
  python tools/brief.py --lang bg      # Bulgarian
  python tools/brief.py --json
"""
import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tasks import CONFIG, days_left, load, localize, score  # noqa: E402

# Task titles are never translated — they must keep matching Brightspace.
STR = {
    "en": {
        "months": ["January", "February", "March", "April", "May", "June", "July",
                   "August", "September", "October", "November", "December"],
        "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        "night": "You are working in the middle of the night",
        "morning": "Good morning", "afternoon": "Good afternoon", "evening": "Good evening",
        "none": "{greet}. You have no open tasks. Either the database is empty or everything is closed.",
        "open": "You have {n} open {tasks}",
        "of_which": ", of which {list}",
        "n_overdue": "{n} overdue",
        "n_urgent": "{n} due in the next three days",
        "n_week": "{n} due this week",
        "task_1": "task", "task_n": "tasks",
        "day_1": "one day", "day_n": "{n} days",
        "no_due": "no deadline", "today": "today", "tomorrow": "tomorrow",
        "in_x": "in {d}", "late_by": "{d} overdue",
        "overdue_line": "Overdue: {title}. It was due {when}. You are {d} late.",
        "earlier": "earlier",
        "next_line": "The next deadline is {title}, {rel}{date}.",
        "weight_line": "It is worth {w}.",
        "hand_in": "You hand in: {d}",
        "by_priority": "Next by priority: {list}.",
        "undated": "Another {n} {tasks} are waiting with no announced deadline.",
        "clock": "12h",
        "at_oclock": "{stamp}, at {h} {ampm}",
        "at_time": "{stamp}, at {h}:{m:02d} {ampm}",
        "stale": "Careful. I last checked Brightspace {age}, so anything posted since "
                 "then is not in this. I am checking now.",
        "never_synced": "Careful. Brightspace has never been checked, so this is only "
                        "what is already on the list.",
        "age_hours": "{n} hours ago", "age_hour": "an hour ago",
        "age_days": "{n} days ago", "age_now": "just now",
    },
    "bg": {
        "months": ["януари", "февруари", "март", "април", "май", "юни",
                   "юли", "август", "септември", "октомври", "ноември", "декември"],
        "days": ["понеделник", "вторник", "сряда", "четвъртък", "петък", "събота", "неделя"],
        "night": "Работиш посред нощ",
        "morning": "Добро утро", "afternoon": "Добър ден", "evening": "Добър вечер",
        "none": "{greet}. Нямаш отворени задачи. Базата е празна или всичко е затворено.",
        "open": "Имаш {n} отворени {tasks}",
        "of_which": ", от които {list}",
        "n_overdue": "{n} просрочени",
        "n_urgent": "{n} в следващите три дни",
        "n_week": "{n} тази седмица",
        "task_1": "задача", "task_n": "задачи",
        "day_1": "един ден", "day_n": "{n} дни",
        "no_due": "без срок", "today": "днес", "tomorrow": "утре",
        "in_x": "след {d}", "late_by": "просрочена с {d}",
        "overdue_line": "Просрочена: {title}. Беше за {when}. Закъснението е {d}.",
        "earlier": "по-рано",
        "next_line": "Следващият срок е {title}, {rel}{date}.",
        "weight_line": "Тежи {w}.",
        "hand_in": "Предава се: {d}",
        "by_priority": "Следващите по приоритет: {list}.",
        "undated": "Още {n} {tasks} чакат без обявен срок.",
        "clock": "24h",
        "at_oclock": "{stamp}, в {h} часа",
        "at_time": "{stamp}, в {h} и {m:02d}",
        "stale": "Внимание. Последно проверих Brightspace {age}, така че качено след "
                 "това го няма тук. Проверявам сега.",
        "never_synced": "Внимание. Brightspace никога не е проверяван, така че това е "
                        "само каквото вече е в списъка.",
        "age_hours": "преди {n} часа", "age_hour": "преди час",
        "age_days": "преди {n} дни", "age_now": "току-що",
    },
}


def default_lang():
    if CONFIG.exists():
        return json.loads(CONFIG.read_text(encoding="utf-8")).get("report", {}).get("language", "en")
    return "en"


def S(lang):
    return STR.get(lang, STR["en"])


def address(lang="en") -> str:
    """How to address you — config.languages.<lang>.address. Empty string turns it off."""
    if not CONFIG.exists():
        return ""
    langs = json.loads(CONFIG.read_text(encoding="utf-8")).get("languages", {})
    return (langs.get(lang) or {}).get("address", "") or ""


def greeting(now: datetime, lang="en") -> str:
    s, h = S(lang), now.hour
    if h < 5:
        base = s["night"]
    elif h < 12:
        base = s["morning"]
    elif h < 18:
        base = s["afternoon"]
    else:
        base = s["evening"]
    who = address(lang)
    return f"{base}, {who}" if who else base


def plural_days(n: int, lang="en") -> str:
    s = S(lang)
    return s["day_1"] if n == 1 else s["day_n"].format(n=n)


def when(t, lang="en") -> str:
    s, dl = S(lang), days_left(t)
    if dl is None:
        return s["no_due"]
    if dl < 0:
        return s["late_by"].format(d=plural_days(-dl, lang))
    if dl == 0:
        return s["today"]
    if dl == 1:
        return s["tomorrow"]
    return s["in_x"].format(d=plural_days(dl, lang))


def date_phrase(t, lang="en") -> str:
    s = S(lang)
    raw = (t.get("due") or "").strip()
    if not raw:
        return ""
    try:
        d = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    stamp = f"{s['days'][d.weekday()]}, {d.day} {s['months'][d.month - 1]}"
    if not (d.hour or d.minute):
        return stamp
    # 14:00 read out as "at 14 o'clock" is not English — use the 12-hour clock there
    twelve = s.get("clock") == "12h"
    hour = (d.hour % 12 or 12) if twelve else d.hour
    ampm = ("AM" if d.hour < 12 else "PM") if twelve else ""
    key = "at_time" if d.minute else "at_oclock"
    return s[key].format(stamp=stamp, h=hour, m=d.minute, ampm=ampm)


def weight_phrase(t, lang="en") -> str:
    """Say the weight without the bookkeeping — no grade-item ids out loud."""
    src = localize(t, lang).get("weight") or t.get("weight") or ""
    w = re.sub(r"\([^)]*\)", "", src).strip(" ,;")
    return re.sub(r"^\s*(тежест|weight)[:\s]*", "", w, flags=re.I).strip()


def stale_after_hours() -> float:
    if CONFIG.exists():
        return float(json.loads(CONFIG.read_text(encoding="utf-8"))
                     .get("report", {}).get("stale_after_hours", 6))
    return 6.0


def sync_on_start_after_minutes() -> float:
    """How old the data must be before merely opening the dashboard checks Brightspace.

    Deliberately far shorter than `stale_after_hours`: sitting down at the dashboard is
    the moment you are about to trust what it says, so the bar for re-checking is low.
    It is not zero because the dashboard gets restarted several times in a row while
    something is being fixed, and every check wakes a Claude session and drives Chrome.
    Set it to 0 to check on literally every start.
    """
    if CONFIG.exists():
        return float(json.loads(CONFIG.read_text(encoding="utf-8"))
                     .get("report", {}).get("sync_on_start_after_minutes", 30))
    return 30.0


def sync_age_hours(db):
    """Hours since Brightspace was last checked, or None if it never was."""
    raw = db.get("last_sync")
    if not raw:
        return None
    try:
        return (datetime.now() - datetime.fromisoformat(raw)).total_seconds() / 3600
    except (ValueError, TypeError):
        return None


def age_phrase(hours: float, lang="en") -> str:
    """Hours up to a day and a half, then days.

    Deliberately no "yesterday": at half past midnight that could mean forty minutes
    ago or twenty-three hours ago, and the whole point of this sentence is to say how
    far behind the data is.
    """
    s = S(lang)
    if hours < 1.5:
        return s["age_now"] if hours < 0.75 else s["age_hour"]
    if hours < 36:
        return s["age_hours"].format(n=round(hours))
    return s["age_days"].format(n=round(hours / 24))


def staleness_line(db, lang="en"):
    """The sentence that opens a brief built on data that may have moved on."""
    s = S(lang)
    age = sync_age_hours(db)
    if age is None:
        return s["never_synced"]
    if age >= stale_after_hours():
        return s["stale"].format(age=age_phrase(age, lang))
    return None


def live(db):
    return [t for t in db["tasks"] if t.get("status") not in ("done", "dropped")]


def numbers(db):
    ls = live(db)
    return {
        "open": len(ls),
        "done": len([t for t in db["tasks"] if t.get("status") == "done"]),
        "overdue": [t for t in ls if (days_left(t) is not None and days_left(t) < 0)],
        "urgent": [t for t in ls if (days_left(t) is not None and 0 <= days_left(t) <= 3)],
        "week": [t for t in ls if (days_left(t) is not None and 0 <= days_left(t) <= 7)],
        "undated": [t for t in ls if days_left(t) is None],
        "ranked": sorted(ls, key=score, reverse=True),
    }


def open_line(n, lang) -> str:
    s = S(lang)
    head = s["open"].format(n=n["open"], tasks=s["task_1"] if n["open"] == 1 else s["task_n"])
    bits = []
    if n["overdue"]:
        bits.append(s["n_overdue"].format(n=len(n["overdue"])))
    if n["urgent"]:
        bits.append(s["n_urgent"].format(n=len(n["urgent"])))
    elif n["week"]:
        bits.append(s["n_week"].format(n=len(n["week"])))
    return head + (s["of_which"].format(list=" and ".join(bits) if lang == "en"
                                        else " и ".join(bits)) if bits else "") + "."


def build(now=None, lang=None) -> str:
    lang = lang or default_lang()
    s = S(lang)
    now = now or datetime.now()
    db = load()
    n = numbers(db)
    if not n["ranked"]:
        return s["none"].format(greet=greeting(now, lang))

    out = [f"{greeting(now, lang)}. {open_line(n, lang)}"]

    # said second, right after the greeting, so it is heard rather than tacked on the end
    stale = staleness_line(db, lang)
    if stale:
        out.append(stale)

    for t in n["overdue"]:
        out.append(s["overdue_line"].format(
            title=t["title"], when=date_phrase(t, lang) or s["earlier"],
            d=plural_days(-days_left(t), lang)))

    upcoming = [t for t in n["ranked"] if days_left(t) is not None and days_left(t) >= 0]
    nxt = min(upcoming, key=days_left) if upcoming else None
    if nxt:
        loc = localize(nxt, lang)
        d = date_phrase(nxt, lang)
        out.append(s["next_line"].format(title=nxt["title"], rel=when(nxt, lang),
                                         date=f", {d}" if d else ""))
        if weight_phrase(nxt, lang):
            out.append(s["weight_line"].format(w=weight_phrase(nxt, lang)))
        for item in (loc.get("deliverables") or [])[:2]:
            out.append(s["hand_in"].format(d=item))

    rest = [t for t in n["ranked"] if t is not nxt][:3]
    if rest:
        out.append(s["by_priority"].format(
            list="; ".join(f"{t['title']}, {when(t, lang)}" for t in rest)))

    if n["undated"]:
        cnt = len(n["undated"])
        out.append(s["undated"].format(n=cnt, tasks=s["task_1"] if cnt == 1 else s["task_n"]))

    return "\n\n".join(out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--lang", default=None, choices=list(STR))
    p.add_argument("--json", action="store_true")
    a = p.parse_args()
    lang = a.lang or default_lang()
    text = build(lang=lang)
    if a.json:
        n = numbers(load())
        print(json.dumps({
            "lang": lang, "brief": text, "open": n["open"], "done": n["done"],
            "overdue": [t["id"] for t in n["overdue"]],
            "urgent": [t["id"] for t in n["urgent"]],
        }, ensure_ascii=False, indent=2))
    else:
        print(text)


if __name__ == "__main__":
    main()
