"""Microphone + Windows WASAPI loopback capture into crash-safe WAV files.

Design notes
------------
* soundcard initialises COM when imported. Importing it on the GUI thread
  (before QApplication) puts that thread in the multithreaded apartment and
  breaks Qt's OleInitialize (clipboard, drag & drop). It is therefore only
  imported and used inside worker threads that initialise COM themselves.
* Each source is written by its own thread to a 16-bit PCM WAV whose header is
  patched after every block, so recordings survive a crash or power loss.
* Both files share one wall clock (t0). If a device starts late, stalls or is
  reconnected, silence is inserted so timestamps in both files stay aligned.
* A device that disappears mid-meeting (Bluetooth profile switch, unplugged
  headset, default device change) is reopened automatically; if it is gone
  the current Windows default device is used instead.
"""
from __future__ import annotations

import contextlib
import logging
import sys
import threading
import time
import warnings
import wave
from pathlib import Path
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

RATE = 48000
BLOCK = RATE // 10                  # 100 ms per read
ALIGN_TOLERANCE = 0.25              # seconds behind the wall clock before silence is inserted
MAX_WAV_BYTES = 0xFFFFFFFF - 64 * 1024 * 1024   # stay below the 4 GiB RIFF limit
FLUSH_INTERVAL = 1.0                # seconds between OS-level flushes of the WAV files
START_TIMEOUT = 10.0

_sc = None
_sc_lock = threading.Lock()


def _import_soundcard():
    import soundcard as sc
    if sys.platform == 'win32':
        # We manage COM per thread ourselves; stop soundcard from calling
        # CoUninitialize from an unrelated thread at interpreter exit.
        with contextlib.suppress(Exception):
            sc.mediafoundation._com.com_loaded = False
    return sc


def soundcard():
    """Import soundcard once, on a dedicated thread.

    soundcard calls CoInitializeEx at import time and treats S_FALSE ("already
    initialised") as an error, so the import must not run on a thread that has
    COM initialised already (GUI thread, capture threads). A fresh thread is
    used, leaving the GUI thread's apartment untouched.
    """
    global _sc
    with _sc_lock:
        if _sc is None:
            warnings.filterwarnings('once', message='data discontinuity')
            box: dict = {}

            def target():
                try:
                    box['sc'] = _import_soundcard()
                except BaseException as exc:
                    box['error'] = exc

            t = threading.Thread(target=target, name='soundcard-import', daemon=True)
            t.start()
            t.join(30)
            if 'error' in box:
                raise RuntimeError(f'Windows audio (WASAPI) unavailable: {explain(box["error"])}') from box['error']
            if 'sc' not in box:
                raise TimeoutError('Loading the Windows audio library timed out')
            _sc = box['sc']
        return _sc


_HRESULTS = {
    0x80070005: 'access denied: Windows privacy settings block microphone access. Open Settings > Privacy & security '
                '> Microphone and turn on "Microphone access" and "Let desktop apps access your microphone"',
    0x88890004: 'the audio device was removed, disabled or changed (AUDCLNT_E_DEVICE_INVALIDATED)',
    0x8889000A: 'the device is in exclusive use by another application. In Sound settings > device > Advanced, '
                'untick "Allow applications to take exclusive control" (AUDCLNT_E_DEVICE_IN_USE)',
    0x88890008: 'the device does not support the requested format (AUDCLNT_E_UNSUPPORTED_FORMAT)',
    0x88890001: 'the audio device is not initialised (AUDCLNT_E_NOT_INITIALIZED)',
    0x88890010: 'the Windows audio service is not running (AUDCLNT_E_SERVICE_NOT_RUNNING)',
    0x80070490: 'no such audio device (E_NOTFOUND)',
    0x800401F0: 'COM not initialised on the capture thread (CO_E_NOTINITIALIZED)',
}


def explain(exc: BaseException) -> str:
    """Turn soundcard's 'Error 0x8xxxxxxx' messages into actionable text."""
    import re
    text = str(exc)
    m = re.search(r'0x([0-9a-fA-F]{8,9})', text)
    if m:
        code = int(m.group(1), 16) & 0xFFFFFFFF
        if code in _HRESULTS:
            return f'{_HRESULTS[code]} [0x{code:08X}]'
    return text


@contextlib.contextmanager
def com_initialized():
    """Initialise COM (multithreaded apartment) for the current thread on Windows."""
    if sys.platform != 'win32':
        yield
        return
    import ctypes
    hr = ctypes.windll.ole32.CoInitializeEx(None, 0x0)  # COINIT_MULTITHREADED
    ok = hr in (0, 1)  # S_OK, S_FALSE
    if not ok:
        log.warning('CoInitializeEx returned 0x%08X', hr & 0xFFFFFFFF)
    try:
        yield
    finally:
        if ok:
            ctypes.windll.ole32.CoUninitialize()


