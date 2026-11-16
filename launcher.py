#!/usr/bin/env python3
"""Start the assistant for someone who was handed a copy — and keep it up to date.

Two layers, so that a fix does not mean downloading the app again:

  SHELL    this file, Python itself and the libraries. Downloaded once.
  PAYLOAD  the program: server/, tools/, the dashboard page. A quarter of a megabyte of
           plain files. A copy ships inside the shell, and newer ones are fetched from
           the project's GitHub releases into the user's own folder.

On start the newest usable payload is picked and run. Meanwhile a background check looks
for a newer one, verifies it and unpacks it next to the others; it takes effect the next
time the app is opened, never under a running session.

Three things this must never do, and the reasons the code is shaped the way it is:
  * leave someone with an app that will not start. A payload that fails to load is
    marked bad and the previous one runs instead — an update can be wrong, and the
    person on the other end cannot debug it.
  * run a payload newer than the shell can carry. `min_shell` in the release says what
    it needs; if this shell is older, the page asks for a fresh download instead.
  * touch the user's data. Payloads live in <home>/app/, config and tasks beside it.

The original checkout does not use this file — JARVIS.bat runs server/app.py directly.
"""
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

SHELL_VERSION = 1            # bump when the shell changes: new library, new Python
SHELL = Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent)
# a packaged shell carries its payload in payload/; from source the folder itself is one
BUNDLED = SHELL / "payload" if (SHELL / "payload" / "release.json").is_file() else SHELL
PAYLOAD_MODULES = ("app", "paths", "tasks", "brief", "speak", "ask", "cdp", "autosync",
                   "teams")
VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}$")
MAX_PAYLOAD = 20 * 1024 * 1024

try:                         # keeps the standard library inside a packaged shell
    import shell_keep        # noqa: F401
except ImportError:
    pass


def user_data_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "JARVIS"


# Must be set before the payload's `paths` is imported: it decides where data is kept.
if not os.environ.get("ASSISTANT_HOME"):
    os.environ["ASSISTANT_HOME"] = str(user_data_dir() / "home")
HOME = Path(os.environ["ASSISTANT_HOME"])
UPD = HOME / "app"
VERSIONS = UPD / "versions"

PORT = int(os.environ.get("JARVIS_PORT", "8765"))
os.environ["JARVIS_PORT"] = str(PORT)
# Always 127.0.0.1, never localhost: that resolves to ::1 first, where nothing listens,
# and every request hangs for two seconds before falling back.
URL = f"http://127.0.0.1:{PORT}"


# ---------------------------------------------------------------- which payload runs

def vtuple(v) -> tuple:
    return tuple(int(x) for x in str(v).split(".")) if VERSION_RE.match(str(v or "")) else (0,)


