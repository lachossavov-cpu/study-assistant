#!/usr/bin/env python3
"""Ship a new version to everyone who already has the app.

  python tools/release.py 0.2.0 "Fixed the sign-in button; schedule shows rooms."
  python tools/release.py 0.2.0 "..." --dry-run        # build and check, publish nothing
  python tools/release.py 0.3.0 "..." --min-shell 2    # this version needs a fresh download

What it does, in order — and it stops at the first thing that fails:
  1. runs the tests (a release that breaks goes to every tester at once)
  2. writes the version into release.json
  3. builds the update (payload.zip + latest.json) and both installers
  4. commits, tags, pushes, and publishes a GitHub release with all four files

Testers do nothing. Their app looks at the latest release when it starts, downloads the
quarter-megabyte payload, and runs it the next time it is opened. If the new version
fails to start on their machine, their app goes back to the one before by itself.

All four files go into EVERY release, installers included, because the download links
point at "latest": a release holding only the update would break the download page.

When to pass --min-shell: only when the new version needs something the installed app
does not carry — a new third-party library in requirements.txt, a newer Python. Then
bump SHELL_VERSION in launcher.py to the same number. Old copies will say "this version
needs a fresh download" instead of crashing. Ordinary fixes never need it.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
sys.path.insert(0, str(ROOT / "tools"))
import build_app  # noqa: E402

ASSETS = ["payload.zip", "latest.json", "Assistant-windows.zip", "Assistant-mac.zip"]


def sh(cmd, check=True, capture=False):
    print("  $", " ".join(cmd)[:200])
    return subprocess.run(cmd, cwd=ROOT, check=check, text=True,
                          capture_output=capture, encoding="utf-8", errors="replace")


def gh_exe() -> str | None:
    found = shutil.which("gh")
    if found:
        return found
    for p in (Path(os.environ.get("ProgramFiles", "")) / "GitHub CLI" / "gh.exe",):
        if p.is_file():
            return str(p)
    return None


def repo_slug() -> str:
    """owner/name, from the git remote. The app updates from here and nowhere else."""
    out = sh(["git", "remote", "get-url", "origin"], check=False, capture=True)
    m = re.search(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?\s*$", out.stdout or "")
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def vtuple(v):
    return tuple(int(x) for x in v.split("."))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("version")
    ap.add_argument("notes", help="one or two sentences testers will see in the app")
    ap.add_argument("--min-shell", type=int)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-tests", action="store_true")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if not re.match(r"^\d+\.\d+\.\d+$", a.version):
        sys.exit("version must look like 0.2.0")
    rel_path = ROOT / "release.json"
    rel = json.loads(rel_path.read_text(encoding="utf-8"))
    repo = repo_slug() or rel.get("repo", "")
    if not repo and not a.dry_run:
        sys.exit("No GitHub remote yet. Create the repository and `git remote add origin` first.")
    published = sh(["git", "tag", "--list", f"v{a.version}"], check=False, capture=True).stdout.strip()
    if published:
        sys.exit(f"v{a.version} is already released. Testers only pick up a HIGHER number.")
    if vtuple(a.version) < vtuple(rel["version"]):
        sys.exit(f"{a.version} is lower than the current {rel['version']} — nobody would update.")

    if not a.skip_tests:
        print("== tests")
        if sh([sys.executable, "tests/run_tests.py", "--no-network"], check=False).returncode:
            sys.exit("Tests failed — nothing was released.")

    print("== version")
    rel.update(version=a.version, repo=repo)
    if a.min_shell:
        rel["min_shell"] = a.min_shell
    tmp = rel_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rel, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, rel_path)

    print("== build")
    page = f"https://github.com/{repo}/releases/latest" if repo else ""
    build_app.build_payload(notes=a.notes, download_page=page)
    build_app.build_windows()
    build_app.build_mac()
    missing = [f for f in ASSETS if not (DIST / f).is_file()]
    if missing:
        sys.exit(f"build did not produce: {missing}")

    if a.dry_run:
        print(f"\nDry run: v{a.version} is built in dist/ and NOT published.")
        return

    gh = gh_exe()
    if not gh:
        sys.exit("GitHub CLI not found. Install it, run `gh auth login`, then release again.")
    print("== publish")
    sh(["git", "add", "-A"])
    sh(["git", "commit", "-m", f"Release v{a.version}\n\n{a.notes}"], check=False)
    sh(["git", "tag", f"v{a.version}"])
    sh(["git", "push", "origin", "HEAD", "--tags"])
    sh([gh, "release", "create", f"v{a.version}", *[str(DIST / f) for f in ASSETS],
        "--title", f"v{a.version}", "--notes", a.notes, "--latest"])
    print(f"\nReleased v{a.version}. Every running copy picks it up the next time it starts.")


if __name__ == "__main__":
    main()
