import sys
import os
import hashlib
import shutil
import json
import sqlite3
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from send2trash import send2trash

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QListWidget, QFileDialog, QLabel, QMessageBox,
    QTreeWidget, QTreeWidgetItem, QComboBox, QProgressBar
)
from PySide6.QtCore import Qt, QThread, Signal

APP_NAME = "Duplicate File Finder"

# Proper Windows app storage location
BASE_DIR = os.path.join(os.getenv("APPDATA"), "DuplicateFinder")
os.makedirs(BASE_DIR, exist_ok=True)

DB_FILE = os.path.join(BASE_DIR, "file_index.db")
UNDO_FILE = os.path.join(BASE_DIR, "undo_log.json")

CHUNK = 8192
PARTIAL = 65536
WORKERS = max(2, os.cpu_count() or 4)


# ------------------ Hashing ------------------
def hash_file(path, full=False):
    h = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            if full:
                while True:
                    b = f.read(CHUNK)
                    if not b:
                        break
                    h.update(b)
            else:
                h.update(f.read(PARTIAL))
        return h.hexdigest()
    except:
        return None


# ------------------ DB Cache ------------------
class Cache:
    def __init__(self, db=DB_FILE):
        self.conn = sqlite3.connect(db)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS files(
                path TEXT PRIMARY KEY,
                size INTEGER,
                mtime REAL,
                phash TEXT,
                fhash TEXT
            )
        """)

    def get(self, path, size, mtime):
        cur = self.conn.execute(
            "SELECT phash, fhash FROM files WHERE path=? AND size=? AND mtime=?",
            (path, size, mtime)
        )
        return cur.fetchone()

    def set(self, path, size, mtime, ph, fh):
        self.conn.execute(
            "REPLACE INTO files VALUES (?,?,?,?,?)",
            (path, size, mtime, ph, fh)
        )
        self.conn.commit()


cache = Cache()


# ------------------ Worker ------------------
class ScanWorker(QThread):
    progress = Signal(int)
    status = Signal(str)
    finished = Signal(list)

    def __init__(self, sources, scans):
        super().__init__()
        self.sources = sources
        self.scans = scans

    def collect(self, dirs):
        out = []
        for d in dirs:
            for root, _, files in os.walk(d):
                for f in files:
                    p = os.path.join(root, f)
                    try:
                        st = os.stat(p)
                        out.append((p, st.st_size, st.st_mtime))
                    except:
                        pass
        return out

    def run(self):
        self.status.emit("Collecting files...")
        src = self.collect(self.sources)
        scn = self.collect(self.scans)

        size_map = {}
        for p, s, _ in src:
            size_map.setdefault(s, []).append(p)

        def process_file(p, s, m):
            cached = cache.get(p, s, m)
            if cached:
                return (p, s, cached[0], cached[1])

            ph = hash_file(p, False)
            fh = hash_file(p, True) if ph else None
            cache.set(p, s, m, ph, fh)
            return (p, s, ph, fh)

        self.status.emit("Indexing source...")
        src_hash = []
        with ThreadPoolExecutor(WORKERS) as ex:
            futures = [ex.submit(process_file, p, s, m) for p, s, m in src]
            for i, f in enumerate(as_completed(futures)):
                src_hash.append(f.result())
                self.progress.emit(int(i / len(futures) * 30))

        index = {}
        for p, s, ph, fh in src_hash:
            if fh:
                index.setdefault((s, fh), []).append(p)

        self.status.emit("Scanning...")
        results = []
        with ThreadPoolExecutor(WORKERS) as ex:
            futures = [ex.submit(process_file, p, s, m) for p, s, m in scn]
            for i, f in enumerate(as_completed(futures)):
                p, s, ph, fh = f.result()
                if fh and (s, fh) in index:
                    results.append((p, index[(s, fh)]))
                self.progress.emit(30 + int(i / len(futures) * 70))

        self.finished.emit(results)


# ------------------ UI ------------------
class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1000, 650)

        self.src = QListWidget()
        self.scn = QListWidget()

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Duplicate", "Source Match"])

        self.status = QLabel("Ready")
        self.bar = QProgressBar()

        self.action = QComboBox()
        self.action.addItems(["Prompt", "Delete", "Move to Backup", "Ignore"])

        addS = QPushButton("Add Source")
        addC = QPushButton("Add Scan")
        run = QPushButton("Run")
        save = QPushButton("Save Config")
        load = QPushButton("Load Config")
        undo = QPushButton("Undo Last")

        addS.clicked.connect(lambda: self.add(self.src))
        addC.clicked.connect(lambda: self.add(self.scn))
        run.clicked.connect(self.start)
        save.clicked.connect(self.save_cfg)
        load.clicked.connect(self.load_cfg)
        undo.clicked.connect(self.undo_last)

        top = QHBoxLayout()
        top.addWidget(self.src)
        top.addWidget(self.scn)

        btns = QHBoxLayout()
        for b in (addS, addC, run, save, load, undo):
            btns.addWidget(b)

        layout = QVBoxLayout()
        layout.addLayout(top)
        layout.addLayout(btns)
        layout.addWidget(self.action)
        layout.addWidget(self.tree)
        layout.addWidget(self.bar)
        layout.addWidget(self.status)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

    def add(self, lst):
        p = QFileDialog.getExistingDirectory(self)
        if p:
            lst.addItem(p)

    def paths(self, lst):
        return [Path(lst.item(i).text()).resolve() for i in range(lst.count())]

    def validate(self):
        s = self.paths(self.src)
        c = self.paths(self.scn)
        for a in s:
            for b in c:
                if a in b.parents or b in a.parents or a == b:
                    QMessageBox.critical(self, "Error", f"Overlap:\n{a}\n{b}")
                    return False
        return True

    def start(self):
        if not self.validate():
            return
        self.tree.clear()
        self.worker = ScanWorker(
            [str(p) for p in self.paths(self.src)],
            [str(p) for p in self.paths(self.scn)]
        )
        self.worker.progress.connect(self.bar.setValue)
        self.worker.status.connect(self.status.setText)
        self.worker.finished.connect(self.done)
        self.worker.start()

    def done(self, res):
        self.bar.setValue(100)
        self.status.setText(f"Found {len(res)} duplicates")
        for p, matches in res:
            parent = QTreeWidgetItem([p])
            for m in matches:
                parent.addChild(QTreeWidgetItem(["", m]))
            self.tree.addTopLevelItem(parent)

        self.apply_actions(res)

    def apply_actions(self, res):
        mode = self.action.currentText()
        backup = None
        log = []

        if mode == "Move to Backup":
            backup = QFileDialog.getExistingDirectory(self, "Select Backup Folder")
            if not backup:
                return

        for p, _ in res:
            if mode == "Prompt":
                choice = QMessageBox.question(
                    self, "Action", f"Delete file?\n{p}",
                    QMessageBox.Yes | QMessageBox.No
                )
                if choice != QMessageBox.Yes:
                    continue
                mode_exec = "Delete"
            else:
                mode_exec = mode

            if mode_exec == "Delete":
                try:
                    send2trash(p)
                    log.append({"type": "delete", "path": p})
                except:
                    pass

            elif mode_exec == "Move to Backup" and backup:
                try:
                    rel = os.path.relpath(p, start=self.scn.item(0).text())
                    dest = os.path.join(backup, rel)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.move(p, dest)
                    log.append({"type": "move", "src": p, "dest": dest})
                except:
                    pass

        if log:
            with open(UNDO_FILE, "w") as f:
                json.dump(log, f, indent=2)

    def undo_last(self):
        if not os.path.exists(UNDO_FILE):
            QMessageBox.information(self, "Undo", "No undo data found")
            return

        with open(UNDO_FILE) as f:
            log = json.load(f)

        for entry in reversed(log):
            try:
                if entry["type"] == "move":
                    os.makedirs(os.path.dirname(entry["src"]), exist_ok=True)
                    shutil.move(entry["dest"], entry["src"])
            except:
                pass

        QMessageBox.information(
            self,
            "Undo",
            "Undo complete.\n(Moved files restored. Restore deleted files from Recycle Bin manually.)"
        )

    def save_cfg(self):
        data = {
            "src": [self.src.item(i).text() for i in range(self.src.count())],
            "scn": [self.scn.item(i).text() for i in range(self.scn.count())]
        }
        with open(os.path.join(BASE_DIR, "config.json"), "w") as f:
            json.dump(data, f)

    def load_cfg(self):
        try:
            with open(os.path.join(BASE_DIR, "config.json")) as f:
                d = json.load(f)
            self.src.clear()
            self.scn.clear()
            for p in d.get("src", []):
                self.src.addItem(p)
            for p in d.get("scn", []):
                self.scn.addItem(p)
        except:
            pass


# ------------------ Run ------------------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = Main()
    window.show()
    sys.exit(app.exec())