def read_release(folder: Path) -> dict | None:
    try:
        rel = json.loads((folder / "release.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    ok = (VERSION_RE.match(str(rel.get("version", "")))
          and (folder / "server" / "app.py").is_file()
          and (folder / "tools" / "paths.py").is_file())
    return rel if ok else None


def bad_versions() -> set:
    try:
        return set((UPD / "bad.txt").read_text(encoding="utf-8").split())
    except OSError:
        return set()


def mark_bad(version: str, why: str):
    UPD.mkdir(parents=True, exist_ok=True)
    with open(UPD / "bad.txt", "a", encoding="utf-8") as f:
        f.write(version + "\n")
    write_status(state="rolled_back", version=version, message=why[:300])


def pick_payload() -> tuple[Path, dict]:
    """The newest payload that is complete, not known-bad, and fits this shell.

    Every downloaded version is considered, not just the latest: when the newest one
    turns out to be broken, the one before it should run — not the much older copy the
    shell was built with.
    """
    bundled = read_release(BUNDLED) or {"version": "0"}
    best, best_rel = BUNDLED, bundled
    bad = bad_versions()
    try:
        folders = [d for d in VERSIONS.iterdir() if d.is_dir() and VERSION_RE.match(d.name)]
    except OSError:
        folders = []
    for folder in folders:
        if folder.name in bad:
            continue
        rel = read_release(folder)
        if (rel and rel["version"] == folder.name
                and vtuple(rel["version"]) > vtuple(best_rel["version"])
                and int(rel.get("min_shell", 1)) <= SHELL_VERSION):
            best, best_rel = folder, rel
    return best, best_rel


def activate(folder: Path):
    for sub in ("server", "tools"):
        sys.path.insert(0, str(folder / sub))
    os.environ["ASSISTANT_PAYLOAD"] = str(folder)


def deactivate(folder: Path):
    for sub in ("server", "tools"):
        while str(folder / sub) in sys.path:
            sys.path.remove(str(folder / sub))
    for name in PAYLOAD_MODULES:
        sys.modules.pop(name, None)


def load_server():
    """Import the server from the chosen payload; fall back to the bundled one."""
    folder, rel = pick_payload()
    activate(folder)
    try:
        import app as server                             # noqa: PLC0415
        return server, rel
    except Exception as e:                               # noqa: BLE001
        if folder == BUNDLED:
            raise
        mark_bad(rel["version"], f"{type(e).__name__}: {e}")
        deactivate(folder)
        return load_server()             # the next best one; ends at the bundled copy


# ---------------------------------------------------------------- updating

def write_status(**fields):
    """What the dashboard shows about updates. Written whole, via a temp file."""
    try:
        UPD.mkdir(parents=True, exist_ok=True)
        fields["at"] = datetime.now().isoformat(timespec="seconds")
        tmp = UPD / "status.json.tmp"
        tmp.write_text(json.dumps(fields, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, UPD / "status.json")
    except OSError:
        pass


def _get(url: str, limit: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "assistant-updater"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("download is larger than an update should be")
    return data


def update_base(rel: dict) -> str | None:
    """Where updates come from: the project's own GitHub releases, over HTTPS."""
    override = os.environ.get("ASSISTANT_UPDATE_BASE")   # the tests point this at a folder
    if override:
        return override.rstrip("/") + "/"
    repo = str(rel.get("repo") or "")
    if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", repo):
        return None
    return f"https://github.com/{repo}/releases/latest/download/"


def auto_update_on() -> bool:
    if os.environ.get("ASSISTANT_NO_UPDATE"):
        return False
    try:
        cfg = json.loads((HOME / "config.json").read_text(encoding="utf-8"))
        return bool(cfg.get("update", {}).get("auto", True))
    except (OSError, json.JSONDecodeError):
        return True


def unpack(blob: bytes, dest: Path):
    """Extract a payload zip, refusing anything that points outside its folder."""
    root = dest.resolve()
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if not target.is_relative_to(root):
                raise ValueError(f"unsafe path in update: {info.filename}")
        z.extractall(dest)


def check_for_update(running: dict) -> dict:
    """Fetch a newer payload if there is one. Returns the status that was written."""
    base = update_base(running)
    if not base:
        return {"state": "off"}
    try:
        latest = json.loads(_get(base + "latest.json", 64 * 1024).decode("utf-8"))
        version = str(latest.get("version", ""))
        if not VERSION_RE.match(version):
            raise ValueError("the release has no usable version")
        if vtuple(version) <= vtuple(running.get("version")) or version in bad_versions():
            st = {"state": "current", "version": running.get("version")}
            write_status(**st)
            return st
        notes = str(latest.get("notes", ""))[:600]
        if int(latest.get("min_shell", 1)) > SHELL_VERSION:
            # the program moved on further than this shell can follow
            st = {"state": "needs_download", "version": version, "notes": notes,
                  "url": str(latest.get("download_page", ""))[:300]}
            write_status(**st)
            return st
        if read_release(VERSIONS / version) is None:
            blob = _get(base + "payload.zip", MAX_PAYLOAD)
            if hashlib.sha256(blob).hexdigest() != str(latest.get("sha256", "")).lower():
                raise ValueError("the update did not match its checksum")
            VERSIONS.mkdir(parents=True, exist_ok=True)
            tmp = VERSIONS / f"{version}.tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            unpack(blob, tmp)
            rel = read_release(tmp)
            if not rel or rel["version"] != version:
                shutil.rmtree(tmp, ignore_errors=True)
                raise ValueError("the update is incomplete")
            shutil.rmtree(VERSIONS / version, ignore_errors=True)
            os.replace(tmp, VERSIONS / version)
        st = {"state": "ready", "version": version, "notes": notes}
        write_status(**st)
        for old in VERSIONS.iterdir():                   # keep this one and the last one
            if old.is_dir() and old.name not in (version, str(running.get("version"))):
                shutil.rmtree(old, ignore_errors=True)
        return st
    except Exception as e:                               # noqa: BLE001 - never break a start
        st = {"state": "failed", "message": f"{type(e).__name__}: {e}"[:300]}
        write_status(**st)
        return st


# ---------------------------------------------------------------- the window

def already_running() -> bool:
    try:
        with urllib.request.urlopen(f"{URL}/api/setup", timeout=1.5):
            return True
    except Exception:                                    # noqa: BLE001
        return False


def port_free() -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", PORT))
            return True
        except OSError:
            return False


def open_window():
    """The dashboard as its own window, without an address bar."""
    if os.environ.get("ASSISTANT_NO_WINDOW"):            # checking a build, not using it
        return
    exe = None
    try:
        import cdp                                       # noqa: PLC0415
        exe = cdp.chrome_binary()
    except Exception:                                    # noqa: BLE001
        pass
    if exe:
        try:
            subprocess.Popen([str(exe), f"--app={URL}", "--window-size=1360,900",
                              # the voice is synthesised for seconds after the click that
                              # asked for it; without this the browser drops the sound
                              "--autoplay-policy=no-user-gesture-required"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            pass
    import webbrowser                                    # noqa: PLC0415
    webbrowser.open(URL)


def main():
    if already_running():            # second double-click: just show the window again
        activate(pick_payload()[0])
        open_window()
        return
    if not port_free():
        print(f"Port {PORT} is taken by another program. Close it and start again.")
        time.sleep(6)
        sys.exit(1)

    server, running = load_server()
    os.environ["ASSISTANT_SHELL_VERSION"] = str(SHELL_VERSION)
    import uvicorn                                       # noqa: PLC0415

    server._setup_logging()
    print(f"payload {running.get('version')} from {os.environ.get('ASSISTANT_PAYLOAD')}")

    def when_up():
        for _ in range(80):
            if already_running():
                open_window()
                break
            time.sleep(0.25)
        if auto_update_on():
            check_for_update(running)
    threading.Thread(target=when_up, daemon=True).start()

    uvicorn.run(server.app, host=server.bind_host(), port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
