"""One meeting: capture -> live preview -> stop -> final transcript -> export.

Everything runs on a controller thread; the GUI only receives callbacks (which
the GUI turns into queued Qt signals), so it never blocks on audio devices,
thread joins or model loading.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import __version__
from .audio import AudioCapture
from .models import cpu_threads, load_model
from .transcribe import Cancelled, LiveTranscriber, export, final_transcription

log = logging.getLogger(__name__)


@dataclass
class SessionConfig:
    folder: Path
    mic_id: str
    output_id: str
    language: str = 'auto'
    live_model: str = 'base'
    final_model: str = 'small'
    live_enabled: bool = True
    mic_name: str = ''
    output_name: str = ''


@dataclass
class SessionCallbacks:
    status: Callable[[str], None] = lambda s: None
    warning: Callable[[str], None] = lambda s: None
    live_row: Callable[[dict], None] = lambda r: None
    started: Callable[[], None] = lambda: None
    progress: Callable[[float], None] = lambda f: None
    finished: Callable[[dict], None] = lambda r: None
    failed: Callable[[str], None] = lambda s: None


@dataclass
class Session:
    config: SessionConfig
    callbacks: SessionCallbacks = field(default_factory=SessionCallbacks)
    capture_factory: Callable | None = None
    model_loader: Callable | None = None

    def __post_init__(self):
        self.capture_factory = self.capture_factory or AudioCapture
        self.model_loader = self.model_loader or load_model
        self.state = 'idle'
        self.capture = None
        self.live = None
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._thread = threading.Thread(target=self._run, name='session', daemon=True)
        self.started_at = None

    # -- API used by the GUI ----------------------------------------------
    def start(self) -> None:
        self.state = 'starting'
        self._thread.start()

    def request_stop(self) -> None:
        self._stop.set()

    def cancel(self) -> None:
        """Skip (or abort) the final pass. Recordings and live text are kept."""
        self._cancel.set()
        self._stop.set()

    def shutdown(self, timeout: float = 10.0) -> None:
        self.cancel()
        self._thread.join(timeout)

    @property
    def active(self) -> bool:
        return self._thread.is_alive()

    @property
    def levels(self) -> dict[str, float]:
        return self.capture.levels if self.capture else {}

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at if self.started_at else 0.0

    # -- controller thread --------------------------------------------------
    def _run(self) -> None:
        cfg, cb = self.config, self.callbacks
        folder = Path(cfg.folder)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.state = 'failed'
            cb.failed(f'Cannot create the meeting folder {folder}: {exc}')
            return
        log.info('Session start: %s', asdict(cfg))

        if cfg.live_enabled:
            self.live = LiveTranscriber(
                lambda: self.model_loader(cfg.live_model, cpu_threads(live=True), cb.status),
                cfg.language, folder / 'live_transcript.txt', cb.live_row, cb.warning, cb.status)
        sink = self.live.feed if self.live else None
        self.capture = self.capture_factory(folder, cfg.mic_id, cfg.output_id, sink=sink, on_warning=cb.warning)
        cb.status('Opening microphone and system audio…')
        try:
            self.capture.start()
        except Exception as exc:
            log.error('Capture failed to start: %s', exc)
            self.state = 'failed'
            cb.failed(f'Could not start recording:\n{exc}')
            return
        self.started_at = time.monotonic()
        started_wall = datetime.now()
        self.state = 'recording'
        cb.started()
        if self.live:
            cb.status('Recording. Loading the live speech model…')
            self.live.start()
        else:
            cb.status('Recording (live preview off).')

        reason = None
        while not self._stop.wait(0.5):
            if self.capture.failed:
                reason = '; '.join(self.capture.failed)
                cb.warning(f'Recording stopped because of an audio error: {reason}')
                break

        self.state = 'stopping'
        cb.status('Stopping recording…')
        self.capture.stop()
        duration = self.capture.duration
        if self.live:
            self.live.stop()
            self.live.join(15)
        meta = {
            'title': started_wall.strftime('%Y-%m-%d %H:%M:%S'), 'app_version': __version__,
            'duration': round(duration, 2), 'language': cfg.language, 'live_model': cfg.live_model,
            'final_model': cfg.final_model, 'microphone': cfg.mic_name or cfg.mic_id,
            'system_audio': cfg.output_name or cfg.output_id, 'stopped_by_error': reason,
        }
        self._write_meta(folder, meta)

        if self._cancel.is_set():
            self._finish(folder, [], meta, cancelled=True)
            return
        if duration < 1.0:
            self._finish(folder, [], meta, note='Recording was too short to transcribe.')
            return
        self.state = 'transcribing'
        try:
            model = self.model_loader(cfg.final_model, cpu_threads(live=False), cb.status)
            if self._cancel.is_set():
                raise Cancelled()
            cb.status('Creating the final transcript…')
            cb.progress(0.0)
            t = time.monotonic()
            rows, info = final_transcription(model, [('you', folder / 'you.wav'), ('meeting', folder / 'meeting.wav')],
                                             cfg.language, self._cancel, cb.progress, cb.status)
            del model
            meta.update(info)
            meta['final_pass_seconds'] = round(time.monotonic() - t, 1)
            self._write_meta(folder, meta)
            self._finish(folder, rows, meta)
        except Cancelled:
            self._finish(folder, [], meta, cancelled=True)
        except Exception as exc:
            log.exception('Final transcription failed')
            self.state = 'failed'
            cb.failed(f'Final transcription failed: {exc}\n\nThe recordings and the live transcript are saved in:\n{folder}')

    def _finish(self, folder: Path, rows: list, meta: dict, cancelled: bool = False, note: str = '') -> None:
        files = []
        if not cancelled:
            files = [str(p) for p in export(folder, rows, meta)]
        else:
            note = 'Final transcript skipped. Recordings and live transcript are saved.'
        self.state = 'done'
        log.info('Session finished: %d segments, cancelled=%s', len(rows), cancelled)
        self.callbacks.finished({'folder': str(folder), 'rows': rows, 'files': files,
                                 'cancelled': cancelled, 'note': note})

    @staticmethod
    def _write_meta(folder: Path, meta: dict) -> None:
        try:
            (folder / 'session.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError as exc:
            log.warning('Could not write session.json: %s', exc)
