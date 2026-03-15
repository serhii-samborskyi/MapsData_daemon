#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import time
from typing import Optional

import sys

LOCAL_DEPS = os.path.join(os.path.dirname(__file__), ".deps")
if os.path.isdir(LOCAL_DEPS) and LOCAL_DEPS not in sys.path:
    sys.path.insert(0, LOCAL_DEPS)

import maps_scraper
from daemon_config import load_config
from maps_scraper import Campaign, CampaignProcessor, HttpClient, LeadsApiClient


STOP = False
CURRENT_PROCESSOR: Optional[CampaignProcessor] = None


def _handle_signal(signum, frame) -> None:
    global STOP
    STOP = True
    if CURRENT_PROCESSOR is not None:
        CURRENT_PROCESSOR.stop()


def _setup_logging(log_path: str) -> logging.Logger:
    handlers = []
    if log_path:
        log_dir = os.path.dirname(log_path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        handlers.append(logging.FileHandler(log_path))
    handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
        force=True,
    )
    return logging.getLogger("maps_daemon")


def _ensure_queue_dirs(queue_dir: str) -> None:
    os.makedirs(queue_dir, exist_ok=True)
    os.makedirs(os.path.join(queue_dir, "processed"), exist_ok=True)
    os.makedirs(os.path.join(queue_dir, "failed"), exist_ok=True)


def _job_exists(queue_dir: str, filename: str) -> bool:
    for sub in ("", "processed", "failed"):
        check_dir = queue_dir if not sub else os.path.join(queue_dir, sub)
        if os.path.exists(os.path.join(check_dir, filename)):
            return True
    return False


def enqueue_email_job(queue_dir: str, campaign: Campaign, logger: logging.Logger) -> bool:
    _ensure_queue_dirs(queue_dir)
    filename = f"campaign_{campaign.id}.json"
    if _job_exists(queue_dir, filename):
        return False
    payload = {
        "campaign_id": str(campaign.id),
        "campaign_name": campaign.name,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    tmp_path = os.path.join(queue_dir, f".{filename}.tmp")
    final_path = os.path.join(queue_dir, filename)
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp_path, final_path)
    logger.info(f"Queued email job: {final_path}")
    return True


def run_daemon(
    base_url: str,
    poll_interval_s: float,
    queue_dir: str,
    batch_size: int,
    max_concurrent: int,
    scrape_mode: str,
    show_browser: bool,
    slow_place_pause_min_s: float,
    slow_place_pause_max_s: float,
    csv_dir: str,
    log_path: str,
) -> None:
    logger = _setup_logging(log_path)
    logger.info("Maps daemon starting")
    logger.info("Maps scrape mode: %s", maps_scraper.normalize_scrape_mode(scrape_mode))
    logger.info("Maps show browser: %s", maps_scraper.normalize_show_browser(show_browser))

    maps_scraper.MAX_CONCURRENT = max_concurrent
    maps_scraper.DEFAULT_SCRAPE_MODE = maps_scraper.normalize_scrape_mode(scrape_mode)
    maps_scraper.DEFAULT_SHOW_BROWSER = maps_scraper.normalize_show_browser(show_browser)
    (
        maps_scraper.DEFAULT_SLOW_PLACE_PAUSE_MIN_S,
        maps_scraper.DEFAULT_SLOW_PLACE_PAUSE_MAX_S,
    ) = maps_scraper.normalize_pause_range(slow_place_pause_min_s, slow_place_pause_max_s)
    logger.info(
        "Slow mode place pause range: %.2fs..%.2fs",
        maps_scraper.DEFAULT_SLOW_PLACE_PAUSE_MIN_S,
        maps_scraper.DEFAULT_SLOW_PLACE_PAUSE_MAX_S,
    )

    api = LeadsApiClient(base_url, HttpClient())
    processor = CampaignProcessor(
        api,
        batch_size=batch_size,
        csv_dir=csv_dir or None,
        scrape_mode=maps_scraper.DEFAULT_SCRAPE_MODE,
        show_browser=maps_scraper.DEFAULT_SHOW_BROWSER,
        slow_place_pause_min_s=maps_scraper.DEFAULT_SLOW_PLACE_PAUSE_MIN_S,
        slow_place_pause_max_s=maps_scraper.DEFAULT_SLOW_PLACE_PAUSE_MAX_S,
    )
    global CURRENT_PROCESSOR
    CURRENT_PROCESSOR = processor

    while not STOP:
        try:
            active = api.get_active_campaigns()
        except Exception as exc:
            logger.error(f"Failed to fetch active campaigns: {exc}")
            active = []

        if not active:
            time.sleep(poll_interval_s)
            continue

        for camp in active:
            if STOP:
                break
            logger.info(f"Processing campaign {camp.id}: {camp.name}")
            try:
                processor.process_campaign(camp)
                queued = enqueue_email_job(queue_dir, camp, logger)
                if not queued:
                    logger.info(f"Email job already queued for campaign {camp.id}")
            except Exception as exc:
                logger.exception(f"Campaign {camp.id} failed: {exc}")

        time.sleep(poll_interval_s)

    logger.info("Maps daemon stopping")


