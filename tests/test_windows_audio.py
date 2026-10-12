"""Real WASAPI checks; only run on Windows (CI runs them on windows-latest)."""
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Windows only')
ROOT = Path(__file__).resolve().parent.parent

# Regression: soundcard's import-time CoInitializeEx returns S_FALSE on a thread
# that already initialised COM and soundcard raised "Error 0x100000001". The
# GUI enumerates devices from a COM-initialised worker thread first, exactly like this.
SCRIPT = '''
import sys
from PySide6.QtWidgets import QApplication
app = QApplication([])          # Qt puts the main thread in an STA, like the real app
from app.audio import list_devices, soundcard
mics, outputs, dmic, dout = list_devices()
print('OK', len(mics), len(outputs))
'''


def test_device_enumeration_in_fresh_process_after_qt():
    r = subprocess.run([sys.executable, '-c', SCRIPT], cwd=ROOT, capture_output=True, text=True, timeout=120,
                       env={**__import__('os').environ, 'QT_QPA_PLATFORM': 'offscreen'})
    assert r.returncode == 0 and 'OK' in r.stdout, r.stdout + r.stderr


def test_explain_access_denied():
    from app.audio import explain
    assert 'Privacy' in explain(RuntimeError('Error 0x80070005'))
