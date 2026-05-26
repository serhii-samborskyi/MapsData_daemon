#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import ssl
import subprocess
import sys
import time
import urllib.request
from typing import Optional

LOCAL_DEPS = os.path.join(os.path.dirname(__file__), ".deps")
if os.path.isdir(LOCAL_DEPS) and LOCAL_DEPS not in sys.path:
    sys.path.insert(0, LOCAL_DEPS)

from daemon_config import load_config
from pipeline_runtime import PipelineContext, default_worker_id, run_pipeline_worker


STOP = False
CURRENT_CHILD: Optional[subprocess.Popen] = None
INSECURE_MODE = False
INSECURE_LOGGED = False


def _handle_signal(signum, frame) -> None:
    global STOP
    STOP = True
    if CURRENT_CHILD is not None and CURRENT_CHILD.poll() is None:
        CURRENT_CHILD.terminate()


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
    return logging.getLogger("email_daemon")


def _ensure_queue_dirs(queue_dir: str) -> None:
    os.makedirs(queue_dir, exist_ok=True)
    os.makedirs(os.path.join(queue_dir, "processed"), exist_ok=True)
    os.makedirs(os.path.join(queue_dir, "failed"), exist_ok=True)


def _job_exists(queue_dir: str, filename: str) -> bool:
    for sub in ("", "processed", "failed"):
        check_dir = queue_dir if not sub else os.path.join(queue_dir, sub)
        if not os.path.isdir(check_dir):
            continue
        for name in os.listdir(check_dir):
            if name == filename or name.startswith(f"{filename}."):
                return True
    return False


def _enqueue_job(queue_dir: str, campaign_id: str, campaign_name: str, logger: logging.Logger) -> bool:
    _ensure_queue_dirs(queue_dir)
    filename = f"campaign_{campaign_id}.json"
    if _job_exists(queue_dir, filename):
        return False
    payload = {
        "campaign_id": str(campaign_id),
        "campaign_name": campaign_name,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    tmp_path = os.path.join(queue_dir, f".{filename}.tmp")
    final_path = os.path.join(queue_dir, filename)
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp_path, final_path)
    logger.info(f"Queued email job: {final_path}")
    return True


def _move_job(job_path: str, dest_dir: str) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    base = os.path.basename(job_path)
    dest = os.path.join(dest_dir, base)
    if os.path.exists(dest):
        stamp = int(time.time())
        dest = os.path.join(dest_dir, f"{base}.{stamp}")
    os.replace(job_path, dest)
    return dest


