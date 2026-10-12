import time
import wave

import numpy as np
import pytest

import app.audio as audio
from app.audio import AudioCapture, RecordingWriteError, WavWriter, fit_channels, list_devices


def wav_info(path):
    with wave.open(str(path), 'rb') as w:
        return w.getnchannels(), w.getnframes() / w.getframerate()


def test_wav_header_valid_while_file_is_open(tmp_path):
    w = WavWriter(tmp_path / 'a.wav', 1)
    w.write(np.full(48000, 0.5, np.float32))
    w._fh.flush()
    # Simulates a crash: file never closed, but the header already describes 1 s of audio.
    assert wav_info(tmp_path / 'a.wav') == (1, 1.0)
    w.write(np.full((24000, 1), 2.0, np.float32))  # clipped, not wrapped
    w.close()
    with wave.open(str(tmp_path / 'a.wav'), 'rb') as r:
        r.setpos(48000)
        data = np.frombuffer(r.readframes(10), '<i2')
    assert (data == 32767).all()


def test_wav_size_limit_is_a_clean_error(tmp_path, monkeypatch):
    monkeypatch.setattr(audio, 'MAX_WAV_BYTES', 1000)
    w = WavWriter(tmp_path / 'a.wav', 2)
    with pytest.raises(RecordingWriteError):
        w.write(np.zeros((1000, 2), np.float32))
    w.close()


def test_fit_channels():
    assert fit_channels(np.ones(5), 2).shape == (5, 2)
    six = np.tile(np.arange(6, dtype=np.float32), (4, 1))
    assert np.allclose(fit_channels(six, 2), 2.5)
    assert fit_channels(six, 1).shape == (4, 1)


def test_list_devices(fake_sc):
    mics, outputs, dmic, dout = list_devices()
    assert mics == [('mic-1', 'USB Microphone')]
    assert [o[0] for o in outputs] == ['spk-1', 'spk-2']
    assert (dmic, dout) == ('mic-1', 'spk-1')


def run_capture(tmp_path, seconds=1.5, **kw):
    blocks = {'you': 0, 'meeting': 0}
    warnings = []

    def sink(source, block):
        blocks[source] += len(block)

    cap = AudioCapture(tmp_path / 'm', 'mic-1', 'spk-1', sink=sink, on_warning=warnings.append, **kw)
    cap.start()
    time.sleep(seconds)
    levels = cap.levels
    cap.stop()
    return cap, blocks, warnings, levels


def test_simultaneous_mic_and_loopback(tmp_path, fake_sc):
    cap, blocks, warnings, levels = run_capture(tmp_path)
    ch_you, d_you = wav_info(tmp_path / 'm' / 'you.wav')
    ch_meet, d_meet = wav_info(tmp_path / 'm' / 'meeting.wav')
    assert (ch_you, ch_meet) == (1, 2)
    assert 1.2 <= d_you <= 2.0 and 1.2 <= d_meet <= 2.0
    assert abs(d_you - d_meet) < 0.35
    assert levels['you'] > 0.1 and levels['meeting'] > 0.1
    assert blocks['you'] > 0 and blocks['meeting'] > 0
    assert not warnings and not cap.failed
    assert fake_sc.loop.opens == 1 and fake_sc.mic.opens == 1


def test_late_starting_device_is_padded_to_shared_clock(tmp_path, fake_sc):
    fake_sc.loop.open_delay = 0.8
    run_capture(tmp_path, seconds=1.5)
    _, d_you = wav_info(tmp_path / 'm' / 'you.wav')
    _, d_meet = wav_info(tmp_path / 'm' / 'meeting.wav')
    assert abs(d_you - d_meet) < 0.35
    with wave.open(str(tmp_path / 'm' / 'meeting.wav'), 'rb') as r:
        head = np.frombuffer(r.readframes(int(0.5 * 48000)), '<i2')
    assert np.abs(head).max() == 0  # leading silence inserted while device was opening


def test_device_invalidated_mid_meeting_reconnects(tmp_path, fake_sc):
    fake_sc.loop.fail_after = 5  # e.g. Bluetooth headset switching to hands-free profile
    cap, _, warnings, _ = run_capture(tmp_path, seconds=2.5)
    assert fake_sc.loop.opens == 2
    assert any('interrupted' in w for w in warnings)
    assert any('reconnected' in w for w in warnings)
    _, d_you = wav_info(tmp_path / 'm' / 'you.wav')
    _, d_meet = wav_info(tmp_path / 'm' / 'meeting.wav')
    assert abs(d_you - d_meet) < 0.35  # gap filled with silence, timeline intact
    assert not cap.failed


def test_unplugged_output_falls_back_to_windows_default(tmp_path, fake_sc):
    fake_sc.loop.fail_after = 3
    fake_sc.loop.vanish_on_fail = True
    cap, _, warnings, _ = run_capture(tmp_path, seconds=2.5)
    assert fake_sc.loop2.opens == 1
    assert any('Laptop speakers' in w for w in warnings)
    assert not cap.failed


def test_missing_device_fails_start_with_clear_message(tmp_path, fake_sc):
    cap = AudioCapture(tmp_path / 'm', 'no-such-mic', 'spk-1')
    with pytest.raises(RuntimeError, match='Microphone: device not found'):
        cap.start()
    assert not any(s.is_alive() for s in cap.streams)


def test_open_error_is_reported_not_retried(tmp_path, fake_sc):
    fake_sc.loop.open_error = 'AUDCLNT_E_UNSUPPORTED_FORMAT'
    cap = AudioCapture(tmp_path / 'm', 'mic-1', 'spk-1')
    with pytest.raises(RuntimeError, match='System audio: AUDCLNT_E_UNSUPPORTED_FORMAT'):
        cap.start()


def test_disk_error_ends_stream_instead_of_retry_loop(tmp_path, fake_sc, monkeypatch):
    monkeypatch.setattr(audio, 'MAX_WAV_BYTES', 48000 * 2)  # ~1 s of mono audio
    cap = AudioCapture(tmp_path / 'm', 'mic-1', 'spk-1')
    cap.start()
    time.sleep(1.5)
    assert any('size limit' in e for e in cap.failed)
    cap.stop()
    assert fake_sc.mic.opens == 1
