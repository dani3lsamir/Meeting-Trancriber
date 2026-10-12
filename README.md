# Meeting Transcriber (Windows)

A small, private desktop app that records **your microphone** and **everything your PC plays**
(Microsoft Teams, Google Meet, Zoom, or any other app) at the same time, shows a provisional live
transcript, and after you stop creates an accurate, timestamped transcript. Everything runs locally
with [faster-whisper](https://github.com/SYSTRAN/faster-whisper). There are no accounts, cloud APIs,
subscriptions or meeting integrations.

## Using the app

1. Start `MeetingTranscriber.exe`.
2. **Microphone (you):** the mic you speak into.
   **Speakers / headset (meeting):** the playback device that Teams, Meet or Zoom uses. This must be
   the device you *hear* the meeting on. If you use a headset, pick the headset, not the laptop
   speakers. Check the device in the meeting app's audio settings, or in Windows Sound settings.
3. Choose the language (Auto detect works; a fixed language is more consistent), the **live model**
   (`tiny` is fastest, `base` is the default, `off` disables live text) and the **final model**
   (`small` is the default; `medium` and `large-v3-turbo` are more accurate but much slower on CPU).
4. Press **Start recording**. Both level meters should move: the mic meter when you talk, the meeting
   meter when others talk or any sound plays.
5. Press **Stop and transcribe**. The live text is replaced by the final transcript when the second
   pass finishes. **Skip final pass** keeps the recordings and the live transcript only.

Each meeting gets its own folder in `%LOCALAPPDATA%\MeetingTranscriber\Meetings\YYYY-MM-DD_HH-MM-SS\`
(the **Open folder** button opens it):

| File | Contents |
|---|---|
| `you.wav` | Original microphone recording (48 kHz, 16-bit, mono) |
| `meeting.wav` | Original system-audio (WASAPI loopback) recording (48 kHz, 16-bit, stereo) |
| `transcript.txt` | Final transcript, one `[HH:MM:SS] You/Meeting: text` line per segment |
| `transcript.md` | Same transcript as Markdown |
| `transcript.json` | Segments with start/end times, source and metadata |
| `live_transcript.txt` | Provisional live text, written while recording |
| `session.json` | Devices, models, duration, detected language |

Both WAV files share one timeline: second 600 in `you.wav` is the same moment as second 600 in
`meeting.wav`. The WAV headers are updated while recording, so the files stay playable even if the
PC crashes.

Logs are in `%LOCALAPPDATA%\MeetingTranscriber\logs\app.log`. Include this file when you report a
problem.

## Models (local only)

The Windows build ships with the `base` and `small` models in `MeetingTranscriber\models\`, so
it works offline out of the box. Other models (`tiny`, `medium`, `large-v3-turbo`) are downloaded
**once** from Hugging Face the first time you select them. They are stored in
`%LOCALAPPDATA%\MeetingTranscriber\models\`, and from then on they run offline.

* To prevent any download, set the environment variable `MEETINGTRANSCRIBER_OFFLINE=1`.
* To add models manually, copy them into the `models` folder next to the exe:
  `python tools/fetch_models.py medium --dest dist\MeetingTranscriber\models`

## Building on Windows

Requirements: Windows 10 or 11 (64-bit), and 64-bit **Python 3.11** (or 3.12) from python.org with
the `py` launcher.

```bat
build_windows.bat              :: builds and bundles the base + small models
build_windows.bat tiny base    :: choose the bundled models
build_windows.bat none         :: no bundled models (downloaded on first use)
```

The script creates `.venv`, installs `requirements-dev.txt`, runs `ruff` and the test suite, builds
with `MeetingTranscriber.spec` (PyInstaller, one-folder, windowed), copies the models, and runs the
built exe's self-test. Result:

```
dist\MeetingTranscriber\MeetingTranscriber.exe
```

Distribute the **whole** `dist\MeetingTranscriber` folder, not only the exe. It has no installer and
needs no admin rights.

Development run: `run_windows.bat` (or `python main.py` inside the venv).

The GitHub Actions workflow `.github/workflows/windows-build.yml` runs the same build on
`windows-latest` on every push and uploads it as the artifact **MeetingTranscriber-windows-x64**.

### Self-test and diagnostics

```bat
MeetingTranscriber.exe --self-test --report report.json
MeetingTranscriber.exe --self-test --capture-test --report report.json
```

`--self-test` runs these checks without the GUI and writes a JSON report:

* the native libraries load;
* the audio devices are listed;
* the WAV files are written in a crash-safe way;
* live chunked transcription works;
* the final pass and the TXT/MD/JSON export work.

It uses a bundled 11-second public-domain speech sample. `--capture-test` also plays that sample
on the default output device and records it via WASAPI loopback (plus the default microphone),
then transcribes the recording.

## How it works

```
 mic  ──WASAPI──► capture thread ──► you.wav      ─┐
                                 └─► live queue ───┼─► live thread (small model, 5–8 s chunks) ─► GUI
 output ─loopback► capture thread ──► meeting.wav ─┘
                                 └─► live queue
 Stop ─► close WAVs ─► final pass (beam 5, VAD, whole files) ─► echo filter ─► transcript.txt/.md/.json
```

* `app/audio.py`: WASAPI capture through `soundcard`. It initialises COM in each capture thread,
  writes crash-safe WAV files, aligns both streams to one wall clock, and reconnects automatically
  if a device disappears.
* `app/transcribe.py`: live chunking (cuts at the quietest moment), the final pass, the echo filter
  and the exports.
* `app/session.py`: the lifecycle of one meeting, on a background thread.
* `app/models.py`: finds a local model or downloads it once.
* `main.py`: the PySide6 GUI. It only reacts to queued signals, so it never blocks.

## Limitations and tips

* **Speakers are labelled "You" and "Meeting"**, not by name. Everything played by the selected
  output device is in `meeting.wav`, including notification sounds and music.
* **Use headphones if you can.** With loudspeakers, the mic also picks up the other participants. The
  final pass drops "You" lines that closely repeat a "Meeting" line at the same time, but partial
  echo may remain.
* **Bluetooth headsets** switch to a "hands-free" device when the meeting app opens their mic. If
  the selected device disappears, the app reconnects or falls back to the Windows default output,
  and inserts silence for the gap. Check the warning shown in the window.
* Live text is provisional and appears with a delay of about 5–10 seconds. If the CPU cannot keep
  up, live preview skips ahead and tells you; the final pass always processes everything.
* Final-pass time on CPU, roughly per hour of meeting (each source is transcribed): `base` is a few
  minutes, `small` is 10–20 minutes, `medium` and `large-v3-turbo` take considerably longer.
* A single WAV file is limited to 4 GB, which is about 6 hours of stereo system audio. Recording
  stops cleanly at that point.
* Get everyone's consent before recording a meeting, and follow your organisation's policies.
