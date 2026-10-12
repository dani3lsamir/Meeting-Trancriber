"""Two independent WASAPI capture streams, saved incrementally to WAV."""
from __future__ import annotations
import threading
from pathlib import Path
import numpy as np
import soundcard as sc
import soundfile as sf

RATE = 48000
BLOCK = 4800


def devices():
    microphones = [(m.id, m.name) for m in sc.all_microphones(include_loopback=False)]
    outputs = [(s.id, s.name) for s in sc.all_speakers()]
    return microphones, outputs


def default_ids():
    mic = sc.default_microphone()
    sp = sc.default_speaker()
    return mic.id if mic else None, sp.id if sp else None


class AudioCapture:
    def __init__(self, folder: Path, mic_id: str, speaker_id: str, on_error):
        self.folder = folder
        self.mic_id = mic_id
        self.speaker_id = speaker_id
        self.on_error = on_error
        self.stop_event = threading.Event()
        self.threads = []
        self.ready = [threading.Event(), threading.Event()]
        self.errors = []

    def start(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        for index, name in enumerate(('you', 'meeting')):
            t = threading.Thread(target=self._capture, args=(index, name), daemon=True)
            self.threads.append(t)
            t.start()
        for event in self.ready:
            event.wait(timeout=10)
        if self.errors or not all(e.is_set() for e in self.ready):
            self.stop()
            raise RuntimeError('; '.join(self.errors) or 'Audio devices did not initialize in time')

    def _capture(self, index, name):
        try:
            if index == 0:
                device = next((m for m in sc.all_microphones(include_loopback=False) if m.id == self.mic_id), None)
            else:
                device = sc.get_microphone(self.speaker_id, include_loopback=True)
            if device is None:
                raise RuntimeError(f'Audio device not found for {name}')
            path = self.folder / f'{name}.wav'
            with sf.SoundFile(str(path), 'w', samplerate=RATE, channels=1, subtype='PCM_16') as output:
                with device.recorder(samplerate=RATE, channels=1 if index == 0 else None, blocksize=BLOCK) as recorder:
                    self.ready[index].set()
                    while not self.stop_event.is_set():
                        chunk = np.asarray(recorder.record(numframes=BLOCK), dtype=np.float32)
                        if chunk.ndim == 2:
                            chunk = chunk.mean(axis=1)
                        output.write(np.clip(chunk, -1, 1))
                        output.flush()
        except Exception as exc:
            msg = f'{name}: {exc}'
            self.errors.append(msg)
            self.ready[index].set()
            self.stop_event.set()
            self.on_error(msg)

    def stop(self):
        self.stop_event.set()
        for t in self.threads:
            t.join(timeout=6)
