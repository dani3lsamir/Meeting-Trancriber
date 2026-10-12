"""GUI smoke test (offscreen): the window builds, lists devices, records and shows the result."""
import time

import pytest
from fakes import FakeModel

pytest.importorskip('PySide6')


def wait_for(app, cond, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_gui_record_and_transcribe(fake_sc, monkeypatch, tmp_path):
    import app.session as session
    import main

    monkeypatch.setattr(session, 'load_model', lambda name, threads, status: FakeModel())
    QApplication, Window = main.build_gui()
    app = QApplication.instance() or QApplication([])
    w = Window()
    w.show()
    assert wait_for(app, lambda: w.mics.count() == 1 and w.outputs.count() == 2)
    assert w.start_btn.isEnabled() and not w.stop_btn.isEnabled()
    w.start_btn.click()
    assert wait_for(app, lambda: w.stop_btn.isEnabled())
    wait_for(app, lambda: w.mic_level.value() > 0, 3)
    assert w.mic_level.value() > 0 and w.out_level.value() > 0
    wait_for(app, lambda: False, 2.0)  # record ~2 s
    w.stop_btn.click()
    assert wait_for(app, lambda: w.session is None, 30)
    assert 'Done' in w.status.text(), w.status.text()
    text = w.transcript.toPlainText()
    assert 'You: good morning everyone' in text and 'Meeting: thanks for joining the call' in text, text
    assert (w.folder / 'transcript.md').exists()
    w.close()
