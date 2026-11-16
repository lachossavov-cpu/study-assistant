#!/usr/bin/env python3
"""Text -> speech (MP3) using edge-tts neural voices.

Usage:
  python tools/speak.py --in reports/2026-09-07.md
  python tools/speak.py --text "Здравей" --voice bg-BG-BorislavNeural --play
  echo "text" | python tools/speak.py --out reports/x.mp3
"""
import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import edge_tts

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import HOME as ROOT  # noqa: E402 - one user's data, not the program folder
CONFIG = ROOT / "config.json"
CHUNK = 2800


def cfg():
    if CONFIG.exists():
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    return {}


def strip_markdown(text: str) -> str:
    """Turn a markdown report into something that sounds right when spoken."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"~~~.*?~~~", " ", text, flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"\1", text)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*\d+[.)]\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*\|.*$", " ", text, flags=re.M)
    text = re.sub(r"^\s*[-=_]{3,}\s*$", " ", text, flags=re.M)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunks(text: str, size: int = CHUNK):
    """Split on paragraph, then sentence, then word boundaries.

    Every character comes out the other side. The previous version truncated any
    sentence longer than `size` with `sent[:size]`, silently dropping the rest —
    whole passages were simply never spoken.
    """
    parts, buf = [], ""

    def flush():
        nonlocal buf
        if buf.strip():
            parts.append(buf.strip())
        buf = ""

    def add(piece, sep):
        nonlocal buf
        if not buf:
            buf = piece
        elif len(buf) + len(sep) + len(piece) <= size:
            buf += sep + piece
        else:
            flush()
            buf = piece

    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= size:
            add(para, "\n\n")
            continue
        for sent in re.split(r"(?<=[.!?…])\s+", para):
            if len(sent) <= size:
                add(sent, " ")
                continue
            # one sentence longer than the limit — break on spaces, never mid-word
            for word in sent.split(" "):
                while len(word) > size:  # a single absurd token; nothing else to do
                    flush()
                    parts.append(word[:size])
                    word = word[size:]
                add(word, " ")
    flush()
    return parts or [text[:size]]


async def stream_synth(text, voice: str, rate: str, volume: str = "+0%", pitch: str = "+0Hz"):
    """Yield MP3 bytes as the voice service produces them.

    edge-tts delivers at roughly the speed the text is spoken — a fifty-second answer
    takes about fifty seconds to synthesise. Waiting for the whole file meant staring at
    a silent dashboard for that entire time. MP3 frames are self-contained, so the
    browser can start playing the first ones while the rest are still arriving.
    """
    for part in chunks(text):
        comm = edge_tts.Communicate(part, voice, rate=rate, volume=volume, pitch=pitch)
        async for msg in comm.stream():
            if msg["type"] == "audio":
                yield msg["data"]


async def synth(text, out: Path, voice: str, rate: str, volume: str, pitch: str):
    out.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out.open("wb") as fh:
        for i, part in enumerate(chunks(text), 1):
            comm = edge_tts.Communicate(part, voice, rate=rate, volume=volume, pitch=pitch)
            async for msg in comm.stream():
                if msg["type"] == "audio":
                    fh.write(msg["data"])
                    written += len(msg["data"])
            print(f"  chunk {i} ok ({len(part)} chars)", file=sys.stderr)
    if written == 0:
        raise RuntimeError("no audio produced")
    return written


def voice_for(c, lang=None):
    """Voice for a language, from config.languages — `report.voice` no longer exists."""
    rep = c.get("report", {})
    langs = c.get("languages", {})
    lang = lang or rep.get("language", "en")
    entry = langs.get(lang) or langs.get("en") or {}
    return entry.get("voice", "en-US-AndrewMultilingualNeural")


def main():
    c = cfg()
    rep = c.get("report", {})
    p = argparse.ArgumentParser(description="Speak a report with a neural voice.")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--in", dest="infile", help="input .md/.txt file")
    src.add_argument("--text", help="literal text to speak")
    p.add_argument("--out", help="output .mp3 (default: reports/<stem>.mp3)")
    p.add_argument("--lang", help="language code from config.json (default: report.language)")
    p.add_argument("--voice", default=None, help="override the language's voice")
    p.add_argument("--rate", default=rep.get("rate", "+0%"))
    p.add_argument("--volume", default="+0%")
    p.add_argument("--pitch", default="+0Hz")
    p.add_argument("--raw", action="store_true", help="do not strip markdown")
    p.add_argument("--play", action="store_true", help="open in the default player")
    a = p.parse_args()
    a.voice = a.voice or voice_for(c, a.lang)

    if a.text:
        text, stem = a.text, datetime.now().strftime("speak-%Y%m%d-%H%M%S")
    elif a.infile:
        src_path = Path(a.infile)
        if not src_path.is_absolute():
            src_path = ROOT / src_path
        text, stem = src_path.read_text(encoding="utf-8"), src_path.stem
    else:
        text, stem = sys.stdin.read(), datetime.now().strftime("speak-%Y%m%d-%H%M%S")

    if not a.raw:
        text = strip_markdown(text)
    if not text.strip():
        sys.exit("nothing to speak")

    out = Path(a.out) if a.out else ROOT / "reports" / f"{stem}.mp3"
    if not out.is_absolute():
        out = ROOT / out

    print(f"voice={a.voice} rate={a.rate} chars={len(text)}", file=sys.stderr)
    size = asyncio.run(synth(text, out, a.voice, a.rate, a.volume, a.pitch))
    print(f"{out}")
    print(f"  {size/1024:.0f} KB", file=sys.stderr)
    if a.play:
        os.startfile(str(out))


if __name__ == "__main__":
    main()
