#!/usr/bin/env python3
"""Get a summary and a step-by-step plan for each assignment, from the briefs service.

The beta has no Claude session of its own, so the reading is done by a small service
(briefs-server/) that holds the API key and asks Claude Opus 5.5. Each assignment is
written up ONCE: the first student in a course pays for it, every classmate after that
gets the stored copy straight away.

  1. a key is made from what identifies the brief without downloading anything: the
     school, the course, the documents' ids and change dates, and the assignment's name
     without the sit/resit words (a resit shares the brief of its first attempt)
  2. the service is asked for that key; if it has it, done
  3. if not, the documents are downloaded through THIS student's signed-in Brightspace,
     sent to the service, and the brief comes back

What leaves the computer: the assignment's documents (to the service and to Anthropic,
which writes the summary) — never the student's name, grades or anything else. The
setup screen and the download page say so; `features.briefs: false` turns it off.
"""
import base64
import hashlib
import http.client
import io
import json
import re
import socket
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cdp                      # noqa: E402

MAX_NEW_PER_RUN = 4             # a sync must not turn into minutes of uploading
MAX_DOC_BYTES = 14 * 1024 * 1024
TIMEOUT = 120                   # writing a brief takes Opus a little while


def service_url() -> str:
    if not cdp.cfg("features", "briefs", default=False):
        return ""
    return str(cdp.cfg("briefs", "url", default="") or "").rstrip("/")


def brief_key(task) -> str | None:
    """Same documents + same assignment = same key, for every student in the course."""
    import autosync                                       # noqa: PLC0415 - avoid a cycle
    docs = task.get("brief_docs") or []
    if not docs:
        return None
    parts = [cdp.bs_host(), str(task.get("source_unit") or ""),
             autosync._assignment_key(task.get("title"))]
    parts += sorted(f"{d.get('id')}:{d.get('mod') or ''}" for d in docs)
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def format_brief(b: dict) -> str:
    """The card's "What you have to do" text. One item per line, never wrapped by hand:
    the card is a narrow column with pre-wrap, and hard breaks read like clippings."""
    out = [b.get("summary", "").strip(), ""]
    steps = [s for s in b.get("steps") or [] if s.strip()]
    if steps:
        out += ["Steps:"] + [f"{i}. {s.strip()}" for i, s in enumerate(steps, 1)] + [""]
    watch = [s for s in b.get("watch_out") or [] if s.strip()]
    if watch:
        out += ["What costs points:"] + [f"- {s.strip()}" for s in watch] + [""]
    if b.get("hand_in"):
        out += [f"Hand in: {b['hand_in'].strip()}"]
    return "\n".join(out).strip()


# ---------------------------------------------------------------- documents

