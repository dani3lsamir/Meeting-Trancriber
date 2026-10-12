# Meeting Transcriber — Windows prototype

Simple independent desktop app: microphone + Windows system audio loopback, provisional live subtitles, a second final offline transcription pass, and `.txt`, `.md`, `.json` exports. No subscription, summary, online API, or ShivOS dependency.

## Setup and build on Windows 10/11

1. Install **64-bit Python 3.11** from python.org and ensure the Python Launcher (`py`) is available.
2. Extract this folder. Double-click **build_windows.bat**. It creates `.venv`, installs dependencies, and uses PyInstaller.
3. Run `dist\MeetingTranscriber\MeetingTranscriber.exe`. Distribute the **whole** `dist\MeetingTranscriber` directory (not the EXE alone).
4. First start: select your mic and the exact speakers/headphones used by Teams/Zoom/Meet; choose a language and models. Click **Start meeting**.
5. Speak and play test audio to verify. Live output is provisional and may be delayed, particularly on CPU.
6. Click **Stop and transcribe**. Wait until the final pass finishes. Files are stored under `%LOCALAPPDATA%\MeetingTranscriber\Meetings\YYYY-MM-DD_HH-MM-SS`.

To run in development mode after installing the dependencies: `run_windows.bat`.

## Important limitations

- Models download from Hugging Face **once** when first used; for completely offline first launch, pre-download/cache the chosen models with a connection. After caching, inference is local. The built EXE doesn't include model weights.
- Both the mic and WASAPI loopback streams must open successfully. Some Bluetooth, USB, virtual, or mono devices have incompatible channel maps. The app reports errors but may need device-specific changes. Select the actual playback output used by the meeting software.
- Live processing is chunked at ~8 s and can fall behind on low-power CPUs. The final pass uses the full recordings and a higher beam search. `small` or `medium` may take considerable time.
- Output labels distinguish **You** and **Meeting**, *not* individual remote speakers. System loopback records **all** PC audio played through that output, including notifications. Headphone monitoring and echo cancellation can affect overlap.
- Clock drift between devices and simultaneous speech can produce imperfect order. Time alignment is approximate, not sample-perfect. Recordings may include duplicate speech if speakers feed into the mic.
- Interruptions or machine shutdown may leave WAV files to be manually recovered; recordings are streamed to disk as the call runs. This prototype does not include automatic recovery or true live word-by-word captions.
- For legal, confidentiality and workplace-policy reasons, obtain consent before recording meetings; consider whether local machine policies allow audio capture.
- PyInstaller **cannot build a Windows EXE on Linux or macOS**. Run the `.bat` on Windows. Packaging and capture have not been hardware-tested in this environment.

## Folder layout

```
MeetingTranscriber/
├── main.py                # PySide6 user interface
├── app/
│   ├── __init__.py
│   ├── audio.py           # WASAPI capture and separate WAV files
│   └── transcribe.py      # preview/final faster-whisper and exports
├── requirements.txt
├── build_windows.bat
├── run_windows.bat
└── README.md
```

## Diagnostics

- If no remote speech appears, choose the actual output used by your meeting application, not a different output.
- If you get a recorder channel/format error, switch to a stereo-capable Windows playback device or its native sample rate; SoundCard/WASAPI can be hardware-sensitive.
- If preview is too slow, choose `tiny` for live and `base` for final. Set a specific language for more consistent text.
- WAV files capture at 48 kHz mono PCM and are resampled to 16 kHz in memory for transcription. Final JSON contains time, source label and text.
