import threading
import time
import wave

import numpy as np
from fakes import FakeModel, tone

import app.transcribe as tr
from app.audio import WavWriter
from app.transcribe import LiveTranscriber, export, final_transcription, remove_echo, stamp, to_16k


def test_stamp():
    assert stamp(0) == '00:00:00'
    assert stamp(3725.9) == '01:02:05'
    assert stamp(-3) == '00:00:00'


def test_resampler_keeps_speech_band_and_rejects_aliasing():
    low = to_16k(tone(1.0, 1000, 0.5))
    high = to_16k(tone(1.0, 12000, 0.5))   # would alias to 4 kHz without filtering
    assert len(low) == 16000
    assert 0.3 < np.abs(low[100:-100]).max() < 0.55
    assert np.abs(high[100:-100]).max() < 0.02


def speech_like(seconds_on, seconds_off, repeats, freq=500):
    parts = []
    for _ in range(repeats):
        parts += [tone(seconds_on, freq), np.zeros(int(seconds_off * 48000), np.float32)]
    return np.concatenate(parts)


def test_live_transcriber_offsets_and_file(tmp_path):
    rows, warnings = [], []
    model = FakeModel()
    live = LiveTranscriber(lambda: model, 'auto', tmp_path / 'live.txt', rows.append, warnings.append)
    live.start()
    you = speech_like(2.0, 1.0, 8)                    # 24 s
    meeting = np.concatenate([np.zeros(48000 * 10, np.float32), speech_like(2.0, 1.0, 4)])
    for i in range(0, len(you), 4800):
        live.feed('you', you[i:i + 4800])
    for i in range(0, len(meeting), 4800):
        live.feed('meeting', meeting[i:i + 4800])
    deadline = time.time() + 10
    while time.time() < deadline and (not live.queue.empty() or any(v >= 8 * 48000 for v in live.buffered.values())):
        time.sleep(0.05)
    live.stop()
    live.join(5)
    assert not warnings
    you_starts = sorted(r['start'] for r in rows if r['speaker'] == 'you')
    meet_starts = sorted(r['start'] for r in rows if r['speaker'] == 'meeting')
    # Tones start every 3 s; cuts happen in silent gaps so each tone is reported once, at the right time.
    assert np.allclose(you_starts[:5], [0.0, 3.0, 6.0, 9.0, 12.0], atol=0.15)
    assert meet_starts and meet_starts[0] >= 9.8
    assert len(you_starts) == len(set(you_starts))
    assert (tmp_path / 'live.txt').read_text(encoding='utf-8').count('You:') == len(you_starts)


def test_live_transcriber_survives_model_load_failure(tmp_path):
    warnings = []

    def broken():
        raise RuntimeError('model missing')

    live = LiveTranscriber(broken, 'en', tmp_path / 'live.txt', lambda r: None, warnings.append)
    live.start()
    live.feed('you', np.zeros(48000, np.float32))
    live.ready.wait(5)
    live.stop()
    live.join(5)
    assert 'Live preview unavailable' in warnings[0]


def test_live_backlog_skips_ahead_with_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, 'LIVE_MAX_BACKLOG', 20.0)
    warnings, rows = [], []
    live = LiveTranscriber(lambda: FakeModel(), 'en', tmp_path / 'l.txt', rows.append, warnings.append)
    live.buffers['you'] = [speech_like(2.0, 1.0, 10)]   # 30 s queued
    live.buffered['you'] = 30 * 48000
    clip, offset = live._next_clip('you')
    assert offset == 22.0 and len(clip) <= 8 * 48000
    assert 'cannot keep up' in warnings[0]


def test_remove_echo():
    rows = [
        {'speaker': 'meeting', 'start': 10.0, 'end': 14.0, 'text': 'Can everyone see my screen now?'},
        {'speaker': 'you', 'start': 10.3, 'end': 14.2, 'text': 'can everyone see my screen now'},
        {'speaker': 'you', 'start': 15.0, 'end': 16.0, 'text': 'Yes, I can see it.'},
        {'speaker': 'you', 'start': 40.0, 'end': 44.0, 'text': 'Can everyone see my screen now?'},
    ]
    kept, dropped = remove_echo(rows)
    assert dropped == 1
    assert [r['start'] for r in kept] == [10.0, 15.0, 40.0]


def write_wav(path, audio, channels):
    w = WavWriter(path, channels)
    w.write(audio if channels == 1 else np.stack([audio] * channels, axis=1))
    w.close()


def test_final_pass_and_export(tmp_path):
    you = speech_like(2.0, 2.0, 3)                                              # speech at 0, 4, 8 s
    meeting = np.concatenate([np.zeros(48000 * 13, np.float32), tone(2.0, 700)])  # speech at 13 s
    write_wav(tmp_path / 'you.wav', you, 1)
    write_wav(tmp_path / 'meeting.wav', meeting, 2)
    progress = []
    rows, info = final_transcription(FakeModel('hello'), [('you', tmp_path / 'you.wav'), ('meeting', tmp_path / 'meeting.wav')],
                                     'en', threading.Event(), progress.append)
    assert [(r['speaker'], round(r['start'])) for r in rows] == [('you', 0), ('you', 4), ('you', 8), ('meeting', 13)]
    assert progress[-1] == 1.0
    files = export(tmp_path, rows, {'title': 't', 'duration': 15, 'final_model': 'small', 'language': 'en'})
    txt = (tmp_path / 'transcript.txt').read_text(encoding='utf-8')
    md = (tmp_path / 'transcript.md').read_text(encoding='utf-8')
    assert '[00:00:13] Meeting: hello' in txt and '[00:00:04] You: hello' in txt
    assert '**[00:00:13] Meeting:** hello' in md
    assert all(p.exists() for p in files)
    with wave.open(str(tmp_path / 'you.wav')) as w:  # originals untouched
        assert w.getnframes() == len(you)


def test_final_pass_cancel(tmp_path):
    write_wav(tmp_path / 'you.wav', speech_like(1.0, 1.0, 3), 1)
    cancel = threading.Event()
    cancel.set()
    try:
        final_transcription(FakeModel(), [('you', tmp_path / 'you.wav')], 'en', cancel)
    except tr.Cancelled:
        return
    raise AssertionError('not cancelled')
