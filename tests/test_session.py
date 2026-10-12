import json
import threading
import time

from fakes import FakeModel

from app.session import Session, SessionCallbacks, SessionConfig


class Recorder:
    def __init__(self):
        self.events = []
        self.done = threading.Event()
        self.started = threading.Event()

    def callbacks(self):
        def add(kind):
            def f(*a):
                self.events.append((kind, *a))
                if kind == 'started':
                    self.started.set()
                if kind in ('finished', 'failed'):
                    self.done.set()
            return f
        return SessionCallbacks(**{k: add(k) for k in ('status', 'warning', 'live_row', 'started', 'progress',
                                                       'finished', 'failed')})

    def of(self, kind):
        return [e[1:] for e in self.events if e[0] == kind]


def make(tmp_path, rec, loader=None, **kw):
    cfg = SessionConfig(folder=tmp_path / 'meeting', mic_id='mic-1', output_id='spk-1', language='en', **kw)
    return Session(cfg, rec.callbacks(), model_loader=loader or (lambda name, threads, status: FakeModel()))


def test_full_session(tmp_path, fake_sc):
    rec = Recorder()
    s = make(tmp_path, rec)
    s.start()
    assert rec.started.wait(5)
    time.sleep(9.5)   # > one live window
    assert s.levels['you'] > 0 and s.levels['meeting'] > 0
    s.request_stop()
    assert rec.done.wait(30)
    (result,) = rec.of('finished')[0]
    folder = tmp_path / 'meeting'
    assert not result['cancelled']
    heard = {(r['speaker'], r['text']) for r in result['rows']}
    assert heard == {('you', 'good morning everyone'), ('meeting', 'thanks for joining the call')}
    assert len(rec.of('live_row')) > 0
    for name in ('you.wav', 'meeting.wav', 'transcript.txt', 'transcript.md', 'transcript.json',
                 'live_transcript.txt', 'session.json'):
        assert (folder / name).is_file(), name
    meta = json.loads((folder / 'session.json').read_text(encoding='utf-8'))
    assert 9 < meta['duration'] < 12
    assert rec.of('progress')[-1] == (1.0,)


def test_start_failure_reports_and_does_not_hang(tmp_path, fake_sc):
    fake_sc.mic.present = False
    rec = Recorder()
    s = make(tmp_path, rec)
    s.start()
    assert rec.done.wait(15)
    assert 'Microphone' in rec.of('failed')[0][0]
    assert not rec.of('started')


def test_final_model_failure_keeps_recordings(tmp_path, fake_sc):
    calls = []

    def loader(name, threads, status):
        calls.append(name)
        if name == 'small':
            raise RuntimeError('model not installed')
        return FakeModel()

    rec = Recorder()
    s = make(tmp_path, rec, loader=loader)
    s.start()
    assert rec.started.wait(5)
    time.sleep(1.5)
    s.request_stop()
    assert rec.done.wait(15)
    msg = rec.of('failed')[0][0]
    assert 'model not installed' in msg and str(tmp_path / 'meeting') in msg
    assert (tmp_path / 'meeting' / 'you.wav').stat().st_size > 48000


def test_cancel_skips_final_pass(tmp_path, fake_sc):
    rec = Recorder()
    s = make(tmp_path, rec, live_enabled=False)
    s.start()
    assert rec.started.wait(5)
    time.sleep(1.5)
    s.shutdown(timeout=10)
    assert not s.active
    (result,) = rec.of('finished')[0]
    assert result['cancelled'] and (tmp_path / 'meeting' / 'meeting.wav').exists()
    assert not (tmp_path / 'meeting' / 'transcript.txt').exists()


def test_device_loss_mid_meeting_does_not_end_session(tmp_path, fake_sc):
    fake_sc.loop.fail_after = 5
    rec = Recorder()
    s = make(tmp_path, rec, live_enabled=False)
    s.start()
    assert rec.started.wait(5)
    time.sleep(2.5)
    assert s.state == 'recording'
    s.request_stop()
    assert rec.done.wait(15)
    assert rec.of('finished')
    assert any('reconnected' in w[0] for w in rec.of('warning'))
