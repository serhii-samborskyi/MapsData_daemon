#!/usr/bin/env python3
from __future__ import annotations

import os
import sys

LOCAL_DEPS = os.path.join(os.path.dirname(__file__), ".deps")
if os.path.isdir(LOCAL_DEPS) and LOCAL_DEPS not in sys.path:
    sys.path.insert(0, LOCAL_DEPS)
from typing import Any, Dict

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except Exception:
    print("PySide6 is required. Install with: pip install PySide6")
    raise

from daemon_config import DEFAULT_CONFIG, load_config, save_config


class DaemonUI(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.config_path = os.path.join(self.base_dir, "daemon_settings.json")
        self.config: Dict[str, Any] = load_config(self.config_path)

        self.maps_proc = QtCore.QProcess(self)
        self.email_proc = QtCore.QProcess(self)
        self.maps_proc.setProcessChannelMode(QtCore.QProcess.MergedChannels)
        self.email_proc.setProcessChannelMode(QtCore.QProcess.MergedChannels)

        self._build_ui()
        self._load_config_to_form()
        self._wire_process(self.maps_proc, "maps")
        self._wire_process(self.email_proc, "email")

    def _build_ui(self) -> None:
        self.setWindowTitle("Maps + Email Daemon Control")
        self.setMinimumSize(980, 680)

        font = QtGui.QFont("IBM Plex Sans", 10)
        QtWidgets.QApplication.instance().setFont(font)

        central = QtWidgets.QWidget(self)
        self.setCentralWidget(central)

        root_layout = QtWidgets.QHBoxLayout(central)

        left = QtWidgets.QWidget(central)
        left_layout = QtWidgets.QVBoxLayout(left)
        left_layout.setSpacing(16)

        right = QtWidgets.QWidget(central)
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setSpacing(12)

        root_layout.addWidget(left, 3)
        root_layout.addWidget(right, 2)

        header = QtWidgets.QLabel("Maps + Email Daemons")
        header.setObjectName("header")
        header.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        left_layout.addWidget(header)

        general_group = QtWidgets.QGroupBox("General")
        general_form = QtWidgets.QFormLayout(general_group)
        self.maps_base_url = QtWidgets.QLineEdit()
        self.email_base_url = QtWidgets.QLineEdit()
        self.queue_dir = QtWidgets.QLineEdit()
        self.maps_poll_interval = QtWidgets.QSpinBox()
        self.maps_poll_interval.setRange(5, 3600)
        self.email_poll_interval = QtWidgets.QSpinBox()
        self.email_poll_interval.setRange(5, 3600)
        general_form.addRow("Maps base URL", self.maps_base_url)
        general_form.addRow("Email base URL", self.email_base_url)
        general_form.addRow("Queue dir", self.queue_dir)
        general_form.addRow("Maps poll interval (s)", self.maps_poll_interval)
        general_form.addRow("Email poll interval (s)", self.email_poll_interval)

        maps_group = QtWidgets.QGroupBox("Maps Daemon")
        maps_form = QtWidgets.QFormLayout(maps_group)
        self.maps_batch_size = QtWidgets.QSpinBox()
        self.maps_batch_size.setRange(1, 500)
        self.maps_max_concurrent = QtWidgets.QSpinBox()
        self.maps_max_concurrent.setRange(1, 20)
        self.maps_csv_dir = QtWidgets.QLineEdit()
        maps_form.addRow("Batch size", self.maps_batch_size)
        maps_form.addRow("Max concurrent", self.maps_max_concurrent)
        maps_form.addRow("CSV output dir", self.maps_csv_dir)

        email_group = QtWidgets.QGroupBox("Email Daemon")
        email_form = QtWidgets.QFormLayout(email_group)
        self.email_batch = QtWidgets.QSpinBox()
        self.email_batch.setRange(1, 200)
        self.email_concurrency = QtWidgets.QSpinBox()
        self.email_concurrency.setRange(1, 20)
        self.email_timeout = QtWidgets.QDoubleSpinBox()
        self.email_timeout.setRange(1.0, 120.0)
        self.email_timeout.setDecimals(1)
        self.email_domain_timeout = QtWidgets.QDoubleSpinBox()
        self.email_domain_timeout.setRange(5.0, 300.0)
        self.email_domain_timeout.setDecimals(1)
        self.email_links = QtWidgets.QSpinBox()
        self.email_links.setRange(0, 20)
        self.email_max_batches = QtWidgets.QSpinBox()
        self.email_max_batches.setRange(0, 200)
        self.email_max_batches_facebook = QtWidgets.QSpinBox()
        self.email_max_batches_facebook.setRange(0, 200)
        self.email_facebook = QtWidgets.QCheckBox("Enable Facebook scraping")
        email_form.addRow("Batch", self.email_batch)
        email_form.addRow("Concurrency", self.email_concurrency)
        email_form.addRow("Timeout (s)", self.email_timeout)
        email_form.addRow("Domain timeout (s)", self.email_domain_timeout)
        email_form.addRow("Links per domain", self.email_links)
        email_form.addRow("Max batches per run (0 = unlimited)", self.email_max_batches)
        email_form.addRow("Max Facebook batches per run (0 = disabled)", self.email_max_batches_facebook)
        email_form.addRow("", self.email_facebook)

        left_layout.addWidget(general_group)
        left_layout.addWidget(maps_group)
        left_layout.addWidget(email_group)
        left_layout.addStretch(1)

        status_group = QtWidgets.QGroupBox("Status")
        status_layout = QtWidgets.QFormLayout(status_group)
        self.maps_status = QtWidgets.QLabel("Stopped")
        self.email_status = QtWidgets.QLabel("Stopped")
        status_layout.addRow("Maps daemon", self.maps_status)
        status_layout.addRow("Email daemon", self.email_status)

        controls_group = QtWidgets.QGroupBox("Controls")
        controls_layout = QtWidgets.QGridLayout(controls_group)
        self.btn_save = QtWidgets.QPushButton("Save Settings")
        self.btn_reload = QtWidgets.QPushButton("Reload Settings")
        self.btn_start_maps = QtWidgets.QPushButton("Start Maps")
        self.btn_stop_maps = QtWidgets.QPushButton("Stop Maps")
        self.btn_start_email = QtWidgets.QPushButton("Start Email")
        self.btn_stop_email = QtWidgets.QPushButton("Stop Email")
        self.btn_start_both = QtWidgets.QPushButton("Start Both")
        self.btn_stop_both = QtWidgets.QPushButton("Stop Both")
        self.btn_clear_log = QtWidgets.QPushButton("Clear Log")

        controls_layout.addWidget(self.btn_save, 0, 0)
        controls_layout.addWidget(self.btn_reload, 0, 1)
        controls_layout.addWidget(self.btn_start_maps, 1, 0)
        controls_layout.addWidget(self.btn_stop_maps, 1, 1)
        controls_layout.addWidget(self.btn_start_email, 2, 0)
        controls_layout.addWidget(self.btn_stop_email, 2, 1)
        controls_layout.addWidget(self.btn_start_both, 3, 0)
        controls_layout.addWidget(self.btn_stop_both, 3, 1)
        controls_layout.addWidget(self.btn_clear_log, 4, 0, 1, 2)

        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)

        right_layout.addWidget(status_group)
        right_layout.addWidget(controls_group)
        right_layout.addWidget(self.log_view, 1)

        self._apply_style()
        self._wire_actions()

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #f8f5ef, stop:1 #eef3f8);
            }
            QLabel#header {
                font-size: 20px;
                font-weight: 600;
                color: #14213d;
                padding: 4px 0;
            }
            QGroupBox {
                border: 1px solid #d6d1c4;
                border-radius: 8px;
                margin-top: 10px;
                padding: 8px;
                background: #ffffff;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 6px;
                color: #6b705c;
                font-weight: 600;
            }
            QLineEdit, QSpinBox, QDoubleSpinBox {
                background: #fbfaf7;
                border: 1px solid #d6d1c4;
                border-radius: 6px;
                padding: 4px 6px;
            }
            QCheckBox {
                padding: 4px 0;
            }
            QPushButton {
                background: #1f4d6b;
                color: #ffffff;
                border-radius: 6px;
                padding: 6px 10px;
            }
            QPushButton:hover {
                background: #2b5f82;
            }
            QPushButton:disabled {
                background: #9aa3a8;
                color: #f0f0f0;
            }
            QPlainTextEdit {
                background: #0b1320;
                color: #e0e5ec;
                border-radius: 8px;
                border: 1px solid #1c2a3a;
                font-family: "JetBrains Mono";
                font-size: 10px;
            }
            """
        )

    def _wire_actions(self) -> None:
        self.btn_save.clicked.connect(self._save_config)
        self.btn_reload.clicked.connect(self._reload_config)
        self.btn_start_maps.clicked.connect(self._start_maps)
        self.btn_stop_maps.clicked.connect(self._stop_maps)
        self.btn_start_email.clicked.connect(self._start_email)
        self.btn_stop_email.clicked.connect(self._stop_email)
        self.btn_start_both.clicked.connect(self._start_both)
        self.btn_stop_both.clicked.connect(self._stop_both)
        self.btn_clear_log.clicked.connect(self.log_view.clear)

    def _wire_process(self, proc: QtCore.QProcess, name: str) -> None:
        proc.readyReadStandardOutput.connect(lambda n=name, p=proc: self._append_log(n, p))
        proc.stateChanged.connect(lambda _state, n=name: self._update_status(n))
        proc.finished.connect(lambda _code, _status, n=name: self._update_status(n))

    def _append_log(self, name: str, proc: QtCore.QProcess) -> None:
        data = proc.readAllStandardOutput().data().decode("utf-8", "ignore")
        if not data:
            return
        for line in data.splitlines():
            self.log_view.appendPlainText(f"[{name}] {line}")

    def _update_status(self, name: str) -> None:
        if name == "maps":
            running = self.maps_proc.state() != QtCore.QProcess.NotRunning
            self.maps_status.setText("Running" if running else "Stopped")
            self.btn_start_maps.setEnabled(not running)
            self.btn_stop_maps.setEnabled(running)
        elif name == "email":
            running = self.email_proc.state() != QtCore.QProcess.NotRunning
            self.email_status.setText("Running" if running else "Stopped")
            self.btn_start_email.setEnabled(not running)
            self.btn_stop_email.setEnabled(running)

    def _load_config_to_form(self) -> None:
        cfg = self.config
        maps_cfg = cfg.get("maps", {})
        email_cfg = cfg.get("email", {})

        self.maps_base_url.setText(cfg.get("maps_base_url", DEFAULT_CONFIG["maps_base_url"]))
        self.email_base_url.setText(cfg.get("email_base_url", DEFAULT_CONFIG["email_base_url"]))
        self.queue_dir.setText(cfg.get("queue_dir", DEFAULT_CONFIG["queue_dir"]))
        self.maps_poll_interval.setValue(int(cfg.get("maps_poll_interval_s", 30)))
        self.email_poll_interval.setValue(int(cfg.get("email_poll_interval_s", 15)))

        self.maps_batch_size.setValue(int(maps_cfg.get("batch_size", 20)))
        self.maps_max_concurrent.setValue(int(maps_cfg.get("max_concurrent", 3)))
        self.maps_csv_dir.setText(maps_cfg.get("csv_dir", ""))

        self.email_batch.setValue(int(email_cfg.get("batch", 10)))
        self.email_concurrency.setValue(int(email_cfg.get("concurrency", 3)))
        self.email_timeout.setValue(float(email_cfg.get("timeout_s", 8.0)))
        self.email_domain_timeout.setValue(float(email_cfg.get("domain_timeout_s", 60.0)))
        self.email_links.setValue(int(email_cfg.get("links", 5)))
        self.email_max_batches.setValue(int(email_cfg.get("max_batches", 0)))
        self.email_max_batches_facebook.setValue(int(email_cfg.get("max_batches_facebook", 0)))
        self.email_facebook.setChecked(bool(email_cfg.get("facebook", False)))

        self._update_status("maps")
        self._update_status("email")

    def _collect_form_config(self) -> Dict[str, Any]:
        cfg = load_config(self.config_path)
        cfg["maps_base_url"] = self.maps_base_url.text().strip()
        cfg["email_base_url"] = self.email_base_url.text().strip()
        cfg["queue_dir"] = self.queue_dir.text().strip()
        cfg["maps_poll_interval_s"] = int(self.maps_poll_interval.value())
        cfg["email_poll_interval_s"] = int(self.email_poll_interval.value())

        cfg["maps"]["batch_size"] = int(self.maps_batch_size.value())
        cfg["maps"]["max_concurrent"] = int(self.maps_max_concurrent.value())
        cfg["maps"]["csv_dir"] = self.maps_csv_dir.text().strip()

        cfg["email"]["batch"] = int(self.email_batch.value())
        cfg["email"]["concurrency"] = int(self.email_concurrency.value())
        cfg["email"]["timeout_s"] = float(self.email_timeout.value())
        cfg["email"]["domain_timeout_s"] = float(self.email_domain_timeout.value())
        cfg["email"]["links"] = int(self.email_links.value())
        cfg["email"]["max_batches"] = int(self.email_max_batches.value())
        cfg["email"]["max_batches_facebook"] = int(self.email_max_batches_facebook.value())
        cfg["email"]["facebook"] = bool(self.email_facebook.isChecked())
        return cfg

    def _save_config(self) -> None:
        cfg = self._collect_form_config()
        save_config(self.config_path, cfg)
        self.config = cfg
        self.log_view.appendPlainText(f"[ui] Settings saved to {self.config_path}")

    def _reload_config(self) -> None:
        self.config = load_config(self.config_path)
        self._load_config_to_form()
        self.log_view.appendPlainText(f"[ui] Settings reloaded from {self.config_path}")

    def _start_maps(self) -> None:
        if self.maps_proc.state() != QtCore.QProcess.NotRunning:
            return
        self._save_config()
        self.maps_proc.setWorkingDirectory(self.base_dir)
        self.maps_proc.start(
            sys.executable,
            ["maps_daemon.py", "--config", self.config_path],
        )

    def _stop_maps(self) -> None:
        if self.maps_proc.state() == QtCore.QProcess.NotRunning:
            return
        self.maps_proc.terminate()
        QtCore.QTimer.singleShot(3000, lambda: self._kill_if_running(self.maps_proc))

    def _start_email(self) -> None:
        if self.email_proc.state() != QtCore.QProcess.NotRunning:
            return
        self._save_config()
        self.email_proc.setWorkingDirectory(self.base_dir)
        self.email_proc.start(
            sys.executable,
            ["email_daemon.py", "--config", self.config_path],
        )

    def _stop_email(self) -> None:
        if self.email_proc.state() == QtCore.QProcess.NotRunning:
            return
        self.email_proc.terminate()
        QtCore.QTimer.singleShot(3000, lambda: self._kill_if_running(self.email_proc))

    def _start_both(self) -> None:
        self._start_maps()
        self._start_email()

    def _stop_both(self) -> None:
        self._stop_maps()
        self._stop_email()

    @staticmethod
    def _kill_if_running(proc: QtCore.QProcess) -> None:
        if proc.state() != QtCore.QProcess.NotRunning:
            proc.kill()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self._stop_both()
        super().closeEvent(event)


def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    ui = DaemonUI()
    ui.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
