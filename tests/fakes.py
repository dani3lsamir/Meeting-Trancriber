"""Fake soundcard backend and fake Whisper model for hardware-free tests."""
from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np

RATE = 48000


def tone(seconds: float, freq: float = 440.0, amp: float = 0.3, rate: int = RATE) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


class FakeRecorder:
    def __init__(self, device, channels):
        self.device = device
        self.channels = channels or device.channels
        self.pos = 0

    def __enter__(self):
        if self.device.open_error:
            raise RuntimeError(self.device.open_error)
        time.sleep(self.device.open_delay)
        self.device.opens += 1
        return self

    def __exit__(self, *exc):
        return False

    def record(self, numframes):
        d = self.device
        if d.fail_after is not None and d.reads >= d.fail_after:
            d.fail_after = None
            if d.vanish_on_fail:
                d.present = False
            raise RuntimeError('AUDCLNT_E_DEVICE_INVALIDATED')
        d.reads += 1
        time.sleep(numframes / RATE)
        sig = d.signal
        idx = (np.arange(numframes) + self.pos) % len(sig)
        self.pos += numframes
        block = sig[idx]
        return np.repeat(block[:, None], self.channels, axis=1)


class FakeDevice:
    def __init__(self, id, name, channels=1, isloopback=False, signal=None):
        self.id, self.name, self.channels, self.isloopback = id, name, channels, isloopback
        self.signal = tone(1.0) if signal is None else signal
        self.open_delay = 0.0
        self.open_error = None
        self.fail_after = None
        self.vanish_on_fail = False
        self.present = True
        self.reads = 0
        self.opens = 0

    def recorder(self, samplerate, channels=None, blocksize=None):
        assert samplerate == RATE
        return FakeRecorder(self, channels)


class FakeSoundcard:
    def __init__(self):
        self.mic = FakeDevice('mic-1', 'USB Microphone', 1, signal=tone(1.0, 300))
        self.speaker = FakeDevice('spk-1', 'Headset', 2, signal=tone(1.0, 600))
        self.loop = FakeDevice('spk-1', 'Headset', 2, isloopback=True, signal=tone(1.0, 600))
        self.speaker2 = FakeDevice('spk-2', 'Laptop speakers', 2)
        self.loop2 = FakeDevice('spk-2', 'Laptop speakers', 2, isloopback=True, signal=tone(1.0, 800))

    def all_microphones(self, include_loopback=False):
        devs = [d for d in (self.mic,) if d.present]
        if include_loopback:
            devs += [d for d in (self.loop, self.loop2) if d.present]
        return devs

    def all_speakers(self):
        return [spk for spk, lb in ((self.speaker, self.loop), (self.speaker2, self.loop2)) if lb.present]

    def default_microphone(self):
        return self.mic if self.mic.present else None

    def default_speaker(self):
        return next(iter(self.all_speakers()), None)

    def get_microphone(self, id, include_loopback=False):
        for d in self.all_microphones(include_loopback):
            if d.id == id:
                return d
        raise IndexError(id)


PHRASES = {300: 'good morning everyone', 500: 'let us review the budget', 600: 'thanks for joining the call'}


class FakeModel:
    """Returns one segment per non-silent region; text names the tone frequency it heard."""

    def __init__(self, text=None):
        self.text = text
        self.calls = 0

    def transcribe(self, audio, language=None, beam_size=1, **kw):
        self.calls += 1
        sr = 16000
        hop = sr // 10
        frames = audio[: len(audio) // hop * hop].reshape(-1, hop)
        loud = np.sqrt((frames ** 2).mean(axis=1)) > 0.01
        segs, start = [], None
        for i, on in enumerate(list(loud) + [False]):
            if on and start is None:
                start = i
            elif not on and start is not None:
                part = audio[start * hop:i * hop]
                freq = round(np.count_nonzero(np.diff(np.signbit(part))) / 2 / (len(part) / sr), -2)
                text = self.text or PHRASES.get(int(freq), f'tone {int(freq)}')
                segs.append(SimpleNamespace(start=start / 10, end=i / 10, text=' ' + text))
                start = None
        return iter(segs), SimpleNamespace(language=language or 'en', language_probability=0.99)
