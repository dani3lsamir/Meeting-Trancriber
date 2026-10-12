"""Local faster-whisper model management.

Models are looked up, in order, in:
  1. <folder of MeetingTranscriber.exe>\\models\\<name>   (bundled with the build)
  2. %LOCALAPPDATA%\\MeetingTranscriber\\models\\<name>    (downloaded once)
If a model is in neither place it is downloaded once from the Hugging Face Hub
(Systran/faster-whisper-<name>) into (2), unless offline mode is set with
MEETINGTRANSCRIBER_OFFLINE=1. Inference always runs locally on the CPU.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Callable

from .paths import app_dir, data_dir

log = logging.getLogger(__name__)

# Approximate download sizes, shown to the user before a first download.
MODEL_SIZES_MB = {'tiny': 75, 'base': 145, 'small': 484, 'medium': 1530, 'large-v3-turbo': 1620}
LIVE_MODELS = ['tiny', 'base', 'small']
FINAL_MODELS = ['base', 'small', 'medium', 'large-v3-turbo']


def model_dirs() -> list[Path]:
    return [app_dir() / 'models', data_dir() / 'models']


def find_local_model(name: str) -> Path | None:
    for root in model_dirs():
        path = root / name
        if (path / 'model.bin').is_file():
            return path
    return None


def offline() -> bool:
    return os.environ.get('MEETINGTRANSCRIBER_OFFLINE', '').strip() in ('1', 'true', 'yes')


def ensure_model(name: str, on_status: Callable[[str], None] = lambda s: None) -> Path:
    """Return the folder of a local model, downloading it once if needed."""
    path = find_local_model(name)
    if path is not None:
        return path
    if offline():
        raise RuntimeError(f'Model "{name}" is not installed and offline mode is on. '
                           f'Copy it to {model_dirs()[0] / name}')
    dest = data_dir() / 'models' / name
    partial = dest.with_name(name + '.partial')
    size = MODEL_SIZES_MB.get(name)
    on_status(f'Downloading speech model "{name}"' + (f' (~{size} MB)' if size else '') + ', one time only…')
    log.info('Downloading model %s to %s', name, dest)
    from faster_whisper.utils import download_model
    try:
        download_model(name, output_dir=str(partial))
    except Exception as exc:
        raise RuntimeError(f'Could not download model "{name}" ({exc}). Check the internet connection once, '
                           f'or copy the model folder to {model_dirs()[0] / name}') from exc
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    partial.rename(dest)
    return dest


def load_model(name: str, threads: int, on_status: Callable[[str], None] = lambda s: None):
    """Load a faster-whisper model on the CPU (int8, falling back to float32)."""
    path = ensure_model(name, on_status)
    on_status(f'Loading speech model "{name}"…')
    from faster_whisper import WhisperModel
    try:
        model = WhisperModel(str(path), device='cpu', compute_type='int8', cpu_threads=threads)
    except (ValueError, RuntimeError) as exc:
        log.warning('int8 not supported (%s); using float32', exc)
        model = WhisperModel(str(path), device='cpu', compute_type='float32', cpu_threads=threads)
    log.info('Loaded model %s from %s with %d threads', name, path, threads)
    return model


def cpu_threads(live: bool) -> int:
    cores = os.cpu_count() or 4
    # Live preview shares the CPU with capture and the GUI; the final pass gets everything.
    return max(1, min(4, cores // 2)) if live else max(1, cores)
