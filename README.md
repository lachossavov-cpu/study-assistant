# Study assistant

A local dashboard that follows your Brightspace courses, tells you what is due, and reads
it out loud. Early access, built by a student for classmates.

**Download:** https://lachossavov-cpu.github.io/study-assistant/

- Runs entirely on your own computer. Nothing is uploaded.
- You sign in yourself, on Brightspace's and Microsoft's own pages; no password is stored.
- Optional read-only Teams timetable, and a phone view over your home Wi-Fi.
- Updates itself from this repository's releases; a broken update rolls back on its own.

## For developers

```bash
python -m pip install -r requirements.txt
python launcher.py                 # run from source, data in your per-user folder
python tests/run_tests.py --no-network
python tools/release.py 0.2.0 "What changed"   # maintainer: test, build, publish
```

The app is two layers: a shell (Python, libraries, `launcher.py`) downloaded once, and a
small payload (`server/`, `tools/`) that the shell fetches from the latest release.
