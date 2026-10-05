"""One-click launcher: creates the virtualenv, installs dependencies, starts ATLAS and opens the browser.

Usage:  python run.py            # install if needed, run, open browser
        python run.py --test     # install if needed, run the test suite
        python run.py --reinstall --port 8080 --no-browser

Only uses the standard library, so it works with a bare Python 3.11+ install.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import socket
import subprocess
import sys
import time
import urllib.request
import venv
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
MARKER = VENV / ".atlas-installed"
WINDOWS = os.name == "nt"
VENV_PY = VENV / ("Scripts/python.exe" if WINDOWS else "bin/python")


def say(msg: str) -> None:
    print(f"\033[1;34m[atlas]\033[0m {msg}" if sys.stdout.isatty() and not WINDOWS else f"[atlas] {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"\n[atlas] ERROR: {msg}\n", flush=True)
    sys.exit(1)


def deps_fingerprint() -> str:
    return hashlib.sha256((ROOT / "pyproject.toml").read_bytes()).hexdigest()


def ensure_env(reinstall: bool) -> None:
    if sys.version_info < (3, 11):  # noqa: UP036 - runs on whatever Python the user has
        fail(f"Python 3.11+ is required, found {sys.version.split()[0]}. Install it from https://www.python.org/downloads/")

    if reinstall and VENV.exists():
        import shutil

        say("Removing previous environment…")
        shutil.rmtree(VENV)

    if not VENV_PY.exists():
        say(f"Creating virtual environment with Python {sys.version.split()[0]}…")
        venv.create(VENV, with_pip=True)

    if MARKER.exists() and MARKER.read_text() == deps_fingerprint():
        say("Dependencies already installed.")
        return

    say("Installing dependencies (first run only, ~1 minute)…")
    pip = [str(VENV_PY), "-m", "pip", "--disable-pip-version-check"]
    subprocess.run([*pip, "install", "-q", "--upgrade", "pip"], cwd=ROOT, check=False)
    result = subprocess.run([*pip, "install", "-q", "-e", ".[ai,dev]"], cwd=ROOT)
    if result.returncode != 0:
        fail("Dependency installation failed. Check your internet connection and run again "
             "(or run with --reinstall).")
    MARKER.write_text(deps_fingerprint())
    say("Dependencies installed.")


def free_port(preferred: int) -> int:
    for port in range(preferred, preferred + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    fail(f"No free port found between {preferred} and {preferred + 49}.")
    return preferred


def wait_until_ready(url: str, proc: subprocess.Popen, timeout: float = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=1):
                return True
        except OSError:
            time.sleep(0.4)
    return False


def serve(port: int, open_browser: bool) -> None:
    port = free_port(port)
    url = f"http://localhost:{port}"
    say(f"Starting ATLAS ONE on {url} …")
    proc = subprocess.Popen(
        [str(VENV_PY), "-m", "uvicorn", "atlas.api:app", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT,
    )
    try:
        if not wait_until_ready(url, proc):
            fail("The server did not start. See the messages above.")
        say(f"Ready → {url}   (API docs: {url}/docs)")
        say("Press Ctrl+C or close this window to stop.")
        if open_browser:
            webbrowser.open(url)
        proc.wait()
    except KeyboardInterrupt:
        say("Stopping…")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def main() -> None:
    parser = argparse.ArgumentParser(description="ATLAS ONE launcher")
    parser.add_argument("--port", type=int, default=int(os.getenv("ATLAS_PORT", "8000")))
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    parser.add_argument("--test", action="store_true", help="run the test suite instead of the server")
    parser.add_argument("--reinstall", action="store_true", help="recreate the virtual environment")
    args = parser.parse_args()

    os.chdir(ROOT)
    ensure_env(args.reinstall)
    if args.test:
        sys.exit(subprocess.run([str(VENV_PY), "-m", "pytest"], cwd=ROOT).returncode)
    serve(args.port, not args.no_browser)


if __name__ == "__main__":
    main()