def main() -> None:
    parser = argparse.ArgumentParser(description="Maps daemon: watch for campaigns and scrape businesses.")
    parser.add_argument("--config", default="daemon_settings.json", help="Path to config JSON")
    parser.add_argument("--maps-base-url", default=None, help="Maps API base URL (includes /api)")
    parser.add_argument("--poll-interval", type=float, default=None, help="Polling interval in seconds")
    parser.add_argument("--queue-dir", default=None, help="Queue directory for email jobs")
    parser.add_argument("--batch-size", type=int, default=None, help="Batch size for maps upload")
    parser.add_argument("--max-concurrent", type=int, default=None, help="Max concurrent maps scraping tasks")
    parser.add_argument("--scrape-mode", choices=["fast", "slow"], default=None, help="Maps scrape mode")
    parser.add_argument("--show-browser", dest="show_browser", action="store_const", const=True, default=None, help="Show browser window while scraping maps")
    parser.add_argument("--hide-browser", dest="show_browser", action="store_const", const=False, help="Run maps scraping headless")
    parser.add_argument("--slow-place-pause-min", type=float, default=None, help="Slow mode minimum pause between place scrapes (seconds)")
    parser.add_argument("--slow-place-pause-max", type=float, default=None, help="Slow mode maximum pause between place scrapes (seconds)")
    parser.add_argument("--csv-dir", default=None, help="Optional CSV output directory")
    parser.add_argument("--log-path", default=None, help="Log file path")
    args = parser.parse_args()

    cfg = load_config(args.config)
    maps_cfg = cfg.get("maps", {})
    base_url = args.maps_base_url or cfg.get("maps_base_url")
    poll_interval_s = args.poll_interval if args.poll_interval is not None else cfg.get("maps_poll_interval_s", 30)
    queue_dir = args.queue_dir or cfg.get("queue_dir", "queue")
    batch_size = args.batch_size if args.batch_size is not None else maps_cfg.get("batch_size", 20)
    max_concurrent = args.max_concurrent if args.max_concurrent is not None else maps_cfg.get("max_concurrent", 3)
    scrape_mode = args.scrape_mode or maps_cfg.get("scrape_mode", "fast")
    show_browser = args.show_browser if args.show_browser is not None else maps_cfg.get("show_browser", False)
    slow_place_pause_min_s = args.slow_place_pause_min if args.slow_place_pause_min is not None else maps_cfg.get("slow_place_pause_min_s", 0.8)
    slow_place_pause_max_s = args.slow_place_pause_max if args.slow_place_pause_max is not None else maps_cfg.get("slow_place_pause_max_s", 1.8)
    csv_dir = args.csv_dir if args.csv_dir is not None else maps_cfg.get("csv_dir", "")
    log_path = args.log_path or cfg.get("logging", {}).get("maps_log", "")

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    run_daemon(
        base_url=base_url,
        poll_interval_s=poll_interval_s,
        queue_dir=queue_dir,
        batch_size=batch_size,
        max_concurrent=max_concurrent,
        scrape_mode=scrape_mode,
        show_browser=show_browser,
        slow_place_pause_min_s=slow_place_pause_min_s,
        slow_place_pause_max_s=slow_place_pause_max_s,
        csv_dir=csv_dir,
        log_path=log_path,
    )


if __name__ == "__main__":
    main()
