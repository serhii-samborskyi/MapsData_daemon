#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from daemon_config import load_config, save_config


ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
UI_PATH = os.path.join(ROOT_DIR, "daemon_web_ui.html")
MAX_LOG_LINES = 4000


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _to_int(value: Any, default: int, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _to_float(value: Any, default: float, minimum: Optional[float] = None, maximum: Optional[float] = None) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _ensure_min_max(min_value: float, max_value: float) -> tuple[float, float]:
    if max_value < min_value:
        return max_value, min_value
    return min_value, max_value


class DaemonWebController:
    def __init__(self, config_path: str) -> None:
        self.base_dir = ROOT_DIR
        self.config_path = config_path
        self.lock = threading.RLock()

        self.config = load_config(config_path)
        self.maps_proc: Optional[subprocess.Popen] = None
        self.email_proc: Optional[subprocess.Popen] = None

        self.maps_current_work = "Stopped"
        self.email_current_work = "Stopped"
        self.email_regular_found = 0
        self.email_facebook_found = 0

        self.log_lines = deque(maxlen=MAX_LOG_LINES)
        self._append_log("ui", f"Web UI booted. config={self.config_path}")

    def _append_log(self, source: str, line: str) -> None:
        entry = f"[{source}] {line.rstrip()}"
        self.log_lines.append(entry)
        if source in {"maps", "email"}:
            self._track_email_found(line)
            self._track_pipeline_activity(source, line)

    def _set_current_work(self, name: str, text: str) -> None:
        if name == "maps":
            self.maps_current_work = text
        elif name == "email":
            self.email_current_work = text

    @staticmethod
    def _human_stage(stage: str) -> str:
        return str(stage or "").strip().replace("_", " ").title()

    def _track_pipeline_activity(self, name: str, line: str) -> None:
        text = (line or "").strip()
        if not text:
            return

        claim_named = re.search(
            r"Claimed pipeline run=(\S+)\s+campaign=(\S+)\s+campaign_name=(.*?)\s+stage=(\S+)",
            text,
        )
        if claim_named:
            campaign_id = claim_named.group(2)
            campaign_name = claim_named.group(3).strip()
            stage = self._human_stage(claim_named.group(4))
            display_name = campaign_name if campaign_name else f"Campaign {campaign_id}"
            self._set_current_work(name, f"{display_name} (#{campaign_id}) · {stage}")
            return

        claim_basic = re.search(r"Claimed pipeline run=(\S+)\s+campaign=(\S+)\s+stage=(\S+)", text)
        if claim_basic:
            campaign_id = claim_basic.group(2)
            stage = self._human_stage(claim_basic.group(3))
            self._set_current_work(name, f"Campaign #{campaign_id} · {stage}")
            return

        if "No claimable run (" in text:
            self._set_current_work(name, "Idle")
            return

        failed = re.search(r"Stage failed: run=\S+\s+campaign=(\S+)\s+stage=(\S+)", text)
        if failed:
            self._set_current_work(name, f"Campaign #{failed.group(1)} · Failed ({self._human_stage(failed.group(2))})")
            return

        if "Pipeline worker stopping" in text:
            self._set_current_work(name, "Stopped")

    def _track_email_found(self, line: str) -> None:
        text = (line or "").strip()
        if not text:
            return

        if "FACEBOOK FOUND " in text and "->" in text:
            self.email_facebook_found += 1
            return

        if "✓ DONE" in text and "->" in text:
            if "[FB priority]" in text:
                self.email_facebook_found += 1
            else:
                self.email_regular_found += 1
            return

        if " - INFO - FOUND " in text and "->" in text:
            self.email_regular_found += 1
            return

        if text.startswith("FOUND ") and "->" in text:
            self.email_regular_found += 1

    def _read_stream(self, name: str, proc: subprocess.Popen) -> None:
        stream = proc.stdout
        if stream is None:
            return
        for raw in iter(stream.readline, ""):
            if not raw:
                break
            with self.lock:
                self._append_log(name, raw.rstrip("\n"))

    def _watch_process(self, name: str, proc: subprocess.Popen) -> None:
        code = proc.wait()
        with self.lock:
            status_name = "NormalExit" if code == 0 else "CrashExit"
            self._append_log("ui", f"{name} exited: code={code}, status={status_name}")
            if name == "maps" and self.maps_proc is proc:
                self.maps_proc = None
                self.maps_current_work = "Stopped"
            elif name == "email" and self.email_proc is proc:
                self.email_proc = None
                self.email_current_work = "Stopped"

    def _daemon_mode_flag(self) -> str:
        pipeline_cfg = self.config.get("pipeline", {})
        enabled = bool(pipeline_cfg.get("enabled", True)) if isinstance(pipeline_cfg, dict) else True
        return "--pipeline-mode" if enabled else "--legacy-mode"

    def _start_daemon(self, name: str) -> Dict[str, Any]:
        script_name = "maps_daemon.py" if name == "maps" else "email_daemon.py"
        with self.lock:
            existing = self.maps_proc if name == "maps" else self.email_proc
            if existing is not None and existing.poll() is None:
                return {"ok": True, "status": "already_running"}

            self.config = load_config(self.config_path)
            mode_flag = self._daemon_mode_flag()
            args = [sys.executable, script_name, "--config", self.config_path, mode_flag]
            proc = subprocess.Popen(
                args,
                cwd=self.base_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )
            if name == "maps":
                self.maps_proc = proc
                self.maps_current_work = "Starting..."
            else:
                self.email_proc = proc
                self.email_current_work = "Starting..."
                self.email_regular_found = 0
                self.email_facebook_found = 0

            threading.Thread(target=self._read_stream, args=(name, proc), daemon=True).start()
            threading.Thread(target=self._watch_process, args=(name, proc), daemon=True).start()
            self._append_log("ui", f"Started {name}: {' '.join(args)}")
            return {"ok": True, "status": "started"}

    def _stop_daemon(self, name: str) -> Dict[str, Any]:
        with self.lock:
            proc = self.maps_proc if name == "maps" else self.email_proc
            if proc is None or proc.poll() is not None:
                return {"ok": True, "status": "already_stopped"}

            proc.terminate()
            self._append_log("ui", f"Stopping {name}...")

        def _kill_later() -> None:
            time.sleep(3)
            with self.lock:
                target = self.maps_proc if name == "maps" else self.email_proc
                if target is proc and target.poll() is None:
                    target.kill()
                    self._append_log("ui", f"Force-killed {name} after timeout")

        threading.Thread(target=_kill_later, daemon=True).start()
        return {"ok": True, "status": "stopping"}

    def start_maps(self) -> Dict[str, Any]:
        return self._start_daemon("maps")

    def stop_maps(self) -> Dict[str, Any]:
        return self._stop_daemon("maps")

    def start_email(self) -> Dict[str, Any]:
        return self._start_daemon("email")

    def stop_email(self) -> Dict[str, Any]:
        return self._stop_daemon("email")

    def start_both(self) -> Dict[str, Any]:
        return {"maps": self.start_maps(), "email": self.start_email(), "ok": True}

    def stop_both(self) -> Dict[str, Any]:
        return {"maps": self.stop_maps(), "email": self.stop_email(), "ok": True}

    def _normalize_payload_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        current = load_config(self.config_path)

        current["maps_base_url"] = str(payload.get("maps_base_url", current.get("maps_base_url", ""))).strip()
        current["email_base_url"] = str(payload.get("email_base_url", current.get("email_base_url", ""))).strip()
        current["queue_dir"] = str(payload.get("queue_dir", current.get("queue_dir", "queue"))).strip() or "queue"
        current["maps_poll_interval_s"] = _to_int(payload.get("maps_poll_interval_s"), int(current.get("maps_poll_interval_s", 30)), 5, 3600)
        current["email_poll_interval_s"] = _to_int(payload.get("email_poll_interval_s"), int(current.get("email_poll_interval_s", 15)), 5, 3600)

        maps_cfg = dict(current.get("maps") or {})
        maps_input = payload.get("maps") if isinstance(payload.get("maps"), dict) else {}
        maps_cfg["batch_size"] = _to_int(maps_input.get("batch_size"), int(maps_cfg.get("batch_size", 20)), 1, 500)
        maps_cfg["max_concurrent"] = _to_int(maps_input.get("max_concurrent"), int(maps_cfg.get("max_concurrent", 1)), 1, 20)
        maps_cfg["detail_workers"] = _to_int(maps_input.get("detail_workers"), int(maps_cfg.get("detail_workers", 1)), 1, 20)
        maps_mode = str(maps_input.get("scrape_mode", maps_cfg.get("scrape_mode", "fast"))).strip().lower()
        maps_cfg["scrape_mode"] = maps_mode if maps_mode in {"fast", "slow"} else "fast"
        maps_cfg["show_browser"] = _to_bool(maps_input.get("show_browser"), bool(maps_cfg.get("show_browser", False)))
        slow_min = _to_float(maps_input.get("slow_place_pause_min_s"), float(maps_cfg.get("slow_place_pause_min_s", 0.8)), 0.0, 30.0)
        slow_max = _to_float(maps_input.get("slow_place_pause_max_s"), float(maps_cfg.get("slow_place_pause_max_s", 1.8)), 0.0, 30.0)
        slow_min, slow_max = _ensure_min_max(slow_min, slow_max)
        maps_cfg["slow_place_pause_min_s"] = slow_min
        maps_cfg["slow_place_pause_max_s"] = slow_max
        scroll_min = _to_float(maps_input.get("scroll_pause_min_s"), float(maps_cfg.get("scroll_pause_min_s", 0.8)), 0.0, 30.0)
        scroll_max = _to_float(maps_input.get("scroll_pause_max_s"), float(maps_cfg.get("scroll_pause_max_s", 0.8)), 0.0, 30.0)
        scroll_min, scroll_max = _ensure_min_max(scroll_min, scroll_max)
        maps_cfg["scroll_pause_min_s"] = scroll_min
        maps_cfg["scroll_pause_max_s"] = scroll_max
        maps_cfg["csv_dir"] = str(maps_input.get("csv_dir", maps_cfg.get("csv_dir", ""))).strip()
        current["maps"] = maps_cfg

        email_cfg = dict(current.get("email") or {})
        email_input = payload.get("email") if isinstance(payload.get("email"), dict) else {}
        email_cfg["batch"] = _to_int(email_input.get("batch"), int(email_cfg.get("batch", 10)), 1, 200)
        email_cfg["concurrency"] = _to_int(email_input.get("concurrency"), int(email_cfg.get("concurrency", 3)), 1, 20)
        email_cfg["timeout_s"] = _to_float(email_input.get("timeout_s"), float(email_cfg.get("timeout_s", 8.0)), 1.0, 120.0)
        email_cfg["domain_timeout_s"] = _to_float(email_input.get("domain_timeout_s"), float(email_cfg.get("domain_timeout_s", 60.0)), 5.0, 300.0)
        email_cfg["links"] = _to_int(email_input.get("links"), int(email_cfg.get("links", 5)), 0, 20)
        email_cfg["min_domain_letters"] = _to_int(email_input.get("min_domain_letters"), int(email_cfg.get("min_domain_letters", 2)), 1, 10)
        email_cfg["max_batches"] = _to_int(email_input.get("max_batches"), int(email_cfg.get("max_batches", 0)), 0, 200)
        email_cfg["max_batches_facebook"] = _to_int(email_input.get("max_batches_facebook"), int(email_cfg.get("max_batches_facebook", 0)), 0, 200)
        email_cfg["facebook"] = _to_bool(email_input.get("facebook"), bool(email_cfg.get("facebook", False)))
        fb_engine = str(email_input.get("facebook_engine", email_cfg.get("facebook_engine", "playwright"))).strip().lower()
        email_cfg["facebook_engine"] = fb_engine if fb_engine in {"playwright", "scrapy"} else "playwright"
        email_cfg["same_domain_only"] = _to_bool(email_input.get("same_domain_only"), bool(email_cfg.get("same_domain_only", True)))
        scraper = str(email_input.get("scraper", email_cfg.get("scraper", "playwright"))).strip().lower()
        email_cfg["scraper"] = scraper if scraper in {"playwright", "scrapy"} else "playwright"
        current["email"] = email_cfg

        pipeline_cfg = dict(current.get("pipeline") or {})
        pipeline_input = payload.get("pipeline") if isinstance(payload.get("pipeline"), dict) else {}
        pipeline_cfg["enabled"] = _to_bool(pipeline_input.get("enabled"), bool(pipeline_cfg.get("enabled", True)))
        pipeline_cfg["base_url"] = str(pipeline_input.get("base_url", pipeline_cfg.get("base_url", ""))).strip()
        pipeline_cfg["actor"] = str(pipeline_input.get("actor", pipeline_cfg.get("actor", "daemon"))).strip() or "daemon"
        pipeline_cfg["worker_id"] = str(pipeline_input.get("worker_id", pipeline_cfg.get("worker_id", ""))).strip()
        pipeline_cfg["claim_interval_s"] = _to_int(pipeline_input.get("claim_interval_s"), int(pipeline_cfg.get("claim_interval_s", 10)), 1, 3600)
        pipeline_cfg["lease_seconds"] = _to_int(pipeline_input.get("lease_seconds"), int(pipeline_cfg.get("lease_seconds", 120)), 10, 7200)
        pipeline_cfg["heartbeat_interval_s"] = _to_int(pipeline_input.get("heartbeat_interval_s"), int(pipeline_cfg.get("heartbeat_interval_s", 30)), 1, 3600)
        fast_scraper = str(pipeline_input.get("fast_scraper", pipeline_cfg.get("fast_scraper", "scrapy"))).strip().lower()
        pipeline_cfg["fast_scraper"] = fast_scraper if fast_scraper in {"scrapy", "playwright"} else "scrapy"
        pipeline_cfg["fast_concurrency"] = _to_int(pipeline_input.get("fast_concurrency"), int(pipeline_cfg.get("fast_concurrency", 3)), 1, 20)
        pipeline_cfg["fast_batches_multiplier"] = _to_float(pipeline_input.get("fast_batches_multiplier"), float(pipeline_cfg.get("fast_batches_multiplier", 1.1)), 0.1, 3.0)
        fast_policy = str(pipeline_input.get("fast_email_policy", pipeline_cfg.get("fast_email_policy", "business_only"))).strip().lower()
        pipeline_cfg["fast_email_policy"] = fast_policy if fast_policy in {"business_only", "business_or_public", "any_valid"} else "business_only"
        pipeline_cfg["fast_max_batches_cap"] = _to_int(pipeline_input.get("fast_max_batches_cap"), int(pipeline_cfg.get("fast_max_batches_cap", 0)), 0, 10000)

        fallback_scraper = str(pipeline_input.get("fallback_scraper", pipeline_cfg.get("fallback_scraper", "playwright"))).strip().lower()
        pipeline_cfg["fallback_scraper"] = fallback_scraper if fallback_scraper in {"scrapy", "playwright"} else "playwright"
        pipeline_cfg["fallback_concurrency"] = _to_int(pipeline_input.get("fallback_concurrency"), int(pipeline_cfg.get("fallback_concurrency", 1)), 1, 20)
        pipeline_cfg["fallback_batches_multiplier"] = _to_float(pipeline_input.get("fallback_batches_multiplier"), float(pipeline_cfg.get("fallback_batches_multiplier", 1.0)), 0.1, 3.0)
        pipeline_cfg["fallback_facebook_batches_multiplier"] = _to_float(pipeline_input.get("fallback_facebook_batches_multiplier"), float(pipeline_cfg.get("fallback_facebook_batches_multiplier", 1.0)), 0.1, 3.0)
        fallback_policy = str(pipeline_input.get("fallback_email_policy", pipeline_cfg.get("fallback_email_policy", "business_or_public"))).strip().lower()
        pipeline_cfg["fallback_email_policy"] = fallback_policy if fallback_policy in {"business_only", "business_or_public", "any_valid"} else "business_or_public"
        pipeline_cfg["fallback_max_batches"] = _to_int(pipeline_input.get("fallback_max_batches"), int(pipeline_cfg.get("fallback_max_batches", 0)), 0, 10000)
        pipeline_cfg["fallback_max_batches_facebook"] = _to_int(pipeline_input.get("fallback_max_batches_facebook"), int(pipeline_cfg.get("fallback_max_batches_facebook", 0)), 0, 10000)
        current["pipeline"] = pipeline_cfg

        return current

    def save_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            normalized = self._normalize_payload_config(payload)
            save_config(self.config_path, normalized)
            self.config = normalized
            self._append_log("ui", f"Settings saved to {self.config_path}")
            return {"ok": True, "config": self.config}

    def reload_config(self) -> Dict[str, Any]:
        with self.lock:
            self.config = load_config(self.config_path)
            self._append_log("ui", f"Settings reloaded from {self.config_path}")
            return {"ok": True, "config": self.config}

    def clear_logs(self) -> Dict[str, Any]:
        with self.lock:
            self.log_lines.clear()
            self._append_log("ui", "Log cleared")
            return {"ok": True}

    def get_state(self, log_limit: int = 300) -> Dict[str, Any]:
        with self.lock:
            maps_running = self.maps_proc is not None and self.maps_proc.poll() is None
            email_running = self.email_proc is not None and self.email_proc.poll() is None
            tail = list(self.log_lines)[-max(1, min(log_limit, MAX_LOG_LINES)):]
            return {
                "ok": True,
                "config": self.config,
                "status": {
                    "maps_running": maps_running,
                    "email_running": email_running,
                    "maps_status": "Running" if maps_running else "Stopped",
                    "email_status": "Running" if email_running else "Stopped",
                    "maps_current_work": self.maps_current_work if maps_running else "Stopped",
                    "email_current_work": self.email_current_work if email_running else "Stopped",
                    "email_regular_found": self.email_regular_found,
                    "email_facebook_found": self.email_facebook_found,
                },
                "logs": tail,
            }

    def shutdown(self) -> None:
        self.stop_both()


CONTROLLER: Optional[DaemonWebController] = None


class Handler(BaseHTTPRequestHandler):
    server_version = "DaemonWeb/1.0"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, html: str, status: int = 200) -> None:
        raw = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def do_GET(self) -> None:
        global CONTROLLER
        if CONTROLLER is None:
            self._send_json({"ok": False, "error": "controller_not_initialized"}, 500)
            return

        if self.path == "/" or self.path.startswith("/?"):
            try:
                with open(UI_PATH, "r", encoding="utf-8") as f:
                    html = f.read()
            except Exception as exc:
                self._send_html(f"<h1>Failed to load UI</h1><pre>{exc}</pre>", 500)
                return
            self._send_html(html)
            return

        if self.path.startswith("/api/state"):
            limit = 300
            if "?" in self.path:
                query = self.path.split("?", 1)[1]
                for part in query.split("&"):
                    if part.startswith("log_limit="):
                        try:
                            limit = int(part.split("=", 1)[1])
                        except Exception:
                            pass
            self._send_json(CONTROLLER.get_state(limit))
            return

        self._send_json({"ok": False, "error": "not_found"}, 404)

    def do_POST(self) -> None:
        global CONTROLLER
        if CONTROLLER is None:
            self._send_json({"ok": False, "error": "controller_not_initialized"}, 500)
            return

        payload = self._read_json()

        if self.path == "/api/config/save":
            self._send_json(CONTROLLER.save_config(payload))
            return

        if self.path == "/api/config/reload":
            self._send_json(CONTROLLER.reload_config())
            return

        if self.path == "/api/logs/clear":
            self._send_json(CONTROLLER.clear_logs())
            return

        if self.path == "/api/start/maps":
            self._send_json(CONTROLLER.start_maps())
            return

        if self.path == "/api/stop/maps":
            self._send_json(CONTROLLER.stop_maps())
            return

        if self.path == "/api/start/email":
            self._send_json(CONTROLLER.start_email())
            return

        if self.path == "/api/stop/email":
            self._send_json(CONTROLLER.stop_email())
            return

        if self.path == "/api/start/both":
            self._send_json(CONTROLLER.start_both())
            return

        if self.path == "/api/stop/both":
            self._send_json(CONTROLLER.stop_both())
            return

        self._send_json({"ok": False, "error": "not_found"}, 404)


def run_server(host: str, port: int, config_path: str) -> None:
    global CONTROLLER
    CONTROLLER = DaemonWebController(config_path=config_path)

    server = ThreadingHTTPServer((host, port), Handler)

    def _signal_handler(signum, frame) -> None:
        if CONTROLLER is not None:
            CONTROLLER.shutdown()
        server.shutdown()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    bind_host = host if host else "0.0.0.0"
    print(f"Daemon Web UI running on http://{bind_host}:{port}")
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run daemon control panel as web UI")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host, default 0.0.0.0")
    parser.add_argument("--port", type=int, default=8787, help="Bind port, default 8787")
    parser.add_argument("--config", default=os.path.join(ROOT_DIR, "daemon_settings.json"), help="Path to daemon settings")
    args = parser.parse_args()

    run_server(host=args.host, port=args.port, config_path=os.path.abspath(args.config))


if __name__ == "__main__":
    main()
