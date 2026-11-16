#!/usr/bin/env python3
"""Read the timetable from Microsoft Teams, through the same signed-in browser profile.

  python tools/teams.py            # fetch the next two weeks, store them
  python tools/teams.py --dry-run  # look, store nothing
  python tools/teams.py --json

How it gets in, and where the line is drawn. The profile that is signed in to Brightspace
is signed in to Microsoft as well, so opening Teams there needs no second login. The
Teams page keeps a Microsoft Graph token; the script below runs INSIDE that page, uses
the token for one read-only request, and hands back the events.

That token can do far more than read a calendar — it can send mail and write files. So:
  * it never leaves the page. Python receives events, never the token, and nothing here
    logs, stores or returns it.
  * exactly one kind of call is made, a GET on the calendar. There is no code path that
    writes, sends or deletes anything, and none should be added to this file.
  * only the fields the dashboard shows are requested: no attendees, no body text.

No Azure app registration and no IT approval — but it leans on how the Teams web client
stores its session, which Microsoft can change without notice. If it stops working, it
fails as "could not read Teams" and the Brightspace side carries on.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cdp                      # noqa: E402
import tasks as tasks_mod       # noqa: E402

TEAMS_URL = "https://teams.microsoft.com/v2/"
HOSTS = ("teams.cloud.microsoft", "teams.microsoft.com")

# Runs in the Teams page. Returns events or a reason — never the token.
CALENDAR_JS = r"""
(async () => {
  const want = 'Calendars.Read';
  let token = null, expires = 0;
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (!/accesstoken/i.test(k)) continue;
      let v; try { v = JSON.parse(localStorage.getItem(k)); } catch (e) { continue; }
      if (!v || !v.secret || !v.target) continue;
      const scopes = v.target.toLowerCase();
      if (!scopes.includes('graph.microsoft.com/' + want.toLowerCase())
          && !(scopes.includes('graph.microsoft.com') && scopes.includes(want.toLowerCase()))) continue;
      const exp = Number(v.expiresOn || v.expires_on || 0);
      if (exp > expires) { expires = exp; token = v.secret; }   // the freshest one
    }
  } catch (e) { return JSON.stringify({ok: false, reason: 'storage', message: String(e).slice(0, 120)}); }
  if (!token) return JSON.stringify({ok: false, reason: 'no_token', host: location.host});
  if (expires && expires * 1000 < Date.now())
    return JSON.stringify({ok: false, reason: 'expired', host: location.host});

  const url = 'https://graph.microsoft.com/v1.0/me/calendarView'
    + '?startDateTime=%(START)s&endDateTime=%(END)s'
    + '&$select=id,subject,start,end,location,isCancelled,isAllDay,isOnlineMeeting'
    + '&$orderby=start/dateTime&$top=100';
  let r;
  try {
    r = await fetch(url, {headers: {Authorization: 'Bearer ' + token,
                                    Prefer: 'outlook.timezone="UTC"'}});
  } catch (e) { return JSON.stringify({ok: false, reason: 'network', message: String(e).slice(0, 120)}); }
  token = null;
  if (!r.ok) return JSON.stringify({ok: false, reason: 'graph', status: r.status});
  const j = await r.json();
  return JSON.stringify({ok: true, events: (j.value || []).map(e => ({
    id: e.id, title: e.subject || '',
    start: e.start && e.start.dateTime, end: e.end && e.end.dateTime,
    where: (e.location && e.location.displayName) || '',
    cancelled: !!e.isCancelled, all_day: !!e.isAllDay, online: !!e.isOnlineMeeting
  }))});
})()
"""


def enabled() -> bool:
    return bool(cdp.cfg("teams", "enabled", default=False))


def _teams_tab(wait=45.0):
    """A tab that has finished landing on Teams, or None.

    Opening Teams goes through login.microsoftonline.com and usually comes back by itself.
    If it does not — an account picker, an MFA prompt — that is for the person to answer
    in the window; nothing here types anything.
    """
    def live():
        return next((t for h in HOSTS if (t := cdp.find(h))), None)
    tab = live()
    if not tab:
        cdp.open_tab(TEAMS_URL)
    end = time.time() + wait
    while time.time() < end:
        tab = live()
        if tab and not any(h in (tab.get("url") or "") for h in cdp.LOGIN_HOSTS):
            return tab
        time.sleep(1.5)
    return None


def to_local(utc_naive: str | None):
    if not utc_naive:
        return None
    try:
        dt = datetime.fromisoformat(utc_naive[:19]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.astimezone().replace(tzinfo=None).isoformat(timespec="minutes")


def fetch(days=14):
    cdp.ensure_session_tab()
    if not cdp.is_up():
        return {"ok": False, "reason": "chrome", "message": "Could not start the browser."}
    tab = _teams_tab()
    if not tab:
        return {"ok": False, "reason": "login",
                "message": "Teams is waiting for a sign-in. Open the sign-in window."}
    now = datetime.now(timezone.utc)
    js = CALENDAR_JS % {"START": now.strftime("%Y-%m-%dT00:00:00Z"),
                        "END": (now + timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")}
    raw = None
    # A page that has only just loaded has not fetched its tokens yet — give it a moment
    # rather than calling a cold start a failure.
    for attempt in range(4):
        tab = _teams_tab(wait=10) or tab
        try:
            raw = cdp.evaluate(tab, js, timeout=40)
        except Exception as e:                                   # noqa: BLE001
            raw = json.dumps({"ok": False, "reason": "evaluate", "message": str(e)[:160]})
        res = json.loads(raw or "{}")
        if res.get("ok") or res.get("reason") not in ("no_token", "expired", "evaluate"):
            break
        time.sleep(4 + 2 * attempt)
    if not res.get("ok"):
        res.setdefault("message", {
            "no_token": "Teams is open but has not signed in yet.",
            "expired": "The Teams session has expired.",
            "graph": f"Microsoft refused the calendar request ({res.get('status')}).",
        }.get(res.get("reason"), "Could not read Teams."))
        return res
    events = []
    for e in res["events"]:
        if e.get("cancelled"):
            continue
        events.append({"id": e["id"], "title": e["title"], "where": e["where"],
                       "start": to_local(e["start"]), "end": to_local(e["end"]),
                       "all_day": e["all_day"], "online": e["online"]})
    return {"ok": True, "events": events}


def run(dry_run=False, days=14):
    res = fetch(days=days)
    if not res.get("ok") or dry_run:
        return res
    db = tasks_mod.load()
    db["schedule"] = res["events"]
    db["schedule_sync"] = tasks_mod.now()
    tasks_mod.save(db)
    return res


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    r = run(dry_run=a.dry_run)
    if a.json:
        print(json.dumps(r, ensure_ascii=False))
    elif not r.get("ok"):
        print(f"Teams: {r.get('reason')} — {r.get('message')}")
    else:
        print(f"{len(r['events'])} events in the next two weeks")
        for e in r["events"][:12]:
            print(f"  {e['start']}  {e['title'][:60]}  {e['where'][:30]}")
    sys.exit(0 if r.get("ok") else 1)


if __name__ == "__main__":
    main()
