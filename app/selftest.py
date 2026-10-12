"""Headless end-to-end check, usable from source and from the built .exe.

    MeetingTranscriber.exe --self-test --sample speech.flac --model tiny --report report.json

Steps: library imports -> audio device enumeration -> (optional) real capture
-> crash-safe WAV writing -> live chunked transcription -> final pass -> export.
The sample is used as the "you" track and, 15 s later, as the "meeting" track,
so both sources and timestamp offsets are checked.
"""
from __future__ import annotations

import json
import logging
import platform
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

MEETING_OFFSET = 15.0


def _versions() -> dict:
    out = {'python': sys.version.split()[0], 'platform': platform.platform(), 'frozen': bool(getattr(sys, 'frozen', False))}
    for mod in ('numpy', 'ctranslate2', 'faster_whisper', 'av', 'onnxruntime', 'tokenizers', 'PySide6', 'soundcard'):
        try:
            m = __import__(mod)
            out[mod] = getattr(m, '__version__', 'ok')
        except Exception as exc:  # soundcard fails on Linux without PulseAudio, etc.
            out[mod] = f'ERROR: {exc}'
    return out


def run_self_test(sample: Path | None, model_name: str | None = None, report: Path | None = None,
                  capture_test: bool = False) -> int:
    from .audio import RATE, WavWriter, list_devices
    from .models import cpu_threads, find_local_model, load_model
    from .paths import resource_dir
    from .transcribe import LiveTranscriber, export, final_transcription, line

    results: dict = {'checks': [], 'ok': False}
    work = Path(tempfile.mkdtemp(prefix='mt-selftest-'))
    results['folder'] = str(work)

    def check(name, ok, detail=''):
        results['checks'].append({'name': name, 'ok': bool(ok), 'detail': detail})
        log.info('SELFTEST %-28s %s %s', name, 'PASS' if ok else 'FAIL', detail)
        print(f'[{"PASS" if ok else "FAIL"}] {name}: {detail}', flush=True)

    try:
        versions = _versions()
        results['versions'] = versions
        required = ('numpy', 'ctranslate2', 'faster_whisper', 'av', 'onnxruntime', 'tokenizers', 'PySide6')
        bad = {k: versions[k] for k in required if str(versions[k]).startswith('ERROR')}
        check('imports', not bad, json.dumps(bad) if bad else ', '.join(f'{k}={versions[k]}' for k in required))

        # Device enumeration is informative: CI machines usually have no audio hardware.
        try:
            mics, outputs, dmic, dout = list_devices()
            results['devices'] = {'microphones': mics, 'outputs': outputs, 'default_mic': dmic, 'default_output': dout}
            check('device enumeration (info)', True, f'{len(mics)} microphones, {len(outputs)} outputs')
        except Exception as exc:
            results['devices'] = {'error': str(exc)}
            check('device enumeration (info)', True, f'not available on this machine: {type(exc).__name__}: {exc}')
            mics, outputs, dmic, dout = [], [], None, None

        if capture_test:
            _capture_test(check, results, work, dmic, dout, model_name, sample)

        if sample is None:
            sample = resource_dir() / 'assets' / 'selftest_jfk.flac'
        if model_name is None:
            model_name = next((m for m in ('tiny', 'base', 'small') if find_local_model(m)), 'tiny')
        results['model'] = model_name
        check('sample', Path(sample).is_file(), str(sample))

        from faster_whisper.audio import decode_audio
        speech48 = decode_audio(str(sample), sampling_rate=RATE)
        sample_len = len(speech48) / RATE
        you = np.concatenate([speech48, np.zeros(int((MEETING_OFFSET + 2) * RATE), np.float32)])
        meeting = np.concatenate([np.zeros(int(MEETING_OFFSET * RATE), np.float32), speech48 * 0.8,
                                  np.zeros(int(2 * RATE), np.float32)])
        n = max(len(you), len(meeting))
        you, meeting = np.pad(you, (0, n - len(you))), np.pad(meeting, (0, n - len(meeting)))

        # Crash-safe WAV: header must be valid while the file is still open.
        import wave
        w_you, w_meet = WavWriter(work / 'you.wav', 1), WavWriter(work / 'meeting.wav', 2)
        live_rows = []
        live = LiveTranscriber(lambda: load_model(model_name, cpu_threads(True)), 'auto', work / 'live_transcript.txt',
                               live_rows.append, lambda m: log.warning('live warning: %s', m))
        live.start()
        block = RATE // 10
        for i in range(0, n, block):
            w_you.write(you[i:i + block])
            w_meet.write(np.stack([meeting[i:i + block]] * 2, axis=1))
            live.feed('you', you[i:i + block])
            live.feed('meeting', meeting[i:i + block])
            if i == 50 * block:
                w_you._fh.flush()
                with wave.open(str(work / 'you.wav'), 'rb') as probe:
                    check('wav valid while recording', probe.getnframes() >= 50 * block, f'{probe.getnframes()} frames')
        w_you.close()
        w_meet.close()
        with wave.open(str(work / 'meeting.wav'), 'rb') as wf:
            check('wav files', wf.getnchannels() == 2 and abs(wf.getnframes() - n) <= 1,
                  f'{wf.getnframes() / RATE:.1f} s, {wf.getnchannels()} ch')

        deadline = time.monotonic() + 300
        while time.monotonic() < deadline and (live.is_alive() and (not live.ready.is_set() or not live.queue.empty()
                                                                    or any(v >= 8 * RATE for v in live.buffered.values()))):
            time.sleep(0.5)
        time.sleep(2)
        live.stop()
        live.join(30)
        check('live model', live.error is None, live.error or model_name)
        live_text = ' '.join(r['text'] for r in live_rows).lower()
        check('live transcription', 'country' in live_text, f'{len(live_rows)} rows: {live_text[:120]}')

        t = time.monotonic()
        model = load_model(model_name, cpu_threads(False))
        rows, info = final_transcription(model, [('you', work / 'you.wav'), ('meeting', work / 'meeting.wav')],
                                         'auto', threading.Event())
        elapsed = time.monotonic() - t
        results['final_rows'] = rows
        you_rows = [r for r in rows if r['speaker'] == 'you']
        meet_rows = [r for r in rows if r['speaker'] == 'meeting']
        check('final: microphone track', any('country' in r['text'].lower() for r in you_rows),
              ' | '.join(line(r) for r in you_rows)[:200])
        check('final: system-audio track', any('country' in r['text'].lower() for r in meet_rows),
              ' | '.join(line(r) for r in meet_rows)[:200])
        check('final: timestamps', bool(you_rows and meet_rows) and you_rows[0]['start'] < 3
              and MEETING_OFFSET - 1 <= meet_rows[0]['start'] <= MEETING_OFFSET + 3,
              f'you@{you_rows[0]["start"] if you_rows else "-"} meeting@{meet_rows[0]["start"] if meet_rows else "-"}')
        check('final: speed', True, f'{elapsed:.1f} s for {2 * sample_len:.1f} s of speech; {info}')
        files = export(work, rows, {'title': 'self-test', 'duration': n / RATE, 'final_model': model_name})
        txt = files[0].read_text(encoding='utf-8')
        check('export txt/md/json', all(p.is_file() and p.stat().st_size > 0 for p in files) and '[00:00:' in txt,
              ', '.join(p.name for p in files))
    except Exception as exc:
        check('exception', False, f'{exc}\n{traceback.format_exc()}')
    return _finish(results, report)


