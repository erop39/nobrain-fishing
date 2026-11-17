"""Local desktop web shell; all fishing operations stay in remote_fishing.App.

Run with --preview to inspect the interface without audio or input automation.
No runtime network resources and no additional Python dependencies are required.
"""
from __future__ import annotations
import argparse
import copy
import importlib.util
import json
import math
import mimetypes
import os
from pathlib import Path
import queue
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
WEB = ROOT / 'desktop_web'
DEFAULTS = dict(automation=False, working_mode='Удалённо', close_behavior='tray',
    wow_process='Автоопределение WoW', audio_name='System default', sample_file='',
    threshold=.65, timeout=30, early_bite_filter=False, min_bite_seconds=0,
    no_splash_stop=False, no_splash_seconds=300, manual_space_reset=True,
    bite_delay_min=.10, bite_delay_max=.25, recast_delay_min=1., recast_delay_max=2.,
    key_method='ScanCode', remote_client='Auto', auto_focus_on_start=True,
    detection_mode='Звук', visual_threshold=.22, visual_auto_find=False,
    audio_diagnostics=True, audio_log_interval=3.)
MODES = ['Звук', 'Пиксели (эксперимент)', 'Звук ИЛИ пиксели', 'Звук + свой поплавок (строго)']
NUMBERS = dict(threshold=(.05, 1), timeout=(3, 120), min_bite_seconds=(0, 28), no_splash_seconds=(10, 86400),
    bite_delay_min=(0, 2), bite_delay_max=(0, 2), recast_delay_min=(0, 8),
    recast_delay_max=(0, 8), visual_threshold=(.05, .95), audio_log_interval=(1, 30))
CHOICES = dict(working_mode=['Удалённо', 'Локально'], close_behavior=['tray', 'exit'],
    key_method=['ScanCode', 'VirtualKey', 'Legacy'], remote_client=['Auto', 'RustDesk', 'AnyDesk', 'TeamViewer'],
    detection_mode=MODES)
PACKAGES = [('numpy', 'NumPy', 'Обработка аудиоданных'), ('scipy', 'SciPy', 'Сопоставление звуков'),
    ('soundcard', 'SoundCard', 'Захват звука Windows'), ('psutil', 'psutil', 'Поиск процесса WoW'),
    ('PIL', 'Pillow', 'Распознавание по изображению'), ('pystray', 'pystray', 'Системный трей'),
    ('cv2', 'OpenCV', 'Поиск поплавка по фотографии')]

def validate_settings(settings, current, devices, samples, processes):
    if not isinstance(settings, dict) or set(settings) - set(DEFAULTS):
        raise ValueError('Неизвестные параметры настроек.')
    merged = dict(current, **settings)
    for key, value in merged.items():
        if isinstance(DEFAULTS[key], bool):
            if not isinstance(value, bool): raise ValueError(f'{key}: нужен переключатель.')
        elif key in NUMBERS:
            lo, hi = NUMBERS[key]
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not lo <= value <= hi:
                raise ValueError(f'{key}: допустимое значение от {lo} до {hi}.')
            if isinstance(DEFAULTS[key], int) and int(value) != value:
                raise ValueError(f'{key}: укажи целое число.')
        elif not isinstance(value, str) or len(value) > 1000:
            raise ValueError(f'{key}: неверное значение.')
        if key in CHOICES and value not in CHOICES[key]: raise ValueError(f'{key}: неизвестный вариант.')
    for prefix in ('bite_delay', 'recast_delay'):
        if merged[prefix+'_min'] > merged[prefix+'_max']: raise ValueError('Минимальная задержка должна быть не больше максимальной.')
    if merged['early_bite_filter'] and merged['min_bite_seconds'] >= merged['timeout']:
        raise ValueError('Фильтр ранних поклёвок должен быть короче заброса.')
    for key, values in [('audio_name', devices), ('sample_file', [''] + samples), ('wow_process', processes)]:
        if merged[key] not in values and merged[key] != current[key]: raise ValueError(f'{key}: обнови список доступных вариантов.')
    return merged

