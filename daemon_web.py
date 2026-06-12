#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import quote, urlparse
from urllib.request import Request, ProxyHandler, build_opener, urlopen

try:
    import requests  # type: ignore
except Exception:
    requests = None  # type: ignore

try:
    import psutil  # type: ignore
except Exception:
    psutil = None  # type: ignore

from daemon_config import apply_browser_blocking_env, load_config, normalize_blocked_resource_extensions, save_config


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


def _normalize_proxy_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"http://{raw}"
    try:
        parsed = urlparse(raw)
    except Exception:
        return ""
    if not parsed.hostname or not parsed.port:
        return ""
    host = parsed.hostname
    port = parsed.port
    username = parsed.username or ""
    password = parsed.password or ""
    if username and password:
        return f"http://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"
    return f"http://{host}:{port}"


def _strip_detail_url_trailing_slash(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except Exception:
        return raw.rstrip("/")
    if parsed.scheme and parsed.netloc:
        path = parsed.path.rstrip("/")
        return parsed._replace(path=path).geturl()
    return raw.rstrip("/")


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
        self._cpu_percent_last = 0.0
        if psutil is not None:
            try:
                psutil.cpu_percent(interval=None)
            except Exception:
                pass

        self.log_lines = deque(maxlen=MAX_LOG_LINES)
        self.debug_runs: Dict[str, Dict[str, Any]] = {}
        self.debug_runs_order = deque(maxlen=50)
        self.live_debug_sessions: Dict[str, Dict[str, Any]] = {}
        self.live_debug_sessions_order = deque(maxlen=20)
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
            apply_browser_blocking_env(self.config)
            mode_flag = self._daemon_mode_flag()
            args = [sys.executable, script_name, "--config", self.config_path, mode_flag]
            maps_cfg = self.config.get("maps", {}) if isinstance(self.config.get("maps"), dict) else {}
            email_cfg = self.config.get("email", {}) if isinstance(self.config.get("email"), dict) else {}
            if name == "maps":
                args.append("--show-browser" if bool(maps_cfg.get("show_browser", False)) else "--hide-browser")
            else:
                args.append("--show-browser" if bool(email_cfg.get("show_browser", False)) else "--hide-browser")
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
            self._append_log(
                "ui",
                (
                    f"Effective settings for {name}: "
                    f"maps_show_browser={bool(maps_cfg.get('show_browser', False))} "
                    f"email_show_browser={bool(email_cfg.get('show_browser', False))}"
                ),
            )
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

        browser_cfg = dict(current.get("browser") or {})
        browser_input = payload.get("browser") if isinstance(payload.get("browser"), dict) else {}
        browser_cfg["block_resource_extensions_enabled"] = _to_bool(
            browser_input.get(
                "block_resource_extensions_enabled",
                browser_cfg.get("block_resource_extensions_enabled", True),
            ),
            True,
        )
        browser_cfg["blocked_resource_extensions"] = normalize_blocked_resource_extensions(
            browser_input.get("blocked_resource_extensions", browser_cfg.get("blocked_resource_extensions", []))
        )
        current["browser"] = browser_cfg

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
        maps_cfg["proxy_url"] = str(maps_input.get("proxy_url", maps_cfg.get("proxy_url", ""))).strip()
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
        email_cfg["show_browser"] = _to_bool(email_input.get("show_browser"), bool(email_cfg.get("show_browser", False)))
        fb_engine = str(email_input.get("facebook_engine", email_cfg.get("facebook_engine", "camoufox"))).strip().lower()
        email_cfg["facebook_engine"] = fb_engine if fb_engine in {"camoufox", "playwright", "scrapy"} else "camoufox"
        email_cfg["facebook_proxy_url"] = str(email_input.get("facebook_proxy_url", email_cfg.get("facebook_proxy_url", ""))).strip()
        email_cfg["same_domain_only"] = _to_bool(email_input.get("same_domain_only"), bool(email_cfg.get("same_domain_only", True)))
        scraper = str(email_input.get("scraper", email_cfg.get("scraper", "camoufox"))).strip().lower()
        email_cfg["scraper"] = scraper if scraper in {"camoufox", "playwright", "scrapy"} else "camoufox"
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
        pipeline_cfg["fast_scraper"] = fast_scraper if fast_scraper in {"scrapy", "camoufox", "playwright"} else "scrapy"
        pipeline_cfg["fast_concurrency"] = _to_int(pipeline_input.get("fast_concurrency"), int(pipeline_cfg.get("fast_concurrency", 3)), 1, 20)
        pipeline_cfg["fast_batches_multiplier"] = _to_float(pipeline_input.get("fast_batches_multiplier"), float(pipeline_cfg.get("fast_batches_multiplier", 1.1)), 0.1, 3.0)
        fast_policy = str(pipeline_input.get("fast_email_policy", pipeline_cfg.get("fast_email_policy", "business_only"))).strip().lower()
        pipeline_cfg["fast_email_policy"] = fast_policy if fast_policy in {"business_only", "business_or_public", "any_valid"} else "business_only"
        pipeline_cfg["fast_max_batches_cap"] = _to_int(pipeline_input.get("fast_max_batches_cap"), int(pipeline_cfg.get("fast_max_batches_cap", 0)), 0, 10000)

        fallback_scraper = str(pipeline_input.get("fallback_scraper", pipeline_cfg.get("fallback_scraper", "camoufox"))).strip().lower()
        pipeline_cfg["fallback_scraper"] = fallback_scraper if fallback_scraper in {"scrapy", "camoufox", "playwright"} else "camoufox"
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

    def _safe_process_count(self) -> int:
        if psutil is not None:
            try:
                return len(psutil.pids())
            except Exception:
                pass
        try:
            out = subprocess.check_output(["ps", "-A", "-o", "pid="], text=True, timeout=2)
            return len([line for line in out.splitlines() if line.strip()])
        except Exception:
            return 0

    def _safe_queue_count(self) -> int:
        try:
            queue_dir = str(self.config.get("queue_dir", "queue") or "queue").strip() or "queue"
            queue_path = queue_dir if os.path.isabs(queue_dir) else os.path.join(self.base_dir, queue_dir)
            if not os.path.isdir(queue_path):
                return 0
            count = 0
            for name in os.listdir(queue_path):
                if name.startswith("."):
                    continue
                full = os.path.join(queue_path, name)
                if os.path.isfile(full):
                    count += 1
            return count
        except Exception:
            return 0

    def _safe_gpu_stats(self) -> Dict[str, Any]:
        try:
            cmd = [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ]
            out = subprocess.check_output(cmd, text=True, timeout=2).strip()
            if not out:
                raise RuntimeError("empty nvidia-smi output")
            rows = [line.strip() for line in out.splitlines() if line.strip()]
            utils = []
            used = []
            total = []
            for row in rows:
                parts = [p.strip() for p in row.split(",")]
                if len(parts) < 3:
                    continue
                try:
                    utils.append(float(parts[0]))
                    used.append(float(parts[1]))
                    total.append(float(parts[2]))
                except Exception:
                    continue
            if not utils or not total:
                raise RuntimeError("no parseable gpu rows")
            gpu_percent = sum(utils) / len(utils)
            mem_used = sum(used)
            mem_total = sum(total)
            mem_percent = (mem_used / mem_total * 100.0) if mem_total > 0 else 0.0
            return {
                "available": True,
                "percent": round(gpu_percent, 1),
                "mem_percent": round(mem_percent, 1),
                "mem_used_mib": round(mem_used, 1),
                "mem_total_mib": round(mem_total, 1),
            }
        except Exception:
            return {
                "available": False,
                "percent": 0.0,
                "mem_percent": 0.0,
                "mem_used_mib": 0.0,
                "mem_total_mib": 0.0,
            }

    def collect_system_stats(self) -> Dict[str, Any]:
        cpu_percent = 0.0
        cpu_cores = os.cpu_count() or 0
        ram_percent = 0.0
        ram_used = 0
        ram_total = 0
        rss = 0

        if psutil is not None:
            try:
                cpu_percent = float(psutil.cpu_percent(interval=None))
            except Exception:
                cpu_percent = self._cpu_percent_last
            self._cpu_percent_last = cpu_percent
            try:
                cpu_cores = int(psutil.cpu_count(logical=True) or cpu_cores)
            except Exception:
                pass
            try:
                vm = psutil.virtual_memory()
                ram_percent = float(vm.percent)
                ram_used = int(vm.used)
                ram_total = int(vm.total)
            except Exception:
                pass
            try:
                rss = int(psutil.Process(os.getpid()).memory_info().rss)
            except Exception:
                pass
        else:
            self._cpu_percent_last = cpu_percent

        try:
            disk = shutil.disk_usage("/")
            disk_total = int(disk.total)
            disk_used = int(disk.used)
            disk_percent = (disk_used / disk_total * 100.0) if disk_total > 0 else 0.0
        except Exception:
            disk_total = 0
            disk_used = 0
            disk_percent = 0.0

        return {
            "processes": {
                "count": self._safe_process_count(),
                "queued": self._safe_queue_count(),
            },
            "cpu": {
                "percent": round(cpu_percent, 1),
                "cores": int(cpu_cores),
            },
            "ram": {
                "percent": round(ram_percent, 1),
                "used_bytes": int(ram_used),
                "total_bytes": int(ram_total),
                "rss_bytes": int(rss),
            },
            "gpu": self._safe_gpu_stats(),
            "disk": {
                "percent": round(disk_percent, 1),
                "used_bytes": int(disk_used),
                "total_bytes": int(disk_total),
            },
        }

    def test_proxy(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        target = str(payload.get("target", "proxy")).strip().lower() or "proxy"
        proxy_input = str(payload.get("proxy_url", "")).strip()
        proxy_url = _normalize_proxy_url(proxy_input)
        if not proxy_url:
            return {"ok": False, "error": "invalid_proxy", "detail": "Proxy URL is empty or invalid.", "target": target}

        ipify_url = "https://api.ipify.org?format=json"
        try:
            if requests is not None:
                resp = requests.get(
                    ipify_url,
                    timeout=20,
                    proxies={"http": proxy_url, "https": proxy_url},
                    headers={"Accept": "application/json", "User-Agent": "ScrapIQ-DaemonWeb/1.0"},
                )
                resp.raise_for_status()
                data = resp.json() if resp.text else {}
                ip = str(data.get("ip", "")).strip()
                if not ip:
                    return {"ok": False, "error": "ip_missing", "detail": f"Unexpected response: {resp.text[:200]}", "target": target}
                return {"ok": True, "target": target, "ip": ip}

            opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
            req = Request(ipify_url, headers={"Accept": "application/json", "User-Agent": "ScrapIQ-DaemonWeb/1.0"})
            with opener.open(req, timeout=20) as raw:
                body = raw.read().decode("utf-8", errors="ignore")
            data = json.loads(body) if body else {}
            ip = str(data.get("ip", "")).strip()
            if not ip:
                return {"ok": False, "error": "ip_missing", "detail": f"Unexpected response: {body[:200]}", "target": target}
            return {"ok": True, "target": target, "ip": ip}
        except Exception as exc:
            return {"ok": False, "error": "proxy_test_failed", "detail": str(exc), "target": target}

    def start_source_debug(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        query = str(payload.get("query") or "").strip()
        detail_url = _strip_detail_url_trailing_slash(payload.get("detail_url"))
        if not source:
            return {"ok": False, "error": "source_required"}
        if not query and not detail_url:
            return {"ok": False, "error": "query_or_detail_url_required"}
        run_id = uuid.uuid4().hex[:12]
        maps_cfg = self.config.get("maps", {}) if isinstance(self.config.get("maps"), dict) else {}
        debug_run: Dict[str, Any] = {
            "id": run_id,
            "ok": True,
            "status": "running",
            "created_at": time.time(),
            "updated_at": time.time(),
            "logs": [],
            "events": [],
            "result": None,
            "error": "",
        }
        with self.lock:
            self.debug_runs[run_id] = debug_run
            self.debug_runs_order.append(run_id)
            while len(self.debug_runs_order) > 40:
                old_id = self.debug_runs_order.popleft()
                self.debug_runs.pop(old_id, None)

        def append_log(message: str) -> None:
            line = f"[{time.strftime('%H:%M:%S')}] {message}"
            with self.lock:
                current = self.debug_runs.get(run_id)
                if not current:
                    return
                current.setdefault("logs", []).append(line)
                current["updated_at"] = time.time()

        def append_event(event: Dict[str, Any]) -> None:
            event_payload = dict(event or {})
            event_payload["time"] = time.strftime("%H:%M:%S")
            with self.lock:
                current = self.debug_runs.get(run_id)
                if not current:
                    return
                events = current.setdefault("events", [])
                events.append(event_payload)
                if len(events) > 500:
                    del events[: len(events) - 500]
                current["updated_at"] = time.time()

        def worker() -> None:
            try:
                from source_runner import debug_source_template
                import asyncio

                result = asyncio.run(debug_source_template(
                    source=source,
                    query=query,
                    detail_url=detail_url,
                    scrape_mode=str(payload.get("scrape_mode") or "fast"),
                    show_browser=_to_bool(payload.get("show_browser"), bool(maps_cfg.get("show_browser", False))),
                    proxy_url=str(payload.get("proxy_url") if payload.get("proxy_url") is not None else maps_cfg.get("proxy_url", "")),
                    max_scrolls_override=_to_int(payload.get("max_scrolls"), 0, 0, 500) or None,
                    max_blocks=_to_int(payload.get("max_blocks"), 5, 1, 30),
                    max_detail_pages=_to_int(payload.get("max_detail_pages"), 3, 0, 20),
                    detail_hold_seconds=_to_float(payload.get("detail_hold_seconds"), 0.0, 0.0, 120.0),
                    log=append_log,
                    progress=append_event,
                ))
                with self.lock:
                    current = self.debug_runs.get(run_id)
                    if current:
                        current["status"] = "completed" if result.get("ok") else "failed"
                        current["result"] = result
                        current["updated_at"] = time.time()
            except Exception as exc:
                append_log(f"Debug worker failed: {exc}")
                with self.lock:
                    current = self.debug_runs.get(run_id)
                    if current:
                        current["status"] = "failed"
                        current["error"] = str(exc)
                        current["updated_at"] = time.time()

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "run_id": run_id, "status": "running"}

    def get_source_debug(self, run_id: str) -> Dict[str, Any]:
        with self.lock:
            run = self.debug_runs.get(run_id)
            if not run:
                return {"ok": False, "error": "debug_run_not_found"}
            payload = dict(run)
            payload["logs"] = list(run.get("logs") or [])
            payload["events"] = list(run.get("events") or [])
            return payload

    def start_live_detail_debug(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        detail_url = _strip_detail_url_trailing_slash(payload.get("detail_url"))
        if not detail_url:
            return {"ok": False, "error": "detail_url_required"}
        run_id = uuid.uuid4().hex[:12]
        maps_cfg = self.config.get("maps", {}) if isinstance(self.config.get("maps"), dict) else {}
        session: Dict[str, Any] = {
            "id": run_id,
            "ok": True,
            "status": "starting",
            "detail_url": detail_url,
            "final_url": "",
            "title": "",
            "created_at": time.time(),
            "updated_at": time.time(),
            "logs": [],
            "error": "",
            "last_result": None,
            "stop_requested": False,
            "loop": None,
            "runtime": None,
            "page": None,
        }
        with self.lock:
            self.live_debug_sessions[run_id] = session
            self.live_debug_sessions_order.append(run_id)
            while len(self.live_debug_sessions_order) > 15:
                old_id = self.live_debug_sessions_order.popleft()
                self._stop_live_detail_debug_locked(old_id)

        def append_log(message: str) -> None:
            line = f"[{time.strftime('%H:%M:%S')}] {message}"
            with self.lock:
                current = self.live_debug_sessions.get(run_id)
                if not current:
                    return
                current.setdefault("logs", []).append(line)
                current["updated_at"] = time.time()

        def worker() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            runtime = None
            try:
                from browser_backend import AsyncBrowserRuntime, install_async_blocked_resource_routes, normalize_proxy_url
                from daemon_config import apply_browser_blocking_env
                with self.lock:
                    apply_browser_blocking_env(self.config)

                async def boot() -> None:
                    nonlocal runtime
                    runtime = AsyncBrowserRuntime(
                        headless=(not _to_bool(payload.get("show_browser"), True)),
                        chromium_args=["--disable-blink-features=AutomationControlled", "--disable-extensions"],
                        camoufox_options={"block_images": False},
                        proxy_url=normalize_proxy_url(str(payload.get("proxy_url") if payload.get("proxy_url") is not None else maps_cfg.get("proxy_url", ""))),
                    )
                    browser = await runtime.launch()
                    context = await browser.new_context()
                    await install_async_blocked_resource_routes(context)
                    page = await context.new_page()
                    append_log(f"Opening live detail page {detail_url}")
                    await page.goto(detail_url, wait_until="domcontentloaded", timeout=60000)
                    try:
                        await page.wait_for_function("() => document.body || document.documentElement", timeout=20000)
                    except Exception as exc:
                        append_log(f"DOM wait warning: {exc}")
                    title = await page.title()
                    with self.lock:
                        current = self.live_debug_sessions.get(run_id)
                        if current and current.get("stop_requested"):
                            append_log("Live detail page opened, but stop was requested before ready.")
                            return
                        if current:
                            current["status"] = "ready"
                            current["loop"] = loop
                            current["runtime"] = runtime
                            current["page"] = page
                            current["title"] = title
                            current["final_url"] = page.url
                            current["updated_at"] = time.time()
                    append_log(f"Live detail page ready title={title!r} url={page.url}")

                loop.run_until_complete(boot())
                with self.lock:
                    should_run = bool(self.live_debug_sessions.get(run_id, {}).get("status") == "ready")
                if should_run:
                    loop.run_forever()
            except Exception as exc:
                append_log(f"Live detail debug failed: {exc}")
                with self.lock:
                    current = self.live_debug_sessions.get(run_id)
                    if current:
                        current["status"] = "failed"
                        current["error"] = str(exc)
                        current["updated_at"] = time.time()
            finally:
                if runtime is not None:
                    try:
                        loop.run_until_complete(runtime.close())
                    except Exception:
                        pass
                with self.lock:
                    current = self.live_debug_sessions.get(run_id)
                    if current and current.get("status") not in {"failed", "stopped"}:
                        current["status"] = "stopped"
                    if current:
                        current["loop"] = None
                        current["runtime"] = None
                        current["page"] = None
                        current["updated_at"] = time.time()
                try:
                    loop.close()
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "session_id": run_id, "status": "starting"}

    async def _evaluate_live_detail_page(self, page, payload: Dict[str, Any]) -> Dict[str, Any]:
        xpath = str(payload.get("xpath") or "").strip()
        regex = str(payload.get("regex") or "").strip()
        strip_html = _to_bool(payload.get("strip_html"), False)
        scrolls = _to_int(payload.get("scrolls"), 0, 0, 50)
        if not xpath:
            return {"ok": False, "error": "xpath_required"}
        return await page.evaluate(
            r"""
            async ({xpath, regex, stripHtml, scrolls}) => {
                const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
                for (let i = 0; i < scrolls; i++) {
                    const root = document.scrollingElement || document.documentElement || document.body;
                    if (root) window.scrollBy(0, Math.max(500, Math.floor((window.innerHeight || 900) * 0.85)));
                    await sleep(650);
                }
                const cleanText = value => String(value || '').replace(/\s+/g, ' ').trim();
                const nodeText = node => {
                    if (!node) return '';
                    if (node.nodeType === Node.ATTRIBUTE_NODE) return node.value || '';
                    if (node.nodeType === Node.TEXT_NODE) return node.textContent || '';
                    if (stripHtml) {
                        const clone = node.cloneNode(true);
                        if (clone.querySelectorAll) clone.querySelectorAll('script,style,noscript,template,svg').forEach(child => child.remove());
                        return node.innerText || clone.textContent || '';
                    }
                    return node.innerText || node.textContent || node.getAttribute?.('href') || '';
                };
                const nodeHtml = node => {
                    if (!node || node.nodeType !== Node.ELEMENT_NODE) return '';
                    return cleanText(node.outerHTML || '').slice(0, 600);
                };
                const values = [];
                const result = document.evaluate(xpath, document, null, XPathResult.ANY_TYPE, null);
                const push = node => values.push({text: cleanText(nodeText(node)), html: nodeHtml(node)});
                switch (result.resultType) {
                    case XPathResult.STRING_TYPE:
                        values.push({text: cleanText(result.stringValue || ''), html: ''});
                        break;
                    case XPathResult.NUMBER_TYPE:
                        values.push({text: String(result.numberValue), html: ''});
                        break;
                    case XPathResult.BOOLEAN_TYPE:
                        values.push({text: String(result.booleanValue), html: ''});
                        break;
                    case XPathResult.UNORDERED_NODE_ITERATOR_TYPE:
                    case XPathResult.ORDERED_NODE_ITERATOR_TYPE: {
                        let node = result.iterateNext();
                        while (node) {
                            push(node);
                            node = result.iterateNext();
                        }
                        break;
                    }
                    case XPathResult.UNORDERED_NODE_SNAPSHOT_TYPE:
                    case XPathResult.ORDERED_NODE_SNAPSHOT_TYPE:
                        for (let i = 0; i < result.snapshotLength; i++) push(result.snapshotItem(i));
                        break;
                    case XPathResult.ANY_UNORDERED_NODE_TYPE:
                    case XPathResult.FIRST_ORDERED_NODE_TYPE:
                        push(result.singleNodeValue);
                        break;
                }
                const filtered = values.filter(item => item.text);
                let rx = null;
                let regexError = '';
                if (regex) {
                    try { rx = new RegExp(regex, 'i'); }
                    catch (err) { regexError = String(err && err.message || err); }
                }
                const attempts = filtered.slice(0, 100).map((item, idx) => {
                    let matched = false;
                    let value = '';
                    if (rx) {
                        const match = item.text.match(rx);
                        if (match) {
                            matched = true;
                            value = cleanText(match[1] || match[0] || '');
                        }
                    }
                    return {
                        index: idx + 1,
                        matched,
                        value,
                        preview: item.text.slice(0, 500),
                        html_preview: item.html,
                    };
                });
                const firstMatch = attempts.find(item => item.matched);
                return {
                    ok: !regexError,
                    error: regexError,
                    url: location.href,
                    title: document.title || '',
                    readyState: document.readyState || '',
                    xpath,
                    regex,
                    strip_html: !!stripHtml,
                    scrolls,
                    total_matches: filtered.length,
                    first_value: firstMatch ? firstMatch.value : (filtered[0] ? filtered[0].text : ''),
                    matched_attempt_index: firstMatch ? firstMatch.index : 0,
                    attempts,
                };
            }
            """,
            {"xpath": xpath, "regex": regex, "stripHtml": strip_html, "scrolls": scrolls},
        )

    def evaluate_live_detail_debug(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        session_id = str(payload.get("session_id") or "").strip()
        with self.lock:
            session = self.live_debug_sessions.get(session_id)
            if not session:
                return {"ok": False, "error": "live_session_not_found"}
            if session.get("status") != "ready":
                return {"ok": False, "error": "live_session_not_ready", "status": session.get("status")}
            loop = session.get("loop")
            page = session.get("page")
        if loop is None or page is None:
            return {"ok": False, "error": "live_session_not_ready"}
        try:
            future = asyncio.run_coroutine_threadsafe(self._evaluate_live_detail_page(page, payload), loop)
            result = future.result(timeout=90)
            with self.lock:
                current = self.live_debug_sessions.get(session_id)
                if current:
                    current["last_result"] = result
                    current["updated_at"] = time.time()
            return {"ok": True, "session_id": session_id, "result": result}
        except Exception as exc:
            return {"ok": False, "error": "live_eval_failed", "detail": str(exc)}

    def _stop_live_detail_debug_locked(self, session_id: str) -> Dict[str, Any]:
        session = self.live_debug_sessions.get(session_id)
        if not session:
            return {"ok": False, "error": "live_session_not_found"}
        loop = session.get("loop")
        runtime = session.get("runtime")
        session["stop_requested"] = True
        session["status"] = "stopped"
        session["updated_at"] = time.time()
        if loop is not None:
            async def close_and_stop() -> None:
                try:
                    if runtime is not None:
                        await runtime.close()
                finally:
                    loop.stop()
            try:
                asyncio.run_coroutine_threadsafe(close_and_stop(), loop)
            except Exception:
                pass
        return {"ok": True, "session_id": session_id, "status": "stopped"}

    def stop_live_detail_debug(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        session_id = str(payload.get("session_id") or "").strip()
        with self.lock:
            return self._stop_live_detail_debug_locked(session_id)

    def get_live_detail_debug(self, session_id: str) -> Dict[str, Any]:
        with self.lock:
            session = self.live_debug_sessions.get(session_id)
            if not session:
                return {"ok": False, "error": "live_session_not_found"}
            return {
                "ok": True,
                "id": session.get("id"),
                "status": session.get("status"),
                "detail_url": session.get("detail_url"),
                "final_url": session.get("final_url"),
                "title": session.get("title"),
                "created_at": session.get("created_at"),
                "updated_at": session.get("updated_at"),
                "logs": list(session.get("logs") or []),
                "error": session.get("error"),
                "last_result": session.get("last_result"),
            }

    def get_state(self, log_limit: int = 300) -> Dict[str, Any]:
        with self.lock:
            maps_running = self.maps_proc is not None and self.maps_proc.poll() is None
            email_running = self.email_proc is not None and self.email_proc.poll() is None
            tail = list(self.log_lines)[-max(1, min(log_limit, MAX_LOG_LINES)):]
            system = self.collect_system_stats()
            return {
                "ok": True,
                "config": self.config,
                "system": system,
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
        with self.lock:
            session_ids = list(self.live_debug_sessions.keys())
            for session_id in session_ids:
                self._stop_live_detail_debug_locked(session_id)
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
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, html: str, status: int = 200) -> None:
        raw = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
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

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

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

        if self.path.startswith("/api/source-debug/live/"):
            session_id = self.path.rstrip("/").split("/")[-1]
            self._send_json(CONTROLLER.get_live_detail_debug(session_id))
            return

        if self.path.startswith("/api/source-debug/"):
            run_id = self.path.rstrip("/").split("/")[-1]
            self._send_json(CONTROLLER.get_source_debug(run_id))
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

        if self.path == "/api/source-debug/run":
            self._send_json(CONTROLLER.start_source_debug(payload))
            return

        if self.path == "/api/source-debug/live-start":
            self._send_json(CONTROLLER.start_live_detail_debug(payload))
            return

        if self.path == "/api/source-debug/live-eval":
            self._send_json(CONTROLLER.evaluate_live_detail_debug(payload))
            return

        if self.path == "/api/source-debug/live-stop":
            self._send_json(CONTROLLER.stop_live_detail_debug(payload))
            return

        if self.path == "/api/proxy/test":
            self._send_json(CONTROLLER.test_proxy(payload))
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