def _capture_test(check, results, work, mic_id, output_id, model_name, sample) -> None:
    """Real WASAPI test: play the sample on the default output and record it via loopback
    (plus the default microphone, if there is one), then transcribe the loopback file."""
    from faster_whisper.audio import decode_audio

    from .audio import RATE, CaptureStream, run_in_com_thread, soundcard
    from .models import cpu_threads, find_local_model, load_model
    from .paths import resource_dir
    from .transcribe import final_transcription

    if not output_id:
        check('real capture: loopback', False, 'no output device on this machine')
        return
    sample = sample or resource_dir() / 'assets' / 'selftest_jfk.flac'
    model_name = model_name or next((m for m in ('tiny', 'base', 'small') if find_local_model(m)), 'tiny')
    folder = work / 'capture'
    folder.mkdir()
    stop = threading.Event()
    t0 = time.monotonic()
    warnings = []
    streams = [CaptureStream('meeting', output_id, folder / 'meeting.wav', 2, True, t0, stop, on_warning=warnings.append)]
    if mic_id:
        streams.append(CaptureStream('you', mic_id, folder / 'you.wav', 1, False, t0, stop, on_warning=warnings.append))
    for s in streams:
        s.start()
    for s in streams:
        s.ready.wait(10)
    errors = [s.error for s in streams if s.error]
    if errors:
        stop.set()
        check('real capture: open devices', False, '; '.join(errors))
        return
    check('real capture: open devices', True, ', '.join(f'{s.source}={s.device_name}' for s in streams))
    audio = decode_audio(str(sample), sampling_rate=RATE)

    def play():
        speaker = next(s for s in soundcard().all_speakers() if s.id == output_id)
        speaker.play(audio * 0.8, samplerate=RATE)

    time.sleep(1.0)
    try:
        run_in_com_thread(play, timeout=len(audio) / RATE + 30)
        played = 'ok'
    except Exception as exc:
        played = f'playback failed: {exc}'
    time.sleep(1.5)
    stop.set()
    for s in streams:
        s.join(10)
    durations = {s.source: round(s.writer.seconds, 2) if s.writer else 0 for s in streams}
    results['capture'] = {'durations': durations, 'warnings': warnings, 'playback': played,
                          'peak_levels': {s.source: s.level for s in streams}}
    spread = max(durations.values()) - min(durations.values())
    check('real capture: files aligned', all(d > 10 for d in durations.values()) and spread < 0.5,
          f'{durations} (playback: {played})')
    model = load_model(model_name, cpu_threads(False))
    rows, _ = final_transcription(model, [('meeting', folder / 'meeting.wav')], 'auto', threading.Event())
    text = ' '.join(r['text'] for r in rows)
    check('real capture: loopback transcribed', 'country' in text.lower(), text[:200])
    if mic_id:
        check('real capture: microphone (info)', True, f'{durations.get("you")} s recorded from {mic_id}')
    else:
        check('real capture: microphone (info)', True, 'no microphone on this machine; mic capture not tested')


def _finish(results: dict, report: Path | None) -> int:
    results['ok'] = all(c['ok'] for c in results['checks'])
    text = json.dumps(results, ensure_ascii=False, indent=2, default=str)
    if report:
        Path(report).write_text(text, encoding='utf-8')
    print('SELF-TEST ' + ('PASSED' if results['ok'] else 'FAILED'), flush=True)
    log.info('Self-test %s', 'passed' if results['ok'] else 'failed')
    return 0 if results['ok'] else 1
