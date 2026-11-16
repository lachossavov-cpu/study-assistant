#!/usr/bin/env python3
"""Drive a Chrome instance over the DevTools Protocol.

  python tools/cdp.py status      # is the JARVIS Chrome up, and is it logged in?
  python tools/cdp.py launch      # start it
  python tools/cdp.py stop

Why a separate Chrome profile and not the one you browse in: `--remote-debugging-port`
only takes effect on the process that Chrome *starts* with. Passing it to an
already-running Chrome does nothing — the second invocation just opens a tab in the
existing process and the flag is dropped. A dedicated `--user-data-dir` always starts
its own process, so the port is always there.

You log into Brightspace in that profile once. Nothing here ever types a password: if
the tab lands on the Microsoft login page, the sync stops and says so.
"""
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths  # noqa: E402

ROOT = paths.HOME
CONFIG = paths.CONFIG
# A handed-out copy keeps its browser profile next to its own data; the original
# checkout keeps the profile where it has always been, so its sign-in survives.
PROFILE = Path(os.environ.get("AVANS_CHROME_PROFILE")
               or (paths.HOME / "chrome-profile" if paths.SHARED
                   else paths.user_data_dir() / "chrome-profile"))


def _browser_candidates():
    """Chrome first, then Edge. Edge is Chromium, speaks the same DevTools protocol, and
    is on every Windows machine — so a classmate without Chrome still works."""
    if os.name == "nt":
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.environ.get("LOCALAPPDATA", "")]
        rels = ["Google/Chrome/Application/chrome.exe",
                "Microsoft/Edge/Application/msedge.exe"]
        return [Path(r) / rel for rel in rels for r in roots if r]
    if sys.platform == "darwin":
        apps = ["/Applications", str(Path.home() / "Applications")]
        rels = ["Google Chrome.app/Contents/MacOS/Google Chrome",
                "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "Chromium.app/Contents/MacOS/Chromium"]
        return [Path(a) / rel for rel in rels for a in apps]
    import shutil
    names = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
             "microsoft-edge"]
    return [Path(w) for w in (shutil.which(n) for n in names) if w]


CHROME_CANDIDATES = _browser_candidates()

LOGIN_HOSTS = ("login.microsoftonline.com", "login.live.com", "adfs.")
# the pid of the Chrome we started, so minimize() targets our window and not
# whatever else the user happens to have open
LAUNCHED: dict = {"pid": None}


def cfg(*path, default=None):
    try:
        d = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    for k in path:
        if not isinstance(d, dict) or k not in d:
            return default
        d = d[k]
    return d


def base_url() -> str:
    """The school's Brightspace. Avans unless the config says otherwise."""
    return (cfg("brightspace", "base_url") or cfg("course", "base_url")
            or "https://brightspace.avans.nl").rstrip("/")


def bs_host() -> str:
    return urllib.parse.urlparse(base_url()).netloc or "brightspace.avans.nl"


def home_url() -> str:
    """Where the session tab sits. Any Brightspace page keeps the sign-in alive."""
    return cfg("course", "lessons_url") or f"{base_url()}/d2l/home"


def courses() -> list:
    """The courses to follow: `courses` when set, else the original single `course`."""
    many = cfg("courses", default=None)
    if isinstance(many, list) and many:
        return [{"org_unit_id": str(c["org_unit_id"]), "name": c.get("name", "")}
                for c in many if isinstance(c, dict) and c.get("org_unit_id")]
    ou = cfg("course", "org_unit_id")
    return [{"org_unit_id": str(ou), "name": cfg("course", "name", default="")}] if ou else []


def port() -> int:
    return int(os.environ.get("AVANS_CDP_PORT") or cfg("sync", "cdp_port", default=9222))


def chrome_binary() -> Path | None:
    override = cfg("sync", "chrome_path")
    if override and Path(override).is_file():
        return Path(override)
    return next((p for p in CHROME_CANDIDATES if p.is_file()), None)


# ------------------------------------------------------------------ HTTP side

def _get(path, method="GET", timeout=3):
    url = f"http://127.0.0.1:{port()}{path}"
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    return json.loads(body) if body.strip().startswith(("{", "[")) else body


def is_up() -> bool:
    try:
        _get("/json/version", timeout=1)
        return True
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return False


def targets():
    try:
        return [t for t in _get("/json/list") if t.get("type") == "page"]
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return []


def find(host_substr):
    """A page whose URL contains `host_substr`, preferring one that is not a login page."""
    pages = [t for t in targets() if host_substr in (t.get("url") or "")]
    good = [t for t in pages if not any(h in t["url"] for h in LOGIN_HOSTS)]
    return (good or pages or [None])[0]


def open_tab(url):
    # Chrome >= 111 wants PUT here; older builds only answer GET. Try both.
    q = urllib.parse.quote(url, safe="")
    for method in ("PUT", "GET"):
        try:
            return _get(f"/json/new?{q}", method=method, timeout=5)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            continue
    return None


