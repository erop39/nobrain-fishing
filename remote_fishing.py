"""Nobrain Companion — sound and pixel-based fishing assistant for Windows."""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import importlib.util
import subprocess
import sys
import re
from collections import deque
import json
import shutil
import queue
import random
import threading
import time
import wave
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from datetime import datetime
import traceback
from contextlib import nullcontext
try:
    from PIL import Image, ImageGrab, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    Image = ImageGrab = ImageTk = None
    PIL_AVAILABLE = False

# Tray backend also depends on Pillow on Windows; load it only if available.
try:
    if PIL_AVAILABLE:
        import pystray
    else:
        pystray = None
except ImportError:
    pystray = None

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    cv2 = None
    CV2_AVAILABLE = False

import psutil

import numpy as np
from scipy.signal import resample_poly, stft

SAMPLE_RATE = 22050
HOP = 256
FFT = 1024
REMOTE_TITLE_TOKENS = {'RustDesk': ('rustdesk',), 'AnyDesk': ('anydesk',), 'TeamViewer': ('teamviewer', 'team viewer')}
WOW_EXECUTABLES = {'wow.exe', 'wowclassic.exe', 'wowclassict.exe', 'wowclassicera.exe', 'wow-64.exe'}
REMOTE_CHOICES = ('Auto', 'RustDesk', 'AnyDesk', 'TeamViewer')
VERSION = '1.6.1'
# In a PyInstaller onedir build, frozen assets live in _internal, while
# user settings and logs must remain writable beside NobrainCompanion.exe.
FROZEN = bool(getattr(sys, 'frozen', False))
RESOURCE_ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
if FROZEN:
    for rel in ('bite_sample.wav', 'samples/bite.wav', 'assets/nobrain.ico', 'assets/nobrain.png'):
        target = ROOT / rel
        source = RESOURCE_ROOT / rel
        if source.is_file() and not target.exists():
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            except OSError:
                # Assets can still be loaded from the bundled directory.
                pass
CONFIG_PATH = ROOT / 'settings.json'
SAMPLE_PATH = ROOT / 'bite_sample.wav'
SAMPLES_DIR = ROOT / 'samples'
SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = ROOT / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / ('remote-fishing-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.log')


def features(samples: np.ndarray) -> np.ndarray:
    """Robust normalized frequency-band spectral signature (time x bands)."""
    if len(samples) < FFT:
        return np.empty((0, 16), np.float32)
    _, _, z = stft(samples, fs=SAMPLE_RATE, nperseg=FFT, noverlap=FFT-HOP,
                   boundary=None, padded=False)
    power = np.abs(z)
    freqs = np.fft.rfftfreq(FFT, 1/SAMPLE_RATE)
    edges = np.geomspace(130, 8000, 17)
    bands = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (freqs >= lo) & (freqs < hi)
        bands.append(np.mean(power[mask], axis=0))
    spec = np.log1p(1000 * np.array(bands).T)
    # Remove volume variations introduced by remote audio encoding.
    spec -= spec.mean(axis=1, keepdims=True)
    norms = np.linalg.norm(spec, axis=1, keepdims=True)
    return (spec / np.maximum(norms, 1e-6)).astype(np.float32)


def sliding_splash_similarity(audio: np.ndarray, template: np.ndarray) -> tuple[float, float]:
    """Find the best position of the WAV template in a longer live-audio buffer.

    Returns the best cosine-like feature score and the age (seconds) of the
    matched segment relative to the end of the rolling buffer.
    """
    obs = features(audio)
    n = len(template)
    if len(obs) < n:
        return -1.0, 0.0
    # windows: [position, frequency-band, template-frame]
    windows = np.lib.stride_tricks.sliding_window_view(obs, n, axis=0)
    scores = np.einsum('pbf,fb->p', windows, template, optimize=True) / n
    best = int(np.argmax(scores))
    # Avoid stale detections: consider only events finishing in the last 0.5s.
    # Older matches would otherwise trigger repeatedly until they leave buffer.
    age = (len(obs) - (best + n)) * HOP / SAMPLE_RATE
    return float(scores[best]), float(age)


