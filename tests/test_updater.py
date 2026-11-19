"""End to end: an old shell picks up a newer payload from a (local) release server,
runs it on the next start, and survives a payload that is broken or tampered with."""
import functools
import hashlib
import http.server
import importlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import build_app  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="upd-"))
served = tmp / "release"
served.mkdir()
os.environ["ASSISTANT_HOME"] = str(tmp / "home")
os.environ["ASSISTANT_NO_WINDOW"] = "1"

handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(served))
handler.log_message = lambda *a, **k: None
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["ASSISTANT_UPDATE_BASE"] = f"http://127.0.0.1:{srv.server_address[1]}"

sys.path.insert(0, str(ROOT))
import launcher  # noqa: E402

results = []


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(("ok   " if cond else "FAIL ") + name, extra)


def publish(version, mutate=None, min_shell=1, lie_about_hash=False):
    """A release folder holding payload.zip + latest.json, as GitHub would serve it."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for arc, data in build_app.payload_files():
            if arc == "release.json":
                data = json.dumps({"version": version, "repo": "", "min_shell": min_shell}).encode()
            if mutate:
                data = mutate(arc, data)
            if data is not None:
                zf.writestr(arc, data)
        if mutate:
            extra = mutate("__extra__", None)
            if extra:
                zf.writestr(*extra)
    blob = buf.getvalue()
    (served / "payload.zip").write_bytes(blob)
    sha = hashlib.sha256(blob).hexdigest()
    (served / "latest.json").write_text(json.dumps({
        "version": version, "min_shell": min_shell, "notes": "Fixed the thing.",
        "sha256": ("0" * 64) if lie_about_hash else sha}))


running = launcher.read_release(launcher.BUNDLED)
REAL = json.loads((ROOT / "release.json").read_text(encoding="utf-8"))["version"]
check("bundled payload is readable", running and running["version"] == REAL, running)
check("first start runs the bundled payload", launcher.pick_payload()[0] == launcher.BUNDLED)

# 1. nothing newer
publish(REAL)
check("same version -> nothing to do", launcher.check_for_update(running)["state"] == "current")

# 2. a tampered download is refused
publish("90.2.0", lie_about_hash=True)
st = launcher.check_for_update(running)
check("checksum mismatch is refused", st["state"] == "failed" and "checksum" in st["message"], st)
check("...and nothing was installed", launcher.pick_payload()[0] == launcher.BUNDLED)

# 3. a zip that tries to write outside its folder is refused
publish("90.2.0", mutate=lambda arc, d: ("../../evil.txt", b"x") if arc == "__extra__" else d)
st = launcher.check_for_update(running)
check("path traversal in an update is refused", st["state"] == "failed" and "unsafe" in st["message"], st)
check("...and nothing escaped", not (tmp / "evil.txt").exists() and not (tmp / "home" / "evil.txt").exists())

# 4. a good update
publish("90.2.0")
st = launcher.check_for_update(running)
check("a newer payload is downloaded", st["state"] == "ready" and st["version"] == "90.2.0", st)
folder, rel = launcher.pick_payload()
check("next start picks it", rel["version"] == "90.2.0" and "versions" in str(folder), folder)

# 5. needs a newer shell -> ask for a download, do not install
publish("90.3.0", min_shell=99)
st = launcher.check_for_update(rel)
check("update needing a newer shell is not installed", st["state"] == "needs_download", st)
check("...still on 0.2.0", launcher.pick_payload()[1]["version"] == "90.2.0")

# 6. an update that cannot even be imported rolls back
publish("90.4.0", mutate=lambda arc, d: b"raise RuntimeError('broken release')\n" if arc == "server/app.py" else d)
st = launcher.check_for_update(rel)
check("broken update downloads fine (the zip is valid)", st["state"] == "ready", st)
os.environ["AVANS_NO_AUTOSYNC"] = "1"
server, ran = launcher.load_server()
check("broken update is rolled back to the version before it", ran["version"] == "90.2.0" and hasattr(server, "app"), ran)
check("...and remembered as bad", "90.4.0" in launcher.bad_versions())
check("...and not offered again", launcher.check_for_update(ran)["state"] == "current")
status = json.loads((launcher.UPD / "status.json").read_text())
check("the user's data folder was never the target", not (tmp / "home" / "server").exists())

# 7. the quick path: a newer version is installed AND used on the same start
sys.path.insert(0, str(ROOT / "tools"))
import updater  # noqa: E402

restarted = []
os.environ["ASSISTANT_PAYLOAD"] = str(launcher.BUNDLED)
publish("90.5.0")
st = updater.quick_update_and_restart(shell=launcher, restart=restarted.append)
check("quick path installs a newer version at start", st.get("state") == "ready", st)
check("...and restarts into it right away", restarted == ["90.5.0"], restarted)
check("...which the restart then picks", launcher.pick_payload()[1]["version"] == "90.5.0")
# what the restarted process sees: it is now running the version it just installed
os.environ["ASSISTANT_PAYLOAD"] = str(launcher.pick_payload()[0])

restarted.clear()
st = updater.quick_update_and_restart(shell=launcher, restart=restarted.append)
check("nothing newer -> no restart", not restarted and st.get("state") == "current", st)

restarted.clear()
os.environ["ASSISTANT_RESTARTED"] = "90.5.0"
publish("90.6.0")
st = updater.quick_update_and_restart(shell=launcher, restart=restarted.append)
check("never restarts twice in one start", not restarted and st.get("state") == "skipped", st)
os.environ.pop("ASSISTANT_RESTARTED")

# 8. a server that has been running for days: opening the dashboard finds the update
publish("90.7.0")
st = updater.check_now(shell=launcher)
check("an open dashboard finds and downloads a newer version",
      st.get("newer") and st.get("version") == "90.7.0" and st.get("running") == "90.5.0", st)
check("...and keeps its notes where the shell's next check cannot wipe them",
      (launcher.UPD / "notes-90.7.0.txt").read_text(encoding="utf-8") == "Fixed the thing.")

restarted.clear()
srv.shutdown()                       # offline: must not hold the start up, must not restart
import time as _t  # noqa: E402
t0 = _t.time()
st = updater.quick_update_and_restart(shell=launcher, restart=restarted.append)
check("offline does not block the start", not restarted and _t.time() - t0 < 10, (st, round(_t.time() - t0, 1)))

check("outside the app (no shell) it does nothing",
      updater.quick_update_and_restart(shell=None, restart=restarted.append)["state"] == "skipped"
      and not restarted)
os.environ.pop("ASSISTANT_PAYLOAD")

srv.shutdown()
shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
