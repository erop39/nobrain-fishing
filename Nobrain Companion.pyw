# Nobrain Companion -- console-free Windows launcher
# Double-click this file (Python .pyw association), not start.bat.
from __future__ import annotations

import os
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOG_DIR = BASE / "logs"
LOG_FILE = LOG_DIR / "launcher.log"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

def log(message: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as file:
        file.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")

def run_setup(args: list[str]) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as file:
        result = subprocess.run(
            args, cwd=BASE, stdout=file, stderr=subprocess.STDOUT,
            creationflags=NO_WINDOW, check=False
        )
    if result.returncode:
        raise RuntimeError(
            f"Setup exited with code {result.returncode}: {args!r}"
        )

def notify_error(message: str) -> None:
    log("ERROR: " + message)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, message + "\n\nDetails: " + str(LOG_FILE),
            "Nobrain Companion - Startup error", 0x10
        )
    except Exception:
        pass

def main() -> None:
    app = BASE / "remote_fishing.py"
    if not app.is_file():
        raise FileNotFoundError(
            "remote_fishing.py was not found. "
            "Copy the complete Nobrain Companion source project to this folder."
        )

    venv = BASE / ".venv"
    python = venv / "Scripts" / "python.exe"
    pythonw = venv / "Scripts" / "pythonw.exe"

    if not python.is_file() or not pythonw.is_file():
        log("Creating isolated Python environment...")
        run_setup([sys.executable, "-m", "venv", str(venv)])
        requirements = BASE / "requirements.txt"
        if requirements.exists():
            log("Installing core dependencies...")
            run_setup([str(python), "-m", "pip", "install", "-r", str(requirements)])

    log("Starting GUI without console...")
    with LOG_FILE.open("a", encoding="utf-8") as output:
        subprocess.Popen(
            [str(pythonw), str(app)],
            cwd=str(BASE), stdout=output, stderr=subprocess.STDOUT,
            creationflags=NO_WINDOW, close_fds=True
        )

if __name__ == "__main__":
    try:
        main()
    except Exception:
        notify_error(traceback.format_exc())
