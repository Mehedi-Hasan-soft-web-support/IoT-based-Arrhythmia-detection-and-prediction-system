"""
ECG Monitor WiFi v4.0
- Real-time ECG from ESP32
- Each recording saved as CSV on Supabase Storage
- Click any recording to view/download CSV
- DELETE recordings from server (with separate Select button)
- GENERATE DATASET (merge all CSVs into one)
- API PANEL (copy Supabase credentials for ML training)
- SERIAL MONITOR: Real UART/USB serial port (COM3, /dev/ttyUSB0 etc.)
"""

import sys
import os
import csv
import re
import io
import json
import time
import socket
import zipfile
import requests
import serial
import serial.tools.list_ports
from datetime import datetime, timezone
from collections import deque

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QFrame, QMessageBox, QInputDialog,
    QProgressDialog, QTableWidget, QTableWidgetItem, QHeaderView,
    QSplitter, QAbstractItemView, QDialog, QTextEdit, QTabWidget,
    QGroupBox, QScrollArea, QSizePolicy, QComboBox
)
from PyQt5.QtCore import QTimer, Qt, QThread, pyqtSignal, QSize
from PyQt5.QtGui import QFont, QColor, QClipboard

import pyqtgraph as pg


# ─────────────────────────────────────────────
#  Supabase credentials
# ─────────────────────────────────────────────
SUPABASE_URL = "https://kzftbxukodzcczpxthdv.supabase.co"
SUPABASE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imt6ZnRieHVrb2R6Y2N6cHh0aGR2Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzUzMzcwMjUsImV4cCI6MjA5MDkxMzAyNX0"
    ".tL3yvFiqQ9ADaxxxS3olol0YbFR2UTDt7_nf4q5b5II"
)
BUCKET = "ecg-recordings"
RECORDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "records")


# ─────────────────────────────────────────────
#  Supabase Storage helpers
# ─────────────────────────────────────────────
def _headers(extra=None):
    h = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    if extra:
        h.update(extra)
    return h


def has_internet(timeout=4):
    try:
        socket.setdefaulttimeout(timeout)
        socket.getaddrinfo("kzftbxukodzcczpxthdv.supabase.co", 443)
        return True
    except OSError:
        return False


def build_csv_bytes(recorded_data):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["timestamp", "raw_ecg", "filtered_ecg", "bpm", "finger"])
    writer.writerows(recorded_data)
    return buf.getvalue().encode("utf-8")


def upload_csv_to_storage(filename, csv_bytes, progress_cb=None):
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{filename}"
    try:
        if progress_cb:
            progress_cb(20)
        resp = requests.post(
            url,
            headers=_headers({"Content-Type": "text/csv"}),
            data=csv_bytes,
            timeout=30,
        )
        if progress_cb:
            progress_cb(80)
        if resp.status_code in (200, 201):
            public_url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{filename}"
            if progress_cb:
                progress_cb(100)
            return True, public_url
        else:
            return False, f"Upload failed ({resp.status_code}): {resp.text}"
    except Exception as e:
        return False, str(e)


def delete_from_storage(filename):
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{filename}"
    try:
        resp = requests.delete(url, headers=_headers(), timeout=15)
        if resp.status_code in (200, 204):
            return True, ""
        return False, f"Delete failed ({resp.status_code}): {resp.text}"
    except Exception as e:
        return False, str(e)


def list_recordings():
    url = f"{SUPABASE_URL}/storage/v1/object/list/{BUCKET}"
    try:
        resp = requests.post(
            url,
            headers=_headers({"Content-Type": "application/json"}),
            json={"prefix": "", "limit": 200, "offset": 0},
            timeout=10,
        )
        if resp.status_code == 200:
            items = resp.json()
            result = []
            for item in items:
                name = item.get("name", "")
                if not name.endswith(".csv"):
                    continue
                meta = item.get("metadata") or {}
                result.append({
                    "name":       name,
                    "size":       meta.get("size", 0),
                    "created_at": item.get("created_at", ""),
                    "url":        f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{name}",
                })
            return sorted(result, key=lambda x: x["created_at"], reverse=True)
        return []
    except Exception:
        return []


def fetch_csv_content(url):
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            return True, resp.text
        return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)


# ─────────────────────────────────────────────
#  Pending queue (offline fallback)
# ─────────────────────────────────────────────
def save_pending(filename, csv_bytes):
    os.makedirs(RECORDS_DIR, exist_ok=True)
    ppath = os.path.join(RECORDS_DIR, filename + ".pending")
    with open(ppath, "wb") as f:
        f.write(csv_bytes)


def load_all_pending():
    result = []
    if not os.path.isdir(RECORDS_DIR):
        return result
    for fname in os.listdir(RECORDS_DIR):
        if fname.endswith(".pending"):
            fpath = os.path.join(RECORDS_DIR, fname)
            csv_name = fname[:-8]
            try:
                with open(fpath, "rb") as f:
                    data = f.read()
                result.append((csv_name, data, fpath))
            except Exception:
                pass
    return result


def delete_pending(fpath):
    try:
        os.remove(fpath)
    except Exception:
        pass


# ─────────────────────────────────────────────
#  Threads
# ─────────────────────────────────────────────
class UploadThread(QThread):
    progress = pyqtSignal(int)
    finished = pyqtSignal(bool, str, str)

    def __init__(self, filename, csv_bytes):
        super().__init__()
        self.filename  = filename
        self.csv_bytes = csv_bytes

    def run(self):
        if not has_internet():
            save_pending(self.filename, self.csv_bytes)
            self.finished.emit(False,
                "⚠ No internet. Saved locally — will auto-upload when online.", "")
            return
        ok, result = upload_csv_to_storage(
            self.filename, self.csv_bytes, progress_cb=self.progress.emit
        )
        if ok:
            self.finished.emit(True, f"✓ Uploaded: {self.filename}", result)
        else:
            save_pending(self.filename, self.csv_bytes)
            self.finished.emit(False, f"✗ {result}\nSaved locally for retry.", "")


class DeleteThread(QThread):
    finished = pyqtSignal(bool, str, str)

    def __init__(self, filename):
        super().__init__()
        self.filename = filename

    def run(self):
        ok, err = delete_from_storage(self.filename)
        if ok:
            self.finished.emit(True, f"✓ Deleted: {self.filename}", self.filename)
        else:
            self.finished.emit(False, f"✗ {err}", self.filename)


class PendingRetryThread(QThread):
    uploaded = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.running = True

    def run(self):
        while self.running:
            pending = load_all_pending()
            if pending and has_internet():
                for csv_name, data, fpath in pending:
                    if not self.running:
                        break
                    ok, _ = upload_csv_to_storage(csv_name, data)
                    if ok:
                        delete_pending(fpath)
                        self.uploaded.emit(csv_name)
            for _ in range(20):
                if not self.running:
                    return
                time.sleep(1)

    def stop(self):
        self.running = False
        self.wait()


class ListThread(QThread):
    result = pyqtSignal(list)

    def run(self):
        items = list_recordings()
        self.result.emit(items)


class FetchCSVThread(QThread):
    result = pyqtSignal(bool, str, str)

    def __init__(self, url, filename):
        super().__init__()
        self.url      = url
        self.filename = filename

    def run(self):
        ok, content = fetch_csv_content(self.url)
        self.result.emit(ok, content, self.filename)


class GenerateDatasetThread(QThread):
    progress  = pyqtSignal(int, int)
    finished  = pyqtSignal(bool, str, bytes, int, int)

    def __init__(self, recordings):
        super().__init__()
        self.recordings = recordings

    def run(self):
        all_rows   = []
        header     = None
        total      = len(self.recordings)
        individual = []
        failed     = 0

        for i, item in enumerate(self.recordings):
            self.progress.emit(i + 1, total)
            ok, content = fetch_csv_content(item["url"])
            if not ok:
                failed += 1
                continue
            raw_bytes = content.encode("utf-8")
            individual.append((item["name"], raw_bytes))

            lines = content.strip().split("\n")
            if not lines:
                continue
            rows = [line.split(",") for line in lines]
            if header is None:
                header = rows[0][:]
                header.append("source_file")
            for row in rows[1:]:
                row_copy = row[:]
                row_copy.append(item["name"])
                all_rows.append(row_copy)

        if not individual:
            self.finished.emit(False, "No data found in recordings.", b"", 0, 0)
            return

        merged_buf = io.StringIO()
        writer = csv.writer(merged_buf)
        writer.writerow(header or ["timestamp","raw_ecg","filtered_ecg","bpm","finger","source_file"])
        writer.writerows(all_rows)
        merged_bytes = merged_buf.getvalue().encode("utf-8")

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("dataset_merged.csv", merged_bytes)
            for fname, fbytes in individual:
                zf.writestr(f"recordings/{fname}", fbytes)
        zip_bytes = zip_buf.getvalue()

        msg = (f"✓ {len(individual)} CSVs downloaded"
               + (f" ({failed} failed)" if failed else "")
               + f" — {len(all_rows)} total samples.")
        self.finished.emit(True, msg, zip_bytes, len(all_rows), len(individual))


