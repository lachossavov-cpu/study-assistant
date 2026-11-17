#!/usr/bin/env python3
"""Пълен тест на системата. Не пипа истинската база — работи с временно копие.

  python tests/run_tests.py
  python tests/run_tests.py --no-network    # прескача edge-tts (иска интернет)
"""
import argparse
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

# Windows конзолата е cp1252 по подразбиране — без това кирилицата гърми
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="avans-tests-"))
os.environ["AVANS_DB"] = str(TMP / "tasks.json")     # must be set before importing tasks
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, str(ROOT / "tools"))

import brief as brief_mod  # noqa: E402
import speak as speak_mod  # noqa: E402
import tasks as tasks_mod  # noqa: E402

def free_port():
    """Ask the OS for a port nobody is using.

    A fixed port meant the suite refused to run for a couple of minutes after an
    interrupted pass, because Windows keeps the old socket in TIME_WAIT. A test run
    should never fail for a reason that has nothing to do with the code.
    """
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
SERVER_LOG = TMP / "server-output.log"

PASSED, FAILED, SKIPPED = [], [], []
_section = ""


def section(name):
    global _section
    _section = name
    print(f"\n\033[36m── {name} {'─' * max(0, 58 - len(name))}\033[0m")


def check(desc, fn):
    try:
        r = fn()
    except Exception as e:
        FAILED.append((_section, desc, f"{type(e).__name__}: {e}"))
        print(f"  \033[31mFAIL\033[0m  {desc}")
        print(f"        {traceback.format_exc().strip().splitlines()[-1]}")
        return False
    if r is False:
        FAILED.append((_section, desc, "returned False"))
        print(f"  \033[31mFAIL\033[0m  {desc}")
        return False
    PASSED.append((_section, desc))
    print(f"  \033[32mok\033[0m    {desc}" + (f"  \033[90m{r}\033[0m" if isinstance(r, str) else ""))
    return True


def skip(desc, why):
    SKIPPED.append((_section, desc, why))
    print(f"  \033[33mskip\033[0m  {desc}  \033[90m({why})\033[0m")


