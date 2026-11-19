#!/usr/bin/env python3
"""Build what gets handed to other people.

  python tools/build_app.py            # everything
  python tools/build_app.py payload    # dist/payload.zip + dist/latest.json  (the update)
  python tools/build_app.py windows    # dist/Assistant-windows.zip  (needs PyInstaller)
  python tools/build_app.py mac        # dist/Assistant-mac.zip      (source + launcher)

Two layers — see launcher.py. The SHELL (Python + libraries + launcher) is what a person
downloads once. The PAYLOAD (server/, tools/, the page) is a quarter-megabyte zip that
the shell fetches by itself from the GitHub release, so a fix reaches testers without
anyone downloading the app again. Releasing is tools/release.py.

What goes in is the PROGRAM only. Never config.json, data/, work/ or reports/ — those are
one person's coursework, and a build that swept them up would hand your tasks and notes
to every tester. The file list below is explicit for that reason: nothing is globbed in.

PyInstaller cannot cross-compile, so from Windows the Mac build is the source plus a
`Start.command` that makes its own virtualenv. It needs Python 3.10+ on the Mac and has
NOT been run on a Mac from here.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
NAME = "Assistant"
STATIC = ROOT / "server" / "static"

# the program, and nothing that belongs to a person
PROGRAM_TOOLS = ["paths.py", "tasks.py", "brief.py", "speak.py", "ask.py", "cdp.py",
                 "autosync.py", "teams.py", "updater.py"]
# third-party libraries the payload uses. The packager cannot see the payload's imports
# (it is loaded from files, not compiled in), so each is collected whole.
SHELL_LIBS = ["fastapi", "starlette", "uvicorn", "pydantic", "pydantic_core", "anyio",
              "h11", "click", "edge_tts", "aiohttp", "websockets", "multipart",
              "python_multipart", "certifi", "typing_extensions", "annotated_types",
              "idna", "sniffio"]

README = """{name} — early access
=====================

A dashboard for your Brightspace deadlines that reads them out loud.

START
  Windows:  open the {name} folder and double-click {name}.exe
            Windows may say "Windows protected your PC" — the app is not signed yet.
            Click "More info", then "Run anyway".
  Mac:      double-click "Start.command"
            The first time, macOS refuses because it was downloaded: right-click the
            file, choose Open, then Open again. First start takes a minute or two
            (it sets itself up). Needs Python 3.10 or newer: https://www.python.org/downloads/

FIRST RUN
  1. Give your assistant a name.
  2. Press "Open sign-in window" and log in to Brightspace with your school account.
     Your password goes to your school's own page. This app never sees or stores it.
  3. Press "Find my courses" and tick the ones to follow.

GOOD TO KNOW
  - Everything stays on your computer. Nothing is uploaded anywhere.
    The only thing that leaves is the text being read aloud, which goes to
    Microsoft's voice service to be turned into speech.
  - It updates itself. When it starts it looks for a newer version on the project's
    GitHub page, downloads it, and uses it the next time you open the app. You do not
    need to download anything again. To switch this off, set "update": {{"auto": false}}
    in config.json.
  - A small extra browser window stays minimised while the app runs. That is what
    keeps you signed in to Brightspace. The sign-in lasts about a day; when it
    runs out the app shows a "Sign in" button.
  - It needs Chrome or Edge installed.
  - This early version tracks deadlines and announcements. It does not submit
    anything to Brightspace for you.

FEEDBACK
  Press the star (top right) and say what worked and what did not. That is the
  whole point of early access — thank you.
