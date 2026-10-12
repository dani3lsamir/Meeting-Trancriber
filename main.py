"""Meeting Transcriber: private, local meeting transcription for Windows."""
from __future__ import annotations

import sys

from app.paths import fix_std_streams

fix_std_streams()  # must run before importing libraries that print (windowed build)

import argparse  # noqa: E402
import logging  # noqa: E402
import math  # noqa: E402
import threading  # noqa: E402
from datetime import datetime  # noqa: E402
from pathlib import Path  # noqa: E402

log = logging.getLogger('main')


def build_gui():
    from PySide6.QtCore import QObject, QSettings, Qt, QTimer, QUrl, Signal
    from PySide6.QtGui import QDesktopServices, QTextCursor
    from PySide6.QtWidgets import (QApplication, QComboBox, QGridLayout, QHBoxLayout, QLabel, QMessageBox,
                                   QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout, QWidget)

    from app import __version__
    from app.audio import list_devices
    from app.models import FINAL_MODELS, LIVE_MODELS
    from app.paths import log_dir, meetings_dir
    from app.session import Session, SessionCallbacks, SessionConfig
    from app.transcribe import LANGUAGES, line

    class Bridge(QObject):
        """Thread-safe hop from worker threads to the GUI thread (queued signals)."""
        status = Signal(str)
        warning = Signal(str)
        live_row = Signal(dict)
        started = Signal()
        progress = Signal(float)
        finished = Signal(dict)
        failed = Signal(str)
        devices = Signal(object)

    class Window(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle(f'Meeting Transcriber {__version__}')
            self.setMinimumSize(760, 560)
            self.setStyleSheet('''
                QWidget {background:#10151c; color:#e9edf4; font-size:13px}
                QLabel#heading {font-size:22px; font-weight:700}
                QLabel#muted {color:#8c97a6}
                QPushButton {background:#263446; border:1px solid #34465a; border-radius:7px; padding:9px 14px}
                QPushButton:hover {background:#314760}
                QPushButton:disabled {color:#6c7887; background:#1b2531}
                QPushButton#start {background:#3f6d2a; color:white; font-weight:700}
                QPushButton#start:disabled {background:#1b2531}
                QPushButton#stop {background:#8a2f2f; color:white; font-weight:700}
                QPushButton#stop:disabled {background:#1b2531}
                QPlainTextEdit {background:#18212d; border:1px solid #334252; border-radius:8px; padding:8px}
                QComboBox {background:#1c2936; border:1px solid #334252; border-radius:5px; padding:6px}
                QProgressBar {background:#18212d; border:1px solid #334252; border-radius:4px; text-align:center}
                QProgressBar::chunk {background:#4f8a34; border-radius:3px}''')
            self.settings = QSettings('MeetingTranscriber', 'MeetingTranscriber')
            self.session: Session | None = None
            self.folder: Path | None = None
            self.bridge = Bridge()
            self.bridge.status.connect(self.on_status)
            self.bridge.warning.connect(self.on_warning)
            self.bridge.live_row.connect(self.on_live_row)
            self.bridge.started.connect(self.on_started)
            self.bridge.progress.connect(self.on_progress)
            self.bridge.finished.connect(self.on_finished)
            self.bridge.failed.connect(self.on_failed)
            self.bridge.devices.connect(self.on_devices)

            root = QVBoxLayout(self)
            head = QLabel('Meeting Transcriber')
            head.setObjectName('heading')
            root.addWidget(head)
            sub = QLabel('Records your microphone and the meeting audio (Teams, Meet, Zoom…) and transcribes locally.')
            sub.setObjectName('muted')
            root.addWidget(sub)

            grid = QGridLayout()
            self.mics, self.outputs = QComboBox(), QComboBox()
            self.mic_level, self.out_level = self._meter(), self._meter()
            self.refresh_btn = QPushButton('Refresh devices')
            self.refresh_btn.clicked.connect(self.refresh_devices)
            grid.addWidget(QLabel('Microphone (you)'), 0, 0)
            grid.addWidget(self.mics, 0, 1)
            grid.addWidget(self.mic_level, 0, 2)
            grid.addWidget(QLabel('Speakers / headset (meeting)'), 1, 0)
            grid.addWidget(self.outputs, 1, 1)
            grid.addWidget(self.out_level, 1, 2)
            grid.addWidget(self.refresh_btn, 0, 3, 2, 1)
            grid.setColumnStretch(1, 1)
            root.addLayout(grid)

            options = QHBoxLayout()
            self.lang, self.live_model, self.final_model = QComboBox(), QComboBox(), QComboBox()
            for label, value in LANGUAGES:
                self.lang.addItem(label, value)
            self.live_model.addItem('off', '')
            for name in LIVE_MODELS:
                self.live_model.addItem(name, name)
            for name in FINAL_MODELS:
                self.final_model.addItem(name, name)
            self._restore(self.lang, 'language', 'auto')
            self._restore(self.live_model, 'live_model', 'base')
            self._restore(self.final_model, 'final_model', 'small')
            for label, widget in (('Language', self.lang), ('Live model', self.live_model), ('Final model', self.final_model)):
                col = QVBoxLayout()
                col.addWidget(QLabel(label))
                col.addWidget(widget)
                options.addLayout(col)
            root.addLayout(options)

            buttons = QHBoxLayout()
            self.start_btn = QPushButton('●  Start recording')
            self.start_btn.setObjectName('start')
            self.start_btn.clicked.connect(self.start_recording)
            self.stop_btn = QPushButton('■  Stop and transcribe')
            self.stop_btn.setObjectName('stop')
            self.stop_btn.clicked.connect(self.stop_recording)
            self.skip_btn = QPushButton('Skip final pass')
            self.skip_btn.clicked.connect(self.skip_final)
            self.open_btn = QPushButton('Open folder')
            self.open_btn.clicked.connect(self.open_folder)
            for b in (self.start_btn, self.stop_btn, self.skip_btn, self.open_btn):
                buttons.addWidget(b)
            root.addLayout(buttons)

            self.status = QLabel('Choose your devices and press Start.')
            self.status.setWordWrap(True)
            self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
            root.addWidget(self.status)
            self.progress = QProgressBar()
            self.progress.setRange(0, 1000)
            self.progress.setFormat('Final transcript %p%')
            self.progress.hide()
            root.addWidget(self.progress)
            self.transcript = QPlainTextEdit()
            self.transcript.setReadOnly(True)
            self.transcript.setPlaceholderText('Live text appears here while recording (provisional). '
                                               'The accurate final transcript replaces it after you stop.')
            root.addWidget(self.transcript, 1)
            foot = QLabel('Record only with the participants’ consent. Audio and text never leave this PC.')
            foot.setObjectName('muted')
            root.addWidget(foot)

            self.timer = QTimer(self)
            self.timer.setInterval(150)
            self.timer.timeout.connect(self.tick)
            self.set_state('idle')
            self.refresh_devices()

        # -- helpers --------------------------------------------------------
        @staticmethod
        def _meter():
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setTextVisible(False)
            bar.setFixedSize(90, 12)
            return bar

        def _restore(self, combo, key, default):
            idx = combo.findData(self.settings.value(key, default))
            combo.setCurrentIndex(idx if idx >= 0 else max(0, combo.findData(default)))

        def set_state(self, state: str):
            idle = state == 'idle'
            self.start_btn.setEnabled(idle and self.mics.count() > 0 and self.outputs.count() > 0)
            self.stop_btn.setEnabled(state == 'recording')
            self.skip_btn.setVisible(state == 'transcribing')
            for w in (self.mics, self.outputs, self.refresh_btn, self.lang, self.live_model, self.final_model):
                w.setEnabled(idle)
            self.progress.setVisible(state == 'transcribing')
            if state == 'recording':
                self.timer.start()
            else:
                self.timer.stop()
                self.mic_level.setValue(0)
                self.out_level.setValue(0)

        # -- devices --------------------------------------------------------
        def refresh_devices(self):
            self.refresh_btn.setEnabled(False)
            self.status.setText('Looking for audio devices…')

            def worker():
                try:
                    self.bridge.devices.emit(list_devices())
                except Exception as exc:
                    log.exception('Device enumeration failed')
                    self.bridge.devices.emit(exc)

            threading.Thread(target=worker, name='devices', daemon=True).start()

        def on_devices(self, result):
            self.refresh_btn.setEnabled(True)
            if isinstance(result, Exception):
                self.status.setText(f'Unable to list audio devices: {result}')
                self.set_state('idle')
                return
            mics, outputs, default_mic, default_out = result
            log.info('Devices: %d microphones, %d outputs', len(mics), len(outputs))
            saved = {'mic': self.settings.value('mic_id'), 'out': self.settings.value('output_id')}
            for combo, items, default, key in ((self.mics, mics, default_mic, 'mic'),
                                               (self.outputs, outputs, default_out, 'out')):
                combo.clear()
                for did, name in items:
                    combo.addItem(name + ('  (Windows default)' if did == default else ''), did)
                idx = combo.findData(saved[key])
                if idx < 0:
                    idx = combo.findData(default)
                combo.setCurrentIndex(max(0, idx))
            if not mics or not outputs:
                missing = ' and '.join(n for n, items in (('microphone', mics), ('output device', outputs)) if not items)
                self.status.setText(f'No {missing} found. Check Windows Sound settings, then press Refresh devices.')
            else:
                self.status.setText('Pick the speakers/headset your meeting app plays through, then press Start.')
            self.set_state('idle')

        # -- recording --------------------------------------------------------
        def start_recording(self):
            if self.mics.currentData() is None or self.outputs.currentData() is None:
                QMessageBox.warning(self, 'Missing devices', 'Select a microphone and a speaker/headset.')
                return
            for key, combo in (('mic_id', self.mics), ('output_id', self.outputs), ('language', self.lang),
                               ('live_model', self.live_model), ('final_model', self.final_model)):
                self.settings.setValue(key, combo.currentData())
            self.folder = meetings_dir() / datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
            b = self.bridge
            config = SessionConfig(
                folder=self.folder, mic_id=self.mics.currentData(), output_id=self.outputs.currentData(),
                language=self.lang.currentData(), live_model=self.live_model.currentData() or 'base',
                final_model=self.final_model.currentData(), live_enabled=bool(self.live_model.currentData()),
                mic_name=self.mics.currentText(), output_name=self.outputs.currentText())
            callbacks = SessionCallbacks(status=b.status.emit, warning=b.warning.emit, live_row=b.live_row.emit,
                                         started=b.started.emit, progress=b.progress.emit,
                                         finished=b.finished.emit, failed=b.failed.emit)
            self.transcript.clear()
            self.session = Session(config, callbacks)
            self.set_state('starting')
            self.start_btn.setEnabled(False)
            self.session.start()

        def on_started(self):
            self.set_state('recording')

        def stop_recording(self):
            if self.session and self.session.state == 'recording':
                self.stop_btn.setEnabled(False)
                self.status.setText('Stopping…')
                self.session.request_stop()
                self.set_state('transcribing')
                self.progress.setValue(0)

        def skip_final(self):
            if self.session:
                self.session.cancel()
                self.status.setText('Skipping the final pass…')

        def tick(self):
            if not self.session:
                return
            levels = self.session.levels
            for bar, key in ((self.mic_level, 'you'), (self.out_level, 'meeting')):
                # RMS -> rough dBFS meter (-60 dB .. 0 dB)
                rms = max(levels.get(key, 0.0), 1e-6)
                db = 20 * math.log10(rms)
                bar.setValue(int(max(0, min(100, (db + 60) / 60 * 100))))
            e = int(self.session.elapsed)
            self.setWindowTitle(f'● REC {e // 3600:02}:{e // 60 % 60:02}:{e % 60:02} — Meeting Transcriber')

        # -- session callbacks (GUI thread) -------------------------------------
        def on_status(self, text):
            self.status.setText(text)

        def on_warning(self, text):
            log.warning('UI warning: %s', text)
            self.status.setText('⚠ ' + text)
            self.transcript.appendPlainText(f'⚠ {text}')

        def on_live_row(self, row):
            if self.session and self.session.state == 'recording':
                self.transcript.appendPlainText(line(row))
                self.transcript.moveCursor(QTextCursor.End)

        def on_progress(self, fraction):
            self.progress.setValue(int(fraction * 1000))

        def on_finished(self, result):
            self.setWindowTitle(f'Meeting Transcriber {__version__}')
            rows = result['rows']
            if result['cancelled']:
                self.status.setText(f'{result["note"]} Folder: {result["folder"]}')
            else:
                self.transcript.setPlainText('\n'.join(line(r) for r in rows) or '(no speech detected)')
                note = f' {result["note"]}' if result.get('note') else ''
                self.status.setText(f'Done: {len(rows)} segments.{note} Saved to {result["folder"]}')
            self.session = None
            self.set_state('idle')

        def on_failed(self, message):
            self.setWindowTitle(f'Meeting Transcriber {__version__}')
            self.status.setText('Error: ' + message.splitlines()[0])
            self.session = None
            self.set_state('idle')
            QMessageBox.warning(self, 'Meeting Transcriber', f'{message}\n\nLog file: {log_dir() / "app.log"}')

        def open_folder(self):
            path = self.folder if self.folder and self.folder.exists() else meetings_dir()
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

        def closeEvent(self, event):
            if self.session and self.session.active:
                answer = QMessageBox.question(
                    self, 'Meeting Transcriber',
                    'A recording or transcription is in progress.\n\nStop and exit? '
                    'Recordings are kept, but the final transcript will be skipped.')
                if answer != QMessageBox.Yes:
                    event.ignore()
                    return
                self.status.setText('Closing…')
                self.session.shutdown(timeout=10)
            event.accept()

    return QApplication, Window


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='MeetingTranscriber')
    parser.add_argument('--self-test', action='store_true', help='run the headless pipeline test and exit')
    parser.add_argument('--sample', type=Path, help='speech audio file for --self-test (default: bundled sample)')
    parser.add_argument('--model', help='model for --self-test (default: smallest installed, else tiny)')
    parser.add_argument('--capture-test', action='store_true',
                        help='--self-test: also play the sample on the default output and record it via WASAPI loopback')
    parser.add_argument('--report', type=Path, help='write the --self-test report (JSON) here')
    parser.add_argument('--gui-smoke-test', type=float, default=0, metavar='SECONDS',
                        help='open the window, close it after SECONDS and exit 0')
    args, qt_args = parser.parse_known_args(argv)

    from app.paths import setup_logging
    log_path = setup_logging()
    from app import __version__
    log.info('Meeting Transcriber %s starting (frozen=%s, python %s)', __version__,
             getattr(sys, 'frozen', False), sys.version.split()[0])

    if args.self_test:
        from app.selftest import run_self_test
        return run_self_test(args.sample, args.model, args.report, args.capture_test)

    QApplication, Window = build_gui()
    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName('Meeting Transcriber')
    window = Window()
    window.show()
    if args.gui_smoke_test:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(int(args.gui_smoke_test * 1000), app.quit)
    code = app.exec()
    log.info('Exit (%s). Log: %s', code, log_path)
    return code


if __name__ == '__main__':
    sys.exit(main())