def silent(fn, *a, **kw):
    """CLI функциите печатат JSON — тук не ни трябва в доклада."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **kw)


def eq(a, b, label=""):
    if a != b:
        raise AssertionError(f"{label}expected {b!r}, got {a!r}")
    return True


# ------------------------------------------------------------------ fixture

def iso(days, hour=None):
    d = date.today() + timedelta(days=days)
    return f"{d.isoformat()}T{hour:02d}:00" if hour is not None else d.isoformat()


FIXTURE = [
    {"id": "t-overdue", "title": "Overdue essay", "type": "assignment",
     "due": iso(-3), "weight": "20% of the grade (item 1)", "source_unit": "x",
     "deliverables": ["PDF in Brightspace"], "requirements": ["APA"], "resources": []},
    {"id": "t-today", "title": "Presentation today", "type": "assignment",
     "due": iso(0, 14), "weight": None, "source_unit": "x",
     "deliverables": [], "requirements": [], "resources": []},
    {"id": "t-soon", "title": "Video opdracht", "type": "assignment",
     "due": iso(2, 12), "weight": "10% of the grade", "source_unit": "x",
     "deliverables": ["A YouTube link"], "requirements": ["the link must work"],
     "resources": [],
     "i18n": {"bg": {"weight": "10% от оценката",
                     "deliverables": ["Линк към YouTube"],
                     "requirements": ["линкът трябва да работи"],
                     "description": "Видео заданието."}}},
    {"id": "t-far", "title": "Big project", "type": "project",
     "due": iso(30), "weight": "40% of the grade", "source_unit": "x",
     "deliverables": [], "requirements": [], "resources": []},
    {"id": "t-undated", "title": "Reading with no deadline", "type": "reading",
     "due": None, "weight": None, "source_unit": "x",
     "deliverables": [], "requirements": [], "resources": []},
]


def write_fixture(data=None):
    p = TMP / "incoming.json"
    p.write_text(json.dumps(data if data is not None else FIXTURE, ensure_ascii=False),
                 encoding="utf-8")
    return p


# ------------------------------------------------------------------ tasks.py

def test_tasks():
    section("tools/tasks.py — база от задачи")

    check("стартира с празна база", lambda: eq(tasks_mod.load()["tasks"], []))

    def upsert_adds():
        silent(tasks_mod.cmd_upsert, argparse.Namespace(file=str(write_fixture())))
        return eq(len(tasks_mod.load()["tasks"]), 5)
    check("upsert добавя 5 задачи", upsert_adds)

    def defaults():
        t = next(x for x in tasks_mod.load()["tasks"] if x["id"] == "t-soon")
        eq(t["status"], "todo", "status: ")
        eq(t["priority"], 3, "priority: ")
        assert t.get("first_seen") and t.get("last_seen")
        return True
    check("новите получават status=todo, priority=3, времеви печат", defaults)

    def preserves_mine():
        db = tasks_mod.load()
        for t in db["tasks"]:
            if t["id"] == "t-soon":
                t["status"], t["priority"], t["notes"] = "in_progress", 1, "моя бележка"
        tasks_mod.save(db)
        changed = [dict(x) for x in FIXTURE]
        for t in changed:
            if t["id"] == "t-soon":
                t["title"] = "Video opdracht (променено)"
        silent(tasks_mod.cmd_upsert, argparse.Namespace(file=str(write_fixture(changed))))
        t = next(x for x in tasks_mod.load()["tasks"] if x["id"] == "t-soon")
        eq(t["status"], "in_progress", "status: ")
        eq(t["priority"], 1, "priority: ")
        eq(t["notes"], "моя бележка", "notes: ")
        eq(t["title"], "Video opdracht (променено)", "title: ")
        return True
    check("ресинхронизацията пази status/priority/notes, но обновява заглавието", preserves_mine)

    def records_diff():
        h = tasks_mod.load()["history"][0]
        d = next(c for c in h["changes"] if c["id"] == "t-soon")
        eq(d["change"], "updated")
        eq(d["diffs"][0]["field"], "title")
        return True
    check("промяната се записва в историята с from/to", records_diff)

    def flags_missing():
        silent(tasks_mod.cmd_upsert, argparse.Namespace(
            file=str(write_fixture([t for t in FIXTURE if t["id"] != "t-far"]))))
        t = next(x for x in tasks_mod.load()["tasks"] if x["id"] == "t-far")
        assert "missing_from_last_sync" in t.get("flags", []), t.get("flags")
        silent(tasks_mod.cmd_upsert, argparse.Namespace(file=str(write_fixture())))
        return True
    check("изчезнала от Brightspace задача се маркира, не се трие", flags_missing)

    def days():
        eq(tasks_mod.days_left({"due": iso(-3)}), -3, "date-only: ")
        eq(tasks_mod.days_left({"due": iso(2, 12)}), 2, "datetime: ")
        eq(tasks_mod.days_left({"due": None}), None, "none: ")
        eq(tasks_mod.days_left({"due": "боклук"}), None, "invalid: ")
        return True
    check("days_left разбира дата, дата+час, null и боклук", days)

    def ordering():
        db = tasks_mod.load()
        by = {t["id"]: tasks_mod.score(t) for t in db["tasks"]}
        assert by["t-overdue"] > by["t-today"] > by["t-far"] > by["t-undated"], by
        return " > ".join(f"{k}:{v}" for k, v in sorted(by.items(), key=lambda x: -x[1]))
    check("приоритетът реди: просрочена > днес > далечна > без срок", ordering)

    def done_stamps():
        silent(tasks_mod.cmd_set, argparse.Namespace(id="t-today", pairs=["status=done"]))
        t = next(x for x in tasks_mod.load()["tasks"] if x["id"] == "t-today")
        eq(t["status"], "done")
        assert t.get("completed")
        eq(tasks_mod.score(t), -100, "score of done: ")
        return True
    check("status=done слага печат и изважда задачата от подредбата", done_stamps)

    def bad_status():
        try:
            silent(tasks_mod.cmd_set, argparse.Namespace(id="t-soon", pairs=["status=нещо"]))
        except SystemExit:
            return True
        raise AssertionError("прие невалиден статус")
    check("невалиден статус се отхвърля", bad_status)

    def stats_shape():
        db = tasks_mod.load()
        n = brief_mod.numbers(db)
        eq(len(n["overdue"]), 1, "overdue: ")
        eq(n["done"], 1, "done: ")
        assert any(t["id"] == "t-soon" for t in n["urgent"]), "t-soon трябва да е спешна"
        return True
    check("броячите смятат вярно (1 просрочена, 1 готова)", stats_shape)


# ------------------------------------------------------------------ brief.py

def test_brief():
    section("tools/brief.py — двуезичен бриф")

    check("езикът по подразбиране е английски", lambda: eq(brief_mod.default_lang(), "en"))

    en = brief_mod.build(lang="en")
    bg = brief_mod.build(lang="bg")

    check("английският бриф се генерира", lambda: len(en) > 80 or False)
    check("българският бриф се генерира", lambda: len(bg) > 80 or False)
    check("двата са наистина различни", lambda: en != bg)
    check("EN е на английски, без кирилица", lambda: not re.search(r"[А-Яа-я]", en) or False)
    check("BG съдържа кирилица", lambda: bool(re.search(r"[А-Яа-я]", bg)))
    check("EN споменава просрочената", lambda: "Overdue" in en or "overdue" in en)
    check("BG споменава просрочената", lambda: "росрочен" in bg)

    def titles_stay():
        # заглавието е името в Brightspace — превод би го направил ненамираем
        for txt in (en, bg):
            assert "Video opdracht" in txt, txt[:120]
        return "„Video opdracht\" е и в двата"
    check("заглавията НЕ се превеждат (иначе не ги намираш в Brightspace)", titles_stay)

    check("id-та не се изговарят", lambda: "t-soon" not in en and "t-soon" not in bg)

    def localized_fields():
        t = next(x for x in tasks_mod.load()["tasks"] if x["id"] == "t-soon")
        eq(tasks_mod.localize(t, "en")["weight"], "10% of the grade", "en: ")
        eq(tasks_mod.localize(t, "bg")["weight"], "10% от оценката", "bg: ")
        eq(tasks_mod.localize(t, "de")["weight"], "10% of the grade", "непознат език пада към EN: ")
        eq(tasks_mod.localize(t, "en")["title"], tasks_mod.localize(t, "bg")["title"], "title: ")
        return True
    check("localize сменя преводимите полета и пада към английски", localized_fields)

    def instructions_are_carried_and_translated():
        """Заданието — какво точно да направиш — стига до картата и се превежда.

        Дропбокс папката носи само как се предава („качи в YouTube, попълни формата");
        самото задание живее в документите, затова е отделно поле, което човек пълни.
        """
        assert "instructions" in tasks_mod.TRANSLATABLE, "заданието не се превежда"
        assert "instructions" in tasks_mod.SCRAPED, \
            "заданието ще се губи при следващ upsert"
        t = {"id": "x", "title": "X", "instructions": "Make a 3 minute video",
             "i18n": {"bg": {"instructions": "Направи видео от 3 минути"}}}
        eq(tasks_mod.localize(t, "en")["instructions"], "Make a 3 minute video", "en: ")
        eq(tasks_mod.localize(t, "bg")["instructions"], "Направи видео от 3 минути", "bg: ")

        html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
        for needle in ("t.instructions", "brief-missing", "briefFrom"):
            assert needle in html, f"картата не показва {needle!r}"
        return True
    check("заданието стига до картата, превежда се и преживява upsert",
          instructions_are_carried_and_translated)

    def report_always_rechecks():
        """Репорт проверява Brightspace ВИНАГИ, не само при остарели данни.

        Преди това проверяваше само ако `freshness.stale`, тоест при праг 6 часа можеше
        да изговори числа отпреди 5 часа, без да провери нищо. Натискането на Репорт е
        моментът, в който тези числа се повтарят на глас - значи е и моментът да се
        проверят.
        """
        html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
        i = html.index("$('report').addEventListener")
        handler = html[i:i + 1400]
        assert "requestSync({ force: true, speakChanges: true })" in handler, \
            "Репорт не иска безусловна проверка"
        assert "freshness.stale) requestSync" not in handler, \
            "още проверява само при остарели данни"

        # промените се казват на глас, но чак когато репортът е свършил -
        # say() отменя предишния говор и щеше да реже репорта по средата
        assert "speakWhenQuiet" in html, "няма изчакване преди да проговори"
        j = html.index("async function speakWhenQuiet")
        assert "while (speech.key" in html[j:j + 400], \
            "speakWhenQuiet не изчаква текущия говор"
        for needle in ("spokenMoved", "spokenNewFolder", "spokenNews",
                       "syncFailedSpoken"):
            assert needle in html, f"липсва изговорим текст {needle!r}"
        return True
    check("Репорт проверява Brightspace всеки път и не реже говора",
          report_always_rechecks)

    def login_is_one_click():
        """Сесията в JARVIS профила умира за около денонощие.

        Ако единственият изход е да намериш терминал и да напишеш команда, функцията
        тихо спира да се ползва. Затова таблото дава бутон, а сървърът само отваря
        прозорец — през него не минава никаква парола.
        """
        html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
        assert "logAction" in html, "няма ред с действие в лога"
        assert "/api/cdp/login" in html, "бутонът не вика endpoint-а"
        assert "loginBtn" in html and "loginOpening" in html, "липсват текстовете"
        return True
    check("изтекла сесия се оправя с един бутон, не с команда", login_is_one_click)

    check("EN тежестта е на английски", lambda: "10% of the grade" in en)
    check("BG тежестта е преведена", lambda: "10% от оценката" in bg)

    check("weight_phrase маха скобите",
          lambda: eq(brief_mod.weight_phrase({"weight": "20% of the grade (item 1)"}),
                     "20% of the grade"))
    check("weight_phrase маха водещото 'weight:'/'тежест:'",
          lambda: eq(brief_mod.weight_phrase({"weight": "weight: 10%"}), "10%")
          and eq(brief_mod.weight_phrase({"weight": "тежест: 10%"}), "10%"))

    def dp_en():
        s = brief_mod.date_phrase({"due": "2026-09-16T14:00"}, "en")
        # „at 14 o'clock" не е английски — 14:00 се чете като 2 PM
        assert "Wednesday" in s and "16 September" in s and "2 PM" in s, s
        assert "14" not in s, "24-часов формат в английски текст: " + s
        eq(brief_mod.date_phrase({"due": "2026-09-16"}, "en"), "Wednesday, 16 September")
        return s
    check("EN дата: ден от седмицата и 12-часов формат", dp_en)

    def dp_bg():
        s = brief_mod.date_phrase({"due": "2026-09-16T14:00"}, "bg")
        assert "сряда" in s and "16 септември" in s and "14 часа" in s, s
        eq(brief_mod.date_phrase({"due": None}, "bg"), "")
        return s
    check("BG дата: ден от седмицата и 24-часов формат", dp_bg)

    def empty():
        keep = tasks_mod.load()
        tasks_mod.save({"tasks": [], "last_sync": None, "history": []})
        e, b = brief_mod.build(lang="en"), brief_mod.build(lang="bg")
        tasks_mod.save(keep)
        assert "no open tasks" in e, e
        assert "Нямаш отворени задачи" in b, b
        return True
    check("празна база дава смислен текст на двата езика", empty)

    def hours():
        for h, en_w, bg_w in ((3, "middle of the night", "посред нощ"),
                              (9, "Good morning", "Добро утро"),
                              (14, "Good afternoon", "Добър ден"),
                              (21, "Good evening", "Добър вечер")):
            assert en_w in brief_mod.greeting(datetime(2026, 1, 1, h), "en"), f"{h}h en"
            assert bg_w in brief_mod.greeting(datetime(2026, 1, 1, h), "bg"), f"{h}h bg"
        return True
    check("поздравът се сменя според часа, на двата езика", hours)

    def staleness():
        """Регресия: брифът никога не докосваше Brightspace и не казваше нито дума, че
        чете от снимка. Веднъж данните бяха на шест дни, а в непрочетените съобщения
        имаше задължителна сесия след три дни."""
        db = tasks_mod.load()
        keep = db.get("last_sync")
        try:
            db["last_sync"] = datetime.now().isoformat(timespec="seconds")
            tasks_mod.save(db)
            assert brief_mod.staleness_line(tasks_mod.load(), "en") is None, \
                "предупреждава, а данните са пресни"

            db["last_sync"] = (datetime.now() - timedelta(hours=30)).isoformat(timespec="seconds")
            tasks_mod.save(db)
            en = brief_mod.staleness_line(tasks_mod.load(), "en")
            bg = brief_mod.staleness_line(tasks_mod.load(), "bg")
            assert en and "30 hours ago" in en, en
            assert bg and "преди 30 часа" in bg, bg
            assert "Careful" in en and "Внимание" in bg

            db["last_sync"] = (datetime.now() - timedelta(days=6)).isoformat(timespec="seconds")
            tasks_mod.save(db)
            assert "6 days ago" in brief_mod.staleness_line(tasks_mod.load(), "en")

            db["last_sync"] = None
            tasks_mod.save(db)
            never = brief_mod.staleness_line(tasks_mod.load(), "en")
            assert "never been checked" in never, never

            db["last_sync"] = (datetime.now() - timedelta(hours=30)).isoformat(timespec="seconds")
            tasks_mod.save(db)
            assert brief_mod.staleness_line(tasks_mod.load(), "en") in brief_mod.build(lang="en"), \
                "предупреждението не влиза в самия бриф"
        finally:
            db["last_sync"] = keep
            tasks_mod.save(db)
        return "пресни → тихо; 30ч / 6 дни / никога → казва го"
    check("брифът казва, ако чете от остарели данни", staleness)

    def ages():
        # нарочно без „вчера": в 00:30 то значи и преди 40 минути, и преди 23 часа
        for hours, want_en in ((0.2, "just now"), (1.0, "an hour ago"), (5, "5 hours ago"),
                               (24, "24 hours ago"), (30, "30 hours ago"),
                               (48, "2 days ago"), (144, "6 days ago")):
            got = brief_mod.age_phrase(hours, "en")
            assert got == want_en, f"{hours}ч → {got!r}, чакано {want_en!r}"
        assert brief_mod.age_phrase(144, "bg") == "преди 6 дни"
        return True
    check("възрастта се изговаря човешки на всеки мащаб", ages)

    def addressed():
        eq(brief_mod.address("en"), "sir", "en: ")
        assert brief_mod.greeting(datetime(2026, 9, 8, 9), "en").endswith(", sir"), \
            brief_mod.greeting(datetime(2026, 9, 8, 9), "en")
        assert brief_mod.greeting(datetime(2026, 9, 8, 9), "bg").endswith(", сър")
        assert en.startswith("Good") and ", sir." in en.split("\n")[0], en[:60]
        return brief_mod.greeting(datetime(2026, 9, 8, 9), "en")
    check("брифът се обръща с обръщението от config.json", addressed)

    def address_optional():
        """Обръщението е настройка, не вградено — празно го изключва."""
        import importlib
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        keep = cfg["languages"]["en"].get("address")
        try:
            cfg["languages"]["en"]["address"] = ""
            (ROOT / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
            importlib.reload(brief_mod)
            g = brief_mod.greeting(datetime(2026, 9, 8, 9), "en")
            assert g == "Good morning", g
        finally:
            cfg["languages"]["en"]["address"] = keep
            (ROOT / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
            importlib.reload(brief_mod)
        return "празно обръщение го маха чисто"
    check("обръщението може да се изключи", address_optional)


# ------------------------------------------------------------------ speak.py

def test_speak(network):
    section("tools/speak.py — глас")

    def md():
        s = speak_mod.strip_markdown(
            "# Заглавие\n\n```python\nprint(1)\n```\n\n- точка **удебелена**\n"
            "[линк](https://x.com) и https://y.com\n\n| a | b |\n|---|---|")
        for bad in ("#", "```", "**", "http", "|"):
            assert bad not in s, f"{bad!r} остана в: {s!r}"
        assert "Заглавие" in s and "удебелена" in s and "линк" in s
        return repr(s[:60])
    check("strip_markdown чисти код, линкове, таблици и оставя думите", md)

    def chunking():
        long = ("Sentence number one. " * 400)
        parts = speak_mod.chunks(long, 2800)
        assert len(parts) > 1, "не се разцепи"
        assert all(len(p) <= 2800 for p in parts), [len(p) for p in parts]
        return f"{len(long)} знака → {len(parts)} парчета"
    check("дългият текст се цепи по изречения", chunking)

    def no_loss():
        """Регресия: старата версия правеше sent[:size] и мълчаливо изхвърляше
        остатъка — цели пасажи никога не се изговаряха."""
        cases = {
            "изречение без точки, по-дълго от лимита": "Word " * 900,
            "нормален текст": "Sentence one. Sentence two. " * 200,
            "параграфи": "A paragraph with text in it.\n\n" * 300,
            "една чудовищна дума": "x" * 9000,
            "смесен": "Language choice video is due Wednesday. " * 250,
        }
        worst = []
        for name, text in cases.items():
            parts = speak_mod.chunks(text, 2800)
            a = re.sub(r"\s+", "", text)
            b = re.sub(r"\s+", "", "".join(parts))
            if len(a) != len(b):
                worst.append(f"{name}: загубени {len(a) - len(b)} знака")
            assert all(len(p) <= 2800 for p in parts), f"{name}: парче над лимита"
        assert not worst, "; ".join(worst)
        return f"{len(cases)} случая, нула загубени знаци"
    check("цепенето НЕ губи нито един знак", no_loss)

    check("късият текст остава едно парче",
          lambda: eq(len(speak_mod.chunks("Short.", 2800)), 1))

    if not network:
        skip("edge-tts произвежда валиден MP3", "--no-network")
        return

    def synth():
        import asyncio
        out = TMP / "voice.mp3"
        size = asyncio.run(speak_mod.synth("Тест на българския глас.", out,
                                           "bg-BG-KalinaNeural", "+0%", "+0%", "+0Hz"))
        b = out.read_bytes()
        # или ID3 таг, или 11-битов MPEG frame sync (0xFF Ex) — edge-tts дава и двете
        assert b[:3] == b"ID3" or (b[0] == 0xFF and (b[1] & 0xE0) == 0xE0), \
            f"не прилича на MP3: {b[:4]!r}"
        assert size > 5000, size
        return f"{size // 1024} KB"
    check("edge-tts произвежда валиден MP3 на български", synth)


# ------------------------------------------------------------------ icons

def test_icons():
    section("икони и manifest")
    st = ROOT / "server" / "static"

    for name, wh in (("icon-192.png", 192), ("icon-512.png", 512),
                     ("icon-512-maskable.png", 512)):
        def one(name=name, wh=wh):
            p = st / name
            assert p.exists(), f"липсва {p}"
            b = p.read_bytes()
            eq(b[:8], b"\x89PNG\r\n\x1a\n", "PNG magic: ")
            w = int.from_bytes(b[16:20], "big")
            h = int.from_bytes(b[20:24], "big")
            eq((w, h), (wh, wh), "размер: ")
            return f"{w}×{h}, {len(b) // 1024} KB"
        check(f"{name} е валиден PNG с верен размер", one)

    def ico():
        p = ROOT / "jarvis.ico"
        assert p.exists(), "липсва jarvis.ico"
        b = p.read_bytes()
        eq(b[:4], b"\x00\x00\x01\x00", "ICO header: ")
        n = int.from_bytes(b[4:6], "little")
        assert n >= 5, f"само {n} размера в иконата"
        return f"{n} размера, {len(b) // 1024} KB"
    check("jarvis.ico съдържа няколко размера", ico)

    def manifest():
        m = json.loads((st / "manifest.webmanifest").read_text(encoding="utf-8"))
        for k in ("name", "short_name", "start_url", "display", "icons"):
            assert k in m, f"липсва {k}"
        eq(m["display"], "standalone", "display: ")
        assert any(i.get("purpose") == "maskable" for i in m["icons"]), "няма maskable"
        for i in m["icons"]:
            rel = i["src"].lstrip("/").replace("static/", "")
            assert (st / rel).exists(), f"manifest сочи липсващ {i['src']}"
        return f"{len(m['icons'])} икони, всички налични"
    check("manifest е валиден и сочи съществуващи файлове", manifest)


# ------------------------------------------------------------------ server

def req(path, method="GET", body=None, timeout=90):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    # кирилица в пътя трябва да се процентно-кодира; вече кодираното (%2F) се пази
    url = BASE + urllib.parse.quote(path, safe="/%?&=")
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
            ct = resp.headers.get("content-type", "")
            return resp.status, (json.loads(raw) if "json" in ct else raw), ct
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("content-type", "")


def start_server():
    env = {**os.environ, "AVANS_DB": str(TMP / "tasks.json"), "AVANS_ASK": str(TMP / "ask"),
           "AVANS_NO_AUTOSYNC": "1",
           "AVANS_WORK": str(TMP / "work")}
    # Into a FILE, not subprocess.PIPE. Nobody read the pipe, and on Windows it holds a
    # few KB: once the server had printed a couple of tracebacks (edge-tts failing, say)
    # its next print blocked for ever, and every later test timed out on a server that
    # was simply stuck writing. That looked exactly like a broken voice.
    log = open(SERVER_LOG, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [sys.executable, "-c",
         f"import uvicorn,sys; sys.path.insert(0,r'{ROOT}');"
         f"from server.app import app; uvicorn.run(app,host='127.0.0.1',port={PORT},log_level='error')"],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(80):
        if proc.poll() is not None:
            raise RuntimeError("сървърът умря:\n" + SERVER_LOG.read_text(encoding="utf-8", errors="replace"))
        try:
            urllib.request.urlopen(BASE + "/api/state", timeout=1)
            return proc
        except Exception:
            time.sleep(0.25)
    proc.terminate()
    raise RuntimeError("сървърът не тръгна за 20 секунди")


def test_shared_copy():
    """Копие, раздадено на друг човек: собствена папка, без курс, без Claude зад него.

    Върви на ОТДЕЛЕН сървър с ASSISTANT_HOME във временна папка. Основният тестов сървър
    чете истинския config.json, а тези тестове пишат в настройките — срещу него биха
    преименували асистента на собственика.
    """
    section("раздадено копие — профил, настройка, отзиви, телефон")
    home = TMP / "shared-home"
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {k: v for k, v in os.environ.items()
           if k not in ("AVANS_DB", "AVANS_WORK", "AVANS_ASK")}
    env.update(ASSISTANT_HOME=str(home), AVANS_NO_AUTOSYNC="1")
    proc = subprocess.Popen(
        [sys.executable, "-c",
         f"import uvicorn,sys; sys.path.insert(0,r'{ROOT}');"
         f"from server.app import app; uvicorn.run(app,host='127.0.0.1',port={port},log_level='error')"],
        cwd=str(ROOT), env=env, stdout=open(TMP / "shared-server.log", "w"),
        stderr=subprocess.STDOUT)       # a file, not an undrained pipe — see start_server()

    def call(path, method="GET", body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        r = urllib.request.Request(base + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if "json" in resp.headers.get("content-type", "") else raw)
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    try:
        for _ in range(80):
            if proc.poll() is not None:
                raise RuntimeError((TMP / "shared-server.log").read_text(errors="replace"))
            try:
                urllib.request.urlopen(base + "/api/setup", timeout=1)
                break
            except Exception:
                time.sleep(0.25)

        def fresh():
            s_, j = call("/api/setup")
            eq(s_, 200)
            eq(j["onboarded"], False, "ново копие: ")
            eq(j["ai"], False, "AI в ранния достъп: ")
            eq(j["courses"], [], "курсове: ")
            assert (home / "config.json").is_file(), "config не е посят в личната папка"
            return "без курс, без AI, config в собствената папка"
        check("ново копие тръгва ненастроено и в собствена папка", fresh)

        def owner_untouched():
            mine = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
            call("/api/setup", "POST", {"name": "Friday", "address": "boss", "lang": "en",
                                        "courses": [{"org_unit_id": "123", "name": "Test course"}]})
            after = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
            eq(after, mine, "config.json на собственика се промени: ")
            _, st = call("/api/state")
            eq(st["assistant"]["name"], "Friday", "име: ")
            eq(st["assistant"]["onboarded"], True, "настроен: ")
            eq(st["course"], "Test course", "курс: ")
            eq(st["features"]["ai"], False)
            _, br = call("/api/brief?lang=en")
            assert ", boss." in br["text"], br["text"][:80]
            _, man = call("/app.webmanifest")
            eq(man["name"], "Friday", "manifest: ")
            return "Friday / boss — и нищо не е пипнато при собственика"
        check("името и обръщението са на потребителя; чуждият config не се пипа", owner_untouched)

        def bad_inputs():
            eq(call("/api/setup", "POST", {"name": "   "})[0], 400, "празно име: ")
            eq(call("/api/setup", "POST", {"base_url": "http://evil.example/x"})[0], 400, "http адрес: ")
            eq(call("/api/setup", "POST", {"base_url": "https://a.b/../c"})[0], 400, "път в адреса: ")
            _, j = call("/api/setup", "POST", {"courses": [{"org_unit_id": "1; rm", "name": "x"}]})
            eq(j["courses"], [], "нечислов курс е влязъл: ")
            call("/api/setup", "POST", {"courses": [{"org_unit_id": "123", "name": "Test course"}]})
            return True
        check("настройката отказва празно име, не-https адрес и нечислов курс", bad_inputs)

        def no_dead_buttons():
            """Без Claude сесия нищо не бива да влиза в опашка, която никой няма да поеме."""
            eq(call("/api/handoff", "POST", {"task_id": "x"})[0], 409, "handoff: ")
            eq(call("/api/submit", "POST", {"task_id": "x", "what": "y"})[0], 409, "submit: ")
            _, j = call("/api/command", "POST", {"text": "what is the weather like", "lang": "en"})
            eq(j["intent"], "unknown", "свободен въпрос: ")
            assert "question_id" not in j, j
            _, q = call("/api/ask?status=pending")
            eq(q["questions"], [], "нещо е влязло в опашката: ")
            s_, j = call("/api/sync?force=true", "POST")
            eq(j["queued"], None, "sync е опашен за Claude: ")
            return "409 / unknown / празна опашка"
        check("без AI: Реши, Предай и свободните въпроси не трупат заявки", no_dead_buttons)

        def teams_flag():
            """Teams е изключен по подразбиране; включва се от настройката и влиза в state."""
            _, st = call("/api/state")
            eq(st["features"]["teams"], False, "по подразбиране: ")
            assert "schedule" in st and "schedule_sync" in st, "state няма разписание"
            s_, j = call("/api/teams/sync", "POST")   # изключен -> не стартира
            eq(j.get("started"), False, "стартира при изключен Teams: ")
            call("/api/setup", "POST", {"teams": True})
            _, st = call("/api/state")
            eq(st["features"]["teams"], True, "след включване: ")
            cfgf = json.loads((home / "config.json").read_text(encoding="utf-8"))
            eq(cfgf["teams"]["enabled"], True)
            call("/api/setup", "POST", {"teams": False})
            return "изключен по подразбиране, state носи schedule, настройката го превключва"
        check("Teams разписанието е изключено по подразбиране и се превключва", teams_flag)

        CYR = re.compile(r"[Ѐ-ӿ]")

        def beta_layout():
            _, st = call("/api/state")
            eq(st["ui"], {"english_only": True, "overdue_tab": True, "split_sources": True},
               "ui в бетата: ")
            _, st = call("/api/state?lang=bg")
            eq(st["lang"], "en", "бетата прие ?lang=bg: ")
            _, langs = call("/api/languages")
            eq([l["code"] for l in langs["available"]], ["en"], "езици: ")
            # личното табло (основният тестов сървър чете config.json на собственика)
            _, mine, _ = req("/api/state")
            assert not any(mine["ui"].values()), f"личното табло получи бета изгледа: {mine['ui']}"
            return "само английски, Overdue и разделени източници; личното табло е както беше"
        check("бетата е на английски, с Overdue и разделени източници — личното не", beta_layout)

        def defaults_reach_old_installs():
            """Копие, инсталирано с 0.1.0, има config.json без `ui`. Новият превключвател
            трябва да стигне до него с обновлението, без човекът да прави нищо."""
            cf = home / "config.json"
            c = json.loads(cf.read_text(encoding="utf-8"))
            c.pop("ui", None)
            c.setdefault("assistant", {})["name"] = "Kept"
            cf.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
            _, st = call("/api/state")
            eq(st["ui"]["overdue_tab"], True, "стар config не получи новия превключвател: ")
            eq(st["assistant"]["name"], "Kept", "собственият избор на човека: ")
            call("/api/setup", "POST", {"name": "Friday"})
            after = json.loads(cf.read_text(encoding="utf-8"))
            assert "ui" not in after, "запис на настройката замрази подразбиращите се в config.json"
            return "подразбиращите се отдолу, изборът на човека отгоре, нищо не се замразява"
        check("нови подразбиращи се стигат до вече инсталирани копия", defaults_reach_old_installs)

        def no_bulgarian_in_beta():
            """Всичко, което бетата показва или изговаря, е на английски — включително
            задачите, които autosync сам създава, и тези, записани на български от 0.1.0."""
            import autosync
            keep = autosync.cdp.courses
            autosync.cdp.courses = lambda: [{"org_unit_id": "11", "name": "Course A"},
                                            {"org_unit_id": "22", "name": "Course B"}]
            try:
                fresh, _ = autosync.diff({"folders": [
                    {"ou": "11", "id": 701, "name": "Essay", "due": "2030-01-10T22:59:00.000Z"},
                    {"ou": "22", "id": 702, "name": "Pitch", "due": "2020-01-10T22:59:00.000Z"}],
                    "grades": [], "news": []}, {"tasks": []})
            finally:
                autosync.cdp.courses = keep
            legacy = dict(fresh[1], id="legacy",
                          description="Нова папка за предаване в Brightspace. Подробностите не са прочетени.",
                          deliverables=["Предава се в Brightspace dropbox #9 'Old'"],
                          requirements=["Автоматично засечена папка — описанието не е минало през преценка"])
            db = {"tasks": [dict(t, status="todo") for t in fresh] + [dict(legacy, status="todo")]}
            eq(autosync.english_placeholders(db), 1, "мигрирани задачи: ")
            (home / "data").mkdir(parents=True, exist_ok=True)
            (home / "data" / "tasks.json").write_text(json.dumps(db, ensure_ascii=False), encoding="utf-8")
            seen = []
            for path in ("/api/state", "/api/brief", "/api/setup", "/api/languages"):
                _, body = call(path)
                seen.append(json.dumps(body, ensure_ascii=False))
            for text in ("report", "what is most urgent", "when is essay due", "blah blah"):
                _, body = call("/api/command", "POST", {"text": text})
                seen.append(json.dumps(body, ensure_ascii=False))
            hits = [m.group(0) for s_ in seen for m in [CYR.search(s_)] if m]
            assert not hits, f"кирилица в бетата: {hits[:3]}"
            _, st = call("/api/state")
            eq({t["course"] for t in st["tasks"]}, {"Course A", "Course B"}, "курс на картата: ")
            eq(st["courses_count"], len(json.loads((home / "config.json").read_text(
                encoding="utf-8")).get("courses", [])), "броят курсове идва от config: ")
            return f"{len(seen)} отговора без нито една кирилска буква"
        check("бетата не показва и не изговаря нищо на български", no_bulgarian_in_beta)

        def sync_reports_teams():
            _, j = call("/api/sync?force=true", "POST")
            assert "teams" in j, f"/api/sync не казва нищо за Teams: {j}"
            return True
        check("↻ отговаря и за Teams", sync_reports_teams)

        def feedback_local():
            eq(call("/api/feedback", "POST", {})[0], 400, "празен отзив: ")
            s_, j = call("/api/feedback", "POST", {"rating": 5, "quote": "Saved my deadline",
                                                   "name": "Sam", "may_quote": True})
            eq(s_, 201)
            files = list((home / "feedback").glob("*.json"))
            eq(len(files), 1, "файлове с отзиви: ")
            rec = json.loads(files[0].read_text(encoding="utf-8"))
            eq(rec["may_quote"], True)
            eq(j["mailto"], None, "няма зададен адрес, а има mailto: ")
            assert "Saved my deadline" in j["text"]
            return "записан локално, нищо не се праща само"
        check("отзивът се записва на машината на тестера", feedback_local)

        def phone_gate():
            _, m = call("/api/mobile")
            eq(m["enabled"], False, "по подразбиране: ")
            _, m = call("/api/mobile", "POST", {"enabled": True})
            assert re.fullmatch(r"\d{6}", m["code"]), m
            eq(m["listening"], False, "сървърът е вързан само за 127.0.0.1: ")
            _, m = call("/api/mobile", "POST", {"enabled": False})
            eq(m["devices"], 0)
            return "изключен по подразбиране, шестцифрен код, иска рестарт"
        check("достъпът от телефон е изключен по подразбиране", phone_gate)

        def guard_logic():
            """Чуждо устройство без сдвояване не стига до API-то. Вика се директно върху
            ASGI слоя с подправен адрес — истинска втора машина в теста няма."""
            code = (
                "import asyncio,json,os,sys;"
                f"sys.path.insert(0,r'{ROOT / 'server'}');import app as A;"
                "c=A.paths.read_config();c['mobile']={'enabled':True,'code':'123456','sessions':['tok']};"
                "A.paths.write_config(c);\n"
                "async def hit(path,cookie=None):\n"
                "    out={}\n"
                "    hdr=[(b'cookie',b'pair='+cookie.encode())] if cookie else []\n"
                "    scope={'type':'http','method':'GET','path':path,'raw_path':path.encode(),"
                "'query_string':b'','headers':hdr,'client':('192.168.1.50',5555),"
                "'server':('x',80),'scheme':'http','http_version':'1.1','root_path':''}\n"
                "    async def rcv(): return {'type':'http.request','body':b'','more_body':False}\n"
                "    async def snd(m):\n"
                "        if m['type']=='http.response.start': out['s']=m['status']\n"
                "    async def inner(sc,r,sn): await sn({'type':'http.response.start','status':200,'headers':[]})\n"
                "    await A.LanGuard(inner)(scope,rcv,snd)\n"
                "    return out['s']\n"
                "r=asyncio.run(hit('/api/state'));r2=asyncio.run(hit('/api/state','wrong'));"
                "r3=asyncio.run(hit('/api/setup','tok'));r4=asyncio.run(hit('/'));"
                "print(json.dumps([r,r2,r3,r4]))"
            )
            out = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(ROOT),
                                 capture_output=True, text=True, timeout=60)
            got = json.loads(out.stdout.strip().splitlines()[-1]) if out.stdout.strip() else out.stderr[-300:]
            eq(got, [401, 401, 200, 401], "без бисквитка / грешна / вярна / страница: ")
            return "несдвоено устройство получава 401, сдвоеното минава"
        check("чуждо устройство минава само след сдвояване", guard_logic)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_multi_course():
    section("tools/autosync.py — няколко курса, без Claude")
    import autosync
    import cdp

    def flattened():
        data = {"folders": [{"ou": "11", "id": 501, "name": "Essay", "due": "2030-01-10T22:59:00.000Z"},
                            {"ou": "22", "id": 502, "name": "Essay", "due": None}],
                "grades": [], "news": []}
        keep = cdp.courses
        cdp.courses = lambda: [{"org_unit_id": "11", "name": "Course A"},
                               {"org_unit_id": "22", "name": "Course B"}]
        try:
            new, moved = autosync.diff(data, {"tasks": []})
        finally:
            cdp.courses = keep
        eq([t["source_unit"] for t in new], ["11", "22"], "курсът на всяка папка: ")
        eq([t["course"] for t in new], ["Course A", "Course B"], "име на курса: ")
        eq(moved, [])
        return "всяка нова папка помни от кой курс е"
    check("папките от различни курсове не се смесват", flattened)

    def js_formats():
        js = autosync.FETCH_JS % {"V": "1.87", "OUS": json.dumps(["11", "22"])}
        assert 'OUS = ["11", "22"]' in js, js[:120]
        assert "%(" not in js and "%(" not in (autosync.COURSES_JS % {"V": "1.87"})
        return True
    check("заявката към Brightspace се сглобява за списък от курсове", js_formats)

    def teams_read_only():
        """tools/teams.py не бива НИКОГА да пише в Graph — нито да връща токена."""
        src = (ROOT / "tools" / "teams.py").read_text(encoding="utf-8")
        for bad in ("method: 'POST'", 'method:"POST"', "method: 'DELETE'", "method: 'PATCH'",
                    "method: 'PUT'", "/sendMail", "method:'POST'"):
            assert bad not in src, f"teams.py съдържа пишещо извикване: {bad}"
        # токенът се нулира в кода и не се връща навън
        assert "token = null" in src, "токенът не се занулява след ползване"
        assert "return" in src and "secret" not in src.split("CALENDAR_JS")[0]
        import teams as teams_mod
        assert teams_mod.enabled() in (True, False)
        return "само GET на календара, токенът не напуска страницата"
    check("Teams четецът е само за четене и не изнася токена", teams_read_only)

    def cross_platform():
        import paths
        assert paths.user_data_dir().name == "JARVIS"
        assert cdp.bs_host() and cdp.home_url().startswith("https://")
        assert isinstance(cdp.CHROME_CANDIDATES, list) and cdp.CHROME_CANDIDATES
        return f"{len(cdp.CHROME_CANDIDATES)} места, където се търси браузър (Chrome, после Edge)"
    check("профилът на браузъра не зависи от LOCALAPPDATA и познава Edge", cross_platform)

    def english_dashboard_strings():
        page = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
        en = page[page.index("\n  en: {"):page.index("\n  bg: {")]
        hit = re.search(r"[Ѐ-ӿ].{0,30}", en)
        assert not hit, f"кирилица в английските надписи: {hit.group(0)}"
        for key in ("tabOverdue", "srcBrightspace", "srcTeams", "teamsChecking", "teamsDone"):
            assert key + ":" in en, f"липсва {key}"
        return "английският блок е изцяло на английски"
    check("английските надписи на таблото нямат кирилица", english_dashboard_strings)

    def browser_takes_turns():
        """↻ пуска Brightspace и Teams заедно; двете карат един профил на браузъра.
        Teams трябва да чака, докато Brightspace държи браузъра."""
        code = (
            "import sys,time,json;"
            f"sys.path.insert(0,r'{ROOT / 'server'}');import app as A;import teams as T;"
            "ran=[];T.run=lambda **k: ran.append(1) or {'ok':True,'events':[]};"
            "A.BROWSER_LOCK.acquire();A.start_teams();time.sleep(0.8);before=len(ran);"
            "A.BROWSER_LOCK.release();time.sleep(0.8);"
            "print(json.dumps([before,len(ran),A.TEAMS['state']]))"
        )
        env = {**os.environ, "AVANS_NO_AUTOSYNC": "1"}
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             timeout=60, env=env, cwd=str(ROOT))
        got = json.loads(out.stdout.strip().splitlines()[-1]) if out.stdout.strip() else out.stderr[-300:]
        eq(got, [0, 1, "done"], "преди / след освобождаване / състояние: ")
        return "Teams изчаква браузъра, после минава"
    check("Brightspace и Teams не карат браузъра едновременно", browser_takes_turns)


def test_server(proc, network):
    section("server/app.py — HTTP")

    def state():
        s, j, _ = req("/api/state")
        eq(s, 200, "status: ")
        for k in ("now", "course", "counts", "tasks", "next_deadline"):
            assert k in j, f"липсва {k}"
        ids = [t["id"] for t in j["tasks"]]
        assert "t-today" not in ids, "готовата задача не бива да е в активните"
        # пръстенът показва най-натежалата ДАТИРАНА задача — просрочената бие бъдещата
        eq(j["next_deadline"]["id"], "t-overdue", "next_deadline: ")
        return f"{len(j['tasks'])} активни, пръстенът сочи {j['next_deadline']['id']}"
    check("GET /api/state връща пълно състояние; пръстенът сочи просрочената", state)

    def ring_moves_on():
        req("/api/task/t-overdue", "PATCH", {"status": "done"})
        _, j, _ = req("/api/state")
        got = j["next_deadline"]["id"]
        req("/api/task/t-overdue", "PATCH", {"status": "todo"})
        eq(got, "t-soon", "след затваряне на просрочената: ")
        return "t-overdue → t-soon"
    check("щом просрочената се затвори, пръстенът минава на следващата", ring_moves_on)

    def html():
        s, b, ct = req("/")
        eq(s, 200)
        # името вече идва от профила, не е заковано в страницата
        assert b'id="brand"' in b and b"app.webmanifest" in b
        assert b"text/html" in ct.encode()
        return f"{len(b) // 1024} KB HTML"
    check("GET / сервира таблото с връзка към manifest", html)

    def no_cache():
        """Регресия: страницата се редактира, докато сървърът върви, а браузърът
        сервираше кеширано копие — „рестартирах и пак е същото"."""
        r = urllib.request.urlopen(BASE + "/", timeout=20)
        cc = (r.headers.get("cache-control") or "").lower()
        assert "no-store" in cc, f"index се кешира: cache-control={cc!r}"
        return cc
    check("GET / забранява кеширане на самата страница", no_cache)

    def manifest_ct():
        s, _, ct = req("/static/manifest.webmanifest")
        eq(s, 200)
        assert "manifest+json" in ct, f"грешен content-type: {ct}"
        return ct
    check("manifest се сервира с application/manifest+json", manifest_ct)

    def langs():
        s, j, _ = req("/api/languages")
        eq(s, 200, "status: ")
        eq(j["active"], "en", "активен език: ")
        codes = [x["code"] for x in j["available"]]
        assert "en" in codes and "bg" in codes, codes
        en = next(x for x in j["available"] if x["code"] == "en")
        assert "Multilingual" in en["voice"], f"английският глас не е многоезичен: {en['voice']}"
        assert en["recognition"] == "en-US", en["recognition"]
        return " + ".join(f"{x['code']}:{x['voice']}" for x in j["available"])
    check("GET /api/languages изброява езиците и гласовете", langs)

    def brief_langs():
        _, e, _ = req("/api/brief?lang=en")
        _, b, _ = req("/api/brief?lang=bg")
        eq(e["lang"], "en", "en: ")
        eq(b["lang"], "bg", "bg: ")
        assert not re.search(r"[А-Яа-я]", e["text"]), "кирилица в английския бриф"
        assert re.search(r"[А-Яа-я]", b["text"]), "няма кирилица в българския бриф"
        assert e["text"] != b["text"]
        return f"en {len(e['text'])} зн. / bg {len(b['text'])} зн."
    check("GET /api/brief?lang= дава различен текст на всеки език", brief_langs)

    def state_langs():
        _, e, _ = req("/api/state?lang=en")
        _, b, _ = req("/api/state?lang=bg")
        te = next(t for t in e["tasks"] if t["id"] == "t-soon")
        tb = next(t for t in b["tasks"] if t["id"] == "t-soon")
        eq(te["weight"], "10% of the grade", "en weight: ")
        eq(tb["weight"], "10% от оценката", "bg weight: ")
        eq(te["title"], tb["title"], "заглавието е едно и също: ")
        assert tb["translated"] is True, "не е отбелязано като преведено"
        return f"{te['weight']} / {tb['weight']}"
    check("GET /api/state?lang= превежда полетата, но не и заглавието", state_langs)

    check("непознат език пада към английски",
          lambda: eq(req("/api/state?lang=klingon")[1]["lang"], "en"))

    section("server/app.py — проверка на Brightspace преди репорт")

    def brief_carries_freshness():
        _, j, _ = req("/api/brief")
        assert "freshness" in j, "брифът не казва колко са стари данните"
        for k in ("last_sync", "age_hours", "stale", "stale_after_hours"):
            assert k in j["freshness"], f"липсва {k}"
        _, st, _ = req("/api/state")
        assert "freshness" in st, "state също трябва да го носи, за да се оцвети хедърът"
        return f"праг {j['freshness']['stale_after_hours']}ч"
    check("брифът и state носят колко стари са данните", brief_carries_freshness)

    def sync_when_stale():
        db = tasks_mod.load()
        keep = db.get("last_sync")
        try:
            db["last_sync"] = datetime.now().isoformat(timespec="seconds")
            tasks_mod.save(db)
            s, fresh, _ = req("/api/sync", "POST")
            eq(fresh["queued"], None, "пресни данни не пускат заявка: ")

            db["last_sync"] = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")
            tasks_mod.save(db)
            s, j, _ = req("/api/sync", "POST")
            eq(s, 201, "status: ")
            assert j["queued"], "остарели данни не пуснаха заявка"
            _, rec, _ = req(f"/api/ask/{j['queued']}")
            eq(rec["kind"], "sync", "вид: ")
            assert "news/" in rec["question"], "не казва да прочете съобщенията"
            assert "--partial" in rec["question"], "не предпазва от маркиране като изчезнали"

            # второ натискане на Репорт не бива да трупа втора заявка
            _, again, _ = req("/api/sync", "POST")
            eq(again["queued"], j["queued"], "дублира заявката: ")
            eq(again["already"], True, "не казва, че вече чака: ")
            req(f"/api/ask/{j['queued']}", "DELETE")
            return f"{j['queued']} · дедупликира се"
        finally:
            db["last_sync"] = keep
            tasks_mod.save(db)
    check("остарели данни пускат проверка, пресни — не, и не се дублира", sync_when_stale)

    def sync_force():
        db = tasks_mod.load()
        keep = db.get("last_sync")
        try:
            db["last_sync"] = datetime.now().isoformat(timespec="seconds")
            tasks_mod.save(db)
            _, j, _ = req("/api/sync?force=true", "POST")
            assert j["queued"], "бутонът ↻ не успя да поиска проверка насила"
            req(f"/api/ask/{j['queued']}", "DELETE")
            return True
        finally:
            db["last_sync"] = keep
            tasks_mod.save(db)
    check("бутонът ↻ може да поиска проверка дори при пресни данни", sync_force)

    section("server/app.py — проверка при отваряне на таблото")

    def sync_on_start_threshold():
        """Отварянето на таблото проверява по МИНУТИ, не по часове.

        Инак рестарт след час чете от вчерашна снимка, а точно това е поводът за
        функцията. Но не и при рестарт веднага след рестарт — всяка проверка буди
        сесия и върти Chrome.
        """
        db = tasks_mod.load()
        keep = db.get("last_sync")
        floor_min = brief_mod.sync_on_start_after_minutes()
        try:
            # току-що проверено → отварянето не пуска нищо
            db["last_sync"] = datetime.now().isoformat(timespec="seconds")
            tasks_mod.save(db)
            _, j, _ = req("/api/sync?on_start=true", "POST")
            eq(j["queued"], None, "рестарт веднага след проверка пусна излишна заявка: ")

            # по-старо от прага, но още „прясно" по правилото за часове → пак проверява
            old = datetime.now() - timedelta(minutes=floor_min + 5)
            db["last_sync"] = old.isoformat(timespec="seconds")
            tasks_mod.save(db)
            _, fr, _ = req("/api/state")
            eq(fr["freshness"]["stale"], False,
               "тестът предполага, че праг в минути е под този в часове: ")
            _, j, _ = req("/api/sync?on_start=true", "POST")
            assert j["queued"], "отварянето на таблото не провери Brightspace"
            _, rec, _ = req(f"/api/ask/{j['queued']}")
            eq(rec["kind"], "sync", "вид: ")
            assert "news/" in rec["question"], "не казва да прочете съобщенията"
            assert "dropbox/folders" in rec["question"], "не казва да провери задачите"
            assert "dashboard being opened" in rec["question"], \
                "не казва на Claude какво е задействало проверката"
            req(f"/api/ask/{j['queued']}", "DELETE")
            return f"праг {floor_min:.0f} мин"
        finally:
            db["last_sync"] = keep
            tasks_mod.save(db)
    check("отварянето на таблото проверява Brightspace, но не при рестарт след рестарт",
          sync_on_start_threshold)

    def watcher_flag():
        """Заявка в опашката ≠ заявка, която някой ще поеме."""
        beat = TMP / "ask" / ".watcher"
        beat.parent.mkdir(parents=True, exist_ok=True)
        try:
            beat.unlink(missing_ok=True)
            _, j, _ = req("/api/state")
            eq(j["freshness"]["watcher"], False, "без heartbeat твърди, че някой слуша: ")

            beat.write_text(json.dumps(
                {"pid": 1, "beat": datetime.now().isoformat(timespec="seconds")}),
                encoding="utf-8")
            _, j, _ = req("/api/state")
            eq(j["freshness"]["watcher"], True, "пресен heartbeat не се вижда: ")

            # умрял watcher не бива да се брои за жив
            beat.write_text(json.dumps(
                {"pid": 1, "beat": (datetime.now() - timedelta(minutes=5))
                 .isoformat(timespec="seconds")}), encoding="utf-8")
            _, j, _ = req("/api/state")
            eq(j["freshness"]["watcher"], False, "стар heartbeat минава за жив: ")
            return True
        finally:
            beat.unlink(missing_ok=True)
    check("таблото различава проверка в ход от заявка, която никой не слуша", watcher_flag)

    def watcher_file_is_not_a_question():
        """`.watcher` живее в същата папка и НЕ бива да се брои за въпрос."""
        beat = TMP / "ask" / ".watcher"
        beat.parent.mkdir(parents=True, exist_ok=True)
        try:
            beat.write_text(json.dumps(
                {"pid": 1, "beat": datetime.now().isoformat(timespec="seconds")}),
                encoding="utf-8")
            _, rows, _ = req("/api/ask")
            ids = [r.get("id") for r in rows["questions"]]
            assert None not in ids, "heartbeat-ът се появи в опашката като въпрос"
            return f"{len(ids)} записа, без heartbeat"
        finally:
            beat.unlink(missing_ok=True)
    check("heartbeat файлът не се брои за въпрос", watcher_file_is_not_a_question)

    def picked_up_is_not_abandoned():
        """Поета заявка не е изоставена.

        watcher-ът излиза в мига, в който подаде заявката на Claude, и heartbeat-ът
        умира с него. Без `picked_up` таблото щеше да казва „никой не слуша" точно
        докато Claude работи по заявката.
        """
        db = tasks_mod.load()
        keep = db.get("last_sync")
        try:
            db["last_sync"] = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")
            tasks_mod.save(db)
            _, j, _ = req("/api/sync", "POST")
            qid = j["queued"]
            eq(j["picked_up"], False, "нова заявка вече е поета?: ")

            path = TMP / "ask" / f"{qid}.json"
            rec = json.loads(path.read_text(encoding="utf-8"))
            rec["picked_up_at"] = datetime.now().isoformat(timespec="seconds")
            path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")

            _, again, _ = req("/api/sync", "POST")
            eq(again["already"], True, "не разпозна същата заявка: ")
            eq(again["picked_up"], True, "поета заявка мина за изоставена: ")
            req(f"/api/ask/{qid}", "DELETE")
            return True
        finally:
            db["last_sync"] = keep
            tasks_mod.save(db)
    check("поета заявка не се показва като изоставена", picked_up_is_not_abandoned)

    section("tools/autosync.py — проверка без Claude сесия")

    def autosync_endpoint():
        s, j, _ = req("/api/autosync")
        eq(s, 200, "status: ")
        assert "state" in j, "няма state"
        return j["state"]
    check("/api/autosync казва в какво състояние е проверката", autosync_endpoint)

    def autosync_off_falls_back():
        """С изключен autosync таблото пак трябва да поиска Claude, не да замълчи."""
        db = tasks_mod.load()
        keep = db.get("last_sync")
        try:
            db["last_sync"] = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")
            tasks_mod.save(db)
            _, j, _ = req("/api/sync", "POST")
            eq(j["autosync"], "off", "тестовият сървър трябва да е с изключен autosync: ")
            assert j["queued"], "изключен autosync не поиска Claude — проверката изчезва"
            req(f"/api/ask/{j['queued']}", "DELETE")
            return True
        finally:
            db["last_sync"] = keep
            tasks_mod.save(db)
    check("изключен autosync пада обратно към Claude, а не мълчи", autosync_off_falls_back)

    def login_endpoint_respects_the_off_switch():
        """`/api/cdp/login` отваря истински Chrome прозорец.

        С AVANS_NO_AUTOSYNC няма JARVIS профил, в който да се влиза — и без тази
        проверка всеки тестов пуск щеше да убива живия профил и да вади прозорец.
        """
        s_, j_, _ = req("/api/cdp/login", "POST")
        eq(s_, 202, "status: ")
        eq(j_.get("opening"), False,
           "тестът щеше да отвори истински Chrome прозорец: ")
        return True
    check("входът не се пуска, когато autosync е изключен",
          login_endpoint_respects_the_off_switch)

    def browser_opens_on_dashboard_open():
        """Отварянето на таблото вдига таба ВИНАГИ, дори проверката да е пропусната.

        Прагът от 30 минути важи за самата проверка, не за прозореца. Прозорецът е това,
        което държи сесията в Brightspace жива — пропуснеш ли го, защото е проверявано
        преди 20 минути, сесията пак умира за една нощ.
        """
        db = tasks_mod.load()
        keep = db.get("last_sync")
        try:
            db["last_sync"] = datetime.now().isoformat(timespec="seconds")
            tasks_mod.save(db)
            _, j, _ = req("/api/sync?on_start=true", "POST")
            eq(j.get("queued"), None, "прясно проверено, а пусна заявка: ")
            eq(j.get("browser"), "opening",
               "таблото не вдига таба, когато проверката е пропусната: ")
            return True
        finally:
            db["last_sync"] = keep
            tasks_mod.save(db)
    check("отварянето на таблото вдига таба дори без проверка",
          browser_opens_on_dashboard_open)

    def headless_detection_reads_the_right_field():
        """`--headless=new` се представя като обикновен Chrome навсякъде освен в UA.

        Четенето на полето `Browser` караше is_headless() винаги да връща False, тоест
        headless профил никога не се подменяше и продължаваше да дава 403.
        """
        sys.path.insert(0, str(ROOT / "tools"))
        import cdp as cdp_mod
        src = (ROOT / "tools" / "cdp.py").read_text(encoding="utf-8")
        i = src.index("def is_headless")
        body = src[i:i + 900]
        assert "User-Agent" in body, "is_headless не чете User-Agent"
        assert 'get("Browser"' not in body, \
            "пак чете полето Browser, което е еднакво за headless и обикновен Chrome"

        # и минимизирането не бива да пипа чужд прозорец
        j = src.index("def minimize")
        mbody = src[j:j + 1400]
        assert "profile_pid()" in mbody, "minimize не се ограничава до нашия процес"
        assert "if not pid:" in mbody, "без pid трябва да не прави нищо"
        # a pid that owns no window must leave every window alone
        eq(cdp_mod.minimize(999999), False,
           "пипна прозорец на чужд процес: ")
        return True
    check("headless се познава по User-Agent, а минимизирането не пипа чужд Chrome",
          headless_detection_reads_the_right_field)

    def autosync_enabled_branch_runs():
        """Включеният клон трябва да се ИЗПЪЛНИ поне веднъж някъде в тестовете.

        Тестовият сървър върви с AVANS_NO_AUTOSYNC=1 и излиза от `autosync_enabled()`
        на първия ред. Заради това NameError вътре в четенето на config.json мина през
        целия пакет и се показа чак на живо, като 500 от таблото. Затова тук функцията
        се вика директно, без променливата — внасянето на app.py не вдига сървър.
        """
        import importlib
        keep = os.environ.pop("AVANS_NO_AUTOSYNC", None)
        try:
            sys.path.insert(0, str(ROOT / "server"))
            app_mod = importlib.import_module("app")
            val = app_mod.autosync_enabled()          # не бива да гърми
            assert isinstance(val, bool), f"връща {type(val).__name__}, не bool"
            # и с изключен ключ трябва да е False
            os.environ["AVANS_NO_AUTOSYNC"] = "1"
            eq(app_mod.autosync_enabled(), False, "ключът за изключване не работи: ")
            return f"config казва {val}"
        finally:
            os.environ.pop("AVANS_NO_AUTOSYNC", None)
            if keep is not None:
                os.environ["AVANS_NO_AUTOSYNC"] = keep
    check("autosync_enabled() се изпълнява наистина, не само късото съединение",
          autosync_enabled_branch_runs)

    def autosync_pure_logic():
        """`diff` и `iso_local` без браузър — тук тиха грешка чупи срокове."""
        sys.path.insert(0, str(ROOT / "tools"))
        import autosync

        # D2L дава UTC със Z; таблото сортира по локално стенно време
        got = autosync.iso_local("2026-10-02T07:00:00.000Z")
        assert got and got.startswith("2026-10-02T"), f"UTC->локално се счупи: {got}"
        eq(autosync.iso_local(None), None, "празна дата: ")
        eq(autosync.iso_local("не е дата"), None, "боклук вместо дата: ")

        db = {"tasks": [
            {"id": "known", "title": "Known", "due": "2026-10-02T09:00",
             "dropbox_ids": [111]},
        ]}
        data = {"folders": [
            {"id": 111, "name": "Known", "due": "2026-10-09T07:00:00.000Z"},
            {"id": 222, "name": "Brand New Thing", "due": "2026-11-02T07:00:00.000Z",
             "pts": 35},
        ], "grades": []}
        new, moved = autosync.diff(data, db)
        eq(len(moved), 1, "сменен срок не е засечен: ")
        eq(moved[0]["id"], "known", "сменен срок на грешна задача: ")
        assert moved[0]["to"].startswith("2026-10-09"), moved[0]["to"]
        eq(len(new), 1, "нова папка не е засечена: ")
        eq(new[0]["dropbox_ids"], [222], "нова задача не помни папката си: ")

        # папка, която вече си има задача, не бива да ражда втора
        new2, moved2 = autosync.diff({"folders": [data["folders"][0]], "grades": []}, db)
        eq(len(new2), 0, "дублира задача за вече позната папка: ")
        return f"{len(new)} нова, {len(moved)} сменен срок"
    check("autosync смята срокове и разлики вярно, без браузър", autosync_pure_logic)

    def resit_folder_does_not_move_the_deadline():
        """Задача с папка за Sit И за Resit пази срока на Sit.

        Хванато на живо: докладът държи 230587 (Sit, 2 окт) и 230588 (Resit, 13 окт).
        Преди поправката всяка папка се мереше срещу срока на задачата и втората го
        избутваше с 11 дни напред — тихо, върху верния срок.
        """
        sys.path.insert(0, str(ROOT / "tools"))
        import autosync
        db = {"tasks": [{"id": "report", "title": "Report", "due": "2026-10-02T09:00",
                         "dropbox_ids": [230587, 230588]}]}
        data = {"grades": [], "folders": [
            {"id": 230587, "name": "Group Report - Sit", "due": "2026-10-02T07:00:00.000Z"},
            {"id": 230588, "name": "Group Report  - Resit", "due": "2026-10-13T07:00:00.000Z"},
        ]}
        new, moved = autosync.diff(data, db)
        eq(len(new), 0, "измисли задача за папка, която вече е заявена: ")
        eq(len(moved), 0, "папката за поправка размести срока на редовното предаване: ")

        # а истинско местене на Sit пак трябва да се вижда
        data["folders"][0]["due"] = "2026-10-05T07:00:00.000Z"
        new, moved = autosync.diff(data, db)
        eq(len(moved), 1, "истинско местене на срока се загуби: ")
        assert moved[0]["to"].startswith("2026-10-05"), moved[0]["to"]
        return True
    check("папката за поправка не размества редовния срок",
          resit_folder_does_not_move_the_deadline)

    section("server/app.py — прехвърляне към Claude")

    def handoff_writes():
        s, j, _ = req("/api/handoff", "POST", {"task_id": "t-soon", "lang": "en"})
        eq(s, 200, "status: ")
        pr = j["prompt"]
        assert pr.startswith("/avans-solve t-soon"), pr[:60]
        for needle in ("Video opdracht", "Deadline:", "Weight:", "10% of the grade",
                       "What is handed in", "Requirements and grading", "the link must work"):
            assert needle in pr, f"липсва {needle!r} в промпта"
        f = Path(j["file"])
        if not f.is_absolute():
            f = ROOT / f
        assert f.exists(), f"файлът не е записан: {j['file']}"
        eq(f.read_text(encoding="utf-8"), pr, "файлът се разминава с върнатото: ")
        f.unlink()
        req(f"/api/ask/{j['queued']}", "DELETE")
        return f"{len(pr)} знака, записан в {j['file']}"
    check("POST /api/handoff прави самостоятелен промпт и го записва", handoff_writes)

    def handoff_english_always():
        """Промптът е за мен, не за екрана — винаги на английски, за да не се губи
        нищо при превод."""
        _, j, _ = req("/api/handoff", "POST", {"task_id": "t-soon", "lang": "bg"})
        assert "10% of the grade" in j["prompt"], "тежестта не е английската"
        assert "10% от оценката" not in j["prompt"], "изтекъл е преводът"
        assert "interface language bg" in j["prompt"], "не отбелязва езика на интерфейса"
        f = Path(j["file"])
        (f if f.is_absolute() else ROOT / f).unlink()
        req(f"/api/ask/{j['queued']}", "DELETE")
        return True
    check("промптът е на английски дори когато таблото е на български", handoff_english_always)

    def handoff_queue():
        _, a, _ = req("/api/handoff", "POST", {"task_id": "t-far"})
        _, listing, _ = req("/api/handoff")
        ids = [r["task_id"] for r in listing["pending"]]
        assert "t-far" in ids, ids
        f = Path(a["file"])
        (f if f.is_absolute() else ROOT / f).unlink()
        req(f"/api/ask/{a['queued']}", "DELETE")
        return f"{len(listing['pending'])} чакащи"
    check("GET /api/handoff изброява чакащите заявки", handoff_queue)

    check("прехвърляне на несъществуваща задача дава 404",
          lambda: eq(req("/api/handoff", "POST", {"task_id": "no-such-task"})[0], 404))

    section("server/app.py — въпроси от таблото")

    def ask_round_trip():
        q = "What exactly counts as a working link for the assessors?"
        s, rec, _ = req("/api/ask", "POST", {"text": q, "lang": "en"})
        eq(s, 201, "status: ")
        eq(rec["status"], "pending", "нов въпрос: ")
        eq(rec["question"], q, "текст: ")
        assert rec["answer"] is None

        _, listing, _ = req("/api/ask?status=pending")
        assert any(r["id"] == rec["id"] for r in listing["questions"]), "не е в опашката"

        s2, answered, _ = req(f"/api/ask/{rec['id']}/answer", "POST",
                              {"text": "One a stranger can open without signing in."})
        eq(s2, 200, "отговор: ")
        eq(answered["status"], "answered", "статус: ")
        assert answered["answered_at"], "няма време на отговора"

        _, fetched, _ = req(f"/api/ask/{rec['id']}")
        eq(fetched["answer"], answered["answer"], "прочетеното се разминава: ")
        _, still, _ = req("/api/ask?status=pending")
        assert not any(r["id"] == rec["id"] for r in still["questions"]), "още виси в опашката"

        req(f"/api/ask/{rec['id']}", "DELETE")
        return f"{rec['id']} → pending → answered"
    check("въпрос → опашка → отговор → прочитане", ask_round_trip)

    def answer_arrives_fast():
        """Регресия: страницата питаше на всеки 4 секунди, така че готов отговор
        можеше да стои невидим до 4 секунди."""
        _, rec, _ = req("/api/ask", "POST", {"text": f"How fast is fast? {time.time():.3f}"})
        delay = 1.0
        threading.Thread(
            target=lambda: (time.sleep(delay),
                            req(f"/api/ask/{rec['id']}/answer", "POST", {"text": "This fast."})),
            daemon=True).start()
        t0 = time.time()
        s, j, _ = req(f"/api/ask/{rec['id']}/wait?timeout=20")
        took = time.time() - t0
        eq(s, 200, "status: ")
        eq(j["status"], "answered", "състояние: ")
        assert took < delay + 1.0, f"върна се чак {took:.2f}s след въпроса"
        req(f"/api/ask/{rec['id']}", "DELETE")
        return f"вижда се {(took - delay) * 1000:.0f}ms след написването"
    check("отговорът стига до таблото веднага, не след интервал", answer_arrives_fast)

    def wait_times_out_cleanly():
        _, rec, _ = req("/api/ask", "POST", {"text": f"Never answered {time.time():.3f}"})
        t0 = time.time()
        s, j, _ = req(f"/api/ask/{rec['id']}/wait?timeout=2")
        took = time.time() - t0
        eq(s, 200, "status: ")
        eq(j["status"], "pending", "остава чакащ: ")
        assert 1.5 < took < 4, f"таймаутът е {took:.1f}s вместо ~2s"
        req(f"/api/ask/{rec['id']}", "DELETE")
        return f"връща се след {took:.1f}s без грешка"
    check("неотговорен въпрос изтича чисто и се пита пак", wait_times_out_cleanly)

    check("празен въпрос дава 400",
          lambda: eq(req("/api/ask", "POST", {"text": "   "})[0], 400))
    check("непознат въпрос дава 404",
          lambda: eq(req("/api/ask/nope-1234")[0], 404))
    check("отговор на непознат въпрос дава 404",
          lambda: eq(req("/api/ask/nope-1234/answer", "POST", {"text": "x"})[0], 404))

    def ask_survives_reload():
        """Регресия: списъкът с чакащи живееше само в паметта на страницата. Презареждане
        го губеше, въпросът оставаше pending завинаги и лентата изчезваше — изглеждаше
        като че ли нищо не става. Страницата вече го вдига обратно оттук."""
        _, a, _ = req("/api/ask", "POST", {"text": "Does this survive a reload?"})
        _, pend, _ = req("/api/ask?status=pending")
        row = next((r for r in pend["questions"] if r["id"] == a["id"]), None)
        assert row, "не се вижда в опашката, значи страницата няма как да го вдигне"
        assert row["asked_at"], "няма време на задаване — броячът не може да тръгне"
        req(f"/api/ask/{a['id']}", "DELETE")
        return f"{a['id']} се чете обратно от сървъра"
    check("чакащ въпрос се вдига обратно след презареждане", ask_survives_reload)

    def ask_cancel():
        _, a, _ = req("/api/ask", "POST", {"text": "To be cancelled."})
        s, _, _ = req(f"/api/ask/{a['id']}", "DELETE")
        eq(s, 200, "изтриване: ")
        eq(req(f"/api/ask/{a['id']}")[0], 404, "след изтриване: ")
        eq(req(f"/api/ask/{a['id']}", "DELETE")[0], 404, "повторно изтриване: ")
        return True
    check("чакащ въпрос може да се откаже", ask_cancel)

    def watcher():
        """`ask.py wait` е това, което прави таблото живо: пуска се във фонов режим от
        сесия на Claude, блокира докато не дойде въпрос, и събужда сесията да отговори."""
        env = {**os.environ, "AVANS_ASK": str(TMP / "ask")}
        cmd = [sys.executable, str(ROOT / "tools" / "ask.py"), "wait",
               "--timeout", "4", "--interval", "0.5"]

        empty = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True,
                               text=True, timeout=40)
        eq(empty.returncode, 2, "празна опашка трябва да даде 2: ")

        _, a, _ = req("/api/ask", "POST", {"text": "Is the watcher awake?"})
        t0 = time.time()
        got = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=40)
        took = time.time() - t0
        eq(got.returncode, 0, "чакащ въпрос трябва да даде 0: ")
        assert a["id"] in got.stdout, got.stdout[:200]
        assert "Is the watcher awake?" in got.stdout, "не показва самия въпрос"
        assert took < 3, f"забеляза го чак след {took:.1f}s"
        req(f"/api/ask/{a['id']}", "DELETE")
        return f"празно → 2, въпрос → 0 за {took:.1f}s"
    check("наблюдателят се събужда при нов въпрос", watcher)

    def watcher_survives_cyrillic():
        """Регресия, струвала 18 дни мълчание.

        `ask.py wait` печата въпроса обратно. На Windows конзолата е cp1252, въпрос на
        кирилица гръмваше на самия print с UnicodeEncodeError, процесът умираше, и всеки
        следващ въпрос се трупаше зад мъртъв слушател. Въпросът „Аз съм клас номер 1"
        стоя неотговорен от 29 септември до 17 октомври.

        PYTHONIOENCODING нарочно се МАХА от средата: тестовият пакет го вдига на utf-8
        за себе си, а с него бъгът не се възпроизвежда и тестът минава фалшиво.
        """
        env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
        env["AVANS_ASK"] = str(TMP / "ask")
        env["AVANS_NO_AUTOSYNC"] = "1"
        cmd = [sys.executable, str(ROOT / "tools" / "ask.py"), "wait",
               "--timeout", "4", "--interval", "0.5"]

        _, a, _ = req("/api/ask", "POST",
                      {"text": "Аз съм клас номер 1 — не номер 2. Тире и кирилица."})
        r = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True,
                           encoding="utf-8", errors="replace", timeout=40)
        req(f"/api/ask/{a['id']}", "DELETE")
        assert "UnicodeEncodeError" not in (r.stderr or ""), \
            "наблюдателят пак гърми на кирилица:\n" + (r.stderr or "")[-300:]
        eq(r.returncode, 0, "изход: ")
        assert "клас номер 1" in r.stdout, f"не показа въпроса: {r.stdout[:200]!r}"
        return "кирилица и тире минават без PYTHONIOENCODING"
    check("въпрос на кирилица НЕ убива наблюдателя", watcher_survives_cyrillic)

    def ask_traversal():
        for evil in ("..%2F..%2Fconfig", "....%2F%2Fsecret"):
            code = req("/api/ask/" + evil)[0]
            assert code in (400, 404), f"{evil} върна {code}"
        return "2 опита, всички отказани"
    check("id-то на въпрос не дава излизане от папката", ask_traversal)

    section("server/app.py — файлове по задача")

    def upload_multipart(path, files):
        import uuid
        b = uuid.uuid4().hex
        body = b""
        for name, data in files:
            body += (f'--{b}\r\nContent-Disposition: form-data; name="files"; '
                     f'filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'
                     ).encode("utf-8") + data + b"\r\n"
        body += f"--{b}--\r\n".encode()
        r = urllib.request.Request(BASE + path, data=body, method="POST",
                                   headers={"Content-Type": f"multipart/form-data; boundary={b}"})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def files_round_trip():
        s, j = upload_multipart("/api/files/t-soon/attachments", [
            ("report.pdf", b"%PDF-1.4 pretend"),
            ("bележки.md", "съдържание на кирилица".encode("utf-8")),
        ])
        eq(s, 201, "status: ")
        eq(len(j["saved"]), 2, "записани: ")
        _, listing, _ = req("/api/files/t-soon")
        names = [f["name"] for f in listing["attachments"]]
        assert "report.pdf" in names, names
        url = next(f["url"] for f in listing["attachments"] if f["name"] == "report.pdf")
        s2, raw, _ = req(url)
        eq(s2, 200, "сваляне: ")
        eq(raw, b"%PDF-1.4 pretend", "съдържанието: ")
        return f"{len(names)} файла, свалят се непокътнати"
    check("качване, изброяване и сваляне на файл по задача", files_round_trip)

    def upload_guards():
        """Никакви скриптове, и никакво излизане от папката през името на файла."""
        s, j = upload_multipart("/api/files/t-soon/attachments", [
            ("evil.exe", b"MZ"),
            ("shell.bat", b"@echo off"),
            ("../../escaped.txt", b"should land inside, renamed"),
        ])
        eq(s, 201, "status: ")
        why = {x["name"]: x["why"] for x in j["skipped"]}
        assert "evil.exe" in why and "shell.bat" in why, why
        names = [f["name"] for f in j["attachments"]]
        assert "escaped.txt" in names, names
        for stray in (ROOT / "escaped.txt", TMP / "escaped.txt", TMP / "work" / "escaped.txt"):
            assert not stray.exists(), f"файл е излязъл от папката на задачата: {stray}"
        return f"отказани {sorted(why)}, обезвреден '../../escaped.txt'"
    check("качването отказва скриптове и обезврежда пътища в имената", upload_guards)

    def download_traversal():
        for evil in ("..%2F..%2Fconfig.json", "....%2F%2Fsecret", "..%5C..%5Cconfig.json"):
            code = req(f"/api/files/t-soon/attachments/{evil}")[0]
            assert code == 404, f"{evil} върна {code}"
        assert req("/api/files/t-soon/../../etc/x")[0] in (400, 404), "лош kind мина"
        return "4 опита, всички отказани"
    check("сваляне не дава излизане от папката", download_traversal)

    check("качване към несъществуваща задача дава 404",
          lambda: eq(upload_multipart("/api/files/nope/attachments", [("a.txt", b"x")])[0], 404))

    def delete_attachment():
        s, j, _ = req("/api/files/t-soon/attachments/report.pdf", "DELETE")
        eq(s, 200, "status: ")
        assert "report.pdf" not in [f["name"] for f in j["attachments"]]
        eq(req("/api/files/t-soon/attachments/report.pdf", "DELETE")[0], 404, "втори път: ")
        return True
    check("прикачен файл се маха", delete_attachment)

    def solve_asks_for_work():
        """Регресия: бутонът само подаваше промпт за копиране. Сега слага заявка, която
        иска свършена работа във файл, и изрично изисква физическата част да е описана."""
        s, j, _ = req("/api/handoff", "POST", {"task_id": "t-soon", "lang": "en"})
        eq(s, 200, "status: ")
        assert j.get("queued"), "не влиза в опашката"
        _, q, _ = req(f"/api/ask/{j['queued']}")
        eq(q["kind"], "solve", "вид: ")
        eq(q["about"], "t-soon", "за коя задача: ")
        assert "work/t-soon/solution/" in q["question"], "не казва къде да пише файла"
        assert "Filming, recording" in q["question"], "не изисква физическата част"
        assert "Do not submit anything to Brightspace" in q["question"]
        req(f"/api/ask/{j['queued']}", "DELETE")
        return f"{j['queued']} · иска файл + описание на физическата работа"
    check("„Реши\" поръчва свършена работа, не промпт за копиране", solve_asks_for_work)

    section("server/app.py — предаване в Brightspace")

    def submit_prepares():
        """Не автоматично предаване. Предаване в университетски dropbox е необратимо, а
        на този модул грешно или неотваряемо предаване е скъсване — затова заявката само
        подготвя и изрично спира преди бутона Submit."""
        s, j, _ = req("/api/submit", "POST",
                      {"task_id": "t-soon", "what": "https://youtu.be/abc123"})
        eq(s, 201, "status: ")
        _, pend, _ = req("/api/ask?status=pending")
        rec = next((r for r in pend["questions"] if r["id"] == j["queued"]), None)
        assert rec, "заявката не е в опашката"
        eq(rec["kind"], "submit", "вид: ")
        eq(rec["about"], "t-soon", "за коя задача: ")
        assert "do not press submit" in rec["question"].lower(), rec["question"][-140:]
        assert "https://youtu.be/abc123" in rec["question"], "липсва това, което се предава"
        assert "A YouTube link" in rec["question"], "не носи изискванията на задачата"
        assert "upload the attached files" in rec["question"], "не казва да качи файловете"

        _, st, _ = req("/api/state")
        t = next(x for x in st["tasks"] if x["id"] == "t-soon")
        eq(len(t["submissions"]), 1, "записани предавания: ")
        eq(t["submissions"][0]["status"], "requested", "състояние: ")
        req(f"/api/ask/{j['queued']}", "DELETE")
        return f"{j['queued']} · спира преди Submit"
    check("POST /api/submit подготвя, но НЕ предава само", submit_prepares)

    check("празно предаване дава 400",
          lambda: eq(req("/api/submit", "POST", {"task_id": "t-soon", "what": "  "})[0], 400))
    check("предаване на несъществуваща задача дава 404",
          lambda: eq(req("/api/submit", "POST", {"task_id": "nope", "what": "x"})[0], 404))

    def done_view():
        _, st, _ = req("/api/state")
        assert "done" in st, "няма списък с готови задачи"
        for t in st["tasks"] + st["done"]:
            for k in ("completed", "started", "submissions"):
                assert k in t, f"липсва {k} в задачата {t['id']}"
        return f"{len(st['done'])} готови, всяка с време и предавания"
    check("state носи готовите задачи с време на приключване", done_view)

    def logging_always():
        """Регресия: при наличие на конзола нищо не влизаше в лог файла. Сървърът
        падна и не остави абсолютно никаква следа — причината остана неизвестна."""
        log = ROOT / "reports" / "server.log"
        before = log.stat().st_size if log.exists() else 0
        lport = free_port()
        env = {**os.environ, "AVANS_DB": str(TMP / "tasks.json"),
               "JARVIS_PORT": str(lport)}
        p = subprocess.Popen([sys.executable, str(ROOT / "server" / "app.py")],
                             cwd=str(ROOT), env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            for _ in range(60):
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{lport}/api/state", timeout=1)
                    break
                except Exception:
                    time.sleep(0.25)
            else:
                raise AssertionError(f"сървърът не тръгна на {lport}")
        finally:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        time.sleep(0.4)
        # st_size е в БАЙТОВЕ — режи байтове, после декодирай, иначе кирилицата
        # по-нагоре в лога измества отместването и срезът яде началото на реда
        fresh = log.read_bytes()[before:].decode("utf-8", "replace")
        assert "start" in fresh and "pid=" in fresh, fresh[-300:]
        assert "console=yes" in fresh, "не отчита конзолата: " + fresh[-200:]
        assert f"port={lport}" in fresh, "не записва порта: " + fresh[-200:]
        return "записва се и когато има конзола"
    check("сървърът винаги пише в reports/server.log", logging_always)

    def patch_ok():
        s, j, _ = req("/api/task/t-far", "PATCH", {"status": "in_progress", "priority": 2})
        eq(s, 200, "status: ")
        eq(j["status"], "in_progress", "новият статус: ")
        _, st, _ = req("/api/state")
        t = next(x for x in st["tasks"] if x["id"] == "t-far")
        eq(t["priority"], 2, "приоритетът в state: ")
        return True
    check("PATCH /api/task сменя статус и се вижда в state", patch_ok)

    check("PATCH с невалиден статус дава 400",
          lambda: eq(req("/api/task/t-far", "PATCH", {"status": "измислен"})[0], 400))
    check("PATCH на несъществуваща задача дава 404",
          lambda: eq(req("/api/task/няма-такава", "PATCH", {"status": "done"})[0], 404))

    section("server/app.py — гласови команди")

    cases = [
        ("en", "give me the report", "brief", None),
        ("en", "what do i have waiting", "brief", None),
        ("en", "what is most urgent", "next", "t-overdue"),
        ("en", "when is video opdracht due", "when", "t-soon"),
        ("en", "stop", "stop", None),
        ("en", "write me an essay about globalisation", "question", None),
        ("bg", "давай репорт", "brief", None),
        ("bg", "кое е най-спешно", "next", "t-overdue"),
        ("bg", "кога е video opdracht", "when", "t-soon"),
        ("bg", "спри", "stop", None),
        ("en", "i am done with video opdracht", "done", "t-soon"),
    ]
    for lang, text, want, focus in cases:
        def one(lang=lang, text=text, want=want, focus=focus):
            s, j, _ = req("/api/command", "POST", {"text": text, "lang": lang})
            eq(s, 200, "status: ")
            eq(j["intent"], want, f"интент за {text!r}: ")
            eq(j["lang"], lang, "език: ")
            if focus:
                eq(j.get("focus"), focus, "focus: ")
            if want == "question":
                eq(j.get("passthrough"), text, "passthrough: ")
                assert j.get("question_id"), "въпросът не е сложен в опашката"
                req(f"/api/ask/{j['question_id']}", "DELETE")
            if want == "stop":
                assert j.get("action") == "stop", "спирането не връща action=stop"
            else:
                assert j.get("say"), "няма какво да каже"
                if want == "brief":
                    has_cyr = bool(re.search(r"[А-Яа-я]", j["say"]))
                    assert has_cyr == (lang == "bg"), f"грешен език в отговора за {lang}"
            return (j.get("say") or "—")[:48].replace("\n", " ")
        check(f"[{lang}] „{text}\" → {want}", one)

    def done_applied():
        _, st, _ = req("/api/state")
        assert "t-soon" not in [t["id"] for t in st["tasks"]], "гласовото 'готово' не затвори задачата"
        return True
    check("гласовото „готово с…\" наистина затваря задачата", done_applied)

    def matching_precision():
        """Регресия: гласова команда затваряше ГРЕШНА задача. „done with absolutely
        nothing like that" съвпадаше с „Reading with no deadline" само защото и двете
        съдържаха „with", а размитото сходство добавяше останалото."""
        for tid in ("t-soon", "t-far", "t-undated", "t-overdue"):
            req(f"/api/task/{tid}", "PATCH", {"status": "todo"})
        before = {t["id"] for t in req("/api/state")[1]["tasks"]}

        misses = [("en", "i am done with absolutely nothing like that"),
                  ("en", "done with all of it"),
                  ("en", "i finished the thing"),
                  ("bg", "готово с абсолютно нищо такова"),
                  ("bg", "готово с това нещо")]
        for lang, text in misses:
            _, j, _ = req("/api/command", "POST", {"text": text, "lang": lang})
            assert not j.get("focus"), f"[{lang}] {text!r} закачи {j.get('focus')!r}"
        after = {t["id"] for t in req("/api/state")[1]["tasks"]}
        eq(after, before, "нещо се затвори от неясна команда: ")

        hits = [("en", "i am done with video opdracht", "t-soon"),
                ("en", "i finished the big project", "t-far"),
                ("bg", "готово с big project", "t-far")]
        for lang, text, want in hits:
            req(f"/api/task/{want}", "PATCH", {"status": "todo"})
            _, j, _ = req("/api/command", "POST", {"text": text, "lang": lang})
            eq(j.get("focus"), want, f"[{lang}] {text!r}: ")
        for tid in ("t-soon", "t-far"):
            req(f"/api/task/{tid}", "PATCH", {"status": "todo"})
        return f"{len(misses)} неясни отказани, {len(hits)} ясни познати"
    check("неясна команда НЕ затваря грешна задача", matching_precision)

    section("server/app.py — глас и сигурност")

    if network:
        def voice_per_lang():
            _, e, _ = req("/api/speak", "POST", {"text": "A short test.", "lang": "en"})
            _, b, _ = req("/api/speak", "POST", {"text": "Кратък тест.", "lang": "bg"})
            assert e["voice"].startswith("en-"), e["voice"]
            assert b["voice"].startswith("bg-"), b["voice"]
            assert e["url"] != b["url"], "различните гласове дадоха един и същи файл"
            return f"{e['voice']} / {b['voice']}"
        check("всеки език получава своя глас и свой аудио файл", voice_per_lang)

        def speak_flow():
            s, j, _ = req("/api/speak", "POST", {"text": "Another short test."})
            eq(s, 200, "status: ")
            assert j["url"].endswith(".mp3"), j["url"]
            assert j["url"].startswith(("/audio/", "/api/speech/")), j["url"]
            s2, b, ct = req(j["url"])
            eq(s2, 200, "audio status: ")
            eq(ct, "audio/mpeg", "content-type: ")
            assert len(b) > 5000, len(b)
            return f"{j['url']}, {len(b) // 1024} KB"
        check("POST /api/speak → MP3, който се сваля от /audio", speak_flow)

        def speak_returns_instantly():
            """Регресия: /api/speak чакаше целия синтез, преди да отговори. edge-tts
            произвежда горе-долу със скоростта на говора, така че отговор от 1379 знака
            означаваше 47 секунди мълчание. Сега URL-ът се връща веднага и байтовете
            текат, докато се правят."""
            txt = f"Latency probe {time.time():.3f}."
            t0 = time.time()
            s, j, _ = req("/api/speak", "POST", {"text": txt})
            took = time.time() - t0
            eq(s, 200, "status: ")
            eq(j["cached"], False, "нов текст: ")
            assert j["url"].startswith("/api/speech/"), j["url"]
            assert took < 1.5, f"връща се чак след {took:.2f}s — пак чака синтеза"

            s2, b, ct = req(j["url"])
            eq(s2, 200, "потокът: ")
            eq(ct, "audio/mpeg", "content-type: ")
            assert b[:3] == b"ID3" or (b[0] == 0xFF and (b[1] & 0xE0) == 0xE0), b[:4]

            _, j2, _ = req("/api/speak", "POST", {"text": txt})
            eq(j2["cached"], True, "втори път: ")
            assert j2["url"].startswith("/audio/"), j2["url"]
            return f"POST за {took:.2f}s, {len(b) // 1024} KB, после от кеша"
        check("/api/speak отговаря веднага и стриймва звука", speak_returns_instantly)

        def stream_caches():
            _, j, _ = req("/api/speak", "POST", {"text": f"Cache after stream {time.time():.3f}."})
            key = j["url"].rsplit("/", 1)[-1]
            req(j["url"])                      # изтегляме потока докрай
            time.sleep(0.3)
            s, b, ct = req("/audio/" + key)
            eq(s, 200, "файлът след стрийма: ")
            eq(ct, "audio/mpeg", "content-type: ")
            return f"{len(b) // 1024} KB записани за следващия път"
        check("изслушаният поток се запазва като файл", stream_caches)

        check("непознат speech id дава 404",
              lambda: eq(req("/api/speech/0000000000000000.mp3")[0], 404))

        def voice_health_ok():
            s, j, _ = req("/api/voice/health")
            eq(s, 200, "status: ")
            if not j.get("ok"):
                raise AssertionError(f"{j.get('why')} {j.get('hint')}")
            assert j["bytes"] > 500, j
            return f"{j['voice']}, {j['bytes']} байта"
        check("/api/voice/health потвърждава, че гласът работи", voice_health_ok)

        def partial_stream_not_cached():
            """Регресия: прекъснат поток се записваше в кеша и следващото пускане
            сервираше отрязаното парче завинаги."""
            import http.client
            txt = f"Partial stream probe {time.time():.3f}."
            _, j, _ = req("/api/speak", "POST", {"text": txt})
            key = j["url"].rsplit("/", 1)[-1]
            conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
            conn.request("GET", j["url"])
            r = conn.getresponse()
            first = r.read(800)            # вземи малко и затвори
            conn.close()
            assert first, "потокът не даде нищо"
            time.sleep(1.2)
            eq(req("/audio/" + key)[0], 404, "прекъснатото се е кеширало: ")
            return f"{len(first)} байта прочетени, нищо не се записа"
        check("прекъснат поток НЕ се кешира", partial_stream_not_cached)

        check("празен текст дава 400",
              lambda: eq(req("/api/speak", "POST", {"text": "   "})[0], 400))
    else:
        skip("POST /api/speak → MP3", "--no-network")
        skip("кеширане на озвученото", "--no-network")
        skip("/api/voice/health потвърждава, че гласът работи", "--no-network")
        skip("прекъснат поток НЕ се кешира", "--no-network")

    def voice_error_is_actionable():
        """Гласът замлъкна за месец, а таблото казваше само 'Voice failed'. Съобщението
        трябва да носи командата, с която се оправя."""
        sys.path.insert(0, str(ROOT))
        from server.app import describe_voice_error

        class FakeWS(Exception):
            pass
        ws = FakeWS("403, message='Invalid response status', "
                    "url='wss://speech.platform.bing.com/consumer/speech/synthesize/...'")
        d = describe_voice_error(ws)
        assert "403" in d["why"], d
        assert "upgrade edge-tts" in d["hint"], d["hint"]
        assert "restart" in d["hint"].lower(), "не казва, че трябва рестарт"

        net = describe_voice_error(ConnectionError("Cannot connect to host"))
        assert "internet" in net["hint"].lower(), net

        other = describe_voice_error(ValueError("something else"))
        assert "server.log" in other["hint"], other
        return "403 → командата; мрежа → интернет; друго → логът"
    check("грешката на гласа казва КАК се оправя, не само че е гръмнала",
          voice_error_is_actionable)

    def traversal():
        for evil in ("..%2F..%2Fconfig.json", "..%2F..%2Freports%2Fx.mp3",
                     "....%2F%2Fconfig.json", "nope.mp3"):
            code = req("/audio/" + evil)[0]
            assert code == 404, f"{evil} върна {code}, а трябва 404"
        return "4 опита, всички 404"
    check("/audio не дава излизане от папката (path traversal)", traversal)

    def isolated():
        real = json.loads((ROOT / "data" / "tasks.json").read_text(encoding="utf-8"))
        ids = {t["id"] for t in real["tasks"]}
        assert not (ids & {"t-soon", "t-far", "t-overdue"}), "тестът е замърсил истинската база!"
        assert "coi-language-choice-video" in ids, "истинската база е повредена"
        return f"{len(ids)} истински задачи, непокътнати"
    check("истинската data/tasks.json не е пипната", isolated)


# ------------------------------------------------------------------ files

def test_files():
    section("файлове и стартери")

    def bat():
        t = (ROOT / "JARVIS.bat").read_text(encoding="utf-8", errors="replace")
        assert "127.0.0.1:8765" in t, "стартерът не сочи 127.0.0.1"
        assert "http://localhost:8765" not in t, "останал е localhost — 2 секунди забавяне"
        assert "server\\app.py" in t
        assert "--autoplay-policy=no-user-gesture-required" in t, \
            "без този флаг Chrome мълчаливо отказва да пусне репорта"
        return True
    check("JARVIS.bat ползва 127.0.0.1 и разрешава звука", bat)

    def autostart():
        t = (ROOT / "tools" / "install-autostart.ps1").read_text(encoding="utf-8")
        assert "pythonw.exe" in t, "трябва pythonw, за да няма конзолен прозорец"
        assert "-Remove" in t or "$Remove" in t, "няма как да се махне"
        assert "Startup" in t
        return True
    check("install-autostart.ps1 ползва pythonw и има -Remove", autostart)

    def ps1_is_ascii():
        # Windows PowerShell 5.1 чете .ps1 като ANSI без BOM — кирилица вътре
        # става на боклук и парсерът отказва. Веднъж вече ни счупи скрипта.
        b = (ROOT / "tools" / "install-autostart.ps1").read_bytes()
        bad = [i for i, c in enumerate(b) if c > 127]
        assert not bad, (f"не-ASCII байт на позиция {bad[0]} "
                         f"({b[max(0,bad[0]-20):bad[0]+20]!r}) — PowerShell 5.1 ще се спъне")
        return f"{len(b)} байта, чист ASCII"
    check("install-autostart.ps1 е само ASCII (иначе PS 5.1 не го парсва)", ps1_is_ascii)

    def headless_start():
        """Регресия: под pythonw няма конзола и първият запис в stdout убиваше
        процеса с exit 1 без traceback. Точно както го пуска прекият път."""
        pyw = Path(sys.executable).with_name("pythonw.exe")
        if not pyw.exists():
            raise AssertionError(f"няма pythonw.exe до {sys.executable}")
        port = free_port()
        env = {**os.environ, "AVANS_DB": str(TMP / "tasks.json"), "JARVIS_PORT": str(port)}
        DETACHED_PROCESS = 0x00000008
        p = subprocess.Popen([str(pyw), str(ROOT / "server" / "app.py")],
                             cwd=str(ROOT), env=env, close_fds=True,
                             creationflags=DETACHED_PROCESS)
        try:
            for _ in range(60):
                if p.poll() is not None:
                    raise AssertionError(f"процесът умря с код {p.returncode} — няма конзола")
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1)
                    return f"порт {port}, оцелява без конзола"
                except Exception:
                    time.sleep(0.25)
            raise AssertionError("не отговори за 15 секунди")
        finally:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
    check("сървърът тръгва през pythonw без конзола (както от прекия път)", headless_start)

    for p in ("CLAUDE.md", "config.json", "config.default.json", "launcher.py",
              "tools/paths.py", "tools/tasks.py", "tools/brief.py",
              "tools/speak.py", "tools/make_icons.py", "server/app.py",
              "server/static/index.html", "tools/teams.py", "docs/index.html", "release.json", "tools/release.py",
              ".claude/skills/avans-sync/SKILL.md",
              ".claude/skills/avans-report/SKILL.md", ".claude/skills/avans-solve/SKILL.md"):
        check(f"{p} съществува", lambda p=p: (ROOT / p).exists() or False)

    def config():
        c = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        eq(c["course"]["org_unit_id"], "310580", "orgUnitId: ")
        assert c["course"]["api_version"], "няма версия на D2L API"
        assert c["report"].get("stale_after_hours"), "няма праг за остарели данни"
        return f"{c['course']['name'][:40]}…, праг {c['report']['stale_after_hours']}ч"
    check("config.json пази курса, версията на API-то и прага за свежест", config)

    def sync_skill_covers_news():
        """Съобщенията бяха сляпото петно — шест дни непрочетени, с обявена сесия вътре."""
        t = (ROOT / ".claude" / "skills" / "avans-sync" / "SKILL.md").read_text(encoding="utf-8")
        assert "/news/" in t, "скилът не казва да чете съобщенията"
        assert "--partial" in t, "не предупреждава за флага при частично сливане"
        assert "404" in t, "не отбелязва капана с крайната наклонена черта"
        return True
    check("avans-sync скилът включва съобщенията", sync_skill_covers_news)

    def html_sanity():
        h = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
        for needle, why in (
            ("webkitSpeechRecognition", "гласов вход"),
            ("recognition", "език на разпознаването идва от сървъра"),
            ("bg-BG", "български като резервен вариант"),
            ("is-offline", "офлайн банер"),
            ("setOffline(true)", "офлайн се вдига при грешка"),
            ("/api/command", "гласовите команди"),
            ("/api/languages", "списъкът с езици"),
            ("Math.ceil(days)", "закръгляне на дните нагоре"),
            ("unlockAudio", "отключване на звука при първо докосване"),
            ("SILENT_WAV", "тихият файл, с който се отключва"),
            ("NotAllowedError", "разпознаване на блокиран звук"),
            ('id="tap"', "резервният бутон за пускане"),
            ("const I18N", "таблицата с преводи"),
            ("setLang", "превключване на езика"),
            ("jarvis-lang", "изборът се помни"),
            ("lang=' + LANG", "езикът се подава на API-то"),
            ("stopSpeech", "спиране на говора"),
            ("AbortController", "отменяне на заявка в полет"),
            ("speech.key === key", "второто натискане спира вместо да рестартира"),
            ("markBtn", "обратна връзка на натиснатия бутон"),
            ("/api/handoff", "прехвърлянето към Claude"),
            ("data-act=\"solve\"", "бутонът Реши"),
            ("askQuestion", "въпрос от таблото"),
            ("longPoll", "задържана заявка вместо периодично питане"),
            ("/wait?timeout=", "дългото чакане на отговор"),
            ("ask-input", "полето за въпрос"),
            ("watchAnswer(j.question_id", "гласовият въпрос също се проследява"),
            ("resumePending", "чакащите се вдигат след презареждане"),
            ("showPending", "как се отговаря, при клик върху лентата"),
            ("function elapsed", "броячът на чакането"),
            ("cancelOldest", "отказване на чакащ въпрос"),
            ("tab-done", "разделът с готови задачи"),
            ("setView", "превключване между активни и готови"),
            ("prepareSubmission", "подготовка на предаване"),
            ("/api/submit", "заявката за предаване"),
            ("is-done", "готовите изглеждат различно"),
            ("renderFiles", "списъкът с файлове по задача"),
            ("attachFiles", "прикачването на файлове"),
            ("/attachments", "качването"),
            ('type="file"', "избирачът на файлове"),
            ("data-rm", "махането на прикачен файл"),
            ("requestSync", "проверката на Brightspace преди репорт"),
            ("freshness", "показва колко стари са данните"),
        ):
            assert needle in h, f"липсва {why} ({needle})"
        assert 'html lang="en"' in h, "страницата не тръгва на английски"
        return f"{len(h) // 1024} KB"
    check("index.html: глас, офлайн защита, двуезичие и верните извиквания", html_sanity)

    def config_langs():
        c = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        eq(c["report"]["language"], "en", "по подразбиране: ")
        for code in ("en", "bg"):
            assert code in c["languages"], f"липсва език {code}"
            assert c["languages"][code]["voice"], f"{code} няма глас"
        assert "Andrew" in c["languages"]["en"]["voice"], "английският глас не е мъжкият Andrew"
        assert "Borislav" in c["languages"]["bg"]["voice"], "българският глас не е мъжкият Борислав"
        return f"en={c['languages']['en']['voice']}, bg={c['languages']['bg']['voice']}"
    check("config.json: английски по подразбиране, мъжки гласове и за двата езика", config_langs)


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-network", action="store_true", help="прескача edge-tts тестовете")
    a = ap.parse_args()
    network = not a.no_network

    print(f"\033[36m╔{'═' * 60}╗\033[0m")
    print(f"\033[36m║\033[0m  JARVIS — пълен тест{' ' * 40}\033[36m║\033[0m")
    print(f"\033[36m║\033[0m  временна база: {str(TMP)[:43]:<43}\033[36m║\033[0m")
    print(f"\033[36m╚{'═' * 60}╝\033[0m")

    proc = None
    try:
        test_tasks()
        test_brief()
        test_speak(network)
        test_icons()
        section("server/app.py — стартиране")
        try:
            proc = start_server()
            check("сървърът тръгва и отговаря", lambda: f"порт {PORT}")
        except Exception as e:
            FAILED.append(("server", "сървърът тръгва", str(e)))
            print(f"  \033[31mFAIL\033[0m  сървърът тръгва: {e}")
        if proc:
            test_server(proc, network)
        test_files()
        test_multi_course()
        test_shared_copy()
        section("launcher.py — самообновяване")

        def updater():
            """Отделен процес: тестът сменя sys.path и модулите на живо."""
            env = {k: v for k, v in os.environ.items()
                   if k not in ("AVANS_DB", "AVANS_WORK", "AVANS_ASK")}
            out = subprocess.run([sys.executable, str(ROOT / "tests" / "test_updater.py")],
                                 capture_output=True, text=True, timeout=120, env=env,
                                 encoding="utf-8", errors="replace")
            last = (out.stdout.strip().splitlines() or ["няма изход"])[-1]
            if out.returncode != 0:
                fails = [l for l in out.stdout.splitlines() if l.startswith("FAIL")]
                raise AssertionError("; ".join(fails) or out.stderr[-300:])
            return last
        check("обновяване: сваля, отказва подправено, връща се при счупено", updater)
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if os.environ.get("KEEP_SERVER_LOG") and SERVER_LOG.exists():
            shutil.copy(SERVER_LOG, os.environ["KEEP_SERVER_LOG"])
        shutil.rmtree(TMP, ignore_errors=True)

    total = len(PASSED) + len(FAILED)
    print(f"\n\033[36m{'═' * 62}\033[0m")
    if FAILED:
        print(f"\033[31m  {len(FAILED)} от {total} теста паднаха\033[0m\n")
        for sec, desc, why in FAILED:
            print(f"  \033[31m✕\033[0m [{sec}] {desc}\n      {why}")
    else:
        print(f"\033[32m  Всички {total} теста минаха.\033[0m")
    if SKIPPED:
        print(f"\033[33m  {len(SKIPPED)} прескочени\033[0m")
    print(f"\033[36m{'═' * 62}\033[0m")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