def run_in_com_thread(fn: Callable, timeout: float = 15.0):
    """Run fn() in a short-lived COM-initialised thread and return its result."""
    result: dict = {}

    def target():
        with com_initialized():
            try:
                result['value'] = fn()
            except BaseException as exc:  # re-raised in the caller
                result['error'] = exc

    t = threading.Thread(target=target, name='audio-query', daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError('The Windows audio service did not respond')
    if 'error' in result:
        raise result['error']
    return result['value']


def _default_id(getter):
    try:
        device = getter()
        return device.id if device else None
    except Exception as exc:  # no default endpoint (e.g. no audio hardware) raises E_NOTFOUND
        log.info('No default device: %s', exc)
        return None


def _list_devices():
    sc = soundcard()
    mics = [(m.id, m.name) for m in sc.all_microphones(include_loopback=False)]
    outputs = [(s.id, s.name) for s in sc.all_speakers()]
    return mics, outputs, _default_id(sc.default_microphone), _default_id(sc.default_speaker)


def list_devices():
    """Return (microphones, outputs, default_mic_id, default_output_id).

    microphones / outputs are lists of (device_id, name).
    """
    return run_in_com_thread(_list_devices)


def fit_channels(block: np.ndarray, channels: int) -> np.ndarray:
    """Convert a (frames,) or (frames, n) float block to (frames, channels)."""
    block = np.asarray(block, dtype=np.float32)
    if block.ndim == 1:
        block = block[:, None]
    n = block.shape[1]
    if n == channels:
        return block
    mono = block.mean(axis=1, keepdims=True) if n > 1 else block
    return np.repeat(mono, channels, axis=1) if channels > 1 else mono


class RecordingWriteError(RuntimeError):
    """Writing the WAV file failed (disk full, size limit): fatal, never retried."""


class WavWriter:
    """16-bit PCM WAV writer that keeps the RIFF header valid after every write.

    The stdlib ``wave`` module patches the header lengths on each writeframes()
    call; we additionally flush to the OS once per second, so a crash loses at
    most about a second of audio and never the whole file.
    """

    def __init__(self, path: Path, channels: int, rate: int = RATE):
        self.path = Path(path)
        self.channels = channels
        self.rate = rate
        self.frames = 0
        self._fh = open(self.path, 'wb')
        self._wav = wave.open(self._fh, 'wb')
        self._wav.setnchannels(channels)
        self._wav.setsampwidth(2)
        self._wav.setframerate(rate)
        self._last_flush = time.monotonic()

    @property
    def seconds(self) -> float:
        return self.frames / self.rate

    def write(self, block: np.ndarray) -> None:
        block = fit_channels(block, self.channels)
        if (self.frames + len(block)) * self.channels * 2 > MAX_WAV_BYTES:
            raise RecordingWriteError(f'{self.path.name} reached the 4 GB WAV size limit; recording stopped')
        pcm = (np.clip(block, -1.0, 1.0) * 32767.0).astype('<i2')
        try:
            self._wav.writeframes(pcm.tobytes())
            self.frames += len(block)
            now = time.monotonic()
            if now - self._last_flush >= FLUSH_INTERVAL:
                self._fh.flush()
                self._last_flush = now
        except OSError as exc:
            raise RecordingWriteError(f'cannot write {self.path.name}: {exc}') from exc

    def close(self) -> None:
        if self._fh.closed:
            return
        try:
            self._wav.close()
        finally:
            self._fh.close()


class CaptureStream(threading.Thread):
    """Records one source ('you' = microphone, 'meeting' = output loopback)."""

    def __init__(self, source: str, device_id: str, path: Path, channels: int, loopback: bool,
                 t0: float, stop_event: threading.Event,
                 sink: Callable[[str, np.ndarray], None] | None = None,
                 on_warning: Callable[[str], None] | None = None):
        super().__init__(name=f'capture-{source}', daemon=True)
        self.source = source
        self.device_id = device_id
        self.path = Path(path)
        self.channels = channels
        self.loopback = loopback
        self.t0 = t0
        self.stop_event = stop_event
        self.sink = sink
        self.on_warning = on_warning or (lambda msg: None)
        self.ready = threading.Event()
        self.error: str | None = None       # fatal error, stream has ended
        self.device_name = ''
        self.level = 0.0                    # RMS of the latest block, for the UI meter
        self.writer: WavWriter | None = None

    # -- device handling -------------------------------------------------
    def _find_device(self, sc, allow_fallback: bool):
        if self.loopback:
            candidates = sc.all_microphones(include_loopback=True)
            device = next((d for d in candidates if d.id == self.device_id and d.isloopback), None)
            if device is None and allow_fallback:
                default_id = _default_id(sc.default_speaker)
                if default_id is not None:
                    device = sc.get_microphone(default_id, include_loopback=True)
        else:
            candidates = sc.all_microphones(include_loopback=False)
            device = next((d for d in candidates if d.id == self.device_id), None)
            if device is None and allow_fallback:
                default_id = _default_id(sc.default_microphone)
                device = next((d for d in candidates if d.id == default_id), None)
        return device

    # -- main loop -------------------------------------------------------
    def run(self) -> None:
        with com_initialized():
            try:
                self.writer = WavWriter(self.path, self.channels)
                self._run()
            except Exception as exc:
                self.error = f'{self.label}: {explain(exc)}'
                log.exception('Capture %s failed', self.source)
            finally:
                self.ready.set()
                if self.writer is not None:
                    with contextlib.suppress(Exception):
                        self._align()
                    self.writer.close()
                    log.info('Closed %s (%.1f s)', self.path.name, self.writer.seconds)

    @property
    def label(self) -> str:
        return 'System audio' if self.loopback else 'Microphone'

    def _run(self) -> None:
        sc = soundcard()
        first = True
        failures = 0
        while not self.stop_event.is_set():
            try:
                device = self._find_device(sc, allow_fallback=not first)
                if device is None:
                    raise RuntimeError('device not found (unplugged or disabled?)')
                if device.id != self.device_id and not first:
                    self.on_warning(f'{self.label}: original device unavailable, using Windows default "{device.name}"')
                    self.device_id = device.id
                self.device_name = device.name
                channels = None if self.loopback else 1
                with device.recorder(samplerate=RATE, channels=channels, blocksize=BLOCK) as recorder:
                    log.info('%s opened: %s (loopback=%s)', self.source, device.name, self.loopback)
                    if not first:
                        self.on_warning(f'{self.label} reconnected: {device.name}')
                    first = False
                    failures = 0
                    self.ready.set()
                    while not self.stop_event.is_set():
                        self._write(recorder.record(numframes=BLOCK))
            except Exception as exc:
                if first or isinstance(exc, RecordingWriteError):
                    raise  # initial open failed or disk problem: report, don't retry silently
                failures += 1
                log.warning('%s capture interrupted (%s); retry %d', self.source, exc, failures)
                if failures == 1:
                    self.on_warning(f'{self.label} interrupted ({explain(exc)}); reconnecting…')
                self.level = 0.0
                self.stop_event.wait(min(5.0, 0.5 * failures))

    def _align(self) -> None:
        """Insert silence when the stream has fallen behind the shared wall clock."""
        expected = int((time.monotonic() - self.t0) * RATE)
        deficit = expected - self.writer.frames
        if deficit > ALIGN_TOLERANCE * RATE:
            silence = np.zeros((deficit, self.channels), dtype=np.float32)
            self.writer.write(silence)
            if self.sink:
                self.sink(self.source, silence[:, 0])

    def _write(self, data: np.ndarray) -> None:
        block = np.asarray(data, dtype=np.float32)
        if block.ndim == 1:
            block = block[:, None]
        if len(block) == 0:
            return
        # The block that just arrived ends "now"; anything missing before it is a gap.
        expected_start = int((time.monotonic() - self.t0) * RATE) - len(block)
        deficit = expected_start - self.writer.frames
        if deficit > ALIGN_TOLERANCE * RATE:
            silence = np.zeros((deficit, self.channels), dtype=np.float32)
            self.writer.write(silence)
            if self.sink:
                self.sink(self.source, silence[:, 0])
        self.writer.write(block)
        mono = block.mean(axis=1) if block.shape[1] > 1 else block[:, 0]
        self.level = float(np.sqrt(np.mean(mono * mono)))
        if self.sink:
            self.sink(self.source, mono)


class AudioCapture:
    """Records microphone ('you.wav') and output loopback ('meeting.wav') in parallel."""

    def __init__(self, folder: Path, mic_id: str, output_id: str,
                 sink: Callable[[str, np.ndarray], None] | None = None,
                 on_warning: Callable[[str], None] | None = None):
        self.folder = Path(folder)
        self.stop_event = threading.Event()
        self.t0 = 0.0
        self.streams = [
            CaptureStream('you', mic_id, self.folder / 'you.wav', 1, False, 0.0, self.stop_event, sink, on_warning),
            CaptureStream('meeting', output_id, self.folder / 'meeting.wav', 2, True, 0.0, self.stop_event, sink, on_warning),
        ]

    def start(self) -> None:
        """Open both devices; raises RuntimeError if either cannot be opened."""
        self.folder.mkdir(parents=True, exist_ok=True)
        self.t0 = time.monotonic()
        for s in self.streams:
            s.t0 = self.t0
            s.start()
        deadline = time.monotonic() + START_TIMEOUT
        for s in self.streams:
            s.ready.wait(max(0.0, deadline - time.monotonic()))
        errors = [s.error for s in self.streams if s.error]
        if not errors:
            errors = [f'{s.label}: device did not start within {START_TIMEOUT:.0f} s'
                      for s in self.streams if not s.ready.is_set()]
        if errors:
            self.stop()
            raise RuntimeError('\n'.join(errors))

    @property
    def failed(self) -> list[str]:
        """Fatal errors of streams that have ended on their own."""
        return [s.error for s in self.streams if s.error]

    @property
    def levels(self) -> dict[str, float]:
        return {s.source: s.level for s in self.streams}

    @property
    def duration(self) -> float:
        return max((s.writer.seconds for s in self.streams if s.writer), default=0.0)

    def stop(self, timeout: float = 6.0) -> None:
        self.stop_event.set()
        for s in self.streams:
            if s.is_alive():
                s.join(timeout)
            if s.is_alive():
                log.error('Capture thread %s did not stop in time', s.source)