def close_tab(target_id):
    try:
        _get(f"/json/close/{target_id}", timeout=3)
    except (urllib.error.URLError, OSError):
        pass


def launch(headless=True, url="about:blank") -> bool:
    """Start the dedicated Chrome. Returns True once the debugging port answers.

    Headless by default: this runs on every dashboard start, and a browser window
    jumping to the foreground each time would be worse than the stale data it fixes.
    `login()` is the visible one — you only need it when the Brightspace session dies.

    An initial URL is required, not optional: Chrome with no window and no tab has
    nothing to stay alive for and exits immediately, taking the debugging port with it.
    """
    if is_up():
        return True
    exe = chrome_binary()
    if not exe:
        return False
    PROFILE.mkdir(parents=True, exist_ok=True)
    args = [
        str(exe),
        f"--remote-debugging-port={port()}",
        f"--user-data-dir={PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
        "--disable-background-timer-throttling",
    ]
    if headless:
        args.append("--headless=new")
    args.append(url)
    try:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        LAUNCHED["pid"] = proc.pid
    except OSError:
        return False
    # The port answering is not the same as a usable browser — wait for a real page,
    # otherwise callers get a live port with nothing to drive.
    for _ in range(80):                     # up to ~20s; a cold profile is slow
        if is_up() and targets():
            return True
        time.sleep(0.25)
    return is_up()


def is_headless() -> bool:
    """True when the running profile is headless.

    Read the User-Agent, NOT the `Browser` field: `--headless=new` reports
    `Browser: Chrome/153...` exactly like a normal window, and only the User-Agent still
    says `HeadlessChrome/153...`. Checking the wrong field made this silently always
    answer False, which meant a headless profile was never replaced and kept 403-ing.
    """
    try:
        ver = _get("/json/version", timeout=2)
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return False
    return "headlesschrome" in str((ver or {}).get("User-Agent", "")).lower()


def profile_pid() -> int | None:
    """The pid of the Chrome holding OUR profile, so we never touch the user's browser."""
    if LAUNCHED.get("pid"):
        return LAUNCHED["pid"]
    if os.name != "nt":
        return None
    try:
        needle = str(PROFILE).replace("\\", "\\\\")
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
             f"Where-Object {{ $_.CommandLine -like '*{PROFILE}*' }} | "
             "Select-Object -First 1 -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=12)
        pid = (out.stdout or "").strip().splitlines()
        return int(pid[0]) if pid and pid[0].isdigit() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def minimize(pid: int | None = None) -> bool:
    """Put the JARVIS window out of the way without closing it.

    It has to be a real window - headless cannot renew the Brightspace sign-in on its
    own - but it does not have to be in your face. Minimised still runs JS, so the
    session keeps refreshing.

    Only ever touches the process holding OUR profile. Matching windows by title across
    every process would minimise the browser the user is reading in, which is a rude
    surprise arriving out of nowhere; without a pid we do nothing at all.
    """
    if os.name != "nt":
        return _minimize_cdp()           # no Win32 here; ask the browser itself
    if pid is None:                      # only discover when not told
        pid = profile_pid()
    if not pid:
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return False
    user32 = ctypes.windll.user32
    SW_MINIMIZE = 6
    hit = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            user32.ShowWindow(hwnd, SW_MINIMIZE)
            hit.append(hwnd)
        return True

    try:
        user32.EnumWindows(cb, 0)
    except OSError:
        return False
    return bool(hit)


def _minimize_cdp() -> bool:
    """Minimise through DevTools. It can only ever reach OUR browser — the debugging port
    belongs to our profile — so the rule about not touching the user's own window holds
    without needing a pid."""
    try:
        ver = _get("/json/version", timeout=2)
        ws = ver.get("webSocketDebuggerUrl") if isinstance(ver, dict) else None
        tgt = (targets() or [None])[0]
        if not ws or not tgt:
            return False
        return bool(asyncio.run(_set_window_state(ws, tgt["id"], "minimized")))
    except Exception:                                    # noqa: BLE001
        return False


async def _set_window_state(ws_url, target_id, state):
    import websockets
    async with websockets.connect(ws_url, open_timeout=5) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Browser.getWindowForTarget",
                                  "params": {"targetId": target_id}}))
        win = None
        for _ in range(20):
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
            if msg.get("id") == 1:
                win = (msg.get("result") or {}).get("windowId")
                break
        if win is None:
            return False
        await ws.send(json.dumps({"id": 2, "method": "Browser.setWindowBounds",
                                  "params": {"windowId": win,
                                             "bounds": {"windowState": state}}}))
        for _ in range(20):
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
            if msg.get("id") == 2:
                return "error" not in msg
    return False


