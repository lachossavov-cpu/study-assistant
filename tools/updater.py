#!/usr/bin/env python3
"""Apply a newer version on THIS start, not the one after it.

The shell (launcher.py, frozen into the .exe) only checks for updates once the window is
open, so a new version used to land on the second start after it was released: download
on one, use it on the next. Testers open the app once, see nothing new, and reasonably
conclude it does not update.

This file is part of the PROGRAM, not the shell — so it reaches people through an
ordinary update, with no new download. It runs while the server module is being imported
and before anything is listening on the port:

  1. ask the release for its version (a few seconds at most; offline costs ~nothing)
  2. if it is newer, fetch and verify it with the shell's OWN routine — one copy of the
     checksum and unsafe-path checks, not two
  3. start the app again, which picks the newest version, and leave

It does nothing at all unless it is running inside a shell that can carry the result:
not under the test suite, not from the original checkout's JARVIS.bat, not when updates
are switched off, and never twice in one start.
"""
import os
import subprocess
import sys
import urllib.request

QUICK_TIMEOUT = 4            # seconds per request; a slow network gets the old path


def _shell():
    """The running launcher, if there is one. In a frozen app it is __main__."""
    main = sys.modules.get("__main__")
    needed = ("SHELL_VERSION", "check_for_update", "read_release", "auto_update_on", "_get",
              "pick_payload", "vtuple")
    return main if main is not None and all(hasattr(main, n) for n in needed) else None


def _restart(new_version: str):
    """Start the app again and get out of the way. Nothing is listening yet."""
    env = dict(os.environ, ASSISTANT_RESTARTED=new_version,
               # a frozen app starting itself must not inherit the bootloader's state
               PYINSTALLER_RESET_ENVIRONMENT="1")
    env.pop("ASSISTANT_PAYLOAD", None)
    frozen = getattr(sys, "frozen", False)
    args = [sys.executable] + (sys.argv[1:] if frozen else sys.argv)
    if os.name == "nt":
        subprocess.Popen(args, env=env, close_fds=True,
                         creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        os._exit(0)
    # Mac/Linux: same process, same terminal window — Start.command keeps working
    os.execve(sys.executable, args, env)


def check_now(shell=None) -> dict:
    """Look for a newer version and download it, without restarting. For a server that
    is already running: closing the window does not stop it, so opening the dashboard
    again never was a fresh start, and an update could wait for days. Adds `newer`:
    whether a downloaded version is ahead of the one running."""
    shell = shell or _shell()
    if shell is None or not os.environ.get("ASSISTANT_PAYLOAD"):
        return {"state": "skipped", "why": "not inside the app", "newer": False}
    if os.environ.get("ASSISTANT_NO_UPDATE"):
        return {"state": "skipped", "why": "updates are off", "newer": False}
    from pathlib import Path
    running = shell.read_release(Path(os.environ["ASSISTANT_PAYLOAD"])) or {"version": "0"}
    st = {}
    try:
        if shell.auto_update_on():
            st = _remember_notes(shell, _with_quick_get(shell, lambda: shell.check_for_update(running)))
    except Exception as e:                               # noqa: BLE001
        st = {"state": "failed", "why": f"{type(e).__name__}: {e}"[:200]}
    best = shell.pick_payload()[1]
    newer = shell.vtuple(best.get("version")) > shell.vtuple(running.get("version"))
    return {**st, "running": running.get("version"), "version": best.get("version"),
            "newer": newer}


def restart_detached(new_version: str):
    """Restart a server that is already serving, so the newest version takes over.

    Not `_restart()`: that one runs before anything listens. Here the port is held, and a
    new launcher seeing it taken would just open a window onto THIS old process — the
    very bug. So: start the new one a couple of seconds from now, and exit at once.
    """
    env = dict(os.environ, ASSISTANT_RESTARTED=new_version, PYINSTALLER_RESET_ENVIRONMENT="1",
               ASSISTANT_NO_WINDOW="1")              # the page that asked reloads itself
    env.pop("ASSISTANT_PAYLOAD", None)
    frozen = getattr(sys, "frozen", False)
    args = [sys.executable] + (sys.argv[1:] if frozen else sys.argv)
    if os.name == "nt":
        line = subprocess.list2cmdline(args)
        subprocess.Popen(f'cmd /c "ping -n 3 127.0.0.1 >nul & {line}"', env=env,
                         creationflags=0x08000000)   # CREATE_NO_WINDOW
        os._exit(0)
    os.execve(sys.executable, args, env)             # sockets do not survive exec


def _remember_notes(shell, st):
    """Keep "what changed" where the shell cannot overwrite it. After a restart the shell
    checks again, finds the version current, and writes a status with no notes — so the
    page had nothing to say about the update it had just installed."""
    try:
        if st.get("state") == "ready" and st.get("notes") and getattr(shell, "UPD", None):
            shell.UPD.mkdir(parents=True, exist_ok=True)
            (shell.UPD / f"notes-{st['version']}.txt").write_text(st["notes"], encoding="utf-8")
    except (OSError, KeyError, TypeError):
        pass
    return st


def _with_quick_get(shell, fn):
    slow_get = shell._get

    def quick_get(url, limit):
        req = urllib.request.Request(url, headers={"User-Agent": "assistant-updater"})
        with urllib.request.urlopen(req, timeout=QUICK_TIMEOUT) as r:
            data = r.read(limit + 1)
        if len(data) > limit:
            raise ValueError("download is larger than an update should be")
        return data

    shell._get = quick_get           # the shell's own check, with a short leash
    try:
        return fn()
    finally:
        shell._get = slow_get


def quick_update_and_restart(shell=None, restart=_restart) -> dict:
    """Returns what happened; restarts the process when a newer version was installed."""
    shell = shell or _shell()
    if shell is None or not os.environ.get("ASSISTANT_PAYLOAD"):
        return {"state": "skipped", "why": "not inside the app"}
    if os.environ.get("ASSISTANT_NO_UPDATE") or os.environ.get("ASSISTANT_RESTARTED"):
        return {"state": "skipped", "why": "off, or already restarted once"}
    try:
        if not shell.auto_update_on():
            return {"state": "skipped", "why": "auto update is off"}
        from pathlib import Path
        running = shell.read_release(Path(os.environ["ASSISTANT_PAYLOAD"]))
        if not running:
            return {"state": "skipped", "why": "no release.json"}

        st = _remember_notes(shell, _with_quick_get(shell, lambda: shell.check_for_update(running)))
    except Exception as e:                               # noqa: BLE001 - never block a start
        return {"state": "failed", "why": f"{type(e).__name__}: {e}"[:200]}
    if st.get("state") == "ready" and st.get("version") != running.get("version"):
        restart(st["version"])
    return st
