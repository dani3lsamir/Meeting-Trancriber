"""Download faster-whisper models into a folder so the app can run fully offline.

    python tools/fetch_models.py base small --dest dist/MeetingTranscriber/models
"""
import argparse
import shutil
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('models', nargs='+', help='e.g. tiny base small medium large-v3-turbo')
    parser.add_argument('--dest', type=Path, required=True)
    args = parser.parse_args()
    from faster_whisper.utils import download_model
    for name in args.models:
        target = args.dest / name
        if (target / 'model.bin').is_file():
            print(f'{name}: already present in {target}')
            continue
        print(f'{name}: downloading…', flush=True)
        download_model(name, output_dir=str(target))
        shutil.rmtree(target / '.cache', ignore_errors=True)
        print(f'{name}: {sum(f.stat().st_size for f in target.rglob("*") if f.is_file()) / 1e6:.0f} MB in {target}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