def ensure_session_tab(url=None, minimise=True) -> dict:
    """Make sure a VISIBLE Chrome with a Brightspace tab is up, and put it out of the way.

    This is what keeps the unattended check working. A headless profile cannot complete
    the silent SSO refresh and starts returning 403 after about a day; a real window
    does it by itself, usually without anyone typing. So the dashboard opening brings
    this up rather than waiting for the session to die and asking for a click.

    Returns {started, headless_replaced, minimised, url}.
    """
    target_url = url or home_url()
    out = {"started": False, "headless_replaced": False, "minimised": False,
           "url": target_url}

    if is_up() and is_headless():
        # a headless one is worse than none here - it will go 403 and stay there
        stop()
        out["headless_replaced"] = True

    if not is_up():
        out["started"] = launch(headless=False, url=target_url)
    else:
        tgt = find(bs_host())
        if not tgt:
            open_tab(target_url)
            out["started"] = True

    if minimise and is_up():
        out["minimised"] = minimize(LAUNCHED.get("pid"))
    return out


def login(course_url=None) -> bool:
    """Open the profile in a VISIBLE window so the user can sign in themselves.

    Headless and headed cannot share one user-data-dir at the same time, so the
    background instance is closed first. Nothing here ever types credentials.
    """
    url = course_url or home_url()
    if is_up() and not stop():
        return False            # still holding the profile lock; a new one cannot start
    return launch(headless=False, url=url)


def stop(wait=12.0) -> bool:
    """Shut the browser down and wait for the port to actually go.

    Closing the tabs one by one is not enough: Chrome can sit there with zero pages and
    the port still open, and then `launch()` sees `is_up()` and returns success without
    starting anything. `Browser.close` ends the process. Returns True once the port is
    gone.
    """
    try:
        ver = _get("/json/version", timeout=2)
        ws = ver.get("webSocketDebuggerUrl") if isinstance(ver, dict) else None
        if ws:
            asyncio.run(_browser_close(ws))
    except Exception:                                    # noqa: BLE001
        for t in targets():                              # older builds: close the pages
            close_tab(t["id"])
    end = time.time() + wait
    while time.time() < end:
        if not is_up():
            return True
        time.sleep(0.25)
    return not is_up()


async def _browser_close(ws_url):
    import websockets
    try:
        async with websockets.connect(ws_url, open_timeout=5) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
            await asyncio.wait_for(ws.recv(), timeout=3)
    except Exception:                                    # noqa: BLE001
        pass                                             # the socket dies with the browser


# ------------------------------------------------------------------ WebSocket side

async def _eval(ws_url, expression, timeout):
    import websockets
    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024,
                                  open_timeout=timeout) as ws:
        await ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "awaitPromise": True,          # the expressions here are all async
                "returnByValue": True,
                "userGesture": True,
            },
        }))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if msg.get("id") != 1:
                continue                        # events we did not ask for
            if "error" in msg:
                raise RuntimeError(msg["error"].get("message", "CDP error"))
            res = msg.get("result", {})
            if res.get("exceptionDetails"):
                raise RuntimeError(str(res["exceptionDetails"].get("text", "JS threw")))
            return res.get("result", {}).get("value")


def evaluate(target, expression, timeout=45.0):
    """Run JS in an open tab and return its value. `target` is a /json/list entry."""
    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError("target has no webSocketDebuggerUrl")
    return asyncio.run(_eval(ws_url, expression, timeout))


def navigate(target, url, timeout=30.0):
    """Point a tab at a URL and wait for the document to finish loading."""
    asyncio.run(_navigate(target["webSocketDebuggerUrl"], url, timeout))


async def _navigate(ws_url, url, timeout):
    import websockets
    async with websockets.connect(ws_url, max_size=16 * 1024 * 1024,
                                  open_timeout=timeout) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
        await ws.send(json.dumps({"id": 2, "method": "Page.navigate",
                                  "params": {"url": url}}))
        end = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < end:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            except asyncio.TimeoutError:
                continue
            if msg.get("method") == "Page.loadEventFired":
                return
        raise TimeoutError(f"page did not load within {timeout}s: {url}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "launch":
        ok = launch()
        print("launched (headless)" if ok else "could not launch Chrome")
        sys.exit(0 if ok else 1)
    if cmd == "login":
        ok = login()
        print("Chrome is open - sign in to Brightspace in that window."
              if ok else "could not launch Chrome")
        sys.exit(0 if ok else 1)
    if cmd == "stop":
        stop()
        print("asked Chrome to close")
        return
    # status
    print(f"port       : {port()}")
    print(f"profile    : {PROFILE}")
    print(f"chrome     : {chrome_binary() or 'NOT FOUND'}")
    print(f"debug port : {'up' if is_up() else 'down'}")
    for t in targets():
        url = (t.get("url") or "")[:90]
        flag = "  <-- login page" if any(h in url for h in LOGIN_HOSTS) else ""
        print(f"  tab: {url}{flag}")


if __name__ == "__main__":
    main()