class WiFiReader(QThread):
    data_received    = pyqtSignal(float, float, int, int)
    connection_error = pyqtSignal(str)
    connected        = pyqtSignal()

    def __init__(self, ip_address):
        super().__init__()
        self.ip_address = ip_address
        self.running    = False

    def run(self):
        url = f"http://{self.ip_address}/stream"
        self.running = True
        try:
            with requests.get(url, stream=True, timeout=10) as response:
                if response.status_code == 200:
                    self.connected.emit()
                    for line in response.iter_lines():
                        if not self.running:
                            break
                        if line:
                            try:
                                decoded = line.decode("utf-8")
                                if decoded.startswith("data: "):
                                    parts = decoded[6:].strip().split(",")
                                    if len(parts) == 4:
                                        self.data_received.emit(
                                            float(parts[0]), float(parts[1]),
                                            int(parts[2]),   int(parts[3]),
                                        )
                            except (ValueError, UnicodeDecodeError):
                                pass
                else:
                    self.connection_error.emit(f"HTTP Error: {response.status_code}")
        except requests.exceptions.RequestException as e:
            self.connection_error.emit(str(e))

    def stop(self):
        self.running = False
        self.wait()


# ─────────────────────────────────────────────
#  CSV Viewer Dialog
# ─────────────────────────────────────────────
class CSVViewerDialog(QDialog):
    def __init__(self, filename, content, parent=None):
        super().__init__(parent)
        self.filename = filename
        self.content  = content
        self.setWindowTitle(f"📄 {filename}")
        self.setMinimumSize(900, 600)
        self.setStyleSheet("""
            QDialog       { background: #0d1117; }
            QLabel        { color: #c9d1d9; font-size: 13px; }
            QTableWidget  { background: #161b22; color: #c9d1d9;
                            gridline-color: #30363d; border: none;
                            font-family: 'Courier New', monospace; font-size: 12px; }
            QHeaderView::section { background: #21262d; color: #58a6ff;
                                   padding: 6px; border: 1px solid #30363d;
                                   font-weight: bold; }
            QPushButton   { background: #238636; color: #fff;
                            border: none; padding: 8px 20px;
                            border-radius: 6px; font-weight: bold; }
            QPushButton:hover { background: #2ea043; }
            QPushButton#closeBtn { background: #21262d; color: #c9d1d9; }
            QPushButton#closeBtn:hover { background: #30363d; }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        hdr = QHBoxLayout()
        title = QLabel(f"<b>{filename}</b>")
        title.setStyleSheet("color:#58a6ff; font-size:15px;")
        hdr.addWidget(title)
        hdr.addStretch()
        save_btn = QPushButton("💾 Save CSV")
        save_btn.clicked.connect(self._save_local)
        close_btn = QPushButton("✕ Close")
        close_btn.setObjectName("closeBtn")
        close_btn.clicked.connect(self.close)
        hdr.addWidget(save_btn)
        hdr.addWidget(close_btn)
        layout.addLayout(hdr)

        lines = content.strip().split("\n")
        if not lines:
            layout.addWidget(QLabel("Empty file."))
            return

        rows    = [line.split(",") for line in lines]
        headers = rows[0]
        data    = rows[1:]

        bpms = []
        for row in data:
            try:
                b = int(row[3])
                if b > 0:
                    bpms.append(b)
            except Exception:
                pass
        avg_bpm = round(sum(bpms)/len(bpms), 1) if bpms else "--"
        dur_sec = len(data)

        summary = QLabel(
            f"  Samples: <b style='color:#58a6ff'>{len(data)}</b>"
            f"  &nbsp;|&nbsp;  Avg BPM: <b style='color:#f85149'>{avg_bpm}</b>"
            f"  &nbsp;|&nbsp;  Duration: <b style='color:#3fb950'>~{dur_sec} samples</b>"
        )
        summary.setStyleSheet("background:#21262d; border-radius:6px; padding:8px; color:#8b949e;")
        layout.addWidget(summary)

        SHOW  = min(len(data), 500)
        table = QTableWidget(SHOW, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.verticalHeader().setDefaultSectionSize(22)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setAlternatingRowColors(True)
        table.setStyleSheet(table.styleSheet() +
            "QTableWidget { alternate-background-color: #1c2128; }")

        for r, row in enumerate(data[:SHOW]):
            for c, val in enumerate(row[:len(headers)]):
                item = QTableWidgetItem(val.strip())
                item.setTextAlignment(Qt.AlignCenter)
                if c == 3:
                    try:
                        if int(val) > 0:
                            item.setForeground(QColor("#3fb950"))
                    except Exception:
                        pass
                table.setItem(r, c, item)

        layout.addWidget(table)

        if len(data) > 500:
            note = QLabel(f"  ⚠ Showing first 500 of {len(data)} rows. Save CSV for full data.")
            note.setStyleSheet("color:#d29922; font-size:12px;")
            layout.addWidget(note)

    def _save_local(self):
        from PyQt5.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            self, "Save CSV", self.filename, "CSV Files (*.csv)"
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.content)
            QMessageBox.information(self, "Saved", f"CSV saved to:\n{path}")


# ─────────────────────────────────────────────
#  API Panel Dialog
# ─────────────────────────────────────────────
class APIPanelDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("🔑 API Panel — ML Integration")
        self.setMinimumSize(780, 620)
        self.setStyleSheet("""
            QDialog       { background: #0d1117; }
            QLabel        { color: #c9d1d9; font-size: 13px; }
            QTabWidget::pane { border: 1px solid #30363d; background: #0d1117; border-radius: 6px; }
            QTabBar::tab  { background: #161b22; color: #8b949e; padding: 8px 18px;
                            border: 1px solid #30363d; border-bottom: none;
                            border-radius: 4px 4px 0 0; font-size: 12px; }
            QTabBar::tab:selected { background: #21262d; color: #58a6ff; font-weight: bold; }
            QTextEdit     { background: #161b22; color: #e6edf3;
                            border: 1px solid #30363d; border-radius: 6px;
                            font-family: 'Courier New', monospace; font-size: 12px;
                            padding: 8px; }
            QPushButton   { background: #1f6feb; color: #fff; border: none;
                            padding: 7px 16px; border-radius: 6px;
                            font-size: 12px; font-weight: bold; }
            QPushButton:hover { background: #388bfd; }
            QPushButton#closeBtn { background: #21262d; color: #c9d1d9;
                                   border: 1px solid #30363d; }
            QPushButton#closeBtn:hover { background: #30363d; }
            QLineEdit     { background: #161b22; color: #3fb950;
                            border: 1px solid #30363d; padding: 7px 12px;
                            font-family: 'Courier New', monospace; font-size: 12px;
                            border-radius: 6px; }
            QGroupBox     { border: 1px solid #30363d; border-radius: 8px;
                            margin-top: 10px; color: #8b949e; font-size: 11px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QLabel("🔑  API Panel — Copy credentials & code for ML training")
        title.setStyleSheet("color:#58a6ff; font-size:15px; font-weight:bold;")
        layout.addWidget(title)

        tabs = QTabWidget()
        layout.addWidget(tabs, stretch=1)

        cred_widget = QWidget()
        cred_lay    = QVBoxLayout(cred_widget)
        cred_lay.setSpacing(10)

        def _cred_row(label_text, value, copy_slot):
            grp = QGroupBox(label_text)
            grp_lay = QHBoxLayout(grp)
            field = QLineEdit(value)
            field.setReadOnly(True)
            copy_btn = QPushButton("📋 Copy")
            copy_btn.setFixedWidth(90)
            copy_btn.clicked.connect(copy_slot)
            grp_lay.addWidget(field)
            grp_lay.addWidget(copy_btn)
            return grp, field

        url_grp, self.url_field = _cred_row(
            "Supabase URL", SUPABASE_URL, self._copy_url)
        key_grp, self.key_field = _cred_row(
            "Supabase Anon Key", SUPABASE_KEY, self._copy_key)
        bkt_grp, self.bkt_field = _cred_row(
            "Storage Bucket", BUCKET, self._copy_bucket)

        public_url_val = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/"
        pub_grp, self.pub_field = _cred_row(
            "Public Base URL (for direct CSV access)", public_url_val, self._copy_pub)

        cred_lay.addWidget(url_grp)
        cred_lay.addWidget(key_grp)
        cred_lay.addWidget(bkt_grp)
        cred_lay.addWidget(pub_grp)

        note = QLabel(
            "⚠ This is the anon/public key — safe for read-only access.\n"
            "   For ML training, use the public CSV URLs directly (no auth needed)."
        )
        note.setStyleSheet("color:#d29922; font-size:11px; padding:4px 8px;")
        cred_lay.addWidget(note)
        cred_lay.addStretch()
        tabs.addTab(cred_widget, "🔐 Credentials")

        py_widget = QWidget()
        py_lay    = QVBoxLayout(py_widget)
        py_code = QTextEdit()
        py_code.setReadOnly(True)
        py_code.setPlainText(self._python_snippet())
        py_copy = QPushButton("📋 Copy Python Code")
        py_copy.clicked.connect(lambda: self._copy_text(py_code.toPlainText()))
        py_lay.addWidget(py_code)
        py_lay.addWidget(py_copy)
        tabs.addTab(py_widget, "🐍 Python ML")

        rt_widget = QWidget()
        rt_lay    = QVBoxLayout(rt_widget)
        rt_code = QTextEdit()
        rt_code.setReadOnly(True)
        rt_code.setPlainText(self._realtime_snippet())
        rt_copy = QPushButton("📋 Copy Realtime Code")
        rt_copy.clicked.connect(lambda: self._copy_text(rt_code.toPlainText()))
        rt_lay.addWidget(rt_code)
        rt_lay.addWidget(rt_copy)
        tabs.addTab(rt_widget, "⚡ Realtime (SSE)")

        api_widget = QWidget()
        api_lay    = QVBoxLayout(api_widget)
        api_code = QTextEdit()
        api_code.setReadOnly(True)
        api_code.setPlainText(self._api_snippet())
        api_copy = QPushButton("📋 Copy API Code")
        api_copy.clicked.connect(lambda: self._copy_text(api_code.toPlainText()))
        api_lay.addWidget(api_code)
        api_lay.addWidget(api_copy)
        tabs.addTab(api_widget, "🌐 REST API")

        close_btn = QPushButton("✕ Close")
        close_btn.setObjectName("closeBtn")
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn, alignment=Qt.AlignRight)

    def _copy_text(self, text):
        QApplication.clipboard().setText(text)
        self._flash_status("✓ Copied to clipboard!")

    def _copy_url(self):    self._copy_text(SUPABASE_URL)
    def _copy_key(self):    self._copy_text(SUPABASE_KEY)
    def _copy_bucket(self): self._copy_text(BUCKET)
    def _copy_pub(self):
        self._copy_text(f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/")

    def _flash_status(self, msg):
        orig = self.windowTitle()
        self.setWindowTitle(msg)
        QTimer.singleShot(1500, lambda: self.setWindowTitle(orig))

    def _python_snippet(self):
        return f'''\
# ── ECG ML Training — Load all recordings from Supabase ──────────────────
import requests, pandas as pd, io

SUPABASE_URL = "{SUPABASE_URL}"
SUPABASE_KEY = "{SUPABASE_KEY}"
BUCKET       = "{BUCKET}"

def list_recordings():
    url  = f"{{SUPABASE_URL}}/storage/v1/object/list/{{BUCKET}}"
    resp = requests.post(url,
        headers={{"apikey": SUPABASE_KEY, "Authorization": f"Bearer {{SUPABASE_KEY}}",
                  "Content-Type": "application/json"}},
        json={{"prefix": "", "limit": 200}})
    return [i for i in resp.json() if i["name"].endswith(".csv")]

def load_csv(name):
    pub_url = f"{{SUPABASE_URL}}/storage/v1/object/public/{{BUCKET}}/{{name}}"
    resp    = requests.get(pub_url, timeout=15)
    df      = pd.read_csv(io.StringIO(resp.text))
    df["source_file"] = name
    return df

recordings = list_recordings()
frames     = [load_csv(r["name"]) for r in recordings]
df         = pd.concat(frames, ignore_index=True)
print(f"Total samples: {{len(df)}}")
print(df.head())
'''

    def _realtime_snippet(self):
        return f'''\
# ── ECG Realtime Inference — SSE from ESP32 → ML Model ───────────────────
import requests, joblib, numpy as np

ESP32_IP   = "192.168.x.xx"
MODEL_PATH = "ecg_model.pkl"
model = joblib.load(MODEL_PATH)

def predict_sample(raw, filtered, bpm, finger):
    features = np.array([[raw, filtered, bpm, finger, raw - filtered]])
    prediction = model.predict(features)[0]
    confidence = model.predict_proba(features).max()
    return prediction, confidence

url = f"http://{{ESP32_IP}}/stream"
with requests.get(url, stream=True, timeout=10) as resp:
    for line in resp.iter_lines():
        if line:
            decoded = line.decode("utf-8")
            if decoded.startswith("data: "):
                parts = decoded[6:].strip().split(",")
                if len(parts) == 4:
                    raw, filtered = float(parts[0]), float(parts[1])
                    bpm, finger   = int(parts[2]),   int(parts[3])
                    pred, conf = predict_sample(raw, filtered, bpm, finger)
                    print(f"BPM={{bpm:3d}}  raw={{raw:7.1f}}  pred={{pred}}  conf={{conf:.2f}}")
'''

    def _api_snippet(self):
        return f'''\
# ── Supabase Storage REST API ─────────────────────────────────────────────
import requests

SUPABASE_URL = "{SUPABASE_URL}"
SUPABASE_KEY = "{SUPABASE_KEY}"
BUCKET       = "{BUCKET}"
HEADERS = {{"apikey": SUPABASE_KEY, "Authorization": f"Bearer {{SUPABASE_KEY}}"}}

# 1. List all recordings
resp  = requests.post(f"{{SUPABASE_URL}}/storage/v1/object/list/{{BUCKET}}",
    headers={{**HEADERS, "Content-Type": "application/json"}},
    json={{"prefix": "", "limit": 200}})
files = [f for f in resp.json() if f["name"].endswith(".csv")]

# 2. Download a CSV
filename   = files[0]["name"]
public_url = f"{{SUPABASE_URL}}/storage/v1/object/public/{{BUCKET}}/{{filename}}"
csv_data   = requests.get(public_url).text
'''


# ─────────────────────────────────────────────
#  Serial Monitor Dialog  (Real UART/USB port)
# ─────────────────────────────────────────────
class SerialMonitorDialog(QDialog):
    """
    Reads from real UART/USB serial port (COM3, /dev/ttyUSB0 etc.)
    Exactly like Arduino IDE Serial Monitor.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._reader    = None
        self._lines     = []
        self._max_lines = 2000

        self.setWindowTitle("🖥  Serial Monitor — UART/USB")
        self.setMinimumSize(740, 560)
        self.setStyleSheet("""
            QDialog { background: #0d1117; }
            QLabel  { color: #c9d1d9; font-size: 12px; }
            QTextEdit {
                background: #0d1117; color: #39ff14;
                border: 1px solid #30363d; border-radius: 6px;
                font-family: 'Courier New', monospace;
                font-size: 12px; padding: 6px;
                selection-background-color: #1f6feb;
            }
            QLineEdit {
                background: #161b22; color: #c9d1d9;
                border: 1px solid #30363d; padding: 6px 10px;
                font-size: 12px; border-radius: 5px;
                font-family: 'Courier New', monospace;
            }
            QComboBox {
                background: #161b22; color: #c9d1d9;
                border: 1px solid #30363d; padding: 5px 10px;
                font-size: 12px; border-radius: 5px;
            }
            QComboBox QAbstractItemView {
                background: #161b22; color: #c9d1d9;
                selection-background-color: #1f6feb;
            }
            QPushButton {
                background: #21262d; color: #c9d1d9;
                border: 1px solid #30363d; padding: 6px 14px;
                font-size: 12px; font-weight: bold; border-radius: 5px;
            }
            QPushButton:hover { background: #30363d; border-color: #58a6ff; }
            QPushButton#connectSerial { background: #1f6feb; color:#fff; border-color:#388bfd; }
            QPushButton#connectSerial:hover { background: #388bfd; }
            QPushButton#copyAllBtn  { background: #166534; color:#fff; border-color:#3fb950; }
            QPushButton#copyAllBtn:hover { background: #16a34a; }
            QPushButton#clearBtn    { background: #7f1d1d; color:#fca5a5; border-color:#f85149; }
            QPushButton#clearBtn:hover { background: #991b1b; }
            QPushButton#closeBtn    { background: #21262d; color:#c9d1d9; }
            QPushButton#refreshPorts { background: #064e3b; color:#a7f3d0; border-color:#34d399; }
            QPushButton#refreshPorts:hover { background: #065f46; }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        # ── Top row: Port + Baud + Connect ────────────────────────
        top = QHBoxLayout()
        top.setSpacing(6)

        top.addWidget(QLabel("Port:"))
        self.port_combo = QComboBox()
        self.port_combo.setFixedWidth(160)
        self.port_combo.setEditable(True)
        self._scan_ports()
        top.addWidget(self.port_combo)

        refresh_btn = QPushButton("↻")
        refresh_btn.setObjectName("refreshPorts")
        refresh_btn.setFixedWidth(34)
        refresh_btn.setToolTip("Scan available ports")
        refresh_btn.clicked.connect(self._scan_ports)
        top.addWidget(refresh_btn)

        top.addSpacing(8)
        top.addWidget(QLabel("Baud:"))
        self.baud_combo = QComboBox()
        self.baud_combo.setFixedWidth(100)
        for b in ["9600", "19200", "38400", "57600", "115200", "230400", "250000", "500000", "921600", "1000000"]:
            self.baud_combo.addItem(b)
        self.baud_combo.setCurrentText("115200")
        top.addWidget(self.baud_combo)

        top.addSpacing(8)
        self.conn_btn = QPushButton("▶ Connect")
        self.conn_btn.setObjectName("connectSerial")
        self.conn_btn.setFixedWidth(120)
        self.conn_btn.clicked.connect(self._toggle_connect)
        top.addWidget(self.conn_btn)

        self.status_lbl = QLabel("● Disconnected")
        self.status_lbl.setStyleSheet("color:#f85149; font-weight:bold;")
        top.addWidget(self.status_lbl)
        top.addStretch()

        self.autoscroll_btn = QPushButton("⬇ Auto-scroll ON")
        self.autoscroll_btn.setCheckable(True)
        self.autoscroll_btn.setChecked(True)
        self.autoscroll_btn.clicked.connect(self._toggle_autoscroll)
        top.addWidget(self.autoscroll_btn)

        layout.addLayout(top)

        # ── Send row (like Arduino Serial Monitor) ─────────────────
        send_row = QHBoxLayout()
        self.send_input = QLineEdit()
        self.send_input.setPlaceholderText("Send data to ESP32…")
        self.send_input.returnPressed.connect(self._send_data)
        send_btn = QPushButton("Send")
        send_btn.setFixedWidth(70)
        send_btn.clicked.connect(self._send_data)

        self.newline_combo = QComboBox()
        self.newline_combo.setFixedWidth(110)
        for opt in ["No line ending", "Newline", "Carriage return", "Both NL & CR"]:
            self.newline_combo.addItem(opt)
        self.newline_combo.setCurrentIndex(1)  # Newline by default

        send_row.addWidget(self.send_input)
        send_row.addWidget(self.newline_combo)
        send_row.addWidget(send_btn)
        layout.addLayout(send_row)

        # ── Terminal area ──────────────────────────────────────────
        self.terminal = QTextEdit()
        self.terminal.setReadOnly(True)
        self.terminal.setLineWrapMode(QTextEdit.NoWrap)
        layout.addWidget(self.terminal, stretch=1)

        # ── Bottom toolbar ─────────────────────────────────────────
        bot = QHBoxLayout()
        self.line_count_lbl = QLabel("0 lines")
        self.line_count_lbl.setStyleSheet("color:#484f58; font-size:11px;")
        bot.addWidget(self.line_count_lbl)
        bot.addStretch()

        copy_sel_btn = QPushButton("📋 Copy Selection")
        copy_sel_btn.clicked.connect(self._copy_selection)

        copy_all_btn = QPushButton("📋 Copy All")
        copy_all_btn.setObjectName("copyAllBtn")
        copy_all_btn.clicked.connect(self._copy_all)

        clear_btn = QPushButton("🗑 Clear")
        clear_btn.setObjectName("clearBtn")
        clear_btn.clicked.connect(self._clear)

        close_btn = QPushButton("✕ Close")
        close_btn.setObjectName("closeBtn")
        close_btn.clicked.connect(self.close)

        for w in (copy_sel_btn, copy_all_btn, clear_btn, close_btn):
            bot.addWidget(w)
        layout.addLayout(bot)

        # ── Batch UI update timer ──────────────────────────────────
        self._pending_lines = []
        self._ui_timer = QTimer()
        self._ui_timer.timeout.connect(self._flush_to_terminal)
        self._ui_timer.start(100)
        self._autoscroll = True

    # ── Port scanning ─────────────────────────────────────────────
    def _scan_ports(self):
        current = self.port_combo.currentText()
        self.port_combo.clear()
        ports = serial.tools.list_ports.comports()
        for p in sorted(ports):
            label = f"{p.device}  — {p.description}" if p.description != "n/a" else p.device
            self.port_combo.addItem(p.device, userData=label)
            self.port_combo.setItemData(
                self.port_combo.count() - 1, label, Qt.ToolTipRole
            )
        if not ports:
            self.port_combo.addItem("No ports found")
        # Restore previous selection if still available
        idx = self.port_combo.findText(current)
        if idx >= 0:
            self.port_combo.setCurrentIndex(idx)

    # ── Connection ────────────────────────────────────────────────
    def _toggle_connect(self):
        if self._reader and self._reader.running:
            self._stop_reader()
        else:
            self._start_reader()

    def _start_reader(self):
        port = self.port_combo.currentText().strip().split()[0]  # strip description
        if not port or port == "No ports found":
            QMessageBox.warning(self, "No Port", "Select a valid serial port.")
            return
        try:
            baud = int(self.baud_combo.currentText())
        except ValueError:
            baud = 115200

        self.conn_btn.setText("■ Disconnect")
        self.status_lbl.setText("● Opening…")
        self.status_lbl.setStyleSheet("color:#f59e0b; font-weight:bold;")
        self.port_combo.setEnabled(False)
        self.baud_combo.setEnabled(False)

        self._reader = _SerialPortReader(port, baud)
        self._reader.line_received.connect(self._on_line)
        self._reader.connected.connect(self._on_connected)
        self._reader.error.connect(self._on_error)
        self._reader.start()

    def _stop_reader(self):
        if self._reader:
            self._reader.stop()
            self._reader = None
        self.conn_btn.setText("▶ Connect")
        self.status_lbl.setText("● Disconnected")
        self.status_lbl.setStyleSheet("color:#f85149; font-weight:bold;")
        self.port_combo.setEnabled(True)
        self.baud_combo.setEnabled(True)
        self._append_sys("[Disconnected]")

    def _on_connected(self, port, baud):
        self.status_lbl.setText(f"● {port} @ {baud}")
        self.status_lbl.setStyleSheet("color:#3fb950; font-weight:bold;")
        self._append_sys(f"[Connected: {port} at {baud} baud]")

    def _on_error(self, msg):
        self._append_sys(f"[ERROR: {msg}]")
        self._stop_reader()

    # ── Send data ─────────────────────────────────────────────────
    def _send_data(self):
        if not self._reader or not self._reader.running:
            QMessageBox.warning(self, "Not Connected", "Connect to a serial port first.")
            return
        text = self.send_input.text()
        nl_idx = self.newline_combo.currentIndex()
        if nl_idx == 1:
            text += "\n"
        elif nl_idx == 2:
            text += "\r"
        elif nl_idx == 3:
            text += "\r\n"
        self._reader.send(text.encode("utf-8", errors="replace"))
        self.send_input.clear()

    # ── Data ──────────────────────────────────────────────────────
    def _on_line(self, line):
        self._pending_lines.append(line)

    def _flush_to_terminal(self):
        if not self._pending_lines:
            return
        batch = self._pending_lines[:]
        self._pending_lines.clear()

        for line in batch:
            self._lines.append(line)

        if len(self._lines) > self._max_lines:
            self._lines = self._lines[-self._max_lines:]

        from PyQt5.QtGui import QTextCursor
        cursor = self.terminal.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.terminal.setTextCursor(cursor)
        self.terminal.insertPlainText("\n".join(batch) + "\n")

        self.line_count_lbl.setText(f"{len(self._lines)} lines")

        if self._autoscroll:
            sb = self.terminal.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _append_sys(self, msg):
        self.terminal.append(f"<span style='color:#484f58'>{msg}</span>")

    # ── Controls ──────────────────────────────────────────────────
    def _toggle_autoscroll(self, checked):
        self._autoscroll = checked
        self.autoscroll_btn.setText(
            "⬇ Auto-scroll ON" if checked else "⬇ Auto-scroll OFF"
        )

    def _copy_selection(self):
        sel = self.terminal.textCursor().selectedText()
        if sel:
            QApplication.clipboard().setText(sel)
        else:
            QMessageBox.information(self, "Copy", "No text selected.")

    def _copy_all(self):
        QApplication.clipboard().setText("\n".join(self._lines))
        orig = self.windowTitle()
        self.setWindowTitle("✓ Copied to clipboard!")
        QTimer.singleShot(1500, lambda: self.setWindowTitle(orig))

    def _clear(self):
        self._lines.clear()
        self._pending_lines.clear()
        self.terminal.clear()
        self.line_count_lbl.setText("0 lines")

    def closeEvent(self, event):
        self._stop_reader()
        self._ui_timer.stop()
        event.accept()


class _SerialPortReader(QThread):
    """Reads from a real UART/USB serial port line by line."""
    line_received = pyqtSignal(str)
    connected     = pyqtSignal(str, int)   # port, baud
    error         = pyqtSignal(str)

    def __init__(self, port, baud):
        super().__init__()
        self.port    = port
        self.baud    = baud
        self.running = False
        self._ser    = None

    def run(self):
        self.running = True
        try:
            self._ser = serial.Serial(
                port=self.port,
                baudrate=self.baud,
                timeout=1,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
            )
            self.connected.emit(self.port, self.baud)
            buf = b""
            while self.running:
                if self._ser.in_waiting:
                    chunk = self._ser.read(self._ser.in_waiting)
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        decoded = line.decode("utf-8", errors="replace").rstrip("\r")
                        self.line_received.emit(decoded)
                else:
                    time.sleep(0.005)
        except serial.SerialException as e:
            self.error.emit(str(e))
        except Exception as e:
            self.error.emit(str(e))
        finally:
            if self._ser and self._ser.is_open:
                self._ser.close()

    def send(self, data: bytes):
        """Send bytes to the serial port (thread-safe)."""
        try:
            if self._ser and self._ser.is_open:
                self._ser.write(data)
        except Exception:
            pass

    def stop(self):
        self.running = False
        if self._ser and self._ser.is_open:
            try:
                self._ser.close()
            except Exception:
                pass
        self.wait()


# ─────────────────────────────────────────────
#  Main Window
# ─────────────────────────────────────────────
class ECGMonitor(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ECG Monitor v4.0")
        self.setMinimumSize(1200, 780)
        self.setStyleSheet(self._stylesheet())

        self.buffer_size   = 500
        self.raw_data      = deque(maxlen=self.buffer_size)
        self.filtered_data = deque(maxlen=self.buffer_size)
        self.time_data     = deque(maxlen=self.buffer_size)
        self.sample_count  = 0

        self.is_recording  = False
        self.is_paused     = False
        self.recorded_data = []

        self.wifi_reader   = None
        self._recordings   = []

        # ── Delete selection state ────────────────────────────────
        self._delete_selected_row = -1   # row index marked for deletion

        self._setup_ui()
        self._setup_plots()

        self.plot_timer = QTimer()
        self.plot_timer.timeout.connect(self._update_plots)
        self.plot_timer.start(50)

        self.blink_timer   = QTimer()
        self.blink_timer.timeout.connect(self._blink_dot)
        self.blink_visible = True

        self._retry_thread = PendingRetryThread()
        self._retry_thread.uploaded.connect(self._on_pending_retry)
        self._retry_thread.start()

        self._list_timer = QTimer()
        self._list_timer.timeout.connect(self._refresh_recordings)
        self._list_timer.start(30000)
        QTimer.singleShot(500, self._refresh_recordings)

    # ── Stylesheet ───────────────────────────────────────────────────────
    def _stylesheet(self):
        return """
        QMainWindow, QWidget  { background: #0d1117; }
        QLabel                { color: #c9d1d9; font-size: 13px; }
        QLineEdit {
            background: #161b22; color: #c9d1d9;
            border: 1px solid #30363d; padding: 7px 14px;
            font-size: 13px; border-radius: 6px;
        }
        QLineEdit:focus       { border-color: #58a6ff; }
        QPushButton {
            background: #21262d; color: #c9d1d9;
            border: 1px solid #30363d; padding: 8px 18px;
            font-size: 13px; font-weight: bold;
            border-radius: 6px;
        }
        QPushButton:hover     { background: #30363d; border-color: #58a6ff; }
        QPushButton:disabled  { background: #161b22; color: #484f58; border-color: #21262d; }
        QPushButton#connectBtn  { background: #1f6feb; border-color: #388bfd; color: #fff; }
        QPushButton#connectBtn:hover { background: #388bfd; }
        QPushButton#recordBtn   { background: #b91c1c; border-color: #f85149; color: #fff; }
        QPushButton#recordBtn:hover  { background: #dc2626; }
        QPushButton#pauseBtn    { background: #1d4ed8; border-color: #60a5fa; color: #fff; }
        QPushButton#stopBtn     { background: #92400e; border-color: #f59e0b; color: #fff; }
        QPushButton#uploadBtn   { background: #166534; border-color: #3fb950; color: #fff; }
        QPushButton#uploadBtn:hover  { background: #16a34a; }
        QPushButton#refreshBtn  { background: #1e3a5f; border-color: #58a6ff; color: #58a6ff; }
        QPushButton#selectBtn   { background: #1a3a4a; border-color: #38bdf8; color: #7dd3fc; }
        QPushButton#selectBtn:hover  { background: #1e4a5f; }
        QPushButton#selectBtnActive { background: #0c4a6e; border-color: #38bdf8; color: #38bdf8; }
        QPushButton#deleteBtn   { background: #7f1d1d; border-color: #f85149; color: #fca5a5; }
        QPushButton#deleteBtn:hover  { background: #991b1b; }
        QPushButton#datasetBtn  { background: #4a1d96; border-color: #a78bfa; color: #ddd6fe; }
        QPushButton#datasetBtn:hover { background: #5b21b6; }
        QPushButton#apiBtn      { background: #064e3b; border-color: #34d399; color: #a7f3d0; }
        QPushButton#apiBtn:hover { background: #065f46; }
        QPushButton#serialBtn   { background: #1c1f2e; border-color: #f0c040; color: #f0c040; }
        QPushButton#serialBtn:hover { background: #2a2e42; }
        QFrame#statusFrame {
            background: #161b22; border: 1px solid #30363d;
            border-radius: 10px;
        }
        QSplitter::handle     { background: #30363d; }
        QTableWidget {
            background: #161b22; color: #c9d1d9;
            gridline-color: #21262d; border: 1px solid #30363d;
            border-radius: 6px; font-size: 12px;
        }
        QTableWidget::item:selected { background: #1f6feb; color: #fff; }
        QHeaderView::section {
            background: #21262d; color: #8b949e;
            padding: 6px; border: none;
            border-bottom: 1px solid #30363d;
            font-size: 11px; font-weight: bold; letter-spacing: 0.5px;
        }
        QTableWidget::item:hover { background: #1c2128; }
        QScrollBar:vertical { background: #161b22; width: 8px; border-radius: 4px; }
        QScrollBar::handle:vertical { background: #30363d; border-radius: 4px; }
        """

    # ── UI Layout ────────────────────────────────────────────────────────
    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        # ── Top bar ──────────────────────────────────────────────────
        top = QHBoxLayout()
        top.setSpacing(8)

        lbl = QLabel("ESP32 IP:")
        lbl.setFixedWidth(65)
        self.ip_input = QLineEdit()
        self.ip_input.setPlaceholderText("192.168.x.xx")
        self.ip_input.setFixedWidth(160)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("connectBtn")
        self.connect_btn.setFixedWidth(110)
        self.connect_btn.clicked.connect(self._toggle_connection)

        self.record_btn = QPushButton("⏺ Record")
        self.record_btn.setObjectName("recordBtn")
        self.record_btn.clicked.connect(self._start_recording)
        self.record_btn.setEnabled(False)

        self.pause_btn = QPushButton("⏸ Pause")
        self.pause_btn.setObjectName("pauseBtn")
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.pause_btn.setEnabled(False)

        self.stop_btn = QPushButton("⏹ Stop")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.clicked.connect(self._stop_recording)
        self.stop_btn.setEnabled(False)

        self.upload_btn = QPushButton("☁ Upload")
        self.upload_btn.setObjectName("uploadBtn")
        self.upload_btn.clicked.connect(self._upload_recording)
        self.upload_btn.setEnabled(False)

        top.addWidget(lbl)
        top.addWidget(self.ip_input)
        top.addWidget(self.connect_btn)
        top.addSpacing(16)
        top.addWidget(self.record_btn)
        top.addWidget(self.pause_btn)
        top.addWidget(self.stop_btn)
        top.addWidget(self.upload_btn)
        top.addStretch()

        self.conn_label = QLabel("● Disconnected")
        self.conn_label.setStyleSheet("color:#f85149; font-weight:bold;")
        self.rec_dot = QLabel("●")
        self.rec_dot.setStyleSheet("color:#484f58; font-size:14px;")
        self.rec_label = QLabel("Not recording")
        self.rec_label.setStyleSheet("color:#8b949e;")

        top.addWidget(self.conn_label)
        top.addSpacing(12)
        top.addWidget(self.rec_dot)
        top.addWidget(self.rec_label)
        root.addLayout(top)

        # ── Status bar ───────────────────────────────────────────────
        self.status_bar = QLabel("")
        self.status_bar.setStyleSheet(
            "background:#161b22; border:1px solid #30363d; border-radius:6px;"
            "padding:5px 12px; color:#8b949e; font-size:12px;"
        )
        root.addWidget(self.status_bar)

        # ── Main splitter ─────────────────────────────────────────────
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(6)

        # Left — ECG plots + BPM
        left = QWidget()
        llay = QVBoxLayout(left)
        llay.setContentsMargins(0, 0, 6, 0)
        llay.setSpacing(8)

        bpm_frame = QFrame()
        bpm_frame.setObjectName("statusFrame")
        bpm_lay = QHBoxLayout(bpm_frame)
        bpm_lay.setContentsMargins(16, 10, 16, 10)

        bpm_col = QVBoxLayout()
        bpm_title = QLabel("HEART RATE")
        bpm_title.setStyleSheet("color:#8b949e; font-size:10px; letter-spacing:1px;")

        self.bpm_label = QLabel("--")
        # ── FIX: inline stylesheet overrides global QLabel font-size:13px ──
        self.bpm_label.setStyleSheet(
            "color:#f85149;"
            "font-size:52pt;"
            "font-weight:bold;"
            "font-family:'Courier New';"
        )

        bpm_unit = QLabel("BPM")
        bpm_unit.setStyleSheet("color:#8b949e; font-size:12px;")
        bpm_col.addWidget(bpm_title)
        bpm_col.addWidget(self.bpm_label)
        bpm_col.addWidget(bpm_unit)

        right_info = QVBoxLayout()
        right_info.setSpacing(6)
        self.finger_label = QLabel("Finger: —")
        self.finger_label.setStyleSheet("color:#8b949e;")
        self.samples_label = QLabel("Recorded: 0 samples")
        self.samples_label.setStyleSheet("color:#8b949e; font-size:12px;")
        right_info.addStretch()
        right_info.addWidget(self.finger_label)
        right_info.addWidget(self.samples_label)
        right_info.addStretch()

        bpm_lay.addLayout(bpm_col)
        bpm_lay.addStretch()
        bpm_lay.addLayout(right_info)
        llay.addWidget(bpm_frame)

        self.plot_widget = pg.GraphicsLayoutWidget()
        self.plot_widget.setBackground("#0d1117")
        llay.addWidget(self.plot_widget, stretch=1)

        splitter.addWidget(left)

        # Right — Recordings panel
        right_panel = QWidget()
        rlay = QVBoxLayout(right_panel)
        rlay.setContentsMargins(6, 0, 0, 0)
        rlay.setSpacing(8)

        rec_hdr = QHBoxLayout()
        rec_title = QLabel("☁  SERVER RECORDINGS")
        rec_title.setStyleSheet(
            "color:#58a6ff; font-size:13px; font-weight:bold; letter-spacing:0.5px;"
        )
        self.refresh_btn = QPushButton("↻ Refresh")
        self.refresh_btn.setObjectName("refreshBtn")
        self.refresh_btn.setFixedWidth(90)
        self.refresh_btn.clicked.connect(self._refresh_recordings)
        rec_hdr.addWidget(rec_title)
        rec_hdr.addStretch()
        rec_hdr.addWidget(self.refresh_btn)
        rlay.addLayout(rec_hdr)

        hint = QLabel("Click row → view CSV  |  Select → mark for Delete")
        hint.setStyleSheet("color:#484f58; font-size:11px;")
        rlay.addWidget(hint)

        self.rec_table = QTableWidget(0, 3)
        self.rec_table.setHorizontalHeaderLabels(["FILE NAME", "SIZE", "UPLOADED"])
        self.rec_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.rec_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self.rec_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Fixed)
        self.rec_table.setColumnWidth(1, 75)
        self.rec_table.setColumnWidth(2, 140)
        self.rec_table.verticalHeader().setVisible(False)
        self.rec_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.rec_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.rec_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.rec_table.clicked.connect(self._on_recording_clicked)
        rlay.addWidget(self.rec_table, stretch=1)

        # ── Selected-for-delete indicator ────────────────────────────
        self.selected_label = QLabel("No file selected for delete")
        self.selected_label.setStyleSheet(
            "color:#484f58; font-size:11px; font-style:italic; padding:2px 4px;"
        )
        rlay.addWidget(self.selected_label)

        # ── Action buttons row ────────────────────────────────────────
        #
        #   [ ☑ Select ]  [ 🗑 Delete ]  [ ⚙ Generate Dataset ]  [ 🔑 API ]  [ 🖥 Serial ]
        #
        action_row = QHBoxLayout()
        action_row.setSpacing(6)

        self.select_btn = QPushButton("☑ Select")
        self.select_btn.setObjectName("selectBtn")
        self.select_btn.setToolTip("Mark highlighted row for deletion")
        self.select_btn.clicked.connect(self._select_for_delete)

        self.delete_btn = QPushButton("🗑 Delete")
        self.delete_btn.setObjectName("deleteBtn")
        self.delete_btn.setToolTip("Delete the selected recording from server")
        self.delete_btn.setEnabled(False)
        self.delete_btn.clicked.connect(self._delete_selected)

        self.dataset_btn = QPushButton("⚙ Dataset")
        self.dataset_btn.setObjectName("datasetBtn")
        self.dataset_btn.setToolTip("Merge all server recordings into one CSV")
        self.dataset_btn.clicked.connect(self._generate_dataset)

        self.api_btn = QPushButton("🔑 API")
        self.api_btn.setObjectName("apiBtn")
        self.api_btn.setToolTip("Copy API credentials & ML code snippets")
        self.api_btn.clicked.connect(self._open_api_panel)

        self.serial_btn = QPushButton("🖥 Serial")
        self.serial_btn.setObjectName("serialBtn")
        self.serial_btn.setToolTip("Open Serial Monitor — real UART/USB port")
        self.serial_btn.clicked.connect(self._open_serial_monitor)

        action_row.addWidget(self.select_btn)
        action_row.addWidget(self.delete_btn)
        action_row.addWidget(self.dataset_btn)
        action_row.addWidget(self.api_btn)
        action_row.addWidget(self.serial_btn)
        rlay.addLayout(action_row)

        self.rec_status = QLabel("")
        self.rec_status.setStyleSheet("color:#8b949e; font-size:11px;")
        rlay.addWidget(self.rec_status)

        splitter.addWidget(right_panel)
        splitter.setSizes([720, 380])
        root.addWidget(splitter, stretch=1)

    def _setup_plots(self):
        self.raw_plot  = self.plot_widget.addPlot(row=0, col=0, title="Raw ECG")
        self.raw_plot.showGrid(True, True, alpha=0.15)
        self.raw_plot.getAxis("left").setTextPen("#8b949e")
        self.raw_plot.getAxis("bottom").setTextPen("#8b949e")
        self.raw_plot.titleLabel.setAttr("color", "#8b949e")
        self.raw_curve = self.raw_plot.plot(pen=pg.mkPen("#58a6ff", width=1.5))

        self.filt_plot  = self.plot_widget.addPlot(row=1, col=0, title="Filtered ECG")
        self.filt_plot.showGrid(True, True, alpha=0.15)
        self.filt_plot.getAxis("left").setTextPen("#8b949e")
        self.filt_plot.getAxis("bottom").setTextPen("#8b949e")
        self.filt_plot.titleLabel.setAttr("color", "#8b949e")
        self.filt_curve = self.filt_plot.plot(pen=pg.mkPen("#3fb950", width=1.5))

    # ── Connection ───────────────────────────────────────────────────────
    def _toggle_connection(self):
        if self.wifi_reader and self.wifi_reader.running:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        ip = self.ip_input.text().strip()
        if not ip:
            QMessageBox.warning(self, "No IP", "Enter the ESP32 IP address.")
            return
        self.connect_btn.setText("Connecting…")
        self.connect_btn.setEnabled(False)
        self.wifi_reader = WiFiReader(ip)
        self.wifi_reader.data_received.connect(self._on_data)
        self.wifi_reader.connection_error.connect(self._on_conn_error)
        self.wifi_reader.connected.connect(self._on_connected)
        self.wifi_reader.start()

    def _on_connected(self):
        self.connect_btn.setText("Disconnect")
        self.connect_btn.setEnabled(True)
        self.connect_btn.setObjectName("")
        self.conn_label.setText("● Connected")
        self.conn_label.setStyleSheet("color:#3fb950; font-weight:bold;")
        self.record_btn.setEnabled(True)
        self.ip_input.setEnabled(False)
        self._set_status("Connected to ESP32 — ready to record")

    def _disconnect(self):
        if self.is_recording:
            self._stop_recording()
        if self.wifi_reader:
            self.wifi_reader.stop()
            self.wifi_reader = None
        self.connect_btn.setText("Connect")
        self.connect_btn.setEnabled(True)
        self.connect_btn.setObjectName("connectBtn")
        self.conn_label.setText("● Disconnected")
        self.conn_label.setStyleSheet("color:#f85149; font-weight:bold;")
        self.record_btn.setEnabled(False)
        self.ip_input.setEnabled(True)
        self._set_status("Disconnected")

    def _on_conn_error(self, error):
        QMessageBox.critical(self, "Connection Error", f"Failed to connect:\n{error}")
        self._disconnect()

    # ── Data ─────────────────────────────────────────────────────────────
    def _on_data(self, raw, filtered, bpm, finger):
        self.sample_count += 1
        self.time_data.append(self.sample_count)
        self.raw_data.append(raw)
        self.filtered_data.append(filtered)

        if finger:
            self.bpm_label.setText(str(bpm) if bpm > 0 else "--")
            self.finger_label.setText("Finger: Detected ✓")
            self.finger_label.setStyleSheet("color:#3fb950;")
        else:
            self.bpm_label.setText("--")
            self.finger_label.setText("Finger: —")
            self.finger_label.setStyleSheet("color:#8b949e;")

        if self.is_recording and not self.is_paused:
            self.recorded_data.append(
                [datetime.now(timezone.utc).isoformat(), raw, filtered, bpm, finger]
            )
            self.samples_label.setText(f"Recorded: {len(self.recorded_data)} samples")

    # ── Recording ────────────────────────────────────────────────────────
    def _start_recording(self):
        self.is_recording  = True
        self.is_paused     = False
        self.recorded_data = []
        self.upload_btn.setEnabled(False)
        self.record_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        self.rec_label.setText("Recording…")
        self.rec_label.setStyleSheet("color:#f85149; font-weight:bold;")
        self.rec_dot.setStyleSheet("color:#f85149; font-size:14px;")
        self.samples_label.setText("Recorded: 0 samples")
        self.blink_visible = True
        self.blink_timer.start(500)
        self._set_status("Recording started")

    def _toggle_pause(self):
        self.is_paused = not self.is_paused
        if self.is_paused:
            self.pause_btn.setText("▶ Resume")
            self.rec_label.setText("Paused")
            self.rec_label.setStyleSheet("color:#f59e0b; font-weight:bold;")
            self.blink_timer.stop()
            self.rec_dot.setStyleSheet("color:#f59e0b; font-size:14px;")
        else:
            self.pause_btn.setText("⏸ Pause")
            self.rec_label.setText("Recording…")
            self.rec_label.setStyleSheet("color:#f85149; font-weight:bold;")
            self.rec_dot.setStyleSheet("color:#f85149; font-size:14px;")
            self.blink_visible = True
            self.blink_timer.start(500)

    def _stop_recording(self):
        self.is_recording = False
        self.is_paused    = False
        self.record_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.pause_btn.setText("⏸ Pause")
        self.rec_label.setText("Not recording")
        self.rec_label.setStyleSheet("color:#8b949e;")
        self.rec_dot.setStyleSheet("color:#484f58; font-size:14px;")
        self.blink_timer.stop()

        if not self.recorded_data:
            self._set_status("Stopped — no data recorded")
            return

        n = len(self.recorded_data)
        self._set_status(f"Stopped — {n} samples captured. Click ☁ Upload to save.")
        self.upload_btn.setEnabled(True)

    def _blink_dot(self):
        color = "transparent" if self.blink_visible else "#f85149"
        self.rec_dot.setStyleSheet(f"color:{color}; font-size:14px;")
        self.blink_visible = not self.blink_visible

    # ── Upload ───────────────────────────────────────────────────────────
    def _upload_recording(self):
        if not self.recorded_data:
            QMessageBox.warning(self, "No Data", "No recorded data to upload.")
            return

        try:
            first_ts = self.recorded_data[0][0]
            dt = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
            default_name = dt.strftime("ecg_%Y%m%d_%H%M%S") + ".csv"
        except Exception:
            default_name = datetime.now().strftime("ecg_%Y%m%d_%H%M%S") + ".csv"

        name, ok = QInputDialog.getText(
            self, "File Name", "CSV filename on server:", text=default_name
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        if not name.endswith(".csv"):
            name += ".csv"
        name = re.sub(r"[^\w\-.]", "_", name)

        csv_bytes = build_csv_bytes(self.recorded_data)

        os.makedirs(RECORDS_DIR, exist_ok=True)
        local_path = os.path.join(RECORDS_DIR, name)
        with open(local_path, "wb") as f:
            f.write(csv_bytes)

        self.progress_dlg = QProgressDialog(
            f"Uploading {name} to Supabase…", None, 0, 100, self
        )
        self.progress_dlg.setWindowTitle("☁ Uploading")
        self.progress_dlg.setWindowModality(Qt.WindowModal)
        self.progress_dlg.setCancelButton(None)
        self.progress_dlg.setValue(0)
        self.progress_dlg.show()

        self.upload_btn.setEnabled(False)
        self._uploader = UploadThread(name, csv_bytes)
        self._uploader.progress.connect(self.progress_dlg.setValue)
        self._uploader.finished.connect(self._on_upload_done)
        self._uploader.start()

    def _on_upload_done(self, success, message, url):
        self.progress_dlg.close()
        if success:
            self._set_status(f"☁ {message}")
            QTimer.singleShot(1500, self._refresh_recordings)
        else:
            self._set_status("✗ Upload failed — saved locally")
            self.upload_btn.setEnabled(True)
        QMessageBox.information(self, "Upload Result", message)

    # ── SELECT for delete ─────────────────────────────────────────────────
    def _select_for_delete(self):
        """Mark the currently highlighted table row for deletion."""
        row = self.rec_table.currentRow()
        if row < 0 or row >= len(self._recordings):
            QMessageBox.information(
                self, "No Row Highlighted",
                "Click a row in the table first, then press Select."
            )
            return

        # Clear previous selection highlight
        self._clear_delete_highlight()

        self._delete_selected_row = row
        item = self._recordings[row]

        # Highlight the selected row in red tint
        for col in range(self.rec_table.columnCount()):
            cell = self.rec_table.item(row, col)
            if cell:
                cell.setBackground(QColor("#3d1a1a"))
                cell.setForeground(QColor("#fca5a5"))

        self.selected_label.setText(f"🎯 Selected for delete:  {item['name']}")
        self.selected_label.setStyleSheet(
            "color:#fca5a5; font-size:11px; font-weight:bold; padding:2px 4px;"
        )
        self.delete_btn.setEnabled(True)
        self._set_status(f"Selected: {item['name']} — click 🗑 Delete to remove")

    def _clear_delete_highlight(self):
        """Remove red highlight from previously selected row."""
        if self._delete_selected_row >= 0:
            prev = self._delete_selected_row
            if prev < self.rec_table.rowCount():
                for col in range(self.rec_table.columnCount()):
                    cell = self.rec_table.item(prev, col)
                    if cell:
                        cell.setBackground(QColor("transparent"))
                        # Restore original colors
                        if col == 0:
                            cell.setForeground(QColor("#58a6ff"))
                        else:
                            cell.setForeground(QColor("#8b949e"))
        self._delete_selected_row = -1
        self.selected_label.setText("No file selected for delete")
        self.selected_label.setStyleSheet(
            "color:#484f58; font-size:11px; font-style:italic; padding:2px 4px;"
        )
        self.delete_btn.setEnabled(False)

    # ── DELETE selected recording ─────────────────────────────────────────
    def _delete_selected(self):
        row = self._delete_selected_row
        if row < 0 or row >= len(self._recordings):
            QMessageBox.warning(self, "Nothing Selected",
                                "Use the ☑ Select button to mark a recording first.")
            return

        item = self._recordings[row]
        reply = QMessageBox.question(
            self, "Confirm Delete",
            f"Permanently delete from server?\n\n  📄 {item['name']}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self.delete_btn.setEnabled(False)
        self.select_btn.setEnabled(False)
        self.rec_status.setText(f"Deleting {item['name']}…")
        self._delete_thread = DeleteThread(item["name"])
        self._delete_thread.finished.connect(self._on_delete_done)
        self._delete_thread.start()

    def _on_delete_done(self, ok, message, filename):
        self.select_btn.setEnabled(True)
        self._delete_selected_row = -1
        self.selected_label.setText("No file selected for delete")
        self.selected_label.setStyleSheet(
            "color:#484f58; font-size:11px; font-style:italic; padding:2px 4px;"
        )
        self.delete_btn.setEnabled(False)

        if ok:
            self._set_status(f"🗑 {message}")
            self.rec_status.setText(message)
            QTimer.singleShot(800, self._refresh_recordings)
        else:
            self.delete_btn.setEnabled(True)
            self.rec_status.setText(f"✗ {message}")
            QMessageBox.warning(self, "Delete Failed", message)

    # ── GENERATE DATASET ──────────────────────────────────────────────────
    def _generate_dataset(self):
        if not self._recordings:
            QMessageBox.warning(self, "No Recordings",
                                "No server recordings found. Refresh first.")
            return

        reply = QMessageBox.question(
            self, "Generate Dataset",
            f"Merge all {len(self._recordings)} recordings into one CSV?\n\n"
            "This will download every file from the server.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return

        self.progress_dlg = QProgressDialog(
            "Downloading & merging recordings…", None, 0, len(self._recordings), self
        )
        self.progress_dlg.setWindowTitle("⚙ Generating Dataset")
        self.progress_dlg.setWindowModality(Qt.WindowModal)
        self.progress_dlg.setCancelButton(None)
        self.progress_dlg.setValue(0)
        self.progress_dlg.show()

        self.dataset_btn.setEnabled(False)
        self._dataset_thread = GenerateDatasetThread(list(self._recordings))
        self._dataset_thread.progress.connect(
            lambda cur, tot: self.progress_dlg.setValue(cur)
        )
        self._dataset_thread.finished.connect(self._on_dataset_done)
        self._dataset_thread.start()

    def _on_dataset_done(self, ok, message, zip_bytes, total_samples, file_count):
        self.progress_dlg.close()
        self.dataset_btn.setEnabled(True)

        if not ok:
            QMessageBox.warning(self, "Dataset Error", message)
            return

        self._set_status(f"⚙ {message}")

        from PyQt5.QtWidgets import QFileDialog
        default = datetime.now().strftime("ecg_dataset_%Y%m%d_%H%M%S.zip")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Dataset ZIP", default, "ZIP Files (*.zip)"
        )
        if path:
            with open(path, "wb") as f:
                f.write(zip_bytes)
            QMessageBox.information(
                self, "Dataset Saved",
                f"{message}\n\n"
                f"📦 ZIP contains:\n"
                f"  • dataset_merged.csv  ({total_samples} rows)\n"
                f"  • recordings/  ({file_count} individual CSV files)\n\n"
                f"Saved to:\n{path}"
            )

    # ── API PANEL ─────────────────────────────────────────────────────────
    def _open_api_panel(self):
        dlg = APIPanelDialog(self)
        dlg.exec_()

    # ── SERIAL MONITOR ────────────────────────────────────────────────────
    def _open_serial_monitor(self):
        dlg = SerialMonitorDialog(parent=self)
        dlg.exec_()

    # ── Recordings list ───────────────────────────────────────────────────
    def _refresh_recordings(self):
        self._clear_delete_highlight()
        self.rec_status.setText("Refreshing…")
        self.refresh_btn.setEnabled(False)
        t = ListThread()
        t.result.connect(self._on_list_result)
        t.start()
        self._list_thread = t

    def _on_list_result(self, items):
        self._recordings = items
        self.rec_table.setRowCount(0)
        self.refresh_btn.setEnabled(True)

        if not items:
            self.rec_status.setText("No recordings on server yet.")
            return

        for item in items:
            row = self.rec_table.rowCount()
            self.rec_table.insertRow(row)

            name_item = QTableWidgetItem(item["name"])
            name_item.setForeground(QColor("#58a6ff"))
            name_item.setToolTip("Click to view CSV")
            self.rec_table.setItem(row, 0, name_item)

            size_kb = item["size"] / 1024 if item["size"] else 0
            size_item = QTableWidgetItem(f"{size_kb:.1f} KB" if size_kb >= 1
                                         else f"{item['size']} B")
            size_item.setTextAlignment(Qt.AlignCenter)
            size_item.setForeground(QColor("#8b949e"))
            self.rec_table.setItem(row, 1, size_item)

            ts = item["created_at"][:19].replace("T", " ") if item["created_at"] else "—"
            ts_item = QTableWidgetItem(ts)
            ts_item.setTextAlignment(Qt.AlignCenter)
            ts_item.setForeground(QColor("#8b949e"))
            self.rec_table.setItem(row, 2, ts_item)

        self.rec_status.setText(f"{len(items)} recording(s) on server  •  click to open")

    def _on_recording_clicked(self, index):
        row = index.row()
        if row < 0 or row >= len(self._recordings):
            return
        item = self._recordings[row]
        self.rec_status.setText(f"Loading {item['name']}…")
        t = FetchCSVThread(item["url"], item["name"])
        t.result.connect(self._on_csv_fetched)
        t.start()
        self._fetch_thread = t

    def _on_csv_fetched(self, ok, content, filename):
        if ok:
            self.rec_status.setText(f"Opened: {filename}")
            dlg = CSVViewerDialog(filename, content, self)
            dlg.exec_()
        else:
            self.rec_status.setText(f"Failed to load: {content}")
            QMessageBox.warning(self, "Error", f"Could not fetch CSV:\n{content}")

    def _on_pending_retry(self, name):
        self._set_status(f"☁ Auto-uploaded: {name}")
        QTimer.singleShot(2000, self._refresh_recordings)

    # ── Plots ─────────────────────────────────────────────────────────────
    def _update_plots(self):
        self.raw_curve.setData(list(self.time_data), list(self.raw_data))
        self.filt_curve.setData(list(self.time_data), list(self.filtered_data))

    def _set_status(self, msg):
        self.status_bar.setText(f"  {msg}")

    def closeEvent(self, event):
        if self.wifi_reader:
            self.wifi_reader.stop()
        self._retry_thread.stop()
        event.accept()


# ─────────────────────────────────────────────
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = ECGMonitor()
    window.show()
    sys.exit(app.exec_())
