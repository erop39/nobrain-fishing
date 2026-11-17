"""Console-free launcher using the existing Nobrain virtual environment."""
import ctypes
import importlib.util
import subprocess
import sys
from pathlib import Path

base = Path(__file__).resolve().parent
local_python = base / '.venv' / 'Scripts' / 'pythonw.exe'
try:
    if local_python.is_file() and Path(sys.executable).resolve() != local_python.resolve():
        logs = base / 'logs'
        logs.mkdir(exist_ok=True)
        with (logs / 'modern-launcher.log').open('a', encoding='utf-8') as log:
            subprocess.Popen([str(local_python), str(base / 'modern_desktop.py')], cwd=base,
                             stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        missing = [m for m in ('numpy', 'scipy', 'psutil', 'soundcard') if importlib.util.find_spec(m) is None]
        if missing:
            raise RuntimeError('Не установлены: ' + ', '.join(missing) + '\nЗапусти start-modern.bat для подготовки окружения.')
        from modern_desktop import main
        main()
except Exception as exc:
    ctypes.windll.user32.MessageBoxW(None, str(exc), 'Nobrain Companion · запуск', 0x10)