class Service:
    def __init__(self, preview=False):
        self.preview = preview
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.pending = queue.Queue()
        self.engine = None
        self.last_seen = time.monotonic()
        self.seen = False
        self.messages = []
        self.errors = []
        self.closed = False
        self.state = dict(preview=preview, settings=DEFAULTS.copy(), devices=['System default'], samples=[],
            processes=['Автоопределение WoW'], stats=dict(casts=0, bites=0, catches=0), running=False,
            paused=False, recording=False, status='Ready', score='', logs=[], components=[],
            detection_modes=MODES, visual_region_description='Область ещё не выбрана',
            visual_photo_status='Фотография не выбрана', sample_description='', messages=[])
        self.refresh_packages()

    def refresh_packages(self):
        result = []
        for module, name, description in PACKAGES:
            try: available = importlib.util.find_spec(module) is not None
            except (ImportError, ValueError): available = False
            result.append(dict(name=name, description=description, available=available))
        self.state['components'] = result

    def snapshot(self):
        with self.lock: return copy.deepcopy(self.state)

    def message(self, kind, title, body, **kwargs):
        item = dict(id=secrets.token_hex(6), kind=kind, title=str(title), body=str(body))
        self.messages.append(item)
        self.messages = self.messages[-20:]
        if kind in ('error', 'warning'): self.errors.append(str(body))
        return 'ok'

    def update(self):
        e = self.engine
        if e is None: return
        state = dict(preview=False, settings={k: getattr(e, k).get() for k in DEFAULTS},
            devices=list(e.devices.cget('values')), samples=list(e.sample_files),
            processes=list(e.wow_combo.cget('values')), stats=dict(casts=e.session_casts,
            bites=e.session_bites, catches=e.estimated_catches), running=e.running,
            paused=e.paused.is_set(), recording=e.recording, status=e.state.get(), score=e.score.get(),
            logs=e.log_rows[-500:], components=self.state['components'],
            detection_modes=MODES if e.pillow_available else ['Звук'],
            visual_region_description=e.visual_region_description.get(),
            visual_photo_status=e.visual_photo_status.get(), sample_description=e.sample_label.cget('text'),
            messages=list(self.messages))
        with self.lock: self.state = state

    def dispatch(self, payload):
        action = payload.get('action')
        if action == 'settings':
            if self.state['running'] or self.state['recording']: raise ValueError('Сначала останови сессию или запись.')
            values = validate_settings(payload.get('settings'), self.state['settings'],
                self.state['devices'], self.state['samples'], self.state['processes'])
            if self.preview:
                self.state['settings'] = values
            else:
                old = self.state['settings'].copy()
                try:
                    for key, value in values.items(): getattr(self.engine, key).set(value)
                    if values['sample_file'] != old['sample_file']: self.engine.on_sample_selected()
                    if values['working_mode'] != old['working_mode']: self.engine.on_mode_changed()
                    if self.errors: raise ValueError(self.errors[-1])
                    self.engine.save_settings()
                    if self.errors: raise ValueError(self.errors[-1])
                except Exception:
                    for key, value in old.items(): getattr(self.engine, key).set(value)
                    raise
        elif self.preview:
            if action in ('save', 'refresh_devices', 'refresh_samples', 'refresh_wow_processes'): pass
            elif action == 'check_components': self.refresh_packages()
            elif action == 'reset_stats_confirmed': self.state['stats'] = dict(casts=0, bites=0, catches=0)
            elif action in ('adjust_minus', 'adjust_plus'):
                self.state['stats']['catches'] = max(0, self.state['stats']['catches'] + (1 if action == 'adjust_plus' else -1))
            else: raise ValueError('Это предпросмотр. Для работы с игрой запусти NobrainModern.exe.')
        else:
            commands = dict(start=self.engine.start, pause=self.engine.toggle_pause, stop=self.engine.halt,
                save=self.engine.save_settings, refresh_devices=self.engine.refresh_devices,
                refresh_samples=self.engine.refresh_samples, refresh_wow_processes=self.engine.refresh_wow_processes,
                load_sample=self.engine.load_sample, record=self.engine.record, capture_recent=self.engine.capture_recent,
                choose_visual_region=self.engine.choose_visual_region, choose_bobber_photo=self.engine.choose_bobber_photo,
                silent_audio_setup=self.engine.silent_audio_setup, test_space=self.engine.test_space,
                check_components=self.engine.check_components, install_optional_components=self.engine.install_optional_components,
                open_log_folder=self.engine.open_log_folder, reset_stats_confirmed=self.engine.reset_stats,
                adjust_minus=lambda: self.engine.adjust_catches(-1), adjust_plus=lambda: self.engine.adjust_catches(1))
            if action not in commands: raise ValueError('Неизвестная команда.')
            commands[action]()
            if action == 'check_components': self.refresh_packages()
        self.update()
        if self.errors: raise ValueError(self.errors[-1])
        return self.snapshot()

    def execute(self, payload):
        if self.preview:
            self.errors.clear()
            return self.dispatch(payload)
        event = threading.Event()
        job = dict(payload=payload, event=event)
        self.pending.put(job)
        if not event.wait(175): raise ValueError('Операция ещё не завершена. Закрой открытый диалог и повтори действие.')
        if 'error' in job: raise ValueError(job['error'])
        return job['result']

    def pump(self):
        if self.closed: return
        try:
            while True:
                job = self.pending.get_nowait()
                self.errors.clear()
                try: job['result'] = self.dispatch(job['payload'])
                except Exception as exc: job['error'] = str(exc)
                finally: job['event'].set()
        except queue.Empty: pass
        self.update()
        # Chromium throttles background timers. Allow ample time before treating
        # a disappeared window as closed; the tray continues to own the process.
        if self.seen and time.monotonic() - self.last_seen > 120:
            if self.engine.close_behavior.get() != 'tray' or not self.engine.tray_ready:
                self.engine.close()
                return
        self.engine.root.after(100, self.pump)

