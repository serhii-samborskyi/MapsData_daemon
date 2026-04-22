from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any, Dict


DEFAULT_CONFIG: Dict[str, Any] = {
    "maps_base_url": "https://scrapiq.leadtechx.com/api",
    "email_base_url": "https://scrapiq.leadtechx.com",
    "maps_poll_interval_s": 30,
    "email_poll_interval_s": 15,
    "queue_dir": "queue",
    "maps": {
        "batch_size": 20,
        "max_concurrent": 1,
        "detail_workers": 1,
        "scrape_mode": "fast",
        "show_browser": False,
        "slow_place_pause_min_s": 0.8,
        "slow_place_pause_max_s": 1.8,
        "scroll_pause_min_s": 0.8,
        "scroll_pause_max_s": 0.8,
        "csv_dir": "",
    },
    "email": {
        "batch": 10,
        "concurrency": 3,
        "timeout_s": 8.0,
        "domain_timeout_s": 60.0,
        "links": 5,
        "facebook": False,
        "facebook_engine": "playwright",
        "max_batches": 0,
        "max_batches_facebook": 0,
        "scraper": "playwright",
        "same_domain_only": True,
        "min_domain_letters": 2,
    },
    "pipeline": {
        "enabled": True,
        "base_url": "",
        "actor": "daemon",
        "machine_id": "",
        "worker_id": "",
        "auto_start_on_run_not_started": True,
        "auto_start_cooldown_s": 30,
        "claim_interval_s": 10,
        "lease_seconds": 120,
        "heartbeat_interval_s": 30,
        "fast_scraper": "scrapy",
        "fast_concurrency": 3,
        "fast_max_batches_cap": 0,
        "fast_batches_multiplier": 1.1,
        "fast_email_policy": "business_only",
        "fallback_scraper": "playwright",
        "fallback_concurrency": 1,
        "fallback_max_batches": 0,
        "fallback_max_batches_facebook": 0,
        "fallback_batches_multiplier": 1.0,
        "fallback_facebook_batches_multiplier": 1.0,
        "fallback_email_policy": "business_or_public",
    },
    "logging": {
        "maps_log": "logs/maps_daemon.log",
        "email_log": "logs/email_daemon.log",
    },
}


def _merge(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _merge(dst[key], value)
        else:
            dst[key] = value
    return dst


def load_config(path: str) -> Dict[str, Any]:
    cfg = deepcopy(DEFAULT_CONFIG)
    if not path or not os.path.exists(path):
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _merge(cfg, data)
    except Exception:
        pass
    return cfg


def save_config(path: str, config: Dict[str, Any]) -> None:
    if not path:
        return
    dir_name = os.path.dirname(path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, sort_keys=True)
