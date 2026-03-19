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

    def _create_collapsible_section(self, title: str, content: QtWidgets.QWidget, expanded: bool = True) -> QtWidgets.QWidget:
        section = QtWidgets.QWidget(self)
        layout = QtWidgets.QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        toggle = QtWidgets.QToolButton(section)
        toggle.setObjectName("sectionToggle")
        toggle.setText(title)
        toggle.setCheckable(True)
        toggle.setChecked(expanded)
        toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        toggle.setArrowType(QtCore.Qt.DownArrow if expanded else QtCore.Qt.RightArrow)
        toggle.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)

        body = QtWidgets.QFrame(section)
        body.setObjectName("sectionBody")
        body_layout = QtWidgets.QVBoxLayout(body)
        body_layout.setContentsMargins(10, 10, 10, 10)
        body_layout.setSpacing(8)
        body_layout.addWidget(content)
        body.setVisible(expanded)

        def _on_toggle(checked: bool) -> None:
            toggle.setArrowType(QtCore.Qt.DownArrow if checked else QtCore.Qt.RightArrow)
            body.setVisible(checked)

        toggle.toggled.connect(_on_toggle)

        layout.addWidget(toggle)
        layout.addWidget(body)
        return section

    def _build_ui(self) -> None:
        self.setWindowTitle("Maps + Email Pipeline Control")
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

        header = QtWidgets.QLabel("Maps + Email Workers")
        header.setObjectName("header")
        header.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        left_layout.addWidget(header)

        general_content = QtWidgets.QWidget()
        general_form = QtWidgets.QFormLayout(general_content)
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

        pipeline_content = QtWidgets.QWidget()
        pipeline_form = QtWidgets.QFormLayout(pipeline_content)
        self.pipeline_enabled = QtWidgets.QCheckBox("Enable pipeline mode")
        self.pipeline_base_url = QtWidgets.QLineEdit()
        self.pipeline_actor = QtWidgets.QLineEdit()
        self.pipeline_worker_id = QtWidgets.QLineEdit()
        self.pipeline_claim_interval = QtWidgets.QSpinBox()
        self.pipeline_claim_interval.setRange(1, 3600)
        self.pipeline_lease_seconds = QtWidgets.QSpinBox()
        self.pipeline_lease_seconds.setRange(10, 7200)
        self.pipeline_heartbeat_interval = QtWidgets.QSpinBox()
        self.pipeline_heartbeat_interval.setRange(1, 3600)
        self.pipeline_fast_scraper = QtWidgets.QComboBox()
        self.pipeline_fast_scraper.addItem("Scrapy (fast)", "scrapy")
        self.pipeline_fast_scraper.addItem("Playwright", "playwright")
        self.pipeline_fast_concurrency = QtWidgets.QSpinBox()
        self.pipeline_fast_concurrency.setRange(1, 20)
        self.pipeline_fast_batches_multiplier = QtWidgets.QDoubleSpinBox()
        self.pipeline_fast_batches_multiplier.setRange(0.1, 3.0)
        self.pipeline_fast_batches_multiplier.setDecimals(2)
        self.pipeline_fast_batches_multiplier.setSingleStep(0.05)
        self.pipeline_fast_max_batches_cap = QtWidgets.QSpinBox()
        self.pipeline_fast_max_batches_cap.setRange(0, 10000)
        self.pipeline_fallback_scraper = QtWidgets.QComboBox()
        self.pipeline_fallback_scraper.addItem("Playwright", "playwright")
        self.pipeline_fallback_scraper.addItem("Scrapy", "scrapy")
        self.pipeline_fallback_concurrency = QtWidgets.QSpinBox()
        self.pipeline_fallback_concurrency.setRange(1, 20)
        self.pipeline_fallback_batches_multiplier = QtWidgets.QDoubleSpinBox()
        self.pipeline_fallback_batches_multiplier.setRange(0.1, 3.0)
        self.pipeline_fallback_batches_multiplier.setDecimals(2)
        self.pipeline_fallback_batches_multiplier.setSingleStep(0.05)
        self.pipeline_fallback_fb_batches_multiplier = QtWidgets.QDoubleSpinBox()
        self.pipeline_fallback_fb_batches_multiplier.setRange(0.1, 3.0)
        self.pipeline_fallback_fb_batches_multiplier.setDecimals(2)
        self.pipeline_fallback_fb_batches_multiplier.setSingleStep(0.05)
        self.pipeline_fallback_max_batches = QtWidgets.QSpinBox()
        self.pipeline_fallback_max_batches.setRange(0, 10000)
        self.pipeline_fallback_max_batches_facebook = QtWidgets.QSpinBox()
        self.pipeline_fallback_max_batches_facebook.setRange(0, 10000)
        pipeline_form.addRow("", self.pipeline_enabled)
        pipeline_form.addRow("Pipeline base URL", self.pipeline_base_url)
        pipeline_form.addRow("Actor", self.pipeline_actor)
        pipeline_form.addRow("Worker ID override", self.pipeline_worker_id)
        pipeline_form.addRow("Claim interval (s)", self.pipeline_claim_interval)
        pipeline_form.addRow("Lease seconds", self.pipeline_lease_seconds)
        pipeline_form.addRow("Heartbeat interval (s)", self.pipeline_heartbeat_interval)
        pipeline_form.addRow("Fast stage scraper", self.pipeline_fast_scraper)
        pipeline_form.addRow("Fast stage concurrency", self.pipeline_fast_concurrency)
        pipeline_form.addRow("Fast stage batches multiplier", self.pipeline_fast_batches_multiplier)
        pipeline_form.addRow("Fast stage max batch cap (0=auto)", self.pipeline_fast_max_batches_cap)
        pipeline_form.addRow("Fallback stage scraper", self.pipeline_fallback_scraper)
        pipeline_form.addRow("Fallback stage concurrency", self.pipeline_fallback_concurrency)
        pipeline_form.addRow("Fallback batches multiplier", self.pipeline_fallback_batches_multiplier)
        pipeline_form.addRow("Fallback FB multiplier", self.pipeline_fallback_fb_batches_multiplier)
        pipeline_form.addRow("Fallback max batches cap (0=no cap)", self.pipeline_fallback_max_batches)
        pipeline_form.addRow("Fallback FB max cap (0=no cap)", self.pipeline_fallback_max_batches_facebook)

        maps_content = QtWidgets.QWidget()
        maps_form = QtWidgets.QFormLayout(maps_content)
        self.maps_batch_size = QtWidgets.QSpinBox()
        self.maps_batch_size.setRange(1, 500)
        self.maps_max_concurrent = QtWidgets.QSpinBox()
        self.maps_max_concurrent.setRange(1, 20)
        self.maps_detail_workers = QtWidgets.QSpinBox()
        self.maps_detail_workers.setRange(1, 20)
        self.maps_scrape_mode = QtWidgets.QComboBox()
        self.maps_scrape_mode.addItem("Fast (list only)", "fast")
        self.maps_scrape_mode.addItem("Slow (open each place details)", "slow")
        self.maps_show_browser = QtWidgets.QCheckBox("Show browser window while scraping maps")
        self.maps_slow_pause_min = QtWidgets.QDoubleSpinBox()
        self.maps_slow_pause_min.setRange(0.0, 30.0)
        self.maps_slow_pause_min.setDecimals(1)
        self.maps_slow_pause_max = QtWidgets.QDoubleSpinBox()
        self.maps_slow_pause_max.setRange(0.0, 30.0)
        self.maps_slow_pause_max.setDecimals(1)
        self.maps_scroll_pause_min = QtWidgets.QDoubleSpinBox()
        self.maps_scroll_pause_min.setRange(0.0, 30.0)
        self.maps_scroll_pause_min.setDecimals(1)
        self.maps_scroll_pause_max = QtWidgets.QDoubleSpinBox()
        self.maps_scroll_pause_max.setRange(0.0, 30.0)
        self.maps_scroll_pause_max.setDecimals(1)
        self.maps_csv_dir = QtWidgets.QLineEdit()
        maps_form.addRow("Batch size", self.maps_batch_size)
        maps_form.addRow("Max concurrent", self.maps_max_concurrent)
        maps_form.addRow("Detail workers", self.maps_detail_workers)
        maps_form.addRow("Scrape mode", self.maps_scrape_mode)
        maps_form.addRow("", self.maps_show_browser)
        maps_form.addRow("Slow pause min (s)", self.maps_slow_pause_min)
        maps_form.addRow("Slow pause max (s)", self.maps_slow_pause_max)
        maps_form.addRow("Scroll pause min (s)", self.maps_scroll_pause_min)
        maps_form.addRow("Scroll pause max (s)", self.maps_scroll_pause_max)
        maps_form.addRow("CSV output dir", self.maps_csv_dir)

        email_content = QtWidgets.QWidget()
        email_form = QtWidgets.QFormLayout(email_content)
        self.email_scraper_engine = QtWidgets.QComboBox()
        self.email_scraper_engine.addItem("Playwright (more emails)", "playwright")
        self.email_scraper_engine.addItem("Scrapy (faster)", "scrapy")
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
        self.email_min_domain_letters = QtWidgets.QSpinBox()
        self.email_min_domain_letters.setRange(1, 10)
        self.email_max_batches = QtWidgets.QSpinBox()
        self.email_max_batches.setRange(0, 200)
        self.email_max_batches_facebook = QtWidgets.QSpinBox()
        self.email_max_batches_facebook.setRange(0, 200)
        self.email_facebook = QtWidgets.QCheckBox("Enable Facebook scraping")
        self.email_facebook_engine = QtWidgets.QComboBox()
        self.email_facebook_engine.addItem("Playwright", "playwright")
        self.email_facebook_engine.addItem("Scrapy (HTTP)", "scrapy")
        self.email_same_domain_only = QtWidgets.QCheckBox("Scrape only within company domain")
        email_form.addRow("Scraper engine", self.email_scraper_engine)
        email_form.addRow("Batch", self.email_batch)
        email_form.addRow("Concurrency", self.email_concurrency)
        email_form.addRow("Timeout (s)", self.email_timeout)
        email_form.addRow("Domain timeout (s)", self.email_domain_timeout)
        email_form.addRow("Links per domain", self.email_links)
        email_form.addRow("Min letters in email domain", self.email_min_domain_letters)
        email_form.addRow("Max batches per run (0 = unlimited)", self.email_max_batches)
        email_form.addRow("Max Facebook batches per run (0 = disabled)", self.email_max_batches_facebook)
        email_form.addRow("Facebook fallback engine", self.email_facebook_engine)
        email_form.addRow("", self.email_facebook)
        email_form.addRow("", self.email_same_domain_only)

        left_layout.addWidget(self._create_collapsible_section("General", general_content, expanded=False))
        left_layout.addWidget(self._create_collapsible_section("Pipeline", pipeline_content, expanded=True))
        left_layout.addWidget(self._create_collapsible_section("Maps Daemon", maps_content, expanded=False))
        left_layout.addWidget(self._create_collapsible_section("Email Daemon", email_content, expanded=False))
        left_layout.addStretch(1)

        status_group = QtWidgets.QGroupBox("Status")
        status_layout = QtWidgets.QFormLayout(status_group)
        self.maps_status = QtWidgets.QLabel("Stopped")
        self.email_status = QtWidgets.QLabel("Stopped")
        self.email_regular_found = QtWidgets.QLabel("0")
        self.email_facebook_found = QtWidgets.QLabel("0")
        status_layout.addRow("Maps daemon", self.maps_status)
        status_layout.addRow("Email daemon", self.email_status)
        status_layout.addRow("Emails found (regular)", self.email_regular_found)
        status_layout.addRow("Emails found (Facebook)", self.email_facebook_found)

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
        self._reset_email_counters()

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
            QToolButton#sectionToggle {
                text-align: left;
                color: #6b705c;
                font-weight: 600;
                background: #ffffff;
                border: 1px solid #d6d1c4;
                border-radius: 8px;
                padding: 6px 10px;
            }
            QToolButton#sectionToggle:hover {
                background: #f7f4ee;
            }
            QFrame#sectionBody {
                background: #ffffff;
                border: 1px solid #d6d1c4;
                border-radius: 8px;
            }
            QLineEdit, QSpinBox, QDoubleSpinBox {
                background: #fbfaf7;
                border: 1px solid #d6d1c4;
                border-radius: 6px;
                padding: 4px 6px;
            }
            QComboBox {
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
        self.pipeline_enabled.stateChanged.connect(self._update_mode_labels)

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
            if name in {"email", "maps"}:
                self._track_email_found(line)

    def _reset_email_counters(self) -> None:
        self._email_regular_found_count = 0
        self._email_facebook_found_count = 0
        self.email_regular_found.setText("0")
        self.email_facebook_found.setText("0")

    def _track_email_found(self, line: str) -> None:
        text = (line or "").strip()
        if not text:
            return

        if "FACEBOOK FOUND " in text and "->" in text:
            self._email_facebook_found_count += 1
            self.email_facebook_found.setText(str(self._email_facebook_found_count))
            return

        if "✓ DONE" in text and "->" in text:
            if "[FB priority]" in text:
                self._email_facebook_found_count += 1
                self.email_facebook_found.setText(str(self._email_facebook_found_count))
            else:
                self._email_regular_found_count += 1
                self.email_regular_found.setText(str(self._email_regular_found_count))
            return

        if " - INFO - FOUND " in text and "->" in text:
            self._email_regular_found_count += 1
            self.email_regular_found.setText(str(self._email_regular_found_count))
            return

        if text.startswith("FOUND ") and "->" in text:
            self._email_regular_found_count += 1
            self.email_regular_found.setText(str(self._email_regular_found_count))

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
        self._update_mode_labels()

    def _update_mode_labels(self) -> None:
        pipeline_mode = bool(self.pipeline_enabled.isChecked())
        if pipeline_mode:
            self.btn_start_maps.setText("Start Worker A")
            self.btn_stop_maps.setText("Stop Worker A")
            self.btn_start_email.setText("Start Worker B")
            self.btn_stop_email.setText("Stop Worker B")
            self.btn_start_both.setText("Start 2 Workers")
            self.btn_stop_both.setText("Stop 2 Workers")
        else:
            self.btn_start_maps.setText("Start Maps")
            self.btn_stop_maps.setText("Stop Maps")
            self.btn_start_email.setText("Start Email")
            self.btn_stop_email.setText("Stop Email")
            self.btn_start_both.setText("Start Both")
            self.btn_stop_both.setText("Stop Both")

    def _daemon_mode_flag(self) -> str:
        return "--pipeline-mode" if bool(self.pipeline_enabled.isChecked()) else "--legacy-mode"

    def _load_config_to_form(self) -> None:
        cfg = self.config
        maps_cfg = cfg.get("maps", {})
        email_cfg = cfg.get("email", {})
        pipeline_cfg = cfg.get("pipeline", {})

        self.maps_base_url.setText(cfg.get("maps_base_url", DEFAULT_CONFIG["maps_base_url"]))
        self.email_base_url.setText(cfg.get("email_base_url", DEFAULT_CONFIG["email_base_url"]))
        self.queue_dir.setText(cfg.get("queue_dir", DEFAULT_CONFIG["queue_dir"]))
        self.maps_poll_interval.setValue(int(cfg.get("maps_poll_interval_s", 30)))
        self.email_poll_interval.setValue(int(cfg.get("email_poll_interval_s", 15)))
        self.pipeline_enabled.setChecked(bool(pipeline_cfg.get("enabled", True)))
        self.pipeline_base_url.setText(str(pipeline_cfg.get("base_url", "")))
        self.pipeline_actor.setText(str(pipeline_cfg.get("actor", "daemon")))
        self.pipeline_worker_id.setText(str(pipeline_cfg.get("worker_id", "")))
        self.pipeline_claim_interval.setValue(int(pipeline_cfg.get("claim_interval_s", 10)))
        self.pipeline_lease_seconds.setValue(int(pipeline_cfg.get("lease_seconds", 120)))
        self.pipeline_heartbeat_interval.setValue(int(pipeline_cfg.get("heartbeat_interval_s", 30)))
        fast_engine = str(pipeline_cfg.get("fast_scraper", "scrapy")).lower().strip()
        fast_engine_index = self.pipeline_fast_scraper.findData(fast_engine)
        if fast_engine_index == -1:
            fast_engine_index = 0
        self.pipeline_fast_scraper.setCurrentIndex(fast_engine_index)
        self.pipeline_fast_concurrency.setValue(int(pipeline_cfg.get("fast_concurrency", 3)))
        self.pipeline_fast_batches_multiplier.setValue(float(pipeline_cfg.get("fast_batches_multiplier", 1.1)))
        self.pipeline_fast_max_batches_cap.setValue(int(pipeline_cfg.get("fast_max_batches_cap", 0)))
        fallback_engine = str(pipeline_cfg.get("fallback_scraper", "playwright")).lower().strip()
        fallback_engine_index = self.pipeline_fallback_scraper.findData(fallback_engine)
        if fallback_engine_index == -1:
            fallback_engine_index = 0
        self.pipeline_fallback_scraper.setCurrentIndex(fallback_engine_index)
        self.pipeline_fallback_concurrency.setValue(int(pipeline_cfg.get("fallback_concurrency", 1)))
        self.pipeline_fallback_batches_multiplier.setValue(float(pipeline_cfg.get("fallback_batches_multiplier", 1.0)))
        self.pipeline_fallback_fb_batches_multiplier.setValue(float(pipeline_cfg.get("fallback_facebook_batches_multiplier", 1.0)))
        self.pipeline_fallback_max_batches.setValue(int(pipeline_cfg.get("fallback_max_batches", 0)))
        self.pipeline_fallback_max_batches_facebook.setValue(int(pipeline_cfg.get("fallback_max_batches_facebook", 0)))

        self.maps_batch_size.setValue(int(maps_cfg.get("batch_size", 20)))
        self.maps_max_concurrent.setValue(int(maps_cfg.get("max_concurrent", 1)))
        self.maps_detail_workers.setValue(int(maps_cfg.get("detail_workers", 1)))
        mode = str(maps_cfg.get("scrape_mode", "fast")).lower().strip()
        mode_index = self.maps_scrape_mode.findData(mode)
        if mode_index == -1:
            mode_index = 0
        self.maps_scrape_mode.setCurrentIndex(mode_index)
        self.maps_show_browser.setChecked(bool(maps_cfg.get("show_browser", False)))
        self.maps_slow_pause_min.setValue(float(maps_cfg.get("slow_place_pause_min_s", 0.8)))
        self.maps_slow_pause_max.setValue(float(maps_cfg.get("slow_place_pause_max_s", 1.8)))
        self.maps_scroll_pause_min.setValue(float(maps_cfg.get("scroll_pause_min_s", 0.8)))
        self.maps_scroll_pause_max.setValue(float(maps_cfg.get("scroll_pause_max_s", 0.8)))
        self.maps_csv_dir.setText(maps_cfg.get("csv_dir", ""))

        self.email_batch.setValue(int(email_cfg.get("batch", 10)))
        self.email_concurrency.setValue(int(email_cfg.get("concurrency", 3)))
        self.email_timeout.setValue(float(email_cfg.get("timeout_s", 8.0)))
        self.email_domain_timeout.setValue(float(email_cfg.get("domain_timeout_s", 60.0)))
        self.email_links.setValue(int(email_cfg.get("links", 5)))
        self.email_min_domain_letters.setValue(int(email_cfg.get("min_domain_letters", 2)))
        self.email_max_batches.setValue(int(email_cfg.get("max_batches", 0)))
        self.email_max_batches_facebook.setValue(int(email_cfg.get("max_batches_facebook", 0)))
        self.email_facebook.setChecked(bool(email_cfg.get("facebook", False)))
        self.email_same_domain_only.setChecked(bool(email_cfg.get("same_domain_only", True)))
        facebook_engine = str(email_cfg.get("facebook_engine", "playwright")).lower().strip()
        facebook_engine_index = self.email_facebook_engine.findData(facebook_engine)
        if facebook_engine_index == -1:
            facebook_engine_index = 0
        self.email_facebook_engine.setCurrentIndex(facebook_engine_index)
        engine = str(email_cfg.get("scraper", "playwright")).lower().strip()
        engine_index = self.email_scraper_engine.findData(engine)
        if engine_index == -1:
            engine_index = 0
        self.email_scraper_engine.setCurrentIndex(engine_index)

        self._update_status("maps")
        self._update_status("email")
        self._update_mode_labels()

    def _collect_form_config(self) -> Dict[str, Any]:
        cfg = load_config(self.config_path)
        cfg["maps_base_url"] = self.maps_base_url.text().strip()
        cfg["email_base_url"] = self.email_base_url.text().strip()
        cfg["queue_dir"] = self.queue_dir.text().strip()
        cfg["maps_poll_interval_s"] = int(self.maps_poll_interval.value())
        cfg["email_poll_interval_s"] = int(self.email_poll_interval.value())
        pipeline_cfg = cfg.get("pipeline", {})
        if not isinstance(pipeline_cfg, dict):
            pipeline_cfg = {}
        cfg["pipeline"] = pipeline_cfg
        pipeline_cfg["enabled"] = bool(self.pipeline_enabled.isChecked())
        pipeline_cfg["base_url"] = self.pipeline_base_url.text().strip()
        pipeline_cfg["actor"] = self.pipeline_actor.text().strip() or "daemon"
        pipeline_cfg["worker_id"] = self.pipeline_worker_id.text().strip()
        pipeline_cfg["claim_interval_s"] = int(self.pipeline_claim_interval.value())
        pipeline_cfg["lease_seconds"] = int(self.pipeline_lease_seconds.value())
        pipeline_cfg["heartbeat_interval_s"] = int(self.pipeline_heartbeat_interval.value())
        pipeline_cfg["fast_scraper"] = str(self.pipeline_fast_scraper.currentData() or "scrapy")
        pipeline_cfg["fast_concurrency"] = int(self.pipeline_fast_concurrency.value())
        pipeline_cfg["fast_batches_multiplier"] = float(self.pipeline_fast_batches_multiplier.value())
        pipeline_cfg["fast_max_batches_cap"] = int(self.pipeline_fast_max_batches_cap.value())
        pipeline_cfg["fallback_scraper"] = str(self.pipeline_fallback_scraper.currentData() or "playwright")
        pipeline_cfg["fallback_concurrency"] = int(self.pipeline_fallback_concurrency.value())
        pipeline_cfg["fallback_batches_multiplier"] = float(self.pipeline_fallback_batches_multiplier.value())
        pipeline_cfg["fallback_facebook_batches_multiplier"] = float(self.pipeline_fallback_fb_batches_multiplier.value())
        pipeline_cfg["fallback_max_batches"] = int(self.pipeline_fallback_max_batches.value())
        pipeline_cfg["fallback_max_batches_facebook"] = int(self.pipeline_fallback_max_batches_facebook.value())

        cfg["maps"]["batch_size"] = int(self.maps_batch_size.value())
        cfg["maps"]["max_concurrent"] = int(self.maps_max_concurrent.value())
        cfg["maps"]["detail_workers"] = int(self.maps_detail_workers.value())
        cfg["maps"]["scrape_mode"] = str(self.maps_scrape_mode.currentData() or "fast")
        cfg["maps"]["show_browser"] = bool(self.maps_show_browser.isChecked())
        slow_min = float(self.maps_slow_pause_min.value())
        slow_max = float(self.maps_slow_pause_max.value())
        if slow_max < slow_min:
            slow_min, slow_max = slow_max, slow_min
        scroll_min = float(self.maps_scroll_pause_min.value())
        scroll_max = float(self.maps_scroll_pause_max.value())
        if scroll_max < scroll_min:
            scroll_min, scroll_max = scroll_max, scroll_min
        cfg["maps"]["slow_place_pause_min_s"] = slow_min
        cfg["maps"]["slow_place_pause_max_s"] = slow_max
        cfg["maps"]["scroll_pause_min_s"] = scroll_min
        cfg["maps"]["scroll_pause_max_s"] = scroll_max
        cfg["maps"]["csv_dir"] = self.maps_csv_dir.text().strip()

        cfg["email"]["batch"] = int(self.email_batch.value())
        cfg["email"]["concurrency"] = int(self.email_concurrency.value())
        cfg["email"]["timeout_s"] = float(self.email_timeout.value())
        cfg["email"]["domain_timeout_s"] = float(self.email_domain_timeout.value())
        cfg["email"]["links"] = int(self.email_links.value())
        cfg["email"]["min_domain_letters"] = int(self.email_min_domain_letters.value())
        cfg["email"]["max_batches"] = int(self.email_max_batches.value())
        cfg["email"]["max_batches_facebook"] = int(self.email_max_batches_facebook.value())
        cfg["email"]["facebook"] = bool(self.email_facebook.isChecked())
        cfg["email"]["facebook_engine"] = str(self.email_facebook_engine.currentData() or "playwright")
        cfg["email"]["same_domain_only"] = bool(self.email_same_domain_only.isChecked())
        cfg["email"]["scraper"] = str(self.email_scraper_engine.currentData() or "playwright")
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
        mode_flag = self._daemon_mode_flag()
        self.maps_proc.start(
            sys.executable,
            ["maps_daemon.py", "--config", self.config_path, mode_flag],
        )

    def _stop_maps(self) -> None:
        if self.maps_proc.state() == QtCore.QProcess.NotRunning:
            return
        self.maps_proc.terminate()
        QtCore.QTimer.singleShot(3000, lambda: self._kill_if_running(self.maps_proc))

    def _start_email(self) -> None:
        if self.email_proc.state() != QtCore.QProcess.NotRunning:
            return
        self._reset_email_counters()
        self._save_config()
        self.email_proc.setWorkingDirectory(self.base_dir)
        mode_flag = self._daemon_mode_flag()
        self.email_proc.start(
            sys.executable,
            ["email_daemon.py", "--config", self.config_path, mode_flag],
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
