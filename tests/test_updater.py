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
check("bundled payload is readable", running and running["version"] == "0.1.0", running)
check("first start runs the bundled payload", launcher.pick_payload()[0] == launcher.BUNDLED)

# 1. nothing newer
publish("0.1.0")
check("same version -> nothing to do", launcher.check_for_update(running)["state"] == "current")

# 2. a tampered download is refused
publish("0.2.0", lie_about_hash=True)
st = launcher.check_for_update(running)
check("checksum mismatch is refused", st["state"] == "failed" and "checksum" in st["message"], st)
check("...and nothing was installed", launcher.pick_payload()[0] == launcher.BUNDLED)

# 3. a zip that tries to write outside its folder is refused
publish("0.2.0", mutate=lambda arc, d: ("../../evil.txt", b"x") if arc == "__extra__" else d)
st = launcher.check_for_update(running)
check("path traversal in an update is refused", st["state"] == "failed" and "unsafe" in st["message"], st)
check("...and nothing escaped", not (tmp / "evil.txt").exists() and not (tmp / "home" / "evil.txt").exists())

# 4. a good update
publish("0.2.0")
st = launcher.check_for_update(running)
check("a newer payload is downloaded", st["state"] == "ready" and st["version"] == "0.2.0", st)
folder, rel = launcher.pick_payload()
check("next start picks it", rel["version"] == "0.2.0" and "versions" in str(folder), folder)

# 5. needs a newer shell -> ask for a download, do not install
publish("0.3.0", min_shell=99)
st = launcher.check_for_update(rel)
check("update needing a newer shell is not installed", st["state"] == "needs_download", st)
check("...still on 0.2.0", launcher.pick_payload()[1]["version"] == "0.2.0")

# 6. an update that cannot even be imported rolls back
publish("0.4.0", mutate=lambda arc, d: b"raise RuntimeError('broken release')\n" if arc == "server/app.py" else d)
st = launcher.check_for_update(rel)
check("broken update downloads fine (the zip is valid)", st["state"] == "ready", st)
os.environ["AVANS_NO_AUTOSYNC"] = "1"
server, ran = launcher.load_server()
check("broken update is rolled back to the version before it", ran["version"] == "0.2.0" and hasattr(server, "app"), ran)
check("...and remembered as bad", "0.4.0" in launcher.bad_versions())
check("...and not offered again", launcher.check_for_update(ran)["state"] == "current")
status = json.loads((launcher.UPD / "status.json").read_text())
check("the user's data folder was never the target", not (tmp / "home" / "server").exists())

srv.shutdown()
shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
