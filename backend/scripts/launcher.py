"""Start and stop one EduNexus laptop installation. Called by Start_EduNexus.bat and Stop_EduNexus.bat.

start  prepares the installation (dependencies only when requirements changed, a private .env with a
       newly generated secret, and the demo database only when no database exists yet), then runs the
       API and the built web app on the first free port from 8001 to 8010 and opens the browser.
stop   stops the EduNexus process recorded by start.

Nothing here deletes or resets data.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backup_restore import PORT_RANGE, package_root  # noqa: E402

ROOT = package_root()
BACKEND = ROOT / "backend"
DATA = BACKEND / "data"
SEED = BACKEND / "seed"
LOGS = ROOT / "logs"
RUNTIME = ROOT / "runtime"
PID_FILE = RUNTIME / "edunexus.pid"
REQUIREMENTS = BACKEND / "requirements.txt"
DEPS_STAMP = BACKEND / ".venv" / ".requirements.sha256"
ENV_FILE = BACKEND / ".env"
ENV_TEMPLATE = BACKEND / ".env.example"
PLACEHOLDER_SECRETS = {"", "dev-only-change-me", "replace-with-a-long-random-string", "change-me", "secret"}


def _sha256_text(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _log(message: str) -> None:
    print(message, flush=True)


def ensure_dependencies() -> None:
    wanted = _sha256_text(REQUIREMENTS)
    if DEPS_STAMP.is_file() and DEPS_STAMP.read_text(encoding="utf-8").strip() == wanted:
        return
    _log("Installing EduNexus components. The first start, or the first start after an update, takes a few minutes.")
    LOGS.mkdir(parents=True, exist_ok=True)
    with (LOGS / "install.log").open("a", encoding="utf-8") as log:
        log.write(f"\n=== install {datetime.now().isoformat(timespec='seconds')} ===\n")
        log.flush()
        for command in (
            [sys.executable, "-m", "pip", "install", "--upgrade", "pip"],
            [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)],
        ):
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, cwd=BACKEND)
            if result.returncode != 0:
                _log(f"Installation failed. Check logs\\install.log for details, then start EduNexus again.")
                raise SystemExit(1)
    DEPS_STAMP.parent.mkdir(parents=True, exist_ok=True)
    DEPS_STAMP.write_text(wanted, encoding="utf-8")


def _set_line(text: str, key: str, value: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[index] = f"{key}={value}"
            return "\n".join(lines) + "\n"
    return "\n".join(lines + [f"{key}={value}"]) + "\n"


def ensure_configuration() -> None:
    """Create backend/.env on first start, or fill in a missing JWT secret. Existing values are kept."""
    if not ENV_FILE.is_file():
        text = ENV_TEMPLATE.read_text(encoding="utf-8")
        text = _set_line(text, "SEED_ON_START", "false")
        text = _set_line(text, "SEED_DEMO_ACCOUNTS", "false")
        text = _set_line(text, "APP_ENV", "development")
        text = _set_line(text, "DATABASE_URL", "sqlite:///./data/edunexus.db")
        text = _set_line(text, "UPLOAD_DIR", "./data/uploads")
        text = _set_line(text, "JWT_SECRET", secrets.token_urlsafe(48))
        ENV_FILE.write_text(text, encoding="utf-8")
        _log("Created this laptop's private settings file with a new security key.")
        return
    current = ENV_FILE.read_text(encoding="utf-8")
    secret = next((line.split("=", 1)[1].strip() for line in current.splitlines() if line.startswith("JWT_SECRET=")), "")
    if len(secret) < 32 or secret in PLACEHOLDER_SECRETS:
        ENV_FILE.write_text(_set_line(current, "JWT_SECRET", secrets.token_urlsafe(48)), encoding="utf-8")
        _log("Generated a new security key for this laptop.")


def ensure_database() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    uploads = DATA / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    database = DATA / "edunexus.db"
    if database.exists():
        return
    seed_database = SEED / "edunexus-demo.db"
    if not seed_database.is_file():
        _log("No database exists and this package has no demo data. EduNexus will create an empty database.")
        return
    shutil.copy2(seed_database, database)
    seed_uploads = SEED / "uploads"
    if seed_uploads.is_dir():
        shutil.copytree(seed_uploads, uploads, dirs_exist_ok=True)
    _log("Created this laptop's database from the demo data. Your data is stored only on this laptop from now on.")


def _process_alive(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _health(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def _recorded_run() -> int | None:
    if not PID_FILE.is_file():
        return None
    try:
        record = json.loads(PID_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        PID_FILE.unlink(missing_ok=True)
        return None
    if _process_alive(int(record["pid"])) and _health(int(record["port"])):
        return int(record["port"])
    PID_FILE.unlink(missing_ok=True)
    return None


def find_free_port() -> int:
    for port in PORT_RANGE:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise SystemExit("Ports 8001 to 8010 are all in use. Close other applications, or restart the computer, then start EduNexus again.")


def _wait_ready(port: int, process: subprocess.Popen, timeout: int = 120) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        if _health(port):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as response:
                    if response.status == 200 and b'id="root"' in response.read():
                        return True
            except Exception:
                pass
        time.sleep(1)
    return False


def _tail(path: Path, lines: int = 15) -> str:
    if not path.is_file():
        return ""
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def start(open_browser: bool = True) -> int:
    for folder in (LOGS, RUNTIME):
        folder.mkdir(parents=True, exist_ok=True)
    ensure_dependencies()
    ensure_configuration()
    ensure_database()

    running = _recorded_run()
    if running:
        url = f"http://127.0.0.1:{running}"
        _log(f"EduNexus is already running at {url}")
        if open_browser:
            webbrowser.open(url)
        return 0

    port = find_free_port()
    if port != PORT_RANGE[0]:
        _log(f"Port {PORT_RANGE[0]} is in use by another program, so EduNexus will use port {port}.")
    env = os.environ.copy()
    env["APP_URL"] = f"http://127.0.0.1:{port}"
    env["CORS_ORIGINS"] = f"http://127.0.0.1:{port},http://localhost:{port}"
    env["PYTHONUNBUFFERED"] = "1"
    log_path = LOGS / "backend.log"
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n=== EduNexus start {datetime.now().isoformat(timespec='seconds')} port {port} ===\n")
        log.flush()
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=BACKEND,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    PID_FILE.write_text(json.dumps({"pid": process.pid, "port": port}), encoding="utf-8")
    try:
        _log("Starting EduNexus. Please wait...")
        if not _wait_ready(port, process):
            _log("EduNexus did not start. The last lines of logs\\backend.log are:")
            _log(_tail(log_path))
            return 1
        url = f"http://127.0.0.1:{port}"
        _log("")
        _log(f"EduNexus is running: {url}")
        _log("Keep this window open while you use EduNexus. Close it, or press Ctrl+C, to stop.")
        _log("Your data is saved on this laptop. Use Backup_EduNexus.bat to make a backup.")
        if open_browser:
            webbrowser.open(url)
        return process.wait()
    except KeyboardInterrupt:
        _log("Stopping EduNexus...")
        return 0
    finally:
        if process.poll() is None:
            _stop_process(process.pid)
        if PID_FILE.is_file() and json.loads(PID_FILE.read_text(encoding="utf-8")).get("pid") == process.pid:
            PID_FILE.unlink(missing_ok=True)


def _stop_process(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/PID", str(pid)], capture_output=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and _process_alive(pid):
            time.sleep(0.5)
        if _process_alive(pid):
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
    else:
        os.kill(pid, 15)


def stop() -> int:
    if not PID_FILE.is_file():
        _log("EduNexus is not running.")
        return 0
    record = json.loads(PID_FILE.read_text(encoding="utf-8"))
    pid = int(record["pid"])
    if _process_alive(pid):
        _log("Stopping EduNexus safely...")
        _stop_process(pid)
    PID_FILE.unlink(missing_ok=True)
    _log("EduNexus has stopped. Your data is saved.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else "start"
    if command == "start":
        return start(open_browser=os.environ.get("EDUNEXUS_NO_BROWSER") != "1")
    if command == "stop":
        return stop()
    _log("Usage: launcher.py start | stop")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
