# -*- mode: python ; coding: utf-8 -*-
# Build on Windows with: pyinstaller --noconfirm --clean NobrainFishing.spec
from pathlib import Path
root = Path(SPECPATH)
datas = [
    (str(root / 'assets' / 'nobrain.ico'), 'assets'),
    (str(root / 'assets' / 'nobrain.png'), 'assets'),
    (str(root / 'bite_sample.wav'), '.'),
    (str(root / 'samples' / 'bite.wav'), 'samples'),
]
a = Analysis(
    ['remote_fishing.py'], pathex=[str(root)], binaries=[], datas=datas,
    hiddenimports=['soundcard', 'soundcard.mediafoundation', 'psutil',
                   'scipy.signal', 'PIL.Image', 'PIL.ImageGrab',
                   'PIL.ImageTk', 'pystray', 'cv2'],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True,
          name='NobrainFishing', debug=False, strip=False, upx=False,
          console=False, icon=str(root / 'assets' / 'nobrain.ico'))
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas,
               strip=False, upx=False, name='NobrainFishing')
