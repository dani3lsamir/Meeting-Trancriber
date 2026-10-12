"""Background offline ASR: chunks for live preview, whole WAV files for final pass."""
from __future__ import annotations
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly
from faster_whisper import WhisperModel


def stamp(seconds):
    seconds = max(0, int(seconds))
    return f'{seconds//3600:02}:{seconds//60%60:02}:{seconds%60:02}'


def normalized_audio(wav_path, start=0, end=None):
    with sf.SoundFile(str(wav_path)) as f:
        sr = f.samplerate
        f.seek(min(int(start * sr), len(f)))
        frames = -1 if end is None else max(0, min(int(end * sr), len(f)) - f.tell())
        data = f.read(frames=frames, dtype='float32', always_2d=True).mean(axis=1)
    if sr != 16000:
        from math import gcd
        g = gcd(sr, 16000)
        data = resample_poly(data, 16000 // g, sr // g).astype(np.float32)
    return data


def infer(model, audio, language, beam=1):
    if len(audio) < 16000 or float(np.max(np.abs(audio))) < 0.002:
        return []
    segments, _ = model.transcribe(audio, language=None if language == 'auto' else language,
                                   beam_size=beam, vad_filter=True,
                                   condition_on_previous_text=False)
    return [{'start': float(s.start), 'end': float(s.end), 'text': s.text.strip()}
            for s in segments if s.text.strip()]


class TranscriptionEngine:
    def __init__(self, folder, language, preview_model, final_model, on_preview, on_status, on_done, on_error):
        self.folder = Path(folder)
        self.language = language
        self.preview_model = preview_model
        self.final_model = final_model
        self.on_preview = on_preview
        self.on_status = on_status
        self.on_done = on_done
        self.on_error = on_error
        self.stop_event = threading.Event()
        self.thread = None
        self.preview_rows = []

    def start(self):
        self.thread = threading.Thread(target=self._work, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _work(self):
        try:
            self.on_status('Loading local speech model (first use may download it)…')
            model = WhisperModel(self.preview_model, device='cpu', compute_type='int8')
            cursors = {'you': 0.0, 'meeting': 0.0}
            # 8-second non-overlapping chunks. Provisional, may miss words at boundaries.
            while not self.stop_event.is_set():
                for speaker in ('you', 'meeting'):
                    path = self.folder / f'{speaker}.wav'
                    try:
                        with sf.SoundFile(str(path)) as f:
                            duration = len(f) / f.samplerate
                    except (OSError, RuntimeError):
                        continue
                    cursor = cursors[speaker]
                    if duration - cursor < 8:
                        continue
                    clip = normalized_audio(path, cursor, cursor + 8)
                    for seg in infer(model, clip, self.language):
                        row = {'start': cursor + seg['start'], 'end': cursor + seg['end'],
                               'speaker': speaker, 'text': seg['text']}
                        self.preview_rows.append(row)
                        self.on_preview(row)
                    cursors[speaker] += 8
                time.sleep(0.5)
            # Capture is stopped before this method completes; final pass sees closed WAVs.
            self.on_status('Creating final transcript…')
            del model
            model = WhisperModel(self.final_model, device='cpu', compute_type='int8')
            rows = []
            for speaker in ('you', 'meeting'):
                wav = self.folder / f'{speaker}.wav'
                if not wav.exists():
                    continue
                self.on_status(f'Transcribing final audio: {speaker}…')
                clip = normalized_audio(wav)
                for seg in infer(model, clip, self.language, beam=5):
                    rows.append({**seg, 'speaker': speaker})
            rows.sort(key=lambda r: (r['start'], r['speaker']))
            self._export(rows)
            self.on_done(rows, str(self.folder))
        except Exception as exc:
            self.on_error(str(exc))

    def _export(self, rows):
        title = self.folder.name
        text_lines = [f'[{stamp(r["start"])}] {"You" if r["speaker"] == "you" else "Meeting"}: {r["text"]}' for r in rows]
        (self.folder / 'transcript.txt').write_text('\n'.join(text_lines) + '\n', encoding='utf-8')
        markdown = [f'# Meeting transcript — {title}', '', '> Automatic transcription; verify critical details.', '']
        markdown += [f'**{stamp(r["start"])} · {"You" if r["speaker"] == "you" else "Meeting"}**  \n{r["text"]}\n' for r in rows]
        (self.folder / 'transcript.md').write_text('\n'.join(markdown) + '\n', encoding='utf-8')
        (self.folder / 'transcript.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