def office_text(data: bytes) -> str:
    """Plain text out of a .docx or .pptx — Claude reads PDF and text, not Office files,
    and there is no library for it in the app (a new one would need a new download)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return ""
    names = sorted(n for n in z.namelist()
                   if n == "word/document.xml" or re.match(r"ppt/slides/slide\d+\.xml$", n))
    names.sort(key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)) if "slide" in n else 0)
    chunks = []
    for n in names:
        xml = z.read(n).decode("utf-8", "replace")
        xml = re.sub(r"</w:p>|</a:p>", "\n", xml)
        xml = re.sub(r"<w:tab/>", "\t", xml)
        text = re.sub(r"<[^>]+>", "", xml)
        text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&apos;", "'"))
        chunks.append(re.sub(r"\n\s*\n+", "\n", text).strip())
    return "\n\n".join(c for c in chunks if c)


def prepare(name: str, data: bytes, mime: str):
    """(filename, bytes, mime) ready for the service, or None if it cannot be read."""
    low = name.lower()
    if data[:4] == b"%PDF" or "pdf" in mime:
        return (name if low.endswith(".pdf") else name + ".pdf", data, "application/pdf")
    if low.endswith((".docx", ".pptx")) or "officedocument" in mime:
        text = office_text(data)
        return (re.sub(r"\.\w+$", "", name) + ".txt", text.encode("utf-8"), "text/plain") if text else None
    if mime.startswith("text/") or low.endswith((".txt", ".md")):
        return (name, data, "text/plain")
    return None


DOWNLOAD_JS = r"""
(async () => {
  const MAX = %(MAX)d, out = [];
  for (const p of %(PATHS)s) {
    try {
      const r = await fetch(encodeURI(p), {credentials: 'include'});
      if (!r.ok) { out.push({err: r.status}); continue; }
      const b = new Uint8Array(await r.arrayBuffer());
      if (b.length > MAX) { out.push({err: 'too large'}); continue; }
      let s = '';
      for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
      out.push({mime: r.headers.get('content-type') || '', b64: btoa(s)});
    } catch (e) { out.push({err: String(e).slice(0, 100)}); }
  }
  return JSON.stringify(out);
})()
"""


def download_docs(docs):
    """Fetch the documents in the signed-in Brightspace tab. [(name, bytes, mime) | None]"""
    tab = cdp.find(cdp.bs_host())
    if not tab:
        return [None] * len(docs)
    js = DOWNLOAD_JS % {"MAX": MAX_DOC_BYTES, "PATHS": json.dumps([d["path"] for d in docs])}
    rows = json.loads(cdp.evaluate(tab, js, timeout=120) or "[]")
    out = []
    for d, r in zip(docs, rows):
        if not r or r.get("err"):
            out.append(None)
            continue
        name = Path(str(d.get("path") or d.get("title") or "document")).name
        out.append((name, base64.b64decode(r["b64"]), r.get("mime") or ""))
    return out


# ---------------------------------------------------------------- the service

# Cloudflare answers Python's default agent ("Python-urllib/3.x") with 403 / error 1010
# before the request ever reaches the service. Seen on the first live call.
USER_AGENT = "StudyAssistant/1 (+https://github.com/lachossavov-cpu/study-assistant)"


class _IPv4FirstConnection(http.client.HTTPSConnection):
    """Try IPv4 addresses before IPv6, and give each address at most 10 s to connect.

    Cloudflare publishes IPv6 addresses first, and on a network without working IPv6
    Python waits about 21 s on each of them before trying IPv4: measured 42 s for a
    request that takes 0.2 s over IPv4. Many home networks are like that.
    """

    def connect(self):
        infos = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        infos.sort(key=lambda i: i[0] != socket.AF_INET)
        err = OSError(f"no address for {self.host}")
        for family, kind, proto, _, addr in infos:
            sock = socket.socket(family, kind, proto)
            try:
                sock.settimeout(10)
                sock.connect(addr)
                sock.settimeout(self.timeout)
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                return
            except OSError as e:
                err = e
                sock.close()
        raise err


class _IPv4FirstHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_IPv4FirstConnection, req, context=self._context)


_OPENER = urllib.request.build_opener(_IPv4FirstHandler)


def _req(url, data=None, headers=None, method=None):
    headers = {"user-agent": USER_AGENT, **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=TIMEOUT) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, {}


def upload(base, name, data, mime):
    boundary = "----assistant" + hashlib.sha1(data[:4096] + name.encode()).hexdigest()[:16]
    safe = re.sub(r'["\r\n\\]', "_", name)[:120]
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{safe}"\r\n'
            f"Content-Type: {mime}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    s, j = _req(f"{base}/upload", body, {"content-type": f"multipart/form-data; boundary={boundary}"}, "POST")
    if s != 200 or "ticket" not in j:
        raise RuntimeError(f"upload refused ({s}: {j.get('error')})")
    return j["ticket"]


def ensure_briefs(db, fetch_docs=download_docs, base=None) -> dict:
    """Give every automatically found task with documents a brief. Returns counts.

    `fetch_docs` is injectable so the tests can run without a browser.
    """
    base = base if base is not None else service_url()
    out = {"written": [], "stored": [], "failed": []}
    if not base:
        return out
    new = 0
    for t in db.get("tasks", []):
        if t.get("status") in ("done", "dropped") or not t.get("auto"):
            continue
        key = brief_key(t)
        if not key or (t.get("brief_key") == key and t.get("instructions")):
            continue
        try:
            s, j = _req(f"{base}/brief?key={key}")
            if s == 200 and j.get("summary"):
                apply(t, key, j)
                out["stored"].append(t["title"])
                continue
            if new >= MAX_NEW_PER_RUN:
                continue                                  # next sync picks it up
            files = [f for f in (prepare(*d) if d else None for d in fetch_docs(t["brief_docs"])) if f]
            if not files:
                out["failed"].append({"title": t["title"], "why": "documents could not be read"})
                continue
            tickets = [{"ticket": upload(base, *f), "name": f[0]} for f in files]
            s, j = _req(f"{base}/brief", json.dumps({"key": key, "title": t["title"], "tickets": tickets}).encode(),
                        {"content-type": "application/json"}, "POST")
            new += 1
            if s == 200 and j.get("summary"):
                apply(t, key, j)
                out["written"].append(t["title"])
            else:
                out["failed"].append({"title": t["title"], "why": j.get("error") or f"HTTP {s}"})
                if s == 429:
                    break                                 # the cap is reached; stop asking today
        except (OSError, RuntimeError, ValueError) as e:
            out["failed"].append({"title": t.get("title"), "why": str(e)[:160]})
    return out


BRIEF_FIELDS = ("brief_key", "brief", "instructions", "instructions_source")


def run() -> dict:
    """One pass: briefs for whatever is missing, saved without clobbering anything.

    Writing can take minutes, and the dashboard may change a task meanwhile (Done,
    Start). So the work is done on a copy, and only the brief fields are then copied
    onto a fresh read of the list.
    """
    import tasks as tasks_mod                            # noqa: PLC0415
    work = tasks_mod.load()
    res = ensure_briefs(work)
    if res["written"] or res["stored"]:
        made = {t["id"]: t for t in work.get("tasks", []) if t.get("brief_key")}
        db = tasks_mod.load()
        for t in db.get("tasks", []):
            src = made.get(t["id"])
            if src and src.get("brief_key") != t.get("brief_key"):
                for k in BRIEF_FIELDS:
                    t[k] = src.get(k)
        tasks_mod.save(db)
    return res


def apply(t, key, b):
    t["brief_key"] = key
    t["brief"] = {k: b.get(k) for k in ("summary", "steps", "watch_out", "hand_in", "made", "model")}
    t["instructions"] = format_brief(b)
    names = ", ".join(b.get("sources") or [d.get("title", "") for d in t.get("brief_docs") or []])
    t["instructions_source"] = f"AI summary by Claude Opus 5.5 of: {names} — check the documents before handing in"
