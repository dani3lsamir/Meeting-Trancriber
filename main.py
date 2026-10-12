"""Meeting Transcriber: standalone Windows desktop client."""
from __future__ import annotations
import os
import sys
from datetime import datetime
from pathlib import Path
from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QComboBox, QPlainTextEdit, QMessageBox, QFileDialog, QCheckBox)
from app.audio import AudioCapture, devices, default_ids
from app.transcribe import TranscriptionEngine, stamp

BASE = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'MeetingTranscriber' / 'Meetings'

class Events(QObject):
    preview = Signal(dict)
    status = Signal(str)
    done = Signal(object, str)
    error = Signal(str)

class Window(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Meeting Transcriber')
        self.setMinimumSize(720, 540)
        self.setStyleSheet('''QWidget {background:#10151c;color:#e9edf4;font-size:13px}
        QLabel#heading {font-size:23px;font-weight:700}
        QPushButton {background:#263446;border:1px solid #34465a;border-radius:8px;padding:10px}
        QPushButton:hover {background:#314760} QPushButton:disabled {color:#778393}
        QPushButton#start {background:#4d722f;color:white;font-weight:700}
        QPlainTextEdit {background:#18212d;border:1px solid #334252;border-radius:9px;padding:10px}
        QComboBox {background:#1c2936;border-radius:5px;padding:7px}''')
        self.events = Events()
        self.events.preview.connect(self.preview)
        self.events.status.connect(self.status.setText if hasattr(self, 'status') else lambda _: None)
        self.events.done.connect(self.finished)
        self.events.error.connect(self.failure)
        self.audio = None
        self.engine = None
        self.recording = False
        self.processing = False
        self.folder = None
        root = QVBoxLayout(self)
        head = QLabel('Meeting Transcriber')
        head.setObjectName('heading')
        root.addWidget(head)
        root.addWidget(QLabel('Private local transcription  ·  Teams / Meet / Zoom  ·  No cloud account'))
        self.mics = QComboBox(); self.speakers = QComboBox()
        root.addWidget(QLabel('Microphone (your voice)'))
        root.addWidget(self.mics)
        root.addWidget(QLabel('Output device (other voices / loopback)'))
        root.addWidget(self.speakers)
        setting = QHBoxLayout()
        self.lang = QComboBox()
        for label, value in [('Auto detect', 'auto'), ('English', 'en'), ('Español', 'es'), ('Português', 'pt')]:
            self.lang.addItem(label, value)
        self.live = QComboBox()
        for label in ('tiny', 'base', 'small'):
            self.live.addItem(label)
        self.final_model = QComboBox()
        for label in ('base', 'small', 'medium'):
            self.final_model.addItem(label)
        self.final_model.setCurrentText('small')
        for label, widget in [('Language', self.lang), ('Live model', self.live), ('Final model', self.final_model)]:
            col = QVBoxLayout(); col.addWidget(QLabel(label)); col.addWidget(widget)
            setting.addLayout(col)
        root.addLayout(setting)
        buttons = QHBoxLayout()
        self.start_btn = QPushButton('● Start meeting')
        self.start_btn.setObjectName('start')
        self.start_btn.clicked.connect(self.start_recording)
        self.stop_btn = QPushButton('■ Stop and transcribe')
        self.stop_btn.clicked.connect(self.stop_recording)
        self.stop_btn.setEnabled(False)
        self.open_btn = QPushButton('Open meeting folder')
        self.open_btn.clicked.connect(self.open_folder)
        for b in (self.start_btn, self.stop_btn, self.open_btn):
            buttons.addWidget(b)
        root.addLayout(buttons)
        self.status = QLabel('Choose your audio devices and press Start.')
        root.addWidget(self.status)
        self.transcript = QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setPlaceholderText('Live transcription appears here every ~8 seconds. Final transcript replaces it at the end.')
        root.addWidget(self.transcript, 1)
        root.addWidget(QLabel('Record only with participants’ knowledge and permission. All files remain on this PC.'))
        self.events.status.disconnect()
        self.events.status.connect(self.status.setText)
        self.refresh_devices()

    def refresh_devices(self):
        try:
            mics, outputs = devices()
            mid, sid = default_ids()
            for combo, items, default in ((self.mics, mics, mid), (self.speakers, outputs, sid)):
                combo.clear()
                for did, name in items:
                    combo.addItem(name, did)
                idx = combo.findData(default)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
            if not mics or not outputs:
                self.status.setText('No microphone or output detected. Check Windows audio settings.')
        except Exception as exc:
            self.status.setText(f'Unable to list audio devices: {exc}')

    def start_recording(self):
        if not self.mics.currentData() or not self.speakers.currentData():
            QMessageBox.warning(self, 'Missing devices', 'Select both a microphone and a sound output.')
            return
        self.folder = BASE / datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        self.transcript.clear()
        self.status.setText('Starting audio devices…')
        QApplication.processEvents()
        self.audio = AudioCapture(self.folder, self.mics.currentData(), self.speakers.currentData(), self.events.error.emit)
        try:
            self.audio.start()
        except Exception as exc:
            self.failure(str(exc))
            return
        self.engine = TranscriptionEngine(self.folder, self.lang.currentData(), self.live.currentText(),
            self.final_model.currentText(), self.events.preview.emit, self.events.status.emit,
            self.events.done.emit, self.events.error.emit)
        self.recording = True
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.mics.setEnabled(False); self.speakers.setEnabled(False)
        self.engine.start()
        self.status.setText('Recording both audio sources. Live text is provisional.')

    def preview(self, row):
        if self.recording:
            who = 'You' if row['speaker'] == 'you' else 'Meeting'
            self.transcript.appendPlainText(f'[{stamp(row["start"])}] {who}: {row["text"]}')

    def stop_recording(self):
        if not self.recording:
            return
        self.recording = False
        self.processing = True
        self.stop_btn.setEnabled(False)
        self.status.setText('Stopping capture; final transcription will start…')
        QApplication.processEvents()
        self.audio.stop()
        self.engine.stop()

    def finished(self, rows, path):
        self.processing = False
        self.transcript.setPlainText('\n'.join(f'[{stamp(r["start"])}] {"You" if r["speaker"] == "you" else "Meeting"}: {r["text"]}' for r in rows))
        self.status.setText(f'Finished. Files saved to {path}')
        self.start_btn.setEnabled(True)
        self.mics.setEnabled(True); self.speakers.setEnabled(True)

    def failure(self, msg):
        self.status.setText(f'Error: {msg}')
        if self.audio:
            self.audio.stop_event.set()
        if self.engine:
            self.engine.stop()
        self.recording = False
        self.processing = False
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.mics.setEnabled(True); self.speakers.setEnabled(True)
        QMessageBox.warning(self, 'Meeting Transcriber', msg)

    def open_folder(self):
        path = self.folder if self.folder and self.folder.exists() else BASE
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path)) if sys.platform == 'win32' else QFileDialog.getExistingDirectory(self, 'Meeting files', str(path))

    def closeEvent(self, event):
        if self.recording or self.processing:
            QMessageBox.information(self, 'Meeting in progress', 'Stop recording and wait for the final transcript before exiting.')
            event.ignore()
        else:
            event.accept()

def main():
    app = QApplication(sys.argv)
    window = Window()
    window.show()
    sys.exit(app.exec())

if __name__ == '__main__':
    main()
