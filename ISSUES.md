# Issues found and resolved

Review of the original prototype (commit `97e4f12`) and what changed.

| # | Area | Problem in the original | Fix |
|---|---|---|---|
| 1 | COM / Qt | `soundcard` was imported before `QApplication`, which put the GUI thread in COM's multithreaded mode. Qt's `OleInitialize` then failed, so clipboard and drag & drop broke. | soundcard is imported on a dedicated thread. Capture and device queries run on threads that initialise COM themselves (`app/audio.py`). |
| 2 | COM | soundcard treats `S_FALSE` from its import-time `CoInitializeEx` as an error. Importing it on a COM-initialised thread raised `Error 0x100000001`. **Found on the Windows CI run**: the GUI could not list devices. | Import on a fresh thread. A Windows regression test reproduces the GUI's order (`tests/test_windows_audio.py`). |
| 3 | Live transcription | The live thread re-opened `you.wav`/`meeting.wav` while they were being written. On Windows that collides with file locking, and libsndfile only finalises the header length on close. | Capture threads feed audio from memory through a queue (`LiveTranscriber.feed`). WAVs are only read after they are closed. |
| 4 | GUI freeze | `stop_recording` joined both capture threads on the GUI thread (up to 12 s), and `processEvents()` was used as a workaround. | A `Session` controller thread owns the whole lifecycle. The GUI only receives queued Qt signals. |
| 5 | Signals | `status` was connected to a context-less lambda, then all of its connections were dropped with `disconnect()`. | A dedicated `Bridge` QObject with explicit signals. |
| 6 | Error flow | A start failure showed two dialogs (the raised error plus the thread's `on_error`). `failure()` called `engine.stop()`, which launched the final pass after the UI had reset, so a stale `done` arrived later. | One failure path through the session. States are idle, starting, recording, transcribing, then done or failed. |
| 7 | Data safety | WAV headers were only written on close, so a crash or kill lost the recording. | `WavWriter` patches the RIFF header after every 100 ms block and flushes once per second. |
| 8 | Robustness | Any device error ended the meeting. That includes a Bluetooth headset switching to hands-free when Teams opens the mic, an unplugged headset, and `AUDCLNT_E_DEVICE_INVALIDATED`. | Automatic reconnect with backoff, falling back to the Windows default device, with a warning in the UI. |
| 9 | Timeline | The mic and loopback started at different times and loopback can stall, so the two files drifted apart. | Both streams follow one shared wall clock. Late starts, stalls and reconnects are padded with silence. |
| 10 | Errors | Disk-full and file-size errors would have been retried forever. | `RecordingWriteError` is fatal: the session stops cleanly and still transcribes what it has. A 4 GB WAV limit guard is included. |
| 11 | Error messages | Raw HRESULTs such as `Error 0x80070005` were shown. | `explain()` maps common codes to fixes, for example the microphone privacy switch or exclusive-mode use. |
| 12 | Memory | The final pass resampled whole files with scipy in float64 (over 1 GB of RAM for a 1-hour call). | It decodes with faster-whisper/PyAV at 16 kHz float32. scipy and soundfile were removed. |
| 13 | Packaging | `--windowed` builds have `sys.stdout`/`stderr = None`, so tqdm/huggingface progress output crashed on the first model download. | `fix_std_streams()` runs before any library import, and HF progress bars are disabled. |
| 14 | Packaging | `onnxruntime` (needed by the VAD filter) wasn't collected. scipy was bundled for no reason. The build used no spec file, and there was no smoke test. | `MeetingTranscriber.spec` collects ctranslate2, onnxruntime, av, tokenizers and the soundcard headers, and excludes unused modules. The built exe runs its own `--self-test`. |
| 15 | Offline | Models were always downloaded on first use. | Lookup order: bundled `models/` next to the exe, then the local cache, then a one-time download. `MEETINGTRANSCRIBER_OFFLINE=1` forbids downloads. The build bundles `base` and `small`. |
| 16 | Observability | There was no logging. | Rotating `app.log`, `crash.log` (faulthandler), unhandled-exception hooks, and a `session.json` for each meeting. |
| 17 | Transcript quality | Fixed 8 s cuts split words. Auto language was detected per chunk. Speaker echo was duplicated. | Live chunks are cut at the quietest 100 ms between 5 and 8 s. The language locks after a confident detection. An echo filter removes "You" lines that repeat simultaneous "Meeting" lines. |
| 18 | Live lag | A slow CPU fell behind without limit. | Live preview skips ahead after 45 s of backlog and warns. The final pass always processes everything. |
| 19 | UX | No sign that audio was being captured. Users could not skip a long final pass, and closing during processing was blocked. | Level meters, a REC timer, a progress bar, "Skip final pass", a safe close, and remembered settings. |
