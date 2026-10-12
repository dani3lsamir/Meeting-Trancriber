"""Offline speech recognition: live preview while recording, accurate pass afterwards."""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

import numpy as np

from .audio import RATE

log = logging.getLogger(__name__)

SR = 16000                      # Whisper's sample rate
LIVE_WINDOW = 8.0               # max seconds per live chunk
LIVE_MIN_CUT = 5.0              # earliest point at which a chunk may be cut (at the quietest moment)
LIVE_MAX_BACKLOG = 45.0         # seconds of unprocessed audio before live preview skips ahead
SILENCE_RMS = 0.0015
SPEAKERS = {'you': 'You', 'meeting': 'Meeting'}

LANGUAGES = [('Auto detect', 'auto'), ('English', 'en'), ('Español', 'es'), ('Português', 'pt'),
             ('Français', 'fr'), ('Deutsch', 'de'), ('Italiano', 'it'), ('Nederlands', 'nl'),
             ('Polski', 'pl'), ('Русский', 'ru'), ('Українська', 'uk'), ('Türkçe', 'tr'),
             ('العربية', 'ar'), ('हिन्दी', 'hi'), ('中文', 'zh'), ('日本語', 'ja'), ('한국어', 'ko')]


class Cancelled(Exception):
    pass


def stamp(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f'{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}'


def line(row: dict) -> str:
    return f'[{stamp(row["start"])}] {SPEAKERS.get(row["speaker"], row["speaker"])}: {row["text"]}'


# 48 kHz -> 16 kHz: windowed-sinc low-pass (cut-off 7.2 kHz) then keep every 3rd sample.
_TAPS = 63
_n = np.arange(_TAPS) - (_TAPS - 1) / 2
_FIR = (np.sinc(2 * 7200 / RATE * _n) * np.hamming(_TAPS)).astype(np.float32)
_FIR /= _FIR.sum()


def to_16k(audio48: np.ndarray) -> np.ndarray:
    assert RATE == 3 * SR
    audio48 = np.asarray(audio48, dtype=np.float32)
    if len(audio48) == 0:
        return audio48
    return np.convolve(audio48, _FIR, mode='same')[::3].astype(np.float32)


def run_model(model, audio16: np.ndarray, language: str | None, beam: int, offset: float = 0.0,
              cancel: threading.Event | None = None, on_segment: Callable | None = None):
    """Transcribe 16 kHz mono audio. Returns (segments, detected_language, probability)."""
    segments, info = model.transcribe(
        audio16, language=language, beam_size=beam, vad_filter=True,
        vad_parameters={'min_silence_duration_ms': 500},
        condition_on_previous_text=False)
    rows = []
    for s in segments:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        text = s.text.strip()
        if text:
            rows.append({'start': round(offset + float(s.start), 2), 'end': round(offset + float(s.end), 2), 'text': text})
        if on_segment:
            on_segment(float(s.end))
    return rows, getattr(info, 'language', None), float(getattr(info, 'language_probability', 0.0) or 0.0)


class LiveTranscriber(threading.Thread):
    """Consumes 48 kHz mono blocks from both capture streams and emits provisional rows."""

    def __init__(self, load_model: Callable[[], object], language: str, out_path: Path,
                 on_row: Callable[[dict], None], on_warning: Callable[[str], None],
                 on_status: Callable[[str], None] = lambda s: None):
        super().__init__(name='live-transcriber', daemon=True)
        self.load_model = load_model
        self.language = None if language == 'auto' else language
        self.out_path = Path(out_path)
        self.on_row = on_row
        self.on_warning = on_warning
        self.on_status = on_status
        self.queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.buffers: dict[str, list[np.ndarray]] = {'you': [], 'meeting': []}
        self.buffered: dict[str, int] = {'you': 0, 'meeting': 0}
        self.offsets: dict[str, float] = {'you': 0.0, 'meeting': 0.0}
        self.rows: list[dict] = []
        self.ready = threading.Event()
        self.error: str | None = None
        self._lag_warned = False

    def feed(self, source: str, block48: np.ndarray) -> None:
        """Called from capture threads; never blocks."""
        if not self.stop_event.is_set():
            self.queue.put((source, block48))

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        try:
            model = self.load_model()
        except Exception as exc:
            log.exception('Live model failed to load')
            self.error = str(exc)
            self.on_warning(f'Live preview unavailable ({exc}). Recording continues; the final transcript is still created.')
            self.ready.set()
            self._drain_forever()
            return
        self.ready.set()
        self.on_status('Recording. Live text is provisional; the final transcript is made after you stop.')
        try:
            with open(self.out_path, 'a', encoding='utf-8') as out:
                while not self.stop_event.is_set():
                    self._drain(timeout=0.2)
                    for source in ('you', 'meeting'):
                        if self.stop_event.is_set():
                            break
                        clip, offset = self._next_clip(source)
                        if clip is not None:
                            self._process(model, source, clip, offset, out)
        except Cancelled:
            pass  # stopped while a chunk was being transcribed
        except Exception as exc:
            log.exception('Live transcription failed')
            self.error = str(exc)
            self.on_warning(f'Live preview stopped ({exc}). Recording continues.')
            self._drain_forever()
        finally:
            del model

    def _drain_forever(self) -> None:
        while not self.stop_event.is_set():
            self._drain(timeout=0.5)
            for source in self.buffers:
                self.buffers[source].clear()
                self.buffered[source] = 0

    def _drain(self, timeout: float) -> None:
        try:
            item = self.queue.get(timeout=timeout)
        except queue.Empty:
            return
        while True:
            source, block = item
            self.buffers[source].append(block)
            self.buffered[source] += len(block)
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                return

    def _next_clip(self, source: str):
        """Cut the next chunk at the quietest 100 ms between LIVE_MIN_CUT and LIVE_WINDOW."""
        if self.buffered[source] < LIVE_WINDOW * RATE:
            return None, 0.0
        audio = np.concatenate(self.buffers[source])
        backlog = len(audio) / RATE
        if backlog > LIVE_MAX_BACKLOG:
            skip = len(audio) - int(LIVE_WINDOW * RATE)
            self.offsets[source] += skip / RATE
            audio = audio[skip:]
            log.warning('Live preview behind by %.0f s on %s; skipped ahead', backlog, source)
            if not self._lag_warned:
                self._lag_warned = True
                self.on_warning('Live preview cannot keep up on this CPU and skipped some audio. '
                                'Choose a smaller live model; the final transcript is unaffected.')
        lo, hi, hop = int(LIVE_MIN_CUT * RATE), int(LIVE_WINDOW * RATE), RATE // 10
        frames = audio[lo:hi][: (hi - lo) // hop * hop].reshape(-1, hop)
        cut = lo + int(np.argmin((frames * frames).mean(axis=1))) * hop + hop // 2
        clip, rest = audio[:cut], audio[cut:]
        offset = self.offsets[source]
        self.buffers[source] = [rest] if len(rest) else []
        self.buffered[source] = len(rest)
        self.offsets[source] += cut / RATE
        return clip, offset

    def _process(self, model, source: str, clip48: np.ndarray, offset: float, out) -> None:
        if float(np.sqrt(np.mean(clip48 * clip48))) < SILENCE_RMS:
            return
        rows, lang, prob = run_model(model, to_16k(clip48), self.language, beam=1, offset=offset,
                                     cancel=self.stop_event)
        if self.language is None and lang and prob >= 0.8:
            # Lock the language after one confident detection so live text stays consistent.
            self.language = lang
            log.info('Live language locked to %s (p=%.2f)', lang, prob)
        for row in rows:
            row['speaker'] = source
            self.rows.append(row)
            out.write(line(row) + '\n')
            out.flush()
            self.on_row(row)


def _normalize(text: str) -> str:
    return re.sub(r'[^\w ]+', '', text.lower()).strip()


def remove_echo(rows: list[dict], window: float = 2.0, threshold: float = 0.75) -> tuple[list[dict], int]:
    """Drop microphone rows that are just the meeting audio leaking from the speakers.

    Without headphones the microphone hears the remote participants, so the
    same sentence appears as "You" and "Meeting" at almost the same time.
    A "You" row is dropped only when its text closely matches an overlapping
    "Meeting" row.
    """
    meeting = [(r, _normalize(r['text'])) for r in rows if r['speaker'] == 'meeting']
    kept, dropped = [], 0
    for row in rows:
        if row['speaker'] == 'you':
            mine = _normalize(row['text'])
            for other, theirs in meeting:
                if other['start'] - window <= row['end'] and row['start'] <= other['end'] + window and mine and theirs:
                    if SequenceMatcher(None, mine, theirs).ratio() >= threshold or (len(mine) >= 12 and mine in theirs):
                        dropped += 1
                        break
            else:
                kept.append(row)
        else:
            kept.append(row)
    return kept, dropped


def final_transcription(model, sources: list[tuple[str, Path]], language: str, cancel: threading.Event,
                        on_progress: Callable[[float], None] = lambda f: None,
                        on_status: Callable[[str], None] = lambda s: None) -> tuple[list[dict], dict]:
    """Accurate second pass over the complete recordings (beam search 5)."""
    from faster_whisper.audio import decode_audio
    lang = None if language == 'auto' else language
    loaded = []
    for speaker, path in sources:
        if Path(path).is_file() and Path(path).stat().st_size > 1000:
            audio = decode_audio(str(path), sampling_rate=SR)
            loaded.append((speaker, audio))
    total = sum(len(a) for _, a in loaded) / SR or 1.0
    done = 0.0
    rows: list[dict] = []
    detected = {}
    for speaker, audio in loaded:
        duration = len(audio) / SR
        if cancel.is_set():
            raise Cancelled()
        if duration < 0.5 or float(np.max(np.abs(audio))) < 0.002:
            log.info('Final pass: %s is silent, skipped', speaker)
            done += duration
            on_progress(done / total)
            continue
        on_status(f'Final transcript: {SPEAKERS[speaker].lower()} audio…')
        base = done
        segs, detected_lang, prob = run_model(model, audio, lang, beam=5, cancel=cancel,
                                              on_segment=lambda end, base=base: on_progress(min(1.0, (base + end) / total)))
        detected[speaker] = {'language': detected_lang, 'probability': round(prob, 3)}
        for seg in segs:
            seg['speaker'] = speaker
            rows.append(seg)
        done += duration
        on_progress(done / total)
    rows.sort(key=lambda r: (r['start'], r['speaker']))
    rows, dropped = remove_echo(rows)
    if dropped:
        log.info('Removed %d microphone segments that duplicated meeting audio', dropped)
    return rows, {'detected_language': detected, 'echo_segments_removed': dropped}


def export(folder: Path, rows: list[dict], meta: dict) -> list[Path]:
    """Write transcript.txt, transcript.md and transcript.json into folder."""
    folder = Path(folder)
    title = meta.get('title') or folder.name
    duration = stamp(meta.get('duration', 0))
    model = meta.get('final_model', '')
    language = meta.get('language', 'auto')
    txt = [f'Meeting transcript - {title}', f'Duration {duration} | model {model} | language {language}', '']
    txt += [line(r) for r in rows]
    md = [f'# Meeting transcript — {title}', '',
          f'- **Duration:** {duration}', f'- **Model:** {model} (local, offline)', f'- **Language:** {language}',
          '- **Recordings:** `you.wav` (microphone), `meeting.wav` (system audio)', '',
          '> Automatic transcription. Verify important details against the recordings.', '']
    for r in rows:
        text = r['text'].replace('\n', ' ')
        md.append(f'**[{stamp(r["start"])}] {SPEAKERS.get(r["speaker"], r["speaker"])}:** {text}  ')
        md.append('')
    if not rows:
        txt.append('(no speech detected)')
        md.append('_No speech detected._')
    paths = [folder / 'transcript.txt', folder / 'transcript.md', folder / 'transcript.json']
    paths[0].write_text('\n'.join(txt) + '\n', encoding='utf-8')
    paths[1].write_text('\n'.join(md) + '\n', encoding='utf-8')
    paths[2].write_text(json.dumps({'meta': meta, 'segments': rows}, ensure_ascii=False, indent=2), encoding='utf-8')
    return paths
