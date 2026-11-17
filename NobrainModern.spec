# Build: python -m PyInstaller --noconfirm --clean NobrainModern.spec
from pathlib import Path
root = Path(SPECPATH)
datas = [(str(root / 'desktop_web'), 'desktop_web'),
         (str(root / 'assets'), 'assets'), (str(root / 'samples'), 'samples'),
         (str(root / 'bite_sample.wav'), '.')]
a = Analysis(['modern_desktop.py'], pathex=[str(root)], binaries=[], datas=datas,
    hiddenimports=['soundcard', 'soundcard.mediafoundation', 'psutil', 'scipy.signal',
                   'PIL.Image', 'PIL.ImageGrab', 'PIL.ImageTk', 'pystray', 'cv2'],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='NobrainModern',
          debug=False, strip=False, upx=False, console=False,
          icon=str(root / 'assets' / 'nobrain.ico'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='NobrainModern')

