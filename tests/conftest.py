import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv('MEETINGTRANSCRIBER_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('MEETINGTRANSCRIBER_OFFLINE', '1')
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    yield


@pytest.fixture
def fake_sc(monkeypatch):
    from fakes import FakeSoundcard

    import app.audio as audio
    sc = FakeSoundcard()
    monkeypatch.setattr(audio, '_sc', sc)
    return sc