"""


def run(cmd):
    print("  $", " ".join(str(c) for c in cmd)[:400])
    subprocess.run(cmd, check=True, cwd=ROOT)


def release_info() -> dict:
    return json.loads((ROOT / "release.json").read_text(encoding="utf-8"))


def payload_files() -> list:
    """(path inside the payload, bytes) — the one definition of what the program is."""
    rows = [("release.json", (ROOT / "release.json").read_bytes()),
            ("config.default.json", (ROOT / "config.default.json").read_bytes()),
            ("server/app.py", (ROOT / "server" / "app.py").read_bytes())]
    rows += [(f"server/static/{f.name}", f.read_bytes())
             for f in sorted(STATIC.iterdir()) if f.is_file()]
    rows += [(f"tools/{n}", (ROOT / "tools" / n).read_bytes()) for n in PROGRAM_TOOLS]
    for arc, _ in rows:
        if Path(arc).name in ("tasks.json", "config.json"):
            sys.exit(f"REFUSING to package personal data: {arc}")
    return rows


def build_payload(notes: str = "", download_page: str = ""):
    """The update itself, and the small file that announces it."""
    DIST.mkdir(exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for arc, data in payload_files():
            info = zipfile.ZipInfo(arc, date_time=(2026, 1, 1, 0, 0, 0))   # reproducible
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            zf.writestr(info, data)
    blob = buf.getvalue()
    (DIST / "payload.zip").write_bytes(blob)
    rel = release_info()
    latest = {"version": rel["version"], "min_shell": rel.get("min_shell", 1),
              "sha256": hashlib.sha256(blob).hexdigest(), "notes": notes,
              "download_page": download_page}
    (DIST / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    print(f"built payload {rel['version']}  ({len(blob) / 1e3:.0f} KB)  sha256 {latest['sha256'][:12]}…")
    return latest


def stage_payload(dest: Path):
    shutil.rmtree(dest, ignore_errors=True)
    for arc, data in payload_files():
        out = dest / arc
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)


def build_windows():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.exit("PyInstaller is not installed:  python -m pip install pyinstaller")
    work = DIST / "_build"
    stage = work / "payload"
    stage_payload(stage)
    sep = os.pathsep
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--name", NAME, "--windowed", "--icon", str(ROOT / "jarvis.ico"),
           "--distpath", str(DIST), "--workpath", str(work / "w"), "--specpath", str(work),
           "--add-data", f"{stage}{sep}payload",
           "--hidden-import", "shell_keep"]
    import importlib.util
    for lib in SHELL_LIBS:
        if importlib.util.find_spec(lib) is not None:
            cmd += ["--collect-all", lib]
    run(cmd + [str(ROOT / "launcher.py")])
    out = DIST / NAME
    (out / "README.txt").write_text(README.format(name=NAME), encoding="utf-8")
    leaked = [p for p in out.rglob("*") if p.name in ("tasks.json", "config.json")]
    if leaked:
        sys.exit(f"REFUSING to package personal data: {leaked}")
    z = DIST / f"{NAME}-windows.zip"
    z.unlink(missing_ok=True)
    shutil.make_archive(str(z.with_suffix("")), "zip", DIST, NAME)
    shutil.rmtree(work, ignore_errors=True)
    print(f"built {z.name}  ({z.stat().st_size / 1e6:.0f} MB)")


START_COMMAND = """#!/bin/bash
# Starts the assistant on a Mac. First run builds a private Python environment
# under ~/Library/Application Support; nothing is installed system-wide.
cd "$(dirname "$0")" || exit 1
DATA="$HOME/Library/Application Support/JARVIS"
VENV="$DATA/venv"

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10 or newer is needed. Get it from https://www.python.org/downloads/ and start this again."
  read -r -p "Press Enter to close."
  exit 1
fi

if [ ! -x "$VENV/bin/python" ]; then
  echo "First start - setting up (a minute or two)..."
  mkdir -p "$DATA"
  "$PY" -m venv "$VENV" || { echo "Could not create the environment."; read -r; exit 1; }
fi
if [ ! -f "$VENV/.ready" ] || [ requirements.txt -nt "$VENV/.ready" ]; then
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  "$VENV/bin/python" -m pip install --quiet -r requirements.txt \\
    || { echo "Could not install what the app needs. Check the internet connection."; read -r; exit 1; }
  touch "$VENV/.ready"
fi
echo "Running. Leave this window open; close it to stop the assistant."
exec "$VENV/bin/python" launcher.py
"""


def build_mac():
    z = DIST / f"{NAME}-mac.zip"
    DIST.mkdir(exist_ok=True)
    z.unlink(missing_ok=True)

    def add(zf, arc, data, mode=0o644):
        info = zipfile.ZipInfo(f"{NAME}/{arc}")
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = (0o100000 | mode) << 16      # unix file + permission bits
        zf.writestr(info, data)

    with zipfile.ZipFile(z, "w") as zf:
        # LF endings and the executable bit: a CRLF shebang or a missing +x and the
        # double-click does nothing at all.
        add(zf, "Start.command", START_COMMAND.replace("\r\n", "\n").encode(), 0o755)
        add(zf, "README.txt", README.format(name=NAME).encode("utf-8"))
        add(zf, "launcher.py", (ROOT / "launcher.py").read_bytes())
        add(zf, "requirements.txt", (ROOT / "requirements.txt").read_bytes())
        for arc, data in payload_files():
            add(zf, f"payload/{arc}", data)
    print(f"built {z.name}  ({z.stat().st_size / 1e3:.0f} KB)  - not run on a Mac from here")


def main():
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("payload", "all"):
        build_payload()
    if what in ("windows", "all"):
        build_windows()
    if what in ("mac", "all"):
        build_mac()


if __name__ == "__main__":
    main()
