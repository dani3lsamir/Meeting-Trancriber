# PyInstaller spec for MeetingTranscriber (Windows, one-folder build).
#   pyinstaller --noconfirm --clean MeetingTranscriber.spec
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_dynamic_libs

datas, binaries, hiddenimports = [], [], []
# Packages with native DLLs / data files that static analysis misses:
#   ctranslate2  - inference engine DLLs (ctranslate2.dll, libiomp5md.dll)
#   faster_whisper - silero VAD .onnx asset
#   onnxruntime  - runs the VAD model
#   av           - FFmpeg DLLs used to decode the WAV files for the final pass
#   tokenizers   - Rust extension
#   soundcard    - CFFI header files (mediafoundation.py.h) parsed at import time
for pkg in ('ctranslate2', 'faster_whisper', 'onnxruntime', 'av', 'tokenizers', 'soundcard'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
hiddenimports += ['soundcard.mediafoundation', '_cffi_backend']
datas += [('assets/selftest_jfk.flac', 'assets')]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'scipy', 'pandas', 'torch', 'tensorflow', 'IPython', 'pytest',
              'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.Qt3DCore', 'PySide6.QtQuick',
              'PySide6.QtQml', 'PySide6.QtMultimedia', 'PySide6.QtCharts', 'PySide6.QtDataVisualization',
              'PySide6.QtPdf', 'PySide6.QtSql', 'PySide6.QtTest'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='MeetingTranscriber',
    debug=False,
    strip=False,
    upx=False,          # UPX-compressed DLLs trigger antivirus false positives and break some Qt DLLs
    console=False,
    version=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='MeetingTranscriber')