def auto_trim_splash(signal: np.ndarray) -> tuple[np.ndarray, float]:
    """Keep a ~0.9s energetic transient, not several seconds of trailing ambience.

    A short lead-in before the energy maximum makes timing less sensitive
    to the exact instant a sample was cropped in an audio editor.
    """
    signal = np.asarray(signal, dtype=np.float32)
    duration = len(signal) / SAMPLE_RATE
    if duration <= 1.05:
        return signal, 0.0
    # Find energetic 0.80s section at 10ms resolution.
    energy_width = int(0.80 * SAMPLE_RATE)
    step = max(1, int(0.01 * SAMPLE_RATE))
    squared = np.square(signal.astype(np.float64))
    cs = np.r_[0, np.cumsum(squared)]
    positions = np.arange(0, len(signal) - energy_width + 1, step)
    energy = cs[positions + energy_width] - cs[positions]
    start = max(0, int(positions[int(np.argmax(energy))]) - int(0.12 * SAMPLE_RATE))
    clip_len = int(0.95 * SAMPLE_RATE)
    start = min(start, len(signal) - clip_len)
    return signal[start:start+clip_len].copy(), start/SAMPLE_RATE


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), 'rb') as wav:
        rate = wav.getframerate()
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        raw = wav.readframes(wav.getnframes())
    if width != 2:
        raise ValueError('Expected 16-bit PCM WAV.')
    signal = np.frombuffer(raw, '<i2').astype(np.float32) / 32768
    if channels > 1:
        signal = signal.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE:
        from math import gcd
        d = gcd(rate, SAMPLE_RATE)
        signal = resample_poly(signal, SAMPLE_RATE//d, rate//d).astype(np.float32)
    return signal


def save_wav(path: Path, signal: np.ndarray):
    clipped = np.clip(signal, -1, 1)
    with wave.open(str(path), 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes((clipped * 32767).astype('<i2').tobytes())




def initialize_com():
    """Initialize Windows COM in the *current* worker thread (WASAPI dependency)."""
    hr = ctypes.windll.ole32.CoInitializeEx(None, 0)
    if hr not in (0, 1):
        raise OSError(f'CoInitializeEx failed: 0x{hr & 0xFFFFFFFF:08X}')
    return True

def selected_speaker(name: str):
    import soundcard as sc
    speakers = sc.all_speakers()
    if name == 'System default':
        return sc.default_speaker()
    for speaker in speakers:
        if speaker.name == name:
            return speaker
    raise RuntimeError(f'Audio device not found: {name}')


def selected_loopback(name: str):
    """Loopback recording device for a Windows playback endpoint (SoundCard API)."""
    import soundcard as sc
    speaker = selected_speaker(name)
    loopback = sc.get_microphone(id=speaker.id, include_loopback=True)
    if loopback is None:
        raise RuntimeError(f'No WASAPI loopback device for {speaker.name}')
    return loopback


def extract_splash_from_recent(data: np.ndarray) -> np.ndarray:
    """Choose the most energetic short transient in the last ~2.5 seconds."""
    chunk = int(.85 * SAMPLE_RATE)
    if len(data) < chunk:
        raise ValueError('Not enough audio collected yet')
    # 40-ms windows and 20-ms search steps, prioritizing transients.
    step = max(1, SAMPLE_RATE // 50)
    windows = np.lib.stride_tricks.sliding_window_view(data, chunk)[::step]
    rms = np.mean(windows ** 2, axis=1)
    if not np.any(rms > 1e-7):
        raise ValueError('Recording is silent. Check RustDesk audio output.')
    index = int(np.argmax(rms)) * step
    return data[index:index+chunk].copy()


def remote_client_from_title(title: str) -> str | None:
    name = title.casefold()
    for client, tokens in REMOTE_TITLE_TOKENS.items():
        if any(token in name for token in tokens):
            return client
    return None


def find_remote_windows(selected: str) -> list[tuple[int, str, str]]:
    """Enumerate visible top-level remote client windows, not just the foreground."""
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    wintypes = ctypes.wintypes
    CALLBACK = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = (CALLBACK, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetWindowTextW.restype = ctypes.c_int
    found = []

    def enum_cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if not n:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
        client = remote_client_from_title(title)
        if client and (selected == 'Auto' or client == selected):
            found.append((int(hwnd), title, client))
        return True

    if not user32.EnumWindows(CALLBACK(enum_cb), 0):
        raise ctypes.WinError(ctypes.get_last_error())
    # Remote sessions are generally more useful than main/settings windows.
    session_tokens = ('remote desktop', 'удалённый', 'session', 'сеанс', ' - ')
    found.sort(key=lambda item: (
        not any(tok in item[1].casefold() for tok in session_tokens),
        item[1].casefold()
    ))
    return found


def find_wow_windows() -> list[tuple[int, int, str, str]]:
    """Visible WoW windows matched by the actual process executable, not just title."""
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    result = []
    def cb(hwnd, unused):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            proc = psutil.Process(pid.value)
            exe = proc.name()
        except (psutil.Error, OSError):
            return True
        if exe.casefold() in WOW_EXECUTABLES or (exe.casefold().startswith('wow') and exe.casefold().endswith('.exe')):
            result.append((int(hwnd), int(pid.value), buf.value, exe))
        return True
    user32.EnumWindows(callback_type(cb), 0)
    return result


def activate_wow_window(pid: int | None = None) -> tuple[bool, str]:
    matches = [x for x in find_wow_windows() if pid is None or x[1] == pid]
    if not matches:
        return False, 'No running visible WoW window matched the selected process'
    user32 = ctypes.windll.user32
    for hwnd, process_id, title, exe in matches:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)
        time.sleep(.12)
        if int(user32.GetForegroundWindow() or 0) == hwnd:
            return True, f'Activated {exe} PID={process_id}: {title!r}'
    return False, 'Windows denied foreground activation; activate WoW manually'


def foreground_is_wow(pid: int | None = None) -> bool:
    user32 = ctypes.windll.user32
    hwnd = int(user32.GetForegroundWindow() or 0)
    return any(x[0] == hwnd and (pid is None or x[1] == pid) for x in find_wow_windows())


def activate_remote_window(selected: str) -> tuple[bool, str]:
    """Foreground a visible client window; never redirect input if activation fails."""
    import ctypes.wintypes  # make wintypes available across Python installations
    matches = find_remote_windows(selected)
    if not matches:
        return False, f'No visible {selected} remote client window found'
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    wt = ctypes.wintypes
    user32.ShowWindow.argtypes = (wt.HWND, ctypes.c_int)
    user32.ShowWindow.restype = wt.BOOL
    user32.IsIconic.argtypes = (wt.HWND,)
    user32.IsIconic.restype = wt.BOOL
    user32.SetForegroundWindow.argtypes = (wt.HWND,)
    user32.SetForegroundWindow.restype = wt.BOOL
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wt.HWND
    for hwnd, title, client in matches:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.12)
        if int(user32.GetForegroundWindow() or 0) == hwnd:
            return True, f'Activated {client}: {title!r}; candidates={len(matches)}'
    return False, f'Windows denied activation of {len(matches)} {selected} window(s); manually click remote session'


def foreground_is_remote(selected: str) -> bool:
    detected = remote_client_from_title(foreground_title())
    return detected is not None if selected == 'Auto' else detected == selected


def foreground_title() -> str:
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value


def send_space(method='ScanCode') -> tuple[int, int]:
    """Send key down/up and return Windows SendInput event counts.

    This only verifies OS injection, not delivery through RustDesk to WoW.
    """
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    if method == 'Legacy':
        user32.keybd_event(0x20, 0, 0, 0)
        time.sleep(0.12)
        user32.keybd_event(0x20, 0, 0x0002, 0)
        return 1, 1  # Legacy API does not report delivery status.

    ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [('dx', ctypes.c_long), ('dy', ctypes.c_long),
                    ('mouseData', ctypes.c_ulong), ('dwFlags', ctypes.c_ulong),
                    ('time', ctypes.c_ulong), ('dwExtraInfo', ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [('wVk', ctypes.c_ushort), ('wScan', ctypes.c_ushort),
                    ('dwFlags', ctypes.c_ulong), ('time', ctypes.c_ulong),
                    ('dwExtraInfo', ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [('uMsg', ctypes.c_ulong), ('wParamL', ctypes.c_ushort),
                    ('wParamH', ctypes.c_ushort)]

    class INPUTUNION(ctypes.Union):
        _fields_ = [('mi', MOUSEINPUT), ('ki', KEYBDINPUT), ('hi', HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [('type', ctypes.c_ulong), ('u', INPUTUNION)]

    user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = ctypes.c_uint
    use_scan = method == 'ScanCode'
    scan_code = 0x39  # Spacebar PC scan code
    def send(up):
        flags = (0x0008 if use_scan else 0) | (0x0002 if up else 0)
        inp = INPUT(type=1, u=INPUTUNION(ki=KEYBDINPUT(
            wVk=0 if use_scan else 0x20,
            wScan=scan_code if use_scan else 0,
            dwFlags=flags, time=0, dwExtraInfo=0)))
        ctypes.set_last_error(0)
        sent = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        if sent != 1:
            raise OSError(ctypes.get_last_error(), 'Windows SendInput returned 0')
        return int(sent)

    down = send(False)
    time.sleep(0.12)
    up = send(True)
    return down, up


def emergency_stop_pressed() -> bool:
    return bool(ctypes.windll.user32.GetAsyncKeyState(0x77) & 0x8000)  # F8



# Global shortcuts: Ctrl+Alt+F6 = Start/Resume, Ctrl+Alt+F7 = Pause/Resume,
# Ctrl+Alt+F8 = Stop. Existing F8 emergency stop remains available.
class GlobalHotkeys:
    def __init__(self, events):
        self.events = events
        self.thread_id = 0
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.ready.wait(2)

    def _run(self):
        user32 = ctypes.windll.user32
        self.thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        # Force creation of a Win32 message queue before registration.
        msg = ctypes.wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        registered = []
        for key_id, vk in ((1,0x75),(2,0x76),(3,0x77)):
            if user32.RegisterHotKey(None, key_id, 0x0002 | 0x0001 | 0x4000, vk):
                registered.append(key_id)
            else:
                self.events.put(('log',f'HOTKEY | Registration failed: Ctrl+Alt+F{key_id+5}'))
        self.ready.set()
        while user32.GetMessageW(ctypes.byref(msg),None,0,0) > 0:
            if msg.message == 0x0312:
                self.events.put(('hotkey',int(msg.wParam)))
        for key_id in registered:
            user32.UnregisterHotKey(None,key_id)

    def close(self):
        if self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id,0x0012,0,0)


def crop_visual_frame(region):
    if not PIL_AVAILABLE:
        raise RuntimeError('Pillow не установлен: режим пикселей недоступен')
    # Screenshot only the region selected on the local screen (the RustDesk viewport).
    im = ImageGrab.grab(bbox=tuple(region), all_screens=False).convert('L')
    # Normalize to fixed dimensions, reducing dependence on DPI/ROI size.
    im = im.resize((96, 96),Image.Resampling.BILINEAR)
    return np.asarray(im,dtype=np.float32)


def visual_change_score(current, reference):
    # Fraction of sufficiently changed pixels, 0..1. A static still photo
    # cannot prove a bite; this measures motion/scene changes in the selected ROI.
    delta = np.abs(current - reference)
    return float(np.mean(delta >= 28.0))

def locate_bobber(template, confidence=0.75):
    """Find reference bobber anywhere on primary display; return (bbox, score).

    A reference photo should include the bobber and little surrounding water.
    The confidence is normalized OpenCV template correlation, not a probability.
    """
    if not (PIL_AVAILABLE and CV2_AVAILABLE):
        return None, 0.0
    screenshot = np.array(ImageGrab.grab(all_screens=False).convert('L'))
    if template is None or screenshot.shape[0] < 32 or screenshot.shape[1] < 32:
        return None, 0.0
    best_score, best_box = -1.0, None
    for scale in (.85, 1.0, 1.15):
        w = round(template.shape[1] * scale)
        h = round(template.shape[0] * scale)
        if w < 12 or h < 12 or w >= screenshot.shape[1] or h >= screenshot.shape[0]:
            continue
        target = cv2.resize(template, (w, h), interpolation=cv2.INTER_AREA)
        # Flat samples cannot be matched reliably with normalized correlation.
        if float(np.std(target)) < 9.0:
            continue
        result = cv2.matchTemplate(screenshot, target, cv2.TM_CCOEFF_NORMED)
        _, score, _, (x, y) = cv2.minMaxLoc(result)
        if score > best_score:
            best_score, best_box = float(score), (x, y, x + w, y + h)
    return (best_box if best_score >= confidence else None), best_score


def expanded_tracking_region(box, pad=32):
    screenw = ctypes.windll.user32.GetSystemMetrics(0) if sys.platform == 'win32' else 1920
    screenh = ctypes.windll.user32.GetSystemMetrics(1) if sys.platform == 'win32' else 1080
    x1, y1, x2, y2 = box
    return (max(0,x1-pad), max(0,y1-pad), min(screenw,x2+pad), min(screenh,y2+pad))

class RangeSlider(ttk.Frame):
    """One horizontal control with draggable MIN and MAX handles (tkinter Canvas)."""
    def __init__(self, parent, low_var, high_var, lower, upper, step=0.05, decimals=2):
        super().__init__(parent)
        self.low_var, self.high_var = low_var, high_var
        self.lower, self.upper, self.step, self.decimals = lower, upper, step, decimals
        self.active = None
        self.caption = ttk.Label(self, text='', font=('Segoe UI', 10, 'bold'))
        self.caption.pack(anchor='w')
        self.canvas = tk.Canvas(self, height=41, highlightthickness=0, cursor='hand2')
        self.canvas.pack(fill='x')
        self.canvas.bind('<Configure>', self.redraw)
        self.canvas.bind('<Button-1>', self.mouse_down)
        self.canvas.bind('<B1-Motion>', self.mouse_move)
        self.canvas.bind('<ButtonRelease-1>', self.mouse_up)
        low_var.trace_add('write', lambda *_: self.redraw())
        high_var.trace_add('write', lambda *_: self.redraw())
        self.redraw()

    def x_for(self, value):
        width = max(30, self.canvas.winfo_width() - 34)
        return 17 + (float(value)-self.lower)/(self.upper-self.lower)*width

    def value_for(self, x):
        width = max(30, self.canvas.winfo_width()-34)
        value = self.lower + (x-17)/width*(self.upper-self.lower)
        value = min(self.upper, max(self.lower, round(value/self.step)*self.step))
        return round(value, self.decimals)

    def mouse_down(self, event):
        lo, hi = self.x_for(self.low_var.get()), self.x_for(self.high_var.get())
        self.active = 'min' if abs(event.x-lo) <= abs(event.x-hi) else 'max'
        self.mouse_move(event)

    def mouse_move(self, event):
        if self.active is None: return
        value = self.value_for(event.x)
        if self.active == 'min': self.low_var.set(min(value, self.high_var.get()))
        else: self.high_var.set(max(value, self.low_var.get()))

    def mouse_up(self, event):
        self.active = None

    def redraw(self, *_):
        if not self.canvas.winfo_exists(): return
        self.canvas.delete('all')
        lo, hi = self.x_for(self.low_var.get()), self.x_for(self.high_var.get())
        width = max(34, self.canvas.winfo_width()-17)
        self.canvas.create_line(17, 21, width, 21, width=6, fill='#CBD5E1', capstyle='round')
        self.canvas.create_line(lo, 21, hi, 21, width=6, fill='#1687BD', capstyle='round')
        for x in (lo, hi):
            self.canvas.create_oval(x-9, 12, x+9, 30, fill='#FFFFFF', outline='#1687BD', width=3)
        self.caption.config(text=f'{self.low_var.get():.{self.decimals}f}–{self.high_var.get():.{self.decimals}f} сек.')


class SingleSlider(ttk.Frame):
    """Single-handle Canvas slider matching the existing MIN/MAX range sliders."""
    def __init__(self, parent, variable, lower, upper, step=1, decimals=0, unit='сек.'):
        super().__init__(parent)
        self.variable, self.lower, self.upper = variable, lower, upper
        self.step, self.decimals, self.unit = step, decimals, unit
        self.dragging = False
        self.caption = ttk.Label(self, font=('Segoe UI', 10, 'bold'))
        self.caption.pack(anchor='w')
        self.canvas = tk.Canvas(self, height=41, highlightthickness=0, cursor='hand2')
        self.canvas.pack(fill='x')
        self.canvas.bind('<Configure>', self.redraw)
        self.canvas.bind('<Button-1>', self.mouse_down)
        self.canvas.bind('<B1-Motion>', self.mouse_move)
        self.canvas.bind('<ButtonRelease-1>', self.mouse_up)
        variable.trace_add('write', lambda *_: self.redraw())
        self.redraw()

    def x_for(self, value):
        width = max(30, self.canvas.winfo_width()-34)
        return 17+(float(value)-self.lower)/(self.upper-self.lower)*width

    def value_for(self, x):
        width = max(30, self.canvas.winfo_width()-34)
        raw = self.lower+(x-17)/width*(self.upper-self.lower)
        stepped = round((raw-self.lower)/self.step)*self.step+self.lower
        return round(max(self.lower, min(self.upper, stepped)), self.decimals)

    def mouse_down(self, event):
        self.dragging = True
        self.mouse_move(event)

    def mouse_move(self, event):
        if self.dragging:
            self.variable.set(self.value_for(event.x))

    def mouse_up(self, event):
        self.dragging = False

    def redraw(self, *_):
        if not self.canvas.winfo_exists():
            return
        self.canvas.delete('all')
        x = self.x_for(self.variable.get())
        right = max(34, self.canvas.winfo_width()-17)
        self.canvas.create_line(17, 21, right, 21, width=6, fill='#CBD5E1', capstyle='round')
        self.canvas.create_line(17, 21, x, 21, width=6, fill='#1687BD', capstyle='round')
        self.canvas.create_oval(x-9, 12, x+9, 30, fill='#FFFFFF', outline='#1687BD', width=3)
        self.caption.config(text=f'{self.variable.get():.{self.decimals}f} {self.unit}')


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title('Nobrain Companion v1.6.3')
        root.geometry(f'900x{min(650, max(480, root.winfo_screenheight()-110))}')
        root.minsize(760, 470)
        self.events = queue.Queue()
        self.close_behavior = tk.StringVar(value='tray')
        self.working_mode = tk.StringVar(value='Удалённо')
        self.wow_process = tk.StringVar(value='Автоопределение WoW')
        self.wow_processes = {}
        self.saved_geometry = ''
        self.tray_icon = None
        self.tray_ready = False
        self._closing = False
        self._brand_icon = None
        self.log_window = None
        self.log_view = None
        self.log_rows = []
        self.log_filter = tk.StringVar(value='ALL')
        self.focus_logged = False
        self.stop = threading.Event()
        self.running = False
        self.recording = False
        self.paused = threading.Event()
        self.hotkey_listener = None
        self.detection_mode = tk.StringVar(value="Звук")
        self.visual_threshold = tk.DoubleVar(value=0.22)
        self.visual_region = None
        self.visual_auto_find = tk.BooleanVar(value=False)
        self.visual_photo = tk.StringVar(value='')
        self.visual_photo_status = tk.StringVar(value='Эталон поплавка не выбран')
        self.visual_region_description = tk.StringVar(value="Область поплавка не выбрана")
        self.record_stop = threading.Event()
        self.recent_lock = threading.Lock()
        self.recent_audio = deque(maxlen=180)
        self.sample = None
        self.sample_file = tk.StringVar(value='')
        self.sample_files = {}
        self.saved_sample_name = ''
        self.audio_name = tk.StringVar(value='System default')
        self.threshold = tk.DoubleVar(value=0.65)
        self.timeout = tk.IntVar(value=30)
        self.early_bite_filter = tk.BooleanVar(value=False)
        self.min_bite_seconds = tk.IntVar(value=0)
        self.cooldown = tk.DoubleVar(value=1.8)  # legacy setting
        self.bite_delay_min = tk.DoubleVar(value=0.10)
        self.bite_delay_max = tk.DoubleVar(value=0.25)
        self.recast_delay_min = tk.DoubleVar(value=1.0)
        self.recast_delay_max = tk.DoubleVar(value=2.0)
        self.automation = tk.BooleanVar(value=False)
        self.key_method = tk.StringVar(value='ScanCode')
        self.remote_client = tk.StringVar(value='Auto')
        self.auto_focus_on_start = tk.BooleanVar(value=True)
        self.estimated_catches = 0
        self.session_casts = 0
        self.session_bites = 0
        self.manual_catch_delta = 0
        self.audio_diagnostics = tk.BooleanVar(value=True)
        self.audio_log_interval = tk.DoubleVar(value=3.0)
        self.score = tk.StringVar(value='—')
        self.state = tk.StringVar(value='Ready')
        self.count = tk.StringVar(value='Забросов: 0    Поклёвок: 0    Предположительно поймано: 0')
        self.load_settings()
        if not PIL_AVAILABLE and self.detection_mode.get() != 'Звук':
            self.detection_mode.set('Звук')
            self.post('DEPENDENCY | Pillow отсутствует; сохранённый визуальный режим заменён на Звук')
        self.setup_app_icon()
        if self.saved_geometry:
            try:
                self.restore_safe_geometry(self.saved_geometry)
            except (tk.TclError, ValueError):
                pass

        # Persistent control/status bar; functionality is grouped in four tabs.
        shell = ttk.Frame(root, padding=(14, 12))
        shell.pack(fill='both', expand=True)
        header = ttk.Frame(shell)
        header.pack(fill='x')
        if PIL_AVAILABLE:
            try:
                mark = Image.open((ROOT / 'assets' / 'nobrain.png') if (ROOT / 'assets' / 'nobrain.png').is_file() else (RESOURCE_ROOT / 'assets' / 'nobrain.png')).convert('RGBA')
                mark.thumbnail((42, 42))
                self._brand_icon = ImageTk.PhotoImage(mark)
                ttk.Label(header, image=self._brand_icon).pack(side='left', padx=(0, 9))
            except Exception as exc:
                self.post(f'BRAND | Logo not loaded: {exc}')
        ttk.Label(header, text='Nobrain Companion', font=('Segoe UI', 15, 'bold')).pack(side='left')
        ttk.Label(shell, text='Локальный WoW или удалённый сеанс · распознавание звука и изображения',
                  foreground='#667085').pack(anchor='w', pady=(1, 5))
        if not PIL_AVAILABLE:
            ttk.Label(shell, text='⚠ Pillow отсутствует: режим пикселей и иконка в трее недоступны. Звуковой режим работает.',
                      foreground='#A16207', wraplength=850).pack(anchor='w', pady=(0, 4))
        elif pystray is None:
            ttk.Label(shell, text='⚠ pystray отсутствует: сворачивание в трей недоступно. Остальные режимы работают.',
                      foreground='#A16207', wraplength=850).pack(anchor='w', pady=(0, 8))

        bar = ttk.Frame(shell)
        bar.pack(fill='x', pady=(0, 3))
        self.start_button = ttk.Button(bar, text='▶ Старт', command=self.start)
        self.start_button.pack(side='left')
        ttk.Button(bar, text='⏸ Пауза / Продолжить', command=self.toggle_pause).pack(side='left', padx=7)
        ttk.Button(bar, text='■ Стоп', command=self.halt).pack(side='left', padx=7)
        ttk.Button(bar, text='Сохранить настройки', command=self.save_settings).pack(side='left')
        ttk.Button(bar, text='Окно логов', command=self.open_log_viewer).pack(side='right')
        ttk.Label(shell, textvariable=self.state, font=('Segoe UI', 10, 'bold')).pack(anchor='w', pady=(2, 1))
        ttk.Label(shell, textvariable=self.score, foreground='#667085').pack(anchor='w', pady=(0, 5))

        notebook = ttk.Notebook(shell)
        notebook.pack(fill='both', expand=True)
        def scrolling_tab(title):
            outer = ttk.Frame(notebook)
            notebook.add(outer, text=title)
            canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
            scrollbar = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview)
            scrollbar.pack(side='right', fill='y')
            canvas.pack(side='left', fill='both', expand=True)
            canvas.configure(yscrollcommand=scrollbar.set)
            inner = ttk.Frame(canvas, padding=(12, 9, 12, 14))
            item = canvas.create_window((0, 0), window=inner, anchor='nw')
            inner.bind('<Configure>', lambda _e, c=canvas: c.configure(scrollregion=c.bbox('all')))
            canvas.bind('<Configure>', lambda e, c=canvas, i=item: c.itemconfigure(i, width=e.width))
            def wheel(event, c=canvas):
                if event.delta:
                    c.yview_scroll(-int(event.delta/120) if abs(event.delta)>=120 else -int(event.delta>0)*2+int(event.delta<0)*2, 'units')
            # Only route wheel events while mouse is inside this tab.
            def bind_wheel(_event):
                canvas.bind_all('<MouseWheel>', wheel)
            def unbind_wheel(_event):
                canvas.unbind_all('<MouseWheel>')
            outer.bind('<Enter>', bind_wheel)
            outer.bind('<Leave>', unbind_wheel)
            return inner

        fishing = scrolling_tab('🎣 Рыбалка')
        audio = scrolling_tab('🔊 Звук')
        vision = scrolling_tab('👁 Пиксели')
        remote = scrolling_tab('⌨ Подключение')
        stats = scrolling_tab('📊 Статистика')
        components = scrolling_tab('⚙ Компоненты')
        logs = ttk.Frame(notebook, padding=10)
        notebook.add(logs, text='📋 Журнал')

        fishing.columnconfigure(0, weight=1)
        action_group = ttk.LabelFrame(fishing, text='Автоматизация', padding=12)
        action_group.grid(row=0, column=0, sticky='ew', pady=(0, 12))
        ttk.Checkbutton(action_group, text='Автоматически нажимать SPACE (заброс + подсечка)',
                        variable=self.automation).pack(anchor='w')
        ttk.Label(action_group, text='Требуется аддон Forever Fishing для заброса и взаимодействия одной клавишей SPACE.',
                  foreground='#A15D08', wraplength=660).pack(anchor='w', pady=(5, 0))
        ttk.Label(action_group, text='Без галочки программа только слушает звук и фиксирует поклёвки.',
                  foreground='#667085').pack(anchor='w', pady=(5, 0))

        def fields_block(parent, title, fields):
            group = ttk.LabelFrame(parent, text=title, padding=12)
            group.pack(fill='x', pady=(0, 12))
            for col in range(min(len(fields), 3)):
                group.columnconfigure(col, weight=1)
            for i, (label, var, hint) in enumerate(fields):
                cell = ttk.Frame(group)
                cell.grid(row=i//3, column=i%3, sticky='ew', padx=(0, 14), pady=5)
                ttk.Label(cell, text=label).pack(anchor='w')
                ttk.Entry(cell, textvariable=var, width=16).pack(anchor='w', pady=(4, 2))
                ttk.Label(cell, text=hint, foreground='#667085').pack(anchor='w')
            return group

        timing_content = ttk.Frame(fishing)
        timing_content.grid(row=1, column=0, sticky='ew')
        fields_block(timing_content, 'Длительность заброса', [
            ('Ожидание поклёвки, сек.', self.timeout, '30 сек. по умолчанию')])
        early_box = ttk.LabelFrame(timing_content, text='Фильтр ранних поклёвок', padding=12)
        early_box.pack(fill='x', pady=(0, 12))
        ttk.Checkbutton(
            early_box, text='Не подсекать раньше заданного времени',
            variable=self.early_bite_filter
        ).pack(anchor='w')
        self.early_slider = SingleSlider(
            early_box, self.min_bite_seconds, 0, 28, step=1, decimals=0
        )
        self.early_slider.pack(fill='x', pady=(5, 0))
        ttk.Label(early_box,
            text='Ранние всплески полностью игнорируются и записываются как BITE_IGNORED. '
                 'Отсчёт начинается после каждого заброса; 0 сек. / выключено — без фильтра.',
            foreground='#667085', wraplength=650
        ).pack(anchor='w', pady=(5, 0))
        reaction_box = ttk.LabelFrame(timing_content, text='После звука поклёвки — задержка подсечки', padding=12)
        reaction_box.pack(fill='x', pady=(0, 12))
        RangeSlider(reaction_box, self.bite_delay_min, self.bite_delay_max, 0.0, 2.0, 0.05).pack(fill='x')
        pause_box = ttk.LabelFrame(timing_content, text='Перед следующим забросом — пауза', padding=12)
        pause_box.pack(fill='x', pady=(0, 12))
        RangeSlider(pause_box, self.recast_delay_min, self.recast_delay_max, 0.0, 8.0, 0.1, 1).pack(fill='x')
        session_box = ttk.LabelFrame(stats, text='Статистика текущей сессии', padding=12)
        session_box.pack(fill='x', pady=(0, 12))
        ttk.Label(session_box, textvariable=self.count, font=('Segoe UI', 12, 'bold')).pack(anchor='w', pady=(0, 12))
        ttk.Label(session_box, text='«Поймано» — приблизительная оценка по отправленным подсечкам. Пустые поклёвки и срывы не определяются автоматически.',
                  foreground='#667085', wraplength=700).pack(anchor='w')
        stats_buttons = ttk.Frame(session_box)
        stats_buttons.pack(anchor='w', pady=(12,0))
        ttk.Button(stats_buttons, text='−1 улов', command=lambda: self.adjust_catches(-1)).pack(side='left')
        ttk.Button(stats_buttons, text='+1 улов', command=lambda: self.adjust_catches(1)).pack(side='left', padx=8)
        ttk.Button(stats_buttons, text='Сброс статистики', command=self.reset_stats).pack(side='left')
        ttk.Label(fishing, text='Порядок: SPACE → до 30 с ожидания → поклёвка и задержка → SPACE → пауза → SPACE.\n'
                  'Если за 30 с поклёвки нет: пауза → новый заброс.',
                  foreground='#667085', justify='left').grid(row=2, column=0, sticky='w')

        device_group = ttk.LabelFrame(audio, text='Источник звука Windows', padding=12)
        device_group.pack(fill='x', pady=(0, 12))
        ttk.Label(device_group, text='Выход, на который удалённый клиент воспроизводит звук WoW').pack(anchor='w')
        self.devices = ttk.Combobox(device_group, textvariable=self.audio_name, state='readonly')
        self.devices.pack(fill='x', pady=(5, 8))
        dev_buttons = ttk.Frame(device_group)
        dev_buttons.pack(fill='x')
        ttk.Button(dev_buttons, text='Обновить устройства', command=self.refresh_devices).pack(side='left')
        ttk.Button(dev_buttons, text='Настройка бесшумного звука', command=self.silent_audio_setup).pack(side='left', padx=8)
        sample_group = ttk.LabelFrame(audio, text='Образец поклёвки (WAV)', padding=12)
        sample_group.pack(fill='x', pady=(0, 12))
        sr = ttk.Frame(sample_group)
        sr.pack(fill='x')
        self.record_button = ttk.Button(sr, text='Слушать (до 90 с)', command=self.record)
        self.record_button.pack(side='left')
        self.capture_button = ttk.Button(sr, text='Сохранить недавний всплеск', command=self.capture_recent, state='disabled')
        self.capture_button.pack(side='left', padx=7)
        ttk.Button(sr, text='Добавить WAV…', command=self.load_sample).pack(side='left')
        choose_row = ttk.Frame(sample_group)
        choose_row.pack(fill='x', pady=(10, 3))
        ttk.Label(choose_row, text='Активный звуковой файл:').pack(side='left', padx=(0, 8))
        self.sample_combo = ttk.Combobox(choose_row, textvariable=self.sample_file, state='readonly', width=44)
        self.sample_combo.pack(side='left', fill='x', expand=True)
        self.sample_combo.bind('<<ComboboxSelected>>', self.on_sample_selected)
        ttk.Button(choose_row, text='Обновить список', command=self.refresh_samples).pack(side='left', padx=(8, 0))
        self.sample_label = ttk.Label(sample_group, text='Образец не загружен')
        self.sample_label.pack(anchor='w', pady=(5, 0))
        detect_group = ttk.LabelFrame(audio, text='Распознавание и диагностика', padding=12)
        detect_group.pack(fill='x')
        dr = ttk.Frame(detect_group)
        dr.pack(anchor='w')
        ttk.Label(dr, text='Порог совпадения 0–1:').pack(side='left')
        ttk.Entry(dr, textvariable=self.threshold, width=10).pack(side='left', padx=8)
        ttk.Checkbutton(detect_group, text='Периодические AUDIO/CANDIDATE события',
                        variable=self.audio_diagnostics).pack(anchor='w', pady=(9, 3))
        di = ttk.Frame(detect_group)
        di.pack(anchor='w')
        ttk.Label(di, text='Интервал журнала, сек.:').pack(side='left')
        ttk.Entry(di, textvariable=self.audio_log_interval, width=9).pack(side='left', padx=8)

        visual_group = ttk.LabelFrame(vision, text='Режим распознавания поклёвки', padding=12)
        visual_group.pack(fill='x', pady=(12, 0))
        ttk.Label(visual_group, text='Выбери способ определения поклёвки:').pack(anchor='w')
        modes = ['Звук', 'Пиксели (эксперимент)', 'Звук ИЛИ пиксели', 'Звук + свой поплавок (строго)'] if PIL_AVAILABLE else ['Звук']
        ttk.Combobox(visual_group, textvariable=self.detection_mode, state='readonly',
                     values=modes, width=27).pack(anchor='w', pady=(5,8))
        if not PIL_AVAILABLE:
            ttk.Label(visual_group, text='Для визуального режима установи Pillow: .venv\\Scripts\\python.exe -m pip install Pillow',
                      foreground='#A16207').pack(anchor='w')
        ttk.Label(visual_group, text='Звук + свой поплавок: чужой всплеск сам по себе не запускает подсечку.\n'
                  'Для реакции необходимы звук и изменение изображения выбранного поплавка в пределах 1.5 с.\n'
                  'Экспериментально: движение воды, перекрытое окно или потеря поплавка мешают детекции.',
                  foreground='#667085', wraplength=680).pack(anchor='w')
        vr = ttk.Frame(visual_group)
        vr.pack(fill='x', pady=4)
        ttk.Button(vr, text='Выделить область поплавка на экране', command=self.choose_visual_region,
                   state='normal' if PIL_AVAILABLE else 'disabled').pack(side='left')
        ttk.Label(vr, textvariable=self.visual_region_description).pack(side='left', padx=8)
        vt = ttk.Frame(visual_group)
        vt.pack(anchor='w', pady=(6,0))
        ttk.Label(vt, text='Порог изменения пикселей (0.05–0.95):').pack(side='left')
        ttk.Entry(vt, textvariable=self.visual_threshold, width=8).pack(side='left', padx=8)
        locate_row = ttk.LabelFrame(vision, text='Автопоиск поплавка (эксперимент)', padding=12)
        locate_row.pack(fill='x', pady=(8,0))
        ttk.Checkbutton(locate_row, text='Искать поплавок по фотографии после каждого заброса',
                        variable=self.visual_auto_find).pack(anchor='w')
        ttk.Label(locate_row, text='Фотография: обрезанный PNG/JPG с поплавком, без большого участка воды.\n'
                  'Поиск идёт по экрану, после нахождения — в динамической рамке.\n'
                  'Нужно Pillow + opencv-python; на RustDesk захват экрана должен показывать игру.',
                  foreground='#667085').pack(anchor='w', pady=4)
        photorow = ttk.Frame(locate_row)
        photorow.pack(fill='x')
        ttk.Button(photorow, text='Выбрать фото поплавка…', command=self.choose_bobber_photo).pack(side='left')
        ttk.Label(photorow, textvariable=self.visual_photo_status).pack(side='left', padx=8)

        ttk.Label(visual_group, text='Визуальный режим пока экспериментальный: после заброса программа запоминает\n'
                  'область спустя 2.5 с и ищет последовательные изменения.\n'
                  'Для исходного проверенного поведения выбери «Звук».',
                  foreground='#667085').pack(anchor='w', pady=(5,0))

        mode_group = ttk.LabelFrame(remote, text='Режим работы', padding=12)
        mode_group.pack(fill='x', pady=(0, 12))
        ttk.Radiobutton(mode_group, text='Удалённо (RustDesk / AnyDesk / TeamViewer)', variable=self.working_mode,
                        value='Удалённо', command=self.on_mode_changed).pack(anchor='w')
        ttk.Radiobutton(mode_group, text='Локально (WoW на этом ПК)', variable=self.working_mode,
                        value='Локально', command=self.on_mode_changed).pack(anchor='w', pady=(5, 0))
        ttk.Label(mode_group, text='Локальный режим: риск блокировки учётной записи может быть выше, поскольку программа работает рядом с игрой.\n'
                  'Автоопределение и выбор процесса не обеспечивают безопасность от античита.',
                  foreground='#A15D08', wraplength=710).pack(anchor='w', pady=(7, 0))
        process_row = ttk.Frame(mode_group)
        process_row.pack(fill='x', pady=(8, 2))
        ttk.Label(process_row, text='Окно WoW / PID:').pack(side='left')
        self.wow_combo = ttk.Combobox(process_row, textvariable=self.wow_process, state='readonly', width=48)
        self.wow_combo.pack(side='left', fill='x', expand=True, padx=8)
        ttk.Button(process_row, text='Обновить', command=self.refresh_wow_processes).pack(side='left')
        self.refresh_wow_processes()

        key_group = ttk.LabelFrame(remote, text='Управление клавиатурой и удалённый клиент', padding=12)
        key_group.pack(fill='x', pady=(0, 12))
        ttk.Label(key_group, text='Клиент удалённого доступа:').pack(anchor='w')
        ttk.Combobox(key_group, textvariable=self.remote_client, state='readonly',
                     values=REMOTE_CHOICES, width=24).pack(anchor='w', pady=(4, 12))
        ttk.Checkbutton(key_group, text='Автоматически активировать выбранное окно при старте',
                        variable=self.auto_focus_on_start).pack(anchor='w', pady=(0, 10))
        ttk.Label(key_group, text='Auto определяет клиент по заголовку активного окна.\n'
                  'AnyDesk / TeamViewer требуют проверки передачи клавиши на конкретном ПК.',
                  foreground='#667085').pack(anchor='w', pady=(0, 10))
        ttk.Label(key_group, text='Клавиша игры: SPACE (одна для заброса и подсечки)').pack(anchor='w')
        kr = ttk.Frame(key_group)
        kr.pack(anchor='w', pady=(10, 0))
        ttk.Label(kr, text='Метод отправки:').pack(side='left')
        ttk.Combobox(kr, textvariable=self.key_method, state='readonly',
                     values=['ScanCode', 'VirtualKey', 'Legacy'], width=13).pack(side='left', padx=8)
        ttk.Button(key_group, text='Тест SPACE через 3 секунды', command=self.test_space).pack(anchor='w', pady=(12, 2))
        ttk.Label(key_group, text='Клавиши отправляются только при активном окне WoW или удалённого клиента.\n'
                  'Если автопереключение выключено — активируй сеанс после старта. F8 — остановка.',
                  foreground='#667085').pack(anchor='w', pady=(8, 0))
        preferences = ttk.LabelFrame(remote, text='Окно приложения и системный трей', padding=12)
        preferences.pack(fill='x', pady=(8, 8))
        ttk.Label(preferences, text='При нажатии X:').pack(anchor='w')
        ttk.Radiobutton(preferences, text='Сворачивать в трей (работа продолжается)', variable=self.close_behavior, value='tray').pack(anchor='w')
        ttk.Radiobutton(preferences, text='Закрывать приложение', variable=self.close_behavior, value='exit').pack(anchor='w')
        ttk.Label(preferences, text='Положение и размер окна восстанавливаются при следующем запуске.', foreground='#667085').pack(anchor='w', pady=(5, 0))
        ttk.Label(remote, text='Глобальные горячие клавиши: Ctrl+Alt+F6 — старт/продолжить,\n'
                  'Ctrl+Alt+F7 — пауза/продолжить, Ctrl+Alt+F8 — стоп. F8 — аварийный стоп.',
                  foreground='#667085').pack(anchor='w', pady=(10,6))
        ttk.Button(remote, text='Открыть папку журналов', command=self.open_log_folder).pack(anchor='w')

        self.component_status = tk.StringVar(value='Проверка компонентов…')
        pkgbox = ttk.LabelFrame(components, text='Диагностика зависимостей', padding=12)
        pkgbox.pack(fill='x', pady=(0, 12))
        ttk.Label(pkgbox, text='Основные пакеты устанавливаются при первом запуске. Визуальные и трей — необязательные.',
                  wraplength=680, foreground='#667085').pack(anchor='w', pady=(0, 8))
        ttk.Label(pkgbox, textvariable=self.component_status, justify='left', wraplength=700).pack(anchor='w', pady=(0, 12))
        pkgbuttons = ttk.Frame(pkgbox)
        pkgbuttons.pack(fill='x')
        ttk.Button(pkgbuttons, text='Проверить снова', command=self.check_components).pack(side='left')
        self.install_button = ttk.Button(pkgbuttons, text='Установить недостающие', command=self.install_optional_components)
        self.install_button.pack(side='left', padx=(9, 0))
        ttk.Label(components, text='Установка идёт в окружение .venv этой папки. Для включения новых функций потребуется перезапустить программу.\n'
                  'Если отсутствует обязательный пакет, запусти install-components.bat.',
                  foreground='#667085', wraplength=700).pack(anchor='w', pady=(0, 10))
        self.check_components()

        ttk.Label(logs, text='Последние события. Полный журнал — в отдельном просмотрщике.',
                  foreground='#667085').pack(anchor='w', pady=(0, 8))
        self.log = tk.Text(logs, height=12, state='disabled', wrap='word', font=('Consolas', 9))
        log_scroll = ttk.Scrollbar(logs, orient='vertical', command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side='right', fill='y')
        self.log.pack(side='left', fill='both', expand=True)
        self.refresh_devices()
        self.refresh_samples()
        self.post('INFO | Nobrain Companion v1.6.3 started. Sliding matching enabled. Log file: ' + str(LOG_PATH))
        initial = self.sample_files.get(self.saved_sample_name)
        if initial is None and SAMPLE_PATH.exists():
            initial = SAMPLE_PATH
        if initial is not None:
            try:
                self.set_sample(initial)
            except Exception as exc:
                self.post(f'SAMPLE | Could not load {initial.name}: {exc}')
        self.hotkey_listener = GlobalHotkeys(self.events)
        self.root.after(100, self.poll)
        root.protocol('WM_DELETE_WINDOW', self.on_window_close)
        root.bind('<Configure>', self.on_geometry_change)
        self.setup_tray()

    def selected_wow_pid(self) -> int | None:
        label = self.wow_process.get()
        return self.wow_processes.get(label)

    def refresh_wow_processes(self):
        chosen = self.wow_process.get()
        try:
            matches = find_wow_windows()
            self.wow_processes = {f'{exe} · PID {pid} · {title}': pid for hwnd, pid, title, exe in matches}
            values = ['Автоопределение WoW'] + list(self.wow_processes)
            self.wow_combo.configure(values=values)
            self.wow_process.set(chosen if chosen in values else values[0])
        except Exception as exc:
            self.post(f'LOCAL | Process detection failed: {exc}')

    def on_mode_changed(self):
        if self.working_mode.get() == 'Локально':
            self.refresh_wow_processes()
        self.post(f'MODE | {self.working_mode.get()}')

    def restore_safe_geometry(self, geometry):
        match = re.fullmatch(r'(\d+)x(\d+)\+(-?\d+)\+(-?\d+)', geometry)
        if not match:
            return
        w, h, x, y = map(int, match.groups())
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w = max(760, min(w, sw))
        h = max(470, min(h, sh - 55))
        x = max(0, min(x, max(0, sw - w)))
        y = max(0, min(y, max(0, sh - h - 40)))
        self.root.geometry(f'{w}x{h}+{x}+{y}')

    def check_components(self):
        packages = [('numpy', 'NumPy'), ('scipy', 'SciPy'), ('soundcard', 'SoundCard'),
                    ('psutil', 'psutil'), ('PIL', 'Pillow'), ('pystray', 'pystray'), ('cv2', 'opencv-python')]
        lines = []
        missing = []
        for module, package in packages:
            present = importlib.util.find_spec(module) is not None
            lines.append(f'{"✓" if present else "✗"} {package}: {"установлен" if present else "отсутствует"}')
            if not present:
                missing.append(package)
        if FROZEN:
            # A packaged EXE has no managed pip environment. Dependencies are built in.
            missing = []
            lines.append('Сборка EXE: компоненты включены при сборке; обновление — новой версией EXE.')
        self.missing_packages = missing
        self.component_status.set('\n'.join(lines))
        if hasattr(self, 'install_button'):
            self.install_button.config(state='normal' if missing else 'disabled')

    def install_optional_components(self):
        if FROZEN:
            messagebox.showinfo('Компоненты', 'В EXE зависимости уже встроены. Для обновления установите новую сборку.')
            return
        if not self.missing_packages:
            messagebox.showinfo('Компоненты', 'Все компоненты установлены.')
            return
        missing = list(self.missing_packages)
        if not messagebox.askyesno('Установка компонентов',
                'Установить в текущее окружение Python?\n\n' + ', '.join(missing) +
                '\n\nПотребуется интернет. После установки перезапусти программу.'):
            return
        self.install_button.config(state='disabled')
        self.component_status.set('Устанавливаем: ' + ', '.join(missing) + '…')
        self.post('DEPENDENCY | Installing: ' + ', '.join(missing))
        def worker():
            try:
                result = subprocess.run([sys.executable, '-m', 'pip', 'install', *missing],
                                        capture_output=True, text=True, timeout=240, check=False)
                detail = ((result.stdout or '') + '\n' + (result.stderr or '')).strip()
                self.events.put(('install_result', (result.returncode == 0, detail[-2500:])))
            except Exception as exc:
                self.events.put(('install_result', (False, str(exc))))
        threading.Thread(target=worker, daemon=True).start()

    def setup_app_icon(self):
        path = (ROOT / 'assets' / 'nobrain.ico')
        if not path.is_file():
            path = RESOURCE_ROOT / 'assets' / 'nobrain.ico'
        if path.exists():
            try:
                self.root.iconbitmap(str(path))
            except tk.TclError:
                pass

    def setup_tray(self):
        if not PIL_AVAILABLE or pystray is None:
            self.tray_ready = False
            self.post('DEPENDENCY | Системный трей отключён: требуется Pillow и pystray')
            return
        try:
            icon_path = (ROOT / 'assets' / 'nobrain.png') if (ROOT / 'assets' / 'nobrain.png').is_file() else (RESOURCE_ROOT / 'assets' / 'nobrain.png')
            tray_image = Image.open(icon_path).convert('RGBA')
            menu = pystray.Menu(
                pystray.MenuItem('Показать Nobrain Companion', lambda icon, item: self.events.put(('tray_show', None)), default=True),
                pystray.MenuItem('Старт / продолжить', lambda icon, item: self.events.put(('tray_start', None))),
                pystray.MenuItem('Пауза / продолжить', lambda icon, item: self.events.put(('tray_pause', None))),
                pystray.MenuItem('Стоп', lambda icon, item: self.events.put(('tray_stop', None))),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('Выход', lambda icon, item: self.events.put(('tray_exit', None))))
            self.tray_icon = pystray.Icon('nobrain-companion', tray_image, 'Nobrain Companion', menu)
            self.tray_icon.run_detached()
            self.tray_ready = True
        except Exception as exc:
            self.tray_icon = None
            self.tray_ready = False
            self.post(f'TRAY | Could not initialize: {exc}')

    def on_geometry_change(self, event):
        if event.widget is self.root and self.root.state() == 'normal':
            self.saved_geometry = self.root.geometry()

    def show_from_tray(self):
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass

    def on_window_close(self):
        if self.close_behavior.get() == 'tray' and self.tray_ready:
            self.saved_geometry = self.root.geometry()
            self.save_settings()
            self.root.withdraw()
            self.post('TRAY | Window hidden; monitoring continues')
        else:
            if self.close_behavior.get() == 'tray' and not self.tray_ready:
                self.post('DEPENDENCY | Трей недоступен; окно будет закрыто')
            self.close()

    def update_stats(self):
        self.count.set(f'Забросов: {self.session_casts}    Поклёвок: {self.session_bites}    '
                       f'Предположительно поймано: {self.estimated_catches}')

    def adjust_catches(self, delta):
        self.manual_catch_delta += delta
        self.estimated_catches = max(0, self.session_bites + self.manual_catch_delta)
        self.update_stats()
        self.post(f'STATS | Manual adjustment {delta:+d}; estimated catches={self.estimated_catches}')

    def reset_stats(self):
        self.session_casts = 0
        self.session_bites = 0
        self.estimated_catches = 0
        self.manual_catch_delta = 0
        self.update_stats()

    def test_space(self):
        if self.running or self.recording:
            messagebox.showinfo('Test', 'Stop monitoring/recording first.')
            return
        method = self.key_method.get()
        client = self.remote_client.get()
        mode = self.working_mode.get()
        selected_pid = self.selected_wow_pid()
        self.post('TEST: Активируйте целевое окно (WoW / удалённый клиент). SPACE через 3 секунды.')
        def worker():
            time.sleep(3)
            window = foreground_title()
            if not (foreground_is_wow(selected_pid) if mode == 'Локально' else (remote_client_from_title(window) if client == 'Auto' else remote_client_from_title(window) == client)):
                self.events.put(('log', f'TEST CANCELLED: active window is {window!r}, not selected remote client.'))
                return
            try:
                down, up = send_space(method)
                self.events.put(('log', f'TEST: {method} sent down={down}, up={up}; active={window!r}. Check the remote PC.'))
            except Exception as exc:
                self.events.put(('log', f'TEST FAILED: {exc}'))
        threading.Thread(target=worker, daemon=True).start()

    def silent_audio_setup(self):
        messagebox.showinfo('Silent audio setup — Remote Desktop',
            'SILENT ROUTING (Windows 11)\n\n'
            '1. Install VB-CABLE (or another trusted virtual audio cable).\n'
            '2. Keep the physical speakers as Windows default output.\n'
            '3. Windows Settings > System > Sound > Volume mixer.\n'
            '4. Find RustDesk under Apps, set its Output device to CABLE Input.\n'
            '5. In this program choose Speaker (CABLE Input) / CABLE Input as Audio device.\n'
            '6. Press Refresh audio devices if it is missing.\n'
            '7. Check that AUDIO logs show signal=OK and monitor can detect a splash.\n\n'
            'NOTE: Do not mute the CABLE Input endpoint or the RustDesk app.\n'
            'Do not enable \"Listen to this device\" for CABLE Output.\n'
            'The virtual output is silent physically, but WASAPI loopback can record it.\n'
            'If RustDesk audio does not show up in Windows mixer, route it with '
            'EarTrumpet/Audio Router or use an alternative virtual routing tool.')

    def refresh_devices(self):
        try:
            import soundcard as sc
            names = ['System default'] + [x.name for x in sc.all_speakers()]
            self.devices['values'] = list(dict.fromkeys(names))
            if self.audio_name.get() not in names:
                self.audio_name.set('System default')
        except Exception as exc:
            messagebox.showerror('Audio', str(exc))

    def load_settings(self):
        if CONFIG_PATH.exists():
            try:
                cfg = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
                self.audio_name.set(cfg.get('audio', 'System default'))
                self.working_mode.set(cfg.get('working_mode', 'Удалённо'))
                self.close_behavior.set(cfg.get('close_behavior', 'tray'))
                self.saved_geometry = cfg.get('window_geometry', '')
                self.wow_process.set(cfg.get('wow_process', 'Автоопределение WoW'))
                self.saved_sample_name = cfg.get('selected_sample', '')
                self.threshold.set(cfg.get('threshold', .65))
                self.timeout.set(30 if cfg.get('timeout', 30) == 25 else cfg.get('timeout', 30))  # v0.8 default migration
                self.early_bite_filter.set(bool(cfg.get('early_bite_filter', False)))
                self.min_bite_seconds.set(int(cfg.get('min_bite_seconds', 0)))
                self.cooldown.set(cfg.get('cooldown', 1.8))
                self.bite_delay_min.set(cfg.get('bite_delay_min', 0.10))
                self.bite_delay_max.set(cfg.get('bite_delay_max', 0.25))
                self.recast_delay_min.set(cfg.get('recast_delay_min', 1.0))
                self.recast_delay_max.set(cfg.get('recast_delay_max', 2.0))
                self.key_method.set(cfg.get('key_method', 'ScanCode'))
                self.remote_client.set(cfg.get('remote_client', 'Auto'))
                self.auto_focus_on_start.set(bool(cfg.get('auto_focus_on_start', True)))
                self.audio_diagnostics.set(cfg.get('audio_diagnostics', True))
                self.audio_log_interval.set(cfg.get('audio_log_interval', 3.0))
                self.detection_mode.set(cfg.get('detection_mode', 'Звук'))
                self.visual_threshold.set(cfg.get('visual_threshold', 0.22))
                self.visual_auto_find.set(bool(cfg.get('visual_auto_find', False)))
                self.visual_photo.set(cfg.get('visual_photo', ''))
                if self.visual_photo.get():
                    self.visual_photo_status.set(Path(self.visual_photo.get()).name)
                region = cfg.get('visual_region')
                if isinstance(region,list) and len(region)==4 and all(isinstance(v,int) for v in region):
                    self.visual_region = region
                    self.visual_region_description.set(f'{region[2]-region[0]}×{region[3]-region[1]} px')
            except (ValueError, OSError):
                pass

    def save_settings(self):
        try:
            config = dict(working_mode=self.working_mode.get(), close_behavior=self.close_behavior.get(),
                          window_geometry=self.saved_geometry or self.root.geometry(), wow_process=self.wow_process.get(),
                          audio=self.audio_name.get(), selected_sample=self.sample_file.get(), threshold=float(self.threshold.get()),
                          timeout=int(self.timeout.get()), early_bite_filter=bool(self.early_bite_filter.get()),
                          min_bite_seconds=int(self.min_bite_seconds.get()), key_method=self.key_method.get(),
                          remote_client=self.remote_client.get(),
                          auto_focus_on_start=bool(self.auto_focus_on_start.get()),
                          detection_mode=self.detection_mode.get(),
                          visual_threshold=float(self.visual_threshold.get()),
                          visual_auto_find=bool(self.visual_auto_find.get()), visual_photo=self.visual_photo.get(),
                          visual_region=self.visual_region,
                          audio_diagnostics=bool(self.audio_diagnostics.get()),
                          audio_log_interval=float(self.audio_log_interval.get()),
                          bite_delay_min=float(self.bite_delay_min.get()),
                          bite_delay_max=float(self.bite_delay_max.get()),
                          recast_delay_min=float(self.recast_delay_min.get()),
                          recast_delay_max=float(self.recast_delay_max.get()))
            self.validate_timing(config)
            if not (config['bite_delay_max'] <= 2.0 and config['recast_delay_max'] <= 8.0):
                raise ValueError('Range slider limits exceeded')
            CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding='utf-8')
            self.post('Settings saved')
        except Exception as exc:
            messagebox.showerror('Settings', str(exc))

    @staticmethod
    def validate_timing(config):
        if not (0 < config['threshold'] <= 1) or config['timeout'] < 3:
            raise ValueError('Threshold must be (0,1]; cast timeout >= 3 sec.')
        minimum = int(config.get('min_bite_seconds', 0))
        if not (0 <= minimum <= 28):
            raise ValueError('Minimum time before bite must be between 0 and 28 seconds.')
        if config.get('early_bite_filter', False) and minimum >= config['timeout']:
            raise ValueError('Minimum time before bite must be less than cast timeout.')
        if 'audio_log_interval' in config and not (1.0 <= config['audio_log_interval'] <= 30.0):
            raise ValueError('Audio diagnostics interval must be between 1 and 30 seconds.')
        for prefix in ('bite_delay', 'recast_delay'):
            low, high = config[prefix + '_min'], config[prefix + '_max']
            if not (0 <= low <= high <= 60):
                raise ValueError(f'{prefix}: MIN must be <= MAX; both must be between 0 and 60 sec.')

    def refresh_samples(self):
        files = {}
        # Legacy root sample stays available; imported samples are kept in samples/.
        for path in sorted(SAMPLES_DIR.glob('*.wav'), key=lambda p: p.name.casefold()):
            files[path.name] = path
        if SAMPLE_PATH.is_file() and SAMPLE_PATH.name not in files:
            files[SAMPLE_PATH.name] = SAMPLE_PATH
        self.sample_files = files
        self.sample_combo.config(values=list(files))
        return files

    def on_sample_selected(self, event=None):
        if self.running or self.recording:
            messagebox.showinfo('Образец WAV', 'Остановите мониторинг или запись перед сменой WAV.')
            self.sample_file.set(self.active_sample_name if hasattr(self, 'active_sample_name') else '')
            return
        name = self.sample_file.get()
        path = self.sample_files.get(name)
        if path is None:
            self.post(f'SAMPLE | File not found in library: {name}')
            return
        try:
            self.set_sample(path)
            self.post(f'SAMPLE | Selected {name}')
            self.save_settings()
        except Exception as exc:
            self.sample_file.set(self.active_sample_name if hasattr(self, 'active_sample_name') else '')
            messagebox.showerror('WAV', str(exc))

    def set_sample(self, path: Path):
        signal = load_wav(path)
        if not .35 <= len(signal) / SAMPLE_RATE <= 3.0:
            raise ValueError('Sample must be 0.35–3 seconds')
        original_duration = len(signal) / SAMPLE_RATE
        clip, start = auto_trim_splash(signal)
        sig = features(clip)
        if len(sig) < 20:
            raise ValueError('Sample is too short')
        self.sample = sig
        self.active_sample_name = path.name
        if path.name not in self.sample_files:
            self.refresh_samples()
        self.sample_file.set(path.name)
        self.sample_label.config(text=f'Загружено: {path.name}  •  активный фрагмент {len(clip)/SAMPLE_RATE:.2f} с / файл {original_duration:.2f} с')
        self.post(f'SAMPLE | {path.name}; original={original_duration:.2f}s; '
                  f'active={len(clip)/SAMPLE_RATE:.2f}s; selected={start:.2f}-{start+len(clip)/SAMPLE_RATE:.2f}s; '
                  f'frames={len(sig)}')

    def load_sample(self):
        if self.running or self.recording:
            messagebox.showinfo('Образец WAV', 'Остановите мониторинг или запись перед импортом.')
            return
        f = filedialog.askopenfilename(title='Выбрать звуковой файл поклёвки',
                                       filetypes=[('WAV audio', '*.wav'), ('All files', '*.*')])
        if f:
            try:
                source = Path(f)
                # Validate file and sample before adding it to library.
                signal = load_wav(source)
                if not 0.35 <= len(signal) / SAMPLE_RATE <= 3.0:
                    raise ValueError('Sample must be 0.35–3 seconds')
                dest = SAMPLES_DIR / source.name
                if source.resolve() != dest.resolve():
                    if dest.exists():
                        i = 2
                        while (SAMPLES_DIR / f'{source.stem}_{i}{source.suffix}').exists():
                            i += 1
                        dest = SAMPLES_DIR / f'{source.stem}_{i}{source.suffix}'
                    # Normalize to same format as recording to avoid WAV codec issues.
                    save_wav(dest, signal)
                self.refresh_samples()
                self.set_sample(dest)
                self.save_settings()
                self.post(f'SAMPLE | Imported selected WAV: {dest.name}')
            except Exception as exc:
                messagebox.showerror('WAV', str(exc))

    def capture_recent(self):
        if not self.recording:
            return
        with self.recent_lock:
            parts = list(self.recent_audio)
        if not parts:
            self.post('No audio buffered yet')
            return
        data = np.concatenate(parts)[-int(2.5 * SAMPLE_RATE):]
        try:
            clip = extract_splash_from_recent(data)
            filename = datetime.now().strftime('splash-%Y%m%d-%H%M%S.wav')
            dest = SAMPLES_DIR / filename
            save_wav(dest, clip)
            self.refresh_samples()
            self.set_sample(dest)
            self.save_settings()
            self.post(f'SAMPLE | Recorded and selected: {filename}')
            self.record_stop.set()
        except Exception as exc:
            self.post(f'Capture failed: {exc}')

    def record(self):
        if self.running:
            messagebox.showinfo('Record', 'Stop monitoring before recording')
            return
        if self.recording:
            self.record_stop.set()
            return
        self.record_stop.clear()
        with self.recent_lock:
            self.recent_audio.clear()
        self.recording = True
        self.record_button.config(text='Cancel recording')
        self.capture_button.config(state='normal')
        self.start_button.config(state='disabled')
        self.post('Listening for 90s. After hearing the splash, click Capture recent splash.')
        threading.Thread(target=self.record_worker, daemon=True).start()

    def record_worker(self):
        com_ready = False
        try:
            com_ready = initialize_com()
            mic = selected_loopback(self.audio_name.get())
            max_time = time.monotonic() + 90
            with mic.recorder(samplerate=SAMPLE_RATE, channels=1) as rec:
                while not self.record_stop.is_set() and time.monotonic() < max_time:
                    data = rec.record(numframes=HOP*8).reshape(-1).astype(np.float32)
                    with self.recent_lock:
                        self.recent_audio.append(data)
            if time.monotonic() >= max_time and not self.record_stop.is_set():
                self.events.put(('log', '90s recording limit reached. Try again.'))
        except Exception as exc:
            self.events.put(('log', f'Audio recording failed: {exc}'))
        finally:
            if com_ready:
                ctypes.windll.ole32.CoUninitialize()
            self.events.put(('record_finished', None))

    def choose_visual_region(self):
        if not PIL_AVAILABLE:
            messagebox.showwarning('Недоступная функция',
                'Для выделения поплавка нужен Pillow.\nУстанови: .venv\\Scripts\\python.exe -m pip install Pillow')
            return
        if self.running:
            messagebox.showinfo('Область', 'Сначала останови мониторинг.')
            return
        # Hide the app and expose the remote desktop before taking the selection screenshot.
        self.root.withdraw()
        try:
            activate_remote_window(self.remote_client.get())
            time.sleep(.25)
            shot = ImageGrab.grab(all_screens=False).convert('RGB')
        except Exception as exc:
            self.root.deiconify()
            messagebox.showerror('Снимок экрана',str(exc))
            return
        w,h = shot.size
        overlay = tk.Toplevel(self.root)
        overlay.overrideredirect(True)
        overlay.geometry(f'{w}x{h}+0+0')
        overlay.attributes('-topmost',True)
        canvas = tk.Canvas(overlay,width=w,height=h,highlightthickness=0,cursor='cross')
        canvas.pack(fill='both',expand=True)
        photo = ImageTk.PhotoImage(shot)
        canvas.create_image(0,0,anchor='nw',image=photo)
        canvas.image = photo
        canvas.create_text(22,25,anchor='w',text='Обведи поплавок и немного воды вокруг него. Esc — отмена',fill='#FFFF00',font=('Segoe UI',14,'bold'))
        drag={'start':None,'rectangle':None}
        def down(e):
            drag['start']=(e.x,e.y)
            drag['rectangle']=canvas.create_rectangle(e.x,e.y,e.x,e.y,outline='#00ff9c',width=3)
        def move(e):
            if drag['start'] and drag['rectangle']:
                x,y=drag['start']
                canvas.coords(drag['rectangle'],x,y,e.x,e.y)
        def done(e):
            if not drag['start']:
                return
            x,y=drag['start']
            a,b=sorted((x,e.x)),sorted((y,e.y))
            reg=[a[0],b[0],a[1],b[1]]
            overlay.destroy()
            self.root.deiconify()
            if reg[2]-reg[0]<20 or reg[3]-reg[1]<20:
                messagebox.showwarning('Область', 'Выдели участок минимум 20×20 пикселей.')
                return
            self.visual_region=reg
            self.visual_region_description.set(f'{reg[2]-reg[0]}×{reg[3]-reg[1]} px')
            self.post(f'VISUAL | Selected ROI: {reg}')
            self.save_settings()
        canvas.bind('<ButtonPress-1>',down)
        canvas.bind('<B1-Motion>',move)
        canvas.bind('<ButtonRelease-1>',done)
        overlay.bind('<Escape>',lambda e:(overlay.destroy(),self.root.deiconify()))
        overlay.focus_force()

    def choose_bobber_photo(self):
        path = filedialog.askopenfilename(title='Эталон поплавка',
                                           filetypes=[('Изображения', '*.png *.jpg *.jpeg *.bmp'), ('Все файлы','*.*')])
        if not path:
            return
        if not PIL_AVAILABLE:
            messagebox.showwarning('Pillow', 'Для фотографии поплавка нужен Pillow.')
            return
        try:
            with Image.open(path) as image:
                w, h = image.size
                if w < 16 or h < 16 or w > 500 or h > 500:
                    raise ValueError('Обрежь изображение до 16–500 пикселей по каждой стороне.')
            self.visual_photo.set(path)
            self.visual_photo_status.set(Path(path).name + f' ({w}×{h})')
            self.save_settings()
        except Exception as exc:
            messagebox.showerror('Фотография', str(exc))

    def toggle_pause(self):
        if not self.running:
            self.post('HOTKEY | Pause ignored: monitoring is not running')
            return
        if self.paused.is_set():
            self.paused.clear()
            self.post('HOTKEY | RESUME requested')
        else:
            self.paused.set()
            self.post('HOTKEY | PAUSE requested')

    def start(self):
        if self.running or self.recording:
            return
        mode = self.detection_mode.get()
        if mode != 'Звук' and not PIL_AVAILABLE:
            messagebox.showwarning('Недоступная функция',
                'Распознавание по пикселям требует Pillow. Выбери режим Звук или установи Pillow.')
            return
        if mode != 'Пиксели (эксперимент)' and self.sample is None:
            messagebox.showinfo('Sample', 'Для звукового режима нужен WAV-образец.')
            return
        if mode != 'Звук' and not (0.05 <= float(self.visual_threshold.get()) <= 0.95):
            messagebox.showerror('Пиксели', 'Порог изменения пикселей: от 0.05 до 0.95.')
            return
        if mode != 'Звук' and not self.visual_region and not self.visual_auto_find.get():
            messagebox.showinfo('Пиксели', 'Выдели область поплавка на вкладке Пиксели.')
            return
        if mode != 'Звук' and self.visual_auto_find.get():
            if not (PIL_AVAILABLE and CV2_AVAILABLE):
                messagebox.showwarning('Автопоиск', 'Установи Pillow и opencv-python на вкладке Компоненты.')
                return
            if not Path(self.visual_photo.get()).is_file():
                messagebox.showwarning('Автопоиск', 'Выбери фото поплавка на вкладке Пиксели.')
                return
        try:
            threshold = float(self.threshold.get())
            timeout = int(self.timeout.get())
            timing = dict(threshold=threshold, timeout=timeout,
                          early_bite_filter=bool(self.early_bite_filter.get()),
                          min_bite_seconds=int(self.min_bite_seconds.get()),
                          bite_delay_min=float(self.bite_delay_min.get()),
                          bite_delay_max=float(self.bite_delay_max.get()),
                          recast_delay_min=float(self.recast_delay_min.get()),
                          recast_delay_max=float(self.recast_delay_max.get()),
                          audio_log_interval=float(self.audio_log_interval.get()))
            self.validate_timing(timing)
        except Exception as exc:
            messagebox.showerror('Settings', str(exc))
            return
        self.stop.clear()
        self.paused.clear()
        self.reset_stats()
        self.running = True
        self.start_button.config(state='disabled')
        self.state.set('Starting...')
        local_mode = self.working_mode.get() == 'Локально'
        selected_pid = self.selected_wow_pid()
        if local_mode and self.automation.get() and not find_wow_windows():
            self.running = False
            self.start_button.config(state='normal')
            self.state.set('WoW не найден')
            messagebox.showwarning('Локальный режим', 'Не найдено видимого окна WoW. Запусти игру и нажми «Обновить» в разделе «Подключение».')
            return
        settings = (self.audio_name.get(), threshold, timeout, timing, self.automation.get(),
                    self.key_method.get(), self.audio_diagnostics.get(), float(self.audio_log_interval.get()), self.remote_client.get(),
                    mode, tuple(self.visual_region or ()), float(self.visual_threshold.get()), local_mode, selected_pid,
                    bool(self.visual_auto_find.get()), self.visual_photo.get())
        self.post('INFO | Start clicked; mode=' + self.working_mode.get() + ', target_pid=' + str(selected_pid) + ', client=' + self.remote_client.get() + ', auto_focus=' + str(self.auto_focus_on_start.get()) + ', automation=' + str(settings[4]) + ', device=' + str(settings[0]) + ', threshold=' + str(threshold))
        if self.automation.get() and self.auto_focus_on_start.get():
            try:
                ok, detail = (activate_wow_window(selected_pid) if local_mode else activate_remote_window(self.remote_client.get()))
                self.post(('FOCUS | AUTO-ACTIVATE SUCCESS | ' if ok else 'FOCUS | AUTO-ACTIVATE FAILED | ') + detail)
                if not ok:
                    self.post('FOCUS | Input paused until the selected target window is active.')
            except Exception as exc:
                self.post(f'FOCUS | AUTO-ACTIVATE ERROR | {exc}')
        threading.Thread(target=self.monitor, args=settings, daemon=True).start()

    def halt(self):
        self.stop.set()
        self.paused.clear()
        self.post('INFO | Stop requested')

    def monitor(self, audio, threshold, timeout, timing, auto, method, audio_diagnostics, audio_log_interval, remote_client, mode, region, visual_threshold, local_mode, selected_pid, auto_find=False, visual_photo=''):
        com_ready = False
        try:
            com_ready = initialize_com()
            self.events.put(('log', 'INFO | COM initialized for monitor thread'))
            self.events.put(('log', 'INFO | Monitor thread started; audio=' + audio))
            use_audio = mode != 'Пиксели (эксперимент)'
            use_visual = mode != 'Звук'
            mic = selected_loopback(audio) if use_audio else None
            if mic:
                self.events.put(('log', 'INFO | Loopback selected: ' + mic.name))
            template = self.sample.copy() if use_audio else None
            need = max((len(template)-1)*HOP + FFT, int((len(template)*HOP/SAMPLE_RATE + 1.5)*SAMPLE_RATE)) if use_audio else 0
            buffer = np.zeros(need, np.float32) if use_audio else None
            self.events.put(('log', f'INFO | Detection mode: {mode}; audio_buffer={need/SAMPLE_RATE:.2f}s; ROI={region}; visual_threshold={visual_threshold}'))
            visual_reference = None
            visual_confirm = 0
            visual_last_score = 0.0
            last_audio_event = -100.0
            last_visual_event = -100.0
            confirmation_window = 1.5
            last_rejected_audio_log = -100.0
            last_early_bite_log_at = -100.0
            visual_template = None
            tracked_region = None
            last_locate_at = 0.0
            if use_visual and auto_find:
                visual_template = np.array(Image.open(visual_photo).convert('L'))
                self.events.put(('log', f'VISION | Template loaded: {Path(visual_photo).name}, size={visual_template.shape[1]}×{visual_template.shape[0]}'))
            casts = bites = 0
            phase = 'cast'
            phase_start = time.monotonic()
            arm_at = phase_start + (0.0 if (auto and self.auto_focus_on_start.get() and (foreground_is_wow(selected_pid) if local_mode else foreground_is_remote(remote_client))) else (3.0 if auto else 0.0))
            last_countdown = -1
            focus_was_ok = None
            last_hit = -100.0
            next_action_at = None
            last_phase_heartbeat = 0.0
            audio_last_log = time.monotonic()
            audio_peak = -1.0
            audio_peak_at = None
            audio_peak_age = None
            audio_max_rms = 0.0
            audio_frames_seen = 0
            detected_events = 0
            self.events.put(('log', f"INFO | Timing: reaction {timing['bite_delay_min']:.2f}-{timing['bite_delay_max']:.2f}s; recast pause {timing['recast_delay_min']:.2f}-{timing['recast_delay_max']:.2f}s; timeout {timeout}s; early_bite_filter={timing['early_bite_filter']}; min_bite_seconds={timing['min_bite_seconds']}"))
            context = mic.recorder(samplerate=SAMPLE_RATE, channels=1) if use_audio else nullcontext(None)
            with context as rec:
                self.events.put(('log', 'INFO | ' + ('WASAPI recorder opened; monitoring audio' if use_audio else 'Visual-only mode; no audio device required')))
                while not self.stop.is_set():
                    if emergency_stop_pressed():
                        self.events.put(('log', 'Emergency F8 stop'))
                        break
                    now = time.monotonic()
                    if self.paused.is_set():
                        self.events.put(('state', 'PAUSED — Ctrl+Alt+F7 to resume'))
                        pause_started = time.monotonic()
                        while self.paused.is_set() and not self.stop.is_set():
                            time.sleep(.05)
                        shift = time.monotonic() - pause_started
                        phase_start += shift
                        arm_at += shift
                        if next_action_at is not None:
                            next_action_at += shift
                        if use_audio:
                            buffer.fill(0)
                        visual_reference = None
                        visual_confirm = 0
                        last_audio_event = last_visual_event = -100.0
                        tracked_region = None
                        self.events.put(('log', f'HOTKEY | Resume: timers shifted by {shift:.2f}s'))
                        continue
                    if auto and now < arm_at:
                        seconds = max(1, int(arm_at-now)+1)
                        if seconds != last_countdown:
                            self.events.put(('log', f'INFO | Switching grace period: {seconds}s; activate target window'))
                            last_countdown = seconds
                        self.events.put(('state', f'ARMING IN {seconds}s — focus target window'))
                        time.sleep(.10)
                        continue
                    if auto:
                        focus_ok = (foreground_is_wow(selected_pid) if local_mode else foreground_is_remote(remote_client))
                        if focus_ok != focus_was_ok:
                            self.events.put(('log', 'FOCUS | ' + (f'{"WoW" if local_mode else remote_client_from_title(foreground_title())} active — input enabled' if focus_ok else 'Target window NOT active — input paused. Foreground=' + repr(foreground_title()))))
                            focus_was_ok = focus_ok
                        if not focus_ok:
                            self.events.put(('state', 'PAUSED — focus target game / remote window'))
                            time.sleep(.15)
                            continue
                    if auto and phase == 'cast':
                        down, up = send_space(method)
                        self.events.put(('log', f'INPUT | {method} down={down} up={up}; focus={foreground_title()!r}'))
                        casts += 1
                        phase = 'wait'
                        phase_start = time.monotonic()
                        last_early_bite_log_at = -100.0
                        # Ignore stale audio from the preceding cast.
                        if use_audio:
                            buffer.fill(0)
                        visual_reference = None
                        visual_confirm = 0
                        last_audio_event = last_visual_event = -100.0
                        tracked_region = None
                        last_locate_at = 0.0
                        self.events.put(('log', f'CAST | Cast #{casts} SPACE; fishing window {timeout}s started; deadline in {timeout}s'))
                    audio_score, audio_age, data = -1.0, 0.0, np.zeros(1, np.float32)
                    if use_audio:
                        data = rec.record(numframes=HOP*8).reshape(-1).astype(np.float32)
                        if not len(data):
                            continue
                        if len(data) >= len(buffer):
                            buffer[:] = data[-len(buffer):]
                        else:
                            buffer[:-len(data)] = buffer[len(data):]
                            buffer[-len(data):] = data
                        audio_score, audio_age = sliding_splash_similarity(buffer, template)
                    else:
                        time.sleep(.12)
                    now = time.monotonic()
                    visual_score = -1.0
                    if use_visual and phase == 'wait' and now - phase_start > 2.5:
                        try:
                            active_region = region
                            if auto_find and visual_template is not None:
                                if tracked_region is None or now-last_locate_at >= 2.0:
                                    bbox, match_confidence = locate_bobber(visual_template)
                                    last_locate_at = now
                                    if bbox:
                                        new_region = expanded_tracking_region(bbox)
                                        if tracked_region != new_region:
                                            self.events.put(('log', f'VISION | Bobber located: match={match_confidence:.3f}; bbox={bbox}; tracking={new_region}'))
                                            visual_reference = None
                                            visual_confirm = 0
                                        tracked_region = new_region
                                    elif tracked_region is None:
                                        self.events.put(('state', f'BOBBER SEARCH | confidence {match_confidence:.3f}'))
                                active_region = tracked_region
                            if active_region is None:
                                visual_score = -1.0
                            else:
                                frame = crop_visual_frame(active_region)
                                if visual_reference is None:
                                    visual_reference = frame
                                    self.events.put(('log', 'VISUAL | Baseline captured after cast'))
                                else:
                                    visual_score = visual_change_score(frame, visual_reference)
                                    if visual_score >= visual_threshold:
                                        visual_confirm += 1
                                    else:
                                        visual_confirm = 0
                                        # Adapt slowly to small natural water movements.
                                        visual_reference = .96 * visual_reference + .04 * frame
                                    if visual_confirm == 2:
                                        last_visual_event = now
                                        self.events.put(('log', f'VISUAL | change={visual_score:.3f} >= {visual_threshold:.3f}; consecutive=2'))
                        except Exception as exc:
                            self.events.put(('log', f'VISUAL | Screenshot failed: {exc}'))
                            visual_score = -1.0
                    # Confirm visual change in consecutive frames; audio uses original matcher.
                    visual_detect = use_visual and visual_confirm >= 2
                    audio_detect = use_audio and audio_score >= threshold and audio_age <= .40
                    if mode == 'Звук + свой поплавок (строго)':
                        if phase == 'wait' and audio_detect:
                            last_audio_event = now - max(0.0, audio_age)
                            if now - last_rejected_audio_log > 1.5:
                                self.events.put(('log', f'AUDIO | SPLASH CANDIDATE score={audio_score:.3f}; awaiting own-bobber visual confirmation'))
                                last_rejected_audio_log = now
                        # Require visual confirmation near the sound, in the tracked region.
                        detected = (phase == 'wait' and tracked_region is not None if auto_find else phase == 'wait' and bool(region)) and (
                            abs(last_audio_event - last_visual_event) <= confirmation_window
                            and now - max(last_audio_event, last_visual_event) <= confirmation_window
                            and last_audio_event > phase_start and last_visual_event > phase_start
                        )
                        if detected:
                            self.events.put(('log', f'CONFIRM | OWN BOBBER: audio+visual within {abs(last_audio_event-last_visual_event):.2f}s; region={tracked_region if auto_find else region}'))
                            visual_confirm = 0
                    else:
                        detected = audio_detect or visual_detect
                    score = max(audio_score if use_audio else -1.0, visual_score if use_visual else -1.0)
                    match_age = audio_age if audio_detect else (0.0 if visual_detect else audio_age)
                    if score < 0:
                        score = 0.0  # No candidate in visual-only pause/cast phase; timers still advance.
                    self.events.put(('score', f'Audio: {audio_score:.3f}/{threshold:.3f} | Pixels: {visual_score:.3f}/{visual_threshold:.3f}' if use_visual else f'Splash similarity: {score:.3f} / {threshold:.3f} | segment age {match_age:.2f}s'))
                    now = time.monotonic()
                    audio_frames_seen += 1
                    rms = float(np.sqrt(np.mean(data * data)))
                    audio_max_rms = max(audio_max_rms, rms)
                    if score > audio_peak:
                        audio_peak = score
                        audio_peak_at = now
                        audio_peak_age = match_age
                    if use_visual and now - audio_last_log >= audio_log_interval:
                        self.events.put(('log', f'VISUAL | score={visual_score:.3f} threshold={visual_threshold:.3f} phase={phase}'))
                    if audio_diagnostics and use_audio and now - audio_last_log >= audio_log_interval:
                        is_silent = audio_max_rms < 0.0001
                        self.events.put(('log',
                            f'AUDIO | peak={audio_peak:.3f} threshold={threshold:.3f} '
                            f'gap={threshold-audio_peak:+.3f} match_age={audio_peak_age if audio_peak_age is not None else -1:.2f}s '
                            f'rms_max={audio_max_rms:.5f} '
                            f'frames={audio_frames_seen} phase={phase} '
                            f'{"SILENT" if is_silent else "signal=OK"}'))
                        if audio_peak >= max(0.50, threshold-0.25):
                            self.events.put(('log',
                                f'CANDIDATE | best_score={audio_peak:.3f}, threshold={threshold:.3f}, '
                                f'best_age={audio_peak_age if audio_peak_age is not None else -1:.2f}s, '
                                f'phase={phase}; not necessarily a bite'))
                        audio_last_log = now
                        audio_peak = -1.0
                        audio_peak_at = None
                        audio_peak_age = None
                        audio_max_rms = 0.0
                        audio_frames_seen = 0
                    if not use_audio and now - audio_last_log >= audio_log_interval:
                        audio_last_log = now
                    # A match that STARTED before the configured minimum is discarded,
                    # not delayed until the gate opens. In mixed modes, require a fresh
                    # audio event and (for strict confirmation) both fresh signals.
                    if phase == 'wait' and detected and timing['early_bite_filter']:
                        min_boundary = phase_start + timing['min_bite_seconds']
                        match_started_at = now - max(0.0, audio_age) if audio_detect else now
                        is_early = (now < min_boundary or match_started_at < min_boundary)
                        if mode == 'Звук + свой поплавок (строго)':
                            is_early = is_early or (
                                last_audio_event < min_boundary or last_visual_event < min_boundary
                            )
                        if is_early:
                            if now - last_early_bite_log_at > 1.0:
                                self.events.put(('log',
                                    f'BITE_IGNORED | Early splash; elapsed={now-phase_start:.2f}s; '
                                    f'minimum={timing["min_bite_seconds"]}s; score={score:.3f}; '
                                    f'no SPACE scheduled'))
                                last_early_bite_log_at = now
                            detected = False
                            # Do not carry a strict-mode confirmation into later time.
                            if mode == 'Звук + свой поплавок (строго)':
                                last_audio_event = last_visual_event = -100.0
                    if detected and mode == 'Звук + свой поплавок (строго)':
                        last_audio_event = last_visual_event = -100.0
                    if auto:
                        if phase == 'wait' and now-phase_start >= timeout:
                            delay = random.uniform(timing['recast_delay_min'], timing['recast_delay_max'])
                            phase = 'recast_pause'
                            next_action_at = now + delay
                            self.events.put(('log', f'TIMER | No bite during {timeout}s fishing window; next cast in {delay:.2f}s'))
                        elif phase == 'wait' and now-phase_start > 1.5 and detected and now-last_hit > 2:
                            delay = random.uniform(timing['bite_delay_min'], timing['bite_delay_max'])
                            phase = 'reaction_delay'
                            next_action_at = now + delay
                            detected_events += 1
                            self.events.put(('log',
                                f'DETECT | BITE #{detected_events} CONFIRMED; '
                                f'mode={"audio+own_bobber" if mode == "Звук + свой поплавок (строго)" else ("visual" if visual_detect and not audio_detect else "audio")}; score={score:.3f}; threshold={visual_threshold if visual_detect and not audio_detect else threshold:.3f}; match_age={match_age:.2f}s; '
                                f'elapsed={now-phase_start:.2f}s; '
                                f'SPACE scheduled in {delay:.2f}s'))
                            self.events.put(('state', f'BITE DETECTED ({score:.3f}) — reaction in {delay:.2f}s'))
                        elif phase == 'reaction_delay' and now >= next_action_at:
                            down, up = send_space(method)
                            self.events.put(('log', f'INPUT | Bite SPACE: {method} down={down} up={up}; focus={foreground_title()!r}'))
                            bites += 1
                            last_hit = now
                            delay = random.uniform(timing['recast_delay_min'], timing['recast_delay_max'])
                            phase = 'recast_pause'
                            next_action_at = now + delay
                            self.events.put(('log', f'BITE | Bite #{bites} SPACE; next cast in {delay:.2f}s'))
                        elif phase == 'recast_pause' and now >= next_action_at:
                            phase = 'cast'
                            next_action_at = None
                        if phase in ('reaction_delay', 'recast_pause') and now-last_phase_heartbeat > 1:
                            self.events.put(('state', f'{phase.upper()} | next action in {max(0.0,next_action_at-now):.1f}s'))
                            last_phase_heartbeat = now
                    elif detected and now-last_hit > 2:
                        last_hit = now
                        bites += 1
                        self.events.put(('log',
                            f'DETECT | BITE DETECTED (detector-only): '
                            f'audio_score={audio_score:.3f}; visual_score={visual_score:.3f}; no key sent'))
                    if not (auto and phase in ('reaction_delay', 'recast_pause')):
                        self.events.put(('state', f'{phase.upper()} | {max(0.0, timeout-(now-phase_start)):.1f}s left' if auto and phase == 'wait' else f'{phase.upper()} | {"auto" if auto else "detector only"}'))
                    self.events.put(('stats', (casts, bites)))
        except Exception as exc:
            self.events.put(('log', f'ERROR | {type(exc).__name__}: {exc}\n{traceback.format_exc()}'))
        finally:
            if com_ready:
                ctypes.windll.ole32.CoUninitialize()
            self.events.put(('log', 'INFO | Monitor thread finished'))
            self.events.put(('finished', None))

    def write_log(self, message):
        line = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3] + ' ' + message
        self.log_rows.append(line)
        try:
            with LOG_PATH.open('a', encoding='utf-8') as f:
                f.write(line + '\n')
        except OSError:
            pass
        self.log.config(state='normal')
        self.log.insert('end', line + '\n')
        self.log.see('end')
        self.log.config(state='disabled')
        self.refresh_log_window()

    def open_log_folder(self):
        import os
        os.startfile(str(LOG_DIR))

    def open_log_viewer(self):
        if self.log_window is not None and self.log_window.winfo_exists():
            self.log_window.lift()
            return
        win = tk.Toplevel(self.root)
        self.log_window = win
        win.title('eGoR Fishing — diagnostic log viewer')
        win.geometry('1000x570')
        bar = ttk.Frame(win, padding=8)
        bar.pack(fill='x')
        ttk.Label(bar, text='Filter:').pack(side='left')
        for name in ('ALL', 'DETECT', 'AUDIO', 'TIMER', 'ERROR', 'FOCUS', 'INPUT', 'INFO'):
            ttk.Radiobutton(bar, text=name, variable=self.log_filter, value=name,
                            command=self.refresh_log_window).pack(side='left', padx=4)
        ttk.Button(bar, text='Copy visible', command=self.copy_visible_log).pack(side='right', padx=4)
        ttk.Button(bar, text='Open log folder', command=self.open_log_folder).pack(side='right')
        area = ttk.Frame(win)
        area.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        self.log_view = tk.Text(area, wrap='none', font=('Consolas', 10), state='disabled')
        vertical = ttk.Scrollbar(area, orient='vertical', command=self.log_view.yview)
        horizontal = ttk.Scrollbar(area, orient='horizontal', command=self.log_view.xview)
        self.log_view.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.log_view.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        area.rowconfigure(0, weight=1)
        area.columnconfigure(0, weight=1)
        win.protocol('WM_DELETE_WINDOW', self.close_log_window)
        self.refresh_log_window()

    def close_log_window(self):
        if self.log_window is not None:
            self.log_window.destroy()
        self.log_window = None
        self.log_view = None

    def visible_log(self):
        selection = self.log_filter.get()
        if selection == 'ALL':
            return '\n'.join(self.log_rows)
        return '\n'.join(x for x in self.log_rows if selection + ' |' in x)

    def copy_visible_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.visible_log())

    def refresh_log_window(self):
        if self.log_view is None or not self.log_view.winfo_exists():
            return
        self.log_view.config(state='normal')
        self.log_view.delete('1.0', 'end')
        self.log_view.insert('1.0', self.visible_log())
        self.log_view.see('end')
        self.log_view.config(state='disabled')

    def post(self, msg):
        self.events.put(('log', msg))

    def poll(self):
        try:
            while True:
                action, value = self.events.get_nowait()
                if action == 'tray_show':
                    self.show_from_tray()
                elif action == 'tray_start':
                    self.start() if not self.running else (self.toggle_pause() if self.paused.is_set() else None)
                elif action == 'tray_pause':
                    self.toggle_pause()
                elif action == 'tray_stop':
                    self.halt()
                elif action == 'tray_exit':
                    self.close()
                elif action == 'hotkey':
                    if value == 1:
                        if not self.running:
                            self.start()
                        elif self.paused.is_set():
                            self.toggle_pause()
                    elif value == 2:
                        self.toggle_pause()
                    elif value == 3:
                        self.halt()
                elif action == 'score':
                    self.score.set(value)
                elif action == 'state':
                    self.state.set(value)
                elif action == 'stats':
                    self.session_casts, self.session_bites = value
                    self.estimated_catches = max(0, self.session_bites + self.manual_catch_delta)
                    self.update_stats()
                elif action == 'sample':
                    try:
                        self.set_sample(Path(value))
                    except Exception as exc:
                        self.post(str(exc))
                elif action == 'log':
                    self.write_log(str(value))
                elif action == 'install_result':
                    success, detail = value
                    self.post('DEPENDENCY | Install ' + ('successful' if success else 'failed') + ' | ' + detail[-500:])
                    if success:
                        messagebox.showinfo('Компоненты', 'Пакеты установлены. Перезапусти Nobrain Companion, чтобы активировать новые функции.')
                    else:
                        messagebox.showerror('Компоненты', 'Установка не удалась. Детали смотри в журнале.\n' + detail[-700:])
                    self.check_components()
                elif action == 'record_finished':
                    self.recording = False
                    self.capture_button.config(state='disabled')
                    self.record_button.config(text='Listen for splash (up to 90 s)')
                    self.start_button.config(state='normal')
                elif action == 'finished':
                    self.running = False
                    self.start_button.config(state='normal')
                    self.state.set('Stopped')
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def close(self):
        if self._closing:
            return
        self._closing = True
        if self.root.state() == 'normal':
            self.saved_geometry = self.root.geometry()
        self.save_settings()
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        self.stop.set()
        self.record_stop.set()
        if self.hotkey_listener:
            self.hotkey_listener.close()
        self.root.destroy()


if __name__ == '__main__':
    def _fatal_error(exc_type, exc_value, exc_tb):
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with (LOG_DIR / 'fatal-error.log').open('a', encoding='utf-8') as fh:
                fh.write(''.join(traceback.format_exception(exc_type, exc_value, exc_tb)) + '\n')
            ctypes.windll.user32.MessageBoxW(None,
                'Ошибка Nobrain Companion. Подробности: logs/fatal-error.log',
                'Nobrain Companion', 0x10)
        except Exception:
            pass
    sys.excepthook = _fatal_error
    if not hasattr(ctypes, 'windll'):
        raise SystemExit('This tool requires Windows')
    root = tk.Tk()
    ttk.Style(root).theme_use('clam')
    App(root)
    root.mainloop()