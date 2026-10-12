"""Application folders and logging setup."""
from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)


def app_dir() -> Path:
    """Folder containing the executable (frozen) or the project root (source)."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    """Folder with bundled read-only files (PyInstaller puts them in _internal)."""
    return Path(getattr(sys, '_MEIPASS', app_dir()))


def data_dir() -> Path:
    root = os.environ.get('MEETINGTRANSCRIBER_HOME') or os.environ.get('LOCALAPPDATA') or str(Path.home())
    path = Path(root)
    if 'MEETINGTRANSCRIBER_HOME' not in os.environ:
        path = path / 'MeetingTranscriber'
    path.mkdir(parents=True, exist_ok=True)
    return path


def meetings_dir() -> Path:
    path = data_dir() / 'Meetings'
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_dir() -> Path:
    path = data_dir() / 'logs'
    path.mkdir(parents=True, exist_ok=True)
    return path


def fix_std_streams() -> None:
    """A --windowed PyInstaller build has sys.stdout/sys.stderr set to None.

    Libraries that print progress (tqdm in huggingface_hub, warnings) then crash
    with "'NoneType' object has no attribute 'write'". Point them at devnull.
    """
    for name in ('stdout', 'stderr'):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, 'w', encoding='utf-8'))
    os.environ.setdefault('HF_HUB_DISABLE_PROGRESS_BARS', '1')
    os.environ.setdefault('HF_HUB_DISABLE_TELEMETRY', '1')
    os.environ.setdefault('HF_HUB_DISABLE_SYMLINKS_WARNING', '1')


_fault_file = None


def setup_logging(level: int = logging.INFO) -> Path:
    """Log to %LOCALAPPDATA%\\MeetingTranscriber\\logs\\app.log (rotated) and stderr."""
    global _fault_file
    path = log_dir() / 'app.log'
    root = logging.getLogger()
    if any(getattr(h, '_mt', False) for h in root.handlers):
        return path
    root.setLevel(level)
    fmt = logging.Formatter('%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s')
    file_handler = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding='utf-8')
    file_handler.setFormatter(fmt)
    file_handler._mt = True
    root.addHandler(file_handler)
    if sys.stderr is not None and not getattr(sys, 'frozen', False):
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        console._mt = True
        root.addHandler(console)
    logging.captureWarnings(True)
    for noisy in ('faster_whisper', 'httpx', 'urllib3', 'huggingface_hub'):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    def excepthook(exc_type, exc, tb):
        logging.getLogger('crash').critical('Unhandled exception', exc_info=(exc_type, exc, tb))

    def thread_excepthook(args):
        logging.getLogger('crash').critical('Unhandled exception in thread %s', args.thread.name if args.thread else '?',
                                            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    try:
        _fault_file = open(log_dir() / 'crash.log', 'a', encoding='utf-8')
        faulthandler.enable(_fault_file)
    except OSError:
        pass
    return path