def make_server(service, port=0):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass

        def permitted(self, auth=True):
            host = f'127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') != host: return False
            if self.headers.get('Origin') not in (None, f'http://{host}'): return False
            if self.headers.get('Sec-Fetch-Site') == 'cross-site': return False
            return not auth or secrets.compare_digest(self.headers.get('X-Nobrain-Token', ''), service.token)

        def reply(self, code, content, mime='application/json; charset=utf-8'):
            if isinstance(content, (dict, list)): content = json.dumps(content, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            try: self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError): pass

        def do_GET(self):
            path = self.path.split('?', 1)[0]
            if not self.permitted(auth=path.startswith('/api/') and path != '/api/bootstrap'):
                return self.reply(403, dict(error='Доступ запрещён.'))
            if path == '/api/bootstrap': return self.reply(200, dict(token=service.token))
            if path == '/api/state':
                service.seen = True
                service.last_seen = time.monotonic()
                return self.reply(200, service.snapshot())
            allowed = {'/': 'index.html', '/index.html': 'index.html', '/style.css': 'style.css', '/app.js': 'app.js', '/mark.svg': 'mark.svg'}
            if path not in allowed: return self.reply(404, dict(error='Не найдено.'))
            target = WEB / allowed[path]
            mime = {'.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
                    '.js': 'application/javascript; charset=utf-8', '.svg': 'image/svg+xml'}
            return self.reply(200, target.read_bytes(), mime[target.suffix])

        def do_POST(self):
            if not self.permitted(): return self.reply(403, dict(error='Доступ запрещён.'))
            if self.path != '/api/command': return self.reply(404, dict(error='Не найдено.'))
            try:
                size = int(self.headers.get('Content-Length', 0))
                if not 0 < size <= 32768: raise ValueError('Неверный размер команды.')
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict): raise ValueError('Неверная команда.')
                return self.reply(200, service.execute(payload))
            except (ValueError, TypeError, KeyError) as exc: return self.reply(400, dict(error=str(exc)))
            except Exception as exc: return self.reply(500, dict(error=str(exc)))
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server

def open_window(url):
    if sys.platform == 'win32':
        candidates = [Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe',
                      Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Microsoft/Edge/Application/msedge.exe',
                      Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Google/Chrome/Application/chrome.exe']
        for browser in candidates:
            if browser.is_file():
                subprocess.Popen([str(browser), f'--app={url}', '--window-size=1180,850'],
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                return
    webbrowser.open(url)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--preview', action='store_true')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--self-check', metavar='REPORT_JSON', help='Check bundled UI and engine without starting fishing')
    args = parser.parse_args()
    if args.self_check:
        import tempfile
        from types import SimpleNamespace
        import tkinter as tk
        import remote_fishing as core
        report = {'ok': False, 'frozen': bool(getattr(sys, 'frozen', False))}
        try:
            with tempfile.TemporaryDirectory() as temporary:
                core.CONFIG_PATH = Path(temporary) / 'settings.json'
                core.GlobalHotkeys = lambda events: SimpleNamespace(close=lambda: None)
                class CheckApp(core.App):
                    pillow_available = core.PIL_AVAILABLE
                    def setup_tray(self): pass
                root = tk.Tk()
                root.withdraw()
                try:
                    app = CheckApp(root)
                    root.withdraw()
                    check = Service()
                    check.engine = app
                    check.update()
                    for filename in ('index.html', 'style.css', 'app.js', 'mark.svg'):
                        assert (WEB / filename).stat().st_size > 0, filename
                    assert app.sample is not None, 'No WAV sample loaded'
                    assert not app.running and not app.recording
                    report.update(ok=True, sample_loaded=True, web_assets=True,
                                  components=check.state['components'], fishing_started=False)
                finally:
                    root.destroy()
        except Exception as exc:
            report['error'] = str(exc)
        Path(args.self_check).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        return 0 if report['ok'] else 1
    service = Service(args.preview)
    server = make_server(service, args.port)
    url = f'http://127.0.0.1:{server.server_port}'
    if args.preview:
        print('Preview: ' + url, flush=True)
        if not args.no_browser: open_window(url)
        try: server.serve_forever()
        except KeyboardInterrupt: pass
        finally: server.server_close()
        return
    import tkinter as tk
    from tkinter import messagebox
    import remote_fishing as engine
    root = tk.Tk()
    root.withdraw()
    # Route informational dialogs into the modern window. Installation approval
    # and file/region selection retain the original native dialog behavior.
    for method, kind in [('showinfo', 'info'), ('showwarning', 'warning'), ('showerror', 'error')]:
        setattr(messagebox, method, lambda title='', message='', _kind=kind, **kw: service.message(_kind, title, message, **kw))
    class DesktopApp(engine.App):
        pillow_available = engine.PIL_AVAILABLE
        def show_from_tray(self): open_window(url)
        def close(self):
            service.closed = True
            threading.Thread(target=server.shutdown, daemon=True).start()
            super().close()
    app = DesktopApp(root)
    root.withdraw()
    root.deiconify = lambda: None  # Region selection must never reveal the legacy UI.
    service.engine = app
    service.update()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    service.pump()
    if not args.no_browser: open_window(url)
    try: root.mainloop()
    finally: server.server_close()

if __name__ == '__main__':
    sys.exit(main())

