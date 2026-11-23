"""Run watch.py every 2 minutes for N minutes, committing state.json changes.

Cross-platform (Linux GitHub runners and a Windows self-hosted runner).
Usage: python loop.py 55
"""
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
INTERVAL = 120
BLOCKED_EXIT = 3


def git(*args: str) -> int:
    return subprocess.run(["git", *args], cwd=HERE).returncode


def commit_state() -> None:
    changed = subprocess.run(
        ["git", "status", "--porcelain", "state.json"], cwd=HERE,
        capture_output=True, text=True,
    ).stdout.strip()
    if not changed:
        return
    git("add", "state.json")
    git("commit", "-q", "-m", "Update watch state")
    if git("pull", "-q", "--rebase") != 0 or git("push", "-q") != 0:
        print("state push failed (will retry next check)")


def main() -> None:
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 55
    max_blocked = int(sys.argv[2]) if len(sys.argv) > 2 else 2  # 0 = never give up
    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    end = time.monotonic() + minutes * 60
    blocked = 0
    while True:
        t0 = time.monotonic()
        print(f"::group::Check at {time.strftime('%H:%M:%S')}", flush=True)
        rc = subprocess.run([sys.executable, str(HERE / "watch.py")], cwd=HERE).returncode
        print("::endgroup::", flush=True)
        commit_state()
        blocked = blocked + 1 if rc == BLOCKED_EXIT else 0
        if max_blocked and blocked >= max_blocked:
            print("District blocked this runner repeatedly; ending so the next job gets a new IP.")
            break
        wait = max(1, INTERVAL - (time.monotonic() - t0))
        if time.monotonic() + wait >= end:
            break
        time.sleep(wait)


if __name__ == "__main__":
    main()