def _load_job(job_path: str) -> Optional[dict]:
    try:
        with open(job_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        return None
    return None


def _list_jobs(queue_dir: str) -> list[str]:
    jobs = []
    for name in os.listdir(queue_dir):
        if not name.endswith(".json"):
            continue
        if name.startswith("."):
            continue
        jobs.append(os.path.join(queue_dir, name))
    jobs.sort(key=lambda p: os.path.getmtime(p))
    return jobs


def _normalize_base_url(base_url: str) -> str:
    base = (base_url or "").strip()
    if base.endswith("/api"):
        base = base[:-4]
    return base.rstrip("/")


def _get_json(url: str, logger: logging.Logger, timeout_s: float = 20.0) -> dict:
    global INSECURE_MODE, INSECURE_LOGGED
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        ctx = ssl._create_unverified_context() if INSECURE_MODE else None
        with urllib.request.urlopen(req, timeout=timeout_s, context=ctx) as resp:
            text = resp.read().decode("utf-8", "ignore")
        return json.loads(text) if text else {}
    except Exception as exc:
        if "CERTIFICATE_VERIFY_FAILED" in str(exc):
            try:
                INSECURE_MODE = True
                ctx = ssl._create_unverified_context()
                req = urllib.request.Request(url, headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=timeout_s, context=ctx) as resp:
                    text = resp.read().decode("utf-8", "ignore")
                if not INSECURE_LOGGED:
                    logger.warning("SSL verification failed; switching to insecure HTTPS for discovery calls.")
                    INSECURE_LOGGED = True
                return json.loads(text) if text else {}
            except Exception as exc2:
                logger.warning(f"GET failed for {url} after insecure retry: {exc2}")
                return {}
        logger.warning(f"GET failed for {url}: {exc}")
        return {}


def _discover_jobs(
    base_url: str,
    queue_dir: str,
    logger: logging.Logger,
) -> dict:
    stats = {"active": 0, "queued": 0, "no_nomail": 0}
    if not base_url:
        return stats
    base = _normalize_base_url(base_url)
    data = _get_json(f"{base}/api/campaigns/active", logger)
    campaigns = data.get("campaigns", []) if isinstance(data, dict) else []
    stats["active"] = len(campaigns)
    for camp in campaigns:
        campaign_id = str(camp.get("id", "")).strip()
        campaign_name = str(camp.get("name", "")).strip()
        if not campaign_id:
            continue
        probe = _get_json(f"{base}/api/campaign/{campaign_id}/nomail?batch=1", logger)
        contacts = probe.get("contacts", []) if isinstance(probe, dict) else []
        if not contacts:
            stats["no_nomail"] += 1
            continue
        queued = _enqueue_job(queue_dir, campaign_id, campaign_name, logger)
        if queued:
            logger.info(f"Discovered active campaign {campaign_id} with missing emails.")
            stats["queued"] += 1
    return stats


def _run_email_scraper(
    python_exe: str,
    script_path: str,
    campaign_id: str,
    base_url: str,
    batch: int,
    max_batches: int,
    max_batches_facebook: int,
    concurrency: int,
    timeout_s: float,
    domain_timeout_s: float,
    links: int,
    min_domain_letters: int,
    facebook: bool,
    facebook_engine: str,
    facebook_proxy_url: str,
    same_domain_only: bool,
    logger: logging.Logger,
) -> int:
    args = [
        python_exe,
        script_path,
        "--campaign",
        str(campaign_id),
        "--batch",
        str(batch),
        "--base-url",
        base_url,
        "--concurrency",
        str(concurrency),
        "--timeout",
        str(timeout_s),
        "--links",
        str(links),
        "--min-domain-letters",
        str(min_domain_letters),
        "--domain-timeout",
        str(domain_timeout_s),
    ]
    if max_batches is not None:
        args.extend(["--max-batches", str(max_batches)])
    if max_batches_facebook is not None:
        args.extend(["--max-batches-facebook", str(max_batches_facebook)])
    if facebook:
        args.append("--facebook")
        args.extend(["--facebook-engine", str(facebook_engine or "camoufox")])
        if str(facebook_proxy_url or "").strip():
            args.extend(["--facebook-proxy", str(facebook_proxy_url).strip()])
    if same_domain_only:
        args.append("--same-domain-only")

    logger.info(f"Starting email scraper for campaign {campaign_id}")
    global CURRENT_CHILD
    CURRENT_CHILD = subprocess.Popen(args, cwd=os.path.dirname(script_path))
    return_code = CURRENT_CHILD.wait()
    CURRENT_CHILD = None
    return return_code


def run_daemon(
    queue_dir: str,
    poll_interval_s: float,
    base_url: str,
    batch: int,
    max_batches: int,
    max_batches_facebook: int,
    concurrency: int,
    timeout_s: float,
    domain_timeout_s: float,
    links: int,
    min_domain_letters: int,
    facebook: bool,
    facebook_engine: str,
    facebook_proxy_url: str,
    same_domain_only: bool,
    scraper: str,
    log_path: str,
) -> None:
    logger = _setup_logging(log_path)
    logger.info("Email daemon starting")
    logger.info(
        "Email settings: max_batches=%s, max_batches_facebook=%s, facebook=%s, facebook_engine=%s, facebook_proxy=%s",
        max_batches,
        max_batches_facebook,
        facebook,
        facebook_engine,
        "on" if str(facebook_proxy_url or "").strip() else "off",
    )

    base_url = _normalize_base_url(base_url)

    _ensure_queue_dirs(queue_dir)
    scraper_key = str(scraper or "camoufox").strip().lower()
    script_name = "email_scraper.py"
    if scraper_key == "scrapy":
        script_name = "email_scraper_scrapy.py"
    script_path = os.path.join(os.path.dirname(__file__), script_name)
    if not os.path.exists(script_path):
        logger.warning("Email scraper '%s' not found at %s; falling back to Camoufox engine.", scraper_key, script_path)
        script_path = os.path.join(os.path.dirname(__file__), "email_scraper.py")
        scraper_key = "camoufox"
    logger.info("Email scraper engine: %s", scraper_key)
    python_exe = sys.executable
    last_discovery = 0.0

    while not STOP:
        now = time.monotonic()
        if now - last_discovery >= poll_interval_s:
            stats = _discover_jobs(base_url, queue_dir, logger)
            last_discovery = now
            if stats.get("queued", 0) == 0:
                logger.info(
                    "Discovery: active=%s queued=0 no_nomail=%s",
                    stats.get("active", 0),
                    stats.get("no_nomail", 0),
                )

        jobs = _list_jobs(queue_dir)
        if not jobs:
            time.sleep(poll_interval_s)
            continue

        job_path = jobs[0]
        payload = _load_job(job_path)
        if not payload:
            logger.error(f"Invalid job payload: {job_path}")
            _move_job(job_path, os.path.join(queue_dir, "failed"))
            continue

        campaign_id = str(payload.get("campaign_id", "")).strip()
        if not campaign_id:
            logger.error(f"Missing campaign_id in job: {job_path}")
            _move_job(job_path, os.path.join(queue_dir, "failed"))
            continue

        try:
            code = _run_email_scraper(
                python_exe=python_exe,
                script_path=script_path,
                campaign_id=campaign_id,
                base_url=base_url,
                batch=batch,
                max_batches=max_batches,
                max_batches_facebook=max_batches_facebook,
                concurrency=concurrency,
                timeout_s=timeout_s,
                domain_timeout_s=domain_timeout_s,
                links=links,
                min_domain_letters=min_domain_letters,
                facebook=facebook,
                facebook_engine=facebook_engine,
                facebook_proxy_url=facebook_proxy_url,
                same_domain_only=same_domain_only,
                logger=logger,
            )
        except Exception as exc:
            logger.exception(f"Email scraper failed for campaign {campaign_id}: {exc}")
            code = 1

        if code == 0:
            dest = _move_job(job_path, os.path.join(queue_dir, "processed"))
            logger.info(f"Email job completed: {dest}")
        else:
            dest = _move_job(job_path, os.path.join(queue_dir, "failed"))
            logger.error(f"Email job failed (code={code}): {dest}")

    logger.info("Email daemon stopping")


def run_pipeline_daemon(
    cfg: dict,
    log_path: str,
    worker_id: str,
    actor: str,
    claim_interval_s: float,
    lease_seconds: int,
    heartbeat_interval_s: float,
) -> None:
    logger = _setup_logging(log_path)
    logger.info("Email daemon starting in pipeline mode")

    maps_cfg = cfg.get("maps", {})
    email_cfg = cfg.get("email", {})
    pipeline_cfg = cfg.get("pipeline", {})
    ctx = PipelineContext(
        maps_base_url=str(cfg.get("maps_base_url", "")).strip(),
        email_base_url=str(cfg.get("email_base_url", "")).strip(),
        maps_cfg=maps_cfg if isinstance(maps_cfg, dict) else {},
        email_cfg=email_cfg if isinstance(email_cfg, dict) else {},
        pipeline_cfg=pipeline_cfg if isinstance(pipeline_cfg, dict) else {},
        base_dir=os.path.dirname(__file__),
    )

    run_pipeline_worker(
        logger=logger,
        ctx=ctx,
        worker_id=worker_id,
        actor=actor,
        claim_interval_s=claim_interval_s,
        lease_seconds=lease_seconds,
        heartbeat_interval_s=heartbeat_interval_s,
        should_stop=lambda: STOP,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Email daemon: process queued campaigns for email scraping.")
    parser.add_argument("--config", default="daemon_settings.json", help="Path to config JSON")
    parser.add_argument("--pipeline-mode", dest="pipeline_mode", action="store_const", const=True, default=None, help="Run pipeline worker mode (claim stages from API)")
    parser.add_argument("--legacy-mode", dest="pipeline_mode", action="store_const", const=False, help="Run legacy queue/discovery mode")
    parser.add_argument("--worker-id", default=None, help="Pipeline worker id (defaults to host+pid)")
    parser.add_argument("--actor", default=None, help="Pipeline actor name")
    parser.add_argument("--claim-interval", type=float, default=None, help="Pipeline claim polling interval in seconds")
    parser.add_argument("--lease-seconds", type=int, default=None, help="Pipeline lease length in seconds")
    parser.add_argument("--heartbeat-interval", type=float, default=None, help="Pipeline heartbeat interval in seconds")
    parser.add_argument("--email-base-url", default=None, help="Email API base URL (no /api)")
    parser.add_argument("--poll-interval", type=float, default=None, help="Polling interval in seconds")
    parser.add_argument("--queue-dir", default=None, help="Queue directory for email jobs")
    parser.add_argument("--batch", type=int, default=None, help="Batch size to fetch from /nomail")
    parser.add_argument("--concurrency", type=int, default=None, help="Parallel browser contexts")
    parser.add_argument("--timeout", type=float, default=None, help="Per-page timeout in seconds")
    parser.add_argument("--domain-timeout", type=float, default=None, help="Total timeout per domain")
    parser.add_argument("--links", type=int, default=None, help="Max child pages per domain")
    parser.add_argument("--min-domain-letters", type=int, default=None, help="Minimum letters in email root domain")
    parser.add_argument("--max-batches", type=int, default=None, help="Max batches per campaign run (0 = unlimited)")
    parser.add_argument("--max-batches-facebook", type=int, default=None, help="Max Facebook batches per run (0 = disabled)")
    parser.add_argument("--facebook", action="store_true", help="Enable Facebook page scraping")
    parser.add_argument("--facebook-engine", default=None, help="Facebook fallback engine (camoufox/playwright/scrapy)")
    parser.add_argument("--facebook-proxy", default=None, help="Optional rotating proxy URL for Facebook fallback (user:pass@host:port)")
    parser.add_argument("--same-domain-only", action="store_true", help="Only scrape links within the company domain")
    parser.add_argument("--scraper", default=None, help="Email scraper engine (camoufox/playwright/scrapy)")
    parser.add_argument("--log-path", default=None, help="Log file path")
    args = parser.parse_args()

    cfg = load_config(args.config)
    pipeline_cfg = cfg.get("pipeline", {})
    email_cfg = cfg.get("email", {})
    base_url = args.email_base_url or cfg.get("email_base_url")
    poll_interval_s = args.poll_interval if args.poll_interval is not None else cfg.get("email_poll_interval_s", 15)
    queue_dir = args.queue_dir or cfg.get("queue_dir", "queue")
    batch = args.batch if args.batch is not None else email_cfg.get("batch", 10)
    concurrency = args.concurrency if args.concurrency is not None else email_cfg.get("concurrency", 3)
    timeout_s = args.timeout if args.timeout is not None else email_cfg.get("timeout_s", 8.0)
    domain_timeout_s = args.domain_timeout if args.domain_timeout is not None else email_cfg.get("domain_timeout_s", 60.0)
    links = args.links if args.links is not None else email_cfg.get("links", 5)
    min_domain_letters = (
        args.min_domain_letters
        if args.min_domain_letters is not None
        else email_cfg.get("min_domain_letters", 2)
    )
    max_batches = args.max_batches if args.max_batches is not None else email_cfg.get("max_batches", 0)
    max_batches_facebook = (
        args.max_batches_facebook
        if args.max_batches_facebook is not None
        else email_cfg.get("max_batches_facebook", 0)
    )
    facebook = args.facebook or bool(email_cfg.get("facebook", False))
    facebook_engine = (args.facebook_engine or email_cfg.get("facebook_engine", "camoufox")).strip().lower()
    facebook_proxy_url = str(args.facebook_proxy if args.facebook_proxy is not None else email_cfg.get("facebook_proxy_url", "")).strip()
    same_domain_only = args.same_domain_only or bool(email_cfg.get("same_domain_only", True))
    scraper = (args.scraper or email_cfg.get("scraper", "camoufox")).strip().lower()
    log_path = args.log_path or cfg.get("logging", {}).get("email_log", "")
    pipeline_mode = args.pipeline_mode
    if pipeline_mode is None:
        pipeline_mode = bool(pipeline_cfg.get("enabled", True))
    actor = args.actor or str(pipeline_cfg.get("actor", "daemon")).strip() or "daemon"
    worker_id = args.worker_id or str(pipeline_cfg.get("worker_id", "")).strip() or default_worker_id("email")
    claim_interval_s = (
        args.claim_interval
        if args.claim_interval is not None
        else float(pipeline_cfg.get("claim_interval_s", 10))
    )
    lease_seconds = (
        args.lease_seconds
        if args.lease_seconds is not None
        else int(pipeline_cfg.get("lease_seconds", 120))
    )
    heartbeat_interval_s = (
        args.heartbeat_interval
        if args.heartbeat_interval is not None
        else float(pipeline_cfg.get("heartbeat_interval_s", 30))
    )

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if pipeline_mode:
        run_pipeline_daemon(
            cfg=cfg,
            log_path=log_path,
            worker_id=worker_id,
            actor=actor,
            claim_interval_s=claim_interval_s,
            lease_seconds=lease_seconds,
            heartbeat_interval_s=heartbeat_interval_s,
        )
        return

    run_daemon(
        queue_dir=queue_dir,
        poll_interval_s=poll_interval_s,
        base_url=base_url,
        batch=batch,
        max_batches=max_batches,
        max_batches_facebook=max_batches_facebook,
        concurrency=concurrency,
        timeout_s=timeout_s,
        domain_timeout_s=domain_timeout_s,
        links=links,
        min_domain_letters=min_domain_letters,
        facebook=facebook,
        facebook_engine=facebook_engine,
        facebook_proxy_url=facebook_proxy_url,
        same_domain_only=same_domain_only,
        scraper=scraper,
        log_path=log_path,
    )


if __name__ == "__main__":
    main()
