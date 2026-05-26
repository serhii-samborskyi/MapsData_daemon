from __future__ import annotations

import json
import logging
import math
import os
import socket
import ssl
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

class PipelineApiClient:
    def __init__(self, base_url: str, logger: logging.Logger, timeout_s: float = 20.0) -> None:
        base = (base_url or "").strip().rstrip("/")
        if base.endswith("/api"):
            base = base[:-4]
        self.base_url = base
        self.timeout_s = timeout_s
        self.logger = logger
        self.insecure = False
        self._insecure_logged = False

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
            ctx = ssl._create_unverified_context() if self.insecure else None
            with urllib.request.urlopen(req, timeout=timeout_s or self.timeout_s, context=ctx) as resp:
                body = resp.read().decode("utf-8", "ignore")
                status_code = int(getattr(resp, "status", 200) or 200)
            parsed = json.loads(body) if body else {}
            if isinstance(parsed, dict):
                parsed.setdefault("_ok", True)
                parsed.setdefault("_status", status_code)
                return parsed
            return {"_ok": True, "_status": status_code, "data": parsed}
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "ignore")
            except Exception:
                body = ""
            detail = body.strip() or str(exc)
            self.logger.warning("Pipeline API %s %s failed: HTTP %s %s", method.upper(), path, exc.code, detail[:400])
            return {"_ok": False, "_status": int(exc.code), "_error": detail}
        except Exception as exc:
            if "CERTIFICATE_VERIFY_FAILED" in str(exc) and not self.insecure:
                self.insecure = True
                return self._request_json(method, path, payload, timeout_s)
            if self.insecure and not self._insecure_logged:
                self.logger.warning("SSL verification failed; switching to insecure HTTPS for pipeline calls.")
                self._insecure_logged = True
            self.logger.warning("Pipeline API %s %s failed: %s", method.upper(), path, exc)
            return {"_ok": False, "_status": 0, "_error": str(exc)}

    def claim(self, worker_id: str, machine_id: str, actor: str, lease_seconds: int) -> Dict[str, Any]:
        payload = {
            "worker_id": worker_id,
            "machine_id": machine_id,
            "actor": actor,
            "lease_seconds": int(lease_seconds),
        }
        return self._request_json("POST", "/api/pipeline/claim", payload)

    def list_active_campaigns(self) -> list[Dict[str, Any]]:
        data = self._request_json("GET", "/api/campaigns/active")
        campaigns = data.get("campaigns", []) if isinstance(data, dict) else []
        if not isinstance(campaigns, list):
            return []
        return [campaign for campaign in campaigns if isinstance(campaign, dict)]

    def start_pipeline(self, campaign_id: str, actor: str, retry: bool = False) -> Dict[str, Any]:
        payload = {
            "actor": str(actor or "daemon").strip() or "daemon",
            "retry": bool(retry),
        }
        return self._request_json("POST", f"/api/campaign/{campaign_id}/pipeline/start", payload)

    def heartbeat(self, run_id: str, worker_id: str, machine_id: str, stage: str, lease_seconds: int) -> Dict[str, Any]:
        payload = {
            "worker_id": worker_id,
            "machine_id": machine_id,
            "stage": stage,
            "lease_seconds": int(lease_seconds),
        }
        return self._request_json("POST", f"/api/pipeline/{run_id}/heartbeat", payload)

    def stage_complete(self, run_id: str, worker_id: str, machine_id: str, stage: str) -> Dict[str, Any]:
        payload = {
            "worker_id": worker_id,
            "machine_id": machine_id,
            "stage": stage,
        }
        return self._request_json("POST", f"/api/pipeline/{run_id}/stage-complete", payload)

    def fail(
        self,
        run_id: str,
        worker_id: str,
        machine_id: str,
        stage: str,
        error: str,
        error_payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = {
            "worker_id": worker_id,
            "machine_id": machine_id,
            "stage": stage,
            "error": error,
            "error_payload": error_payload or {},
        }
        return self._request_json("POST", f"/api/pipeline/{run_id}/fail", payload)

    def cleanup_contacts(self, campaign_id: str) -> Dict[str, Any]:
        payload = {"campaign_id": str(campaign_id).strip()}
        return self._request_json("POST", f"/api/campaign/{campaign_id}/contacts/cleanup", payload)

    def get_stats(self, campaign_id: str) -> Dict[str, Any]:
        return self._request_json("GET", f"/api/campaign/{campaign_id}/stats")

    def get_active_campaign_name(self, maps_base_url: str, campaign_id: str) -> str:
        base = (maps_base_url or "").strip().rstrip("/")
        if not base:
            return ""
        url = f"{base}/campaigns/active"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            ctx = ssl._create_unverified_context() if self.insecure else None
            with urllib.request.urlopen(req, timeout=self.timeout_s, context=ctx) as resp:
                body = resp.read().decode("utf-8", "ignore")
            data = json.loads(body) if body else {}
        except Exception:
            return ""

        for camp in data.get("campaigns", []) if isinstance(data, dict) else []:
            cid = str(camp.get("id", "")).strip()
            if cid and cid == str(campaign_id).strip():
                return str(camp.get("name", "")).strip()
        return ""


class Heartbeater:
    def __init__(self, interval_s: float, beat_fn: Callable[[], None], logger: logging.Logger) -> None:
        self.interval_s = max(1.0, float(interval_s))
        self.beat_fn = beat_fn
        self.logger = logger
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return

        def _loop() -> None:
            while not self._stop.wait(self.interval_s):
                try:
                    self.beat_fn()
                except Exception as exc:
                    self.logger.warning("Heartbeat failed: %s", exc)

        self._thread = threading.Thread(target=_loop, name="pipeline-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


@dataclass
class PipelineContext:
    maps_base_url: str
    email_base_url: str
    maps_cfg: Dict[str, Any]
    email_cfg: Dict[str, Any]
    pipeline_cfg: Dict[str, Any]
    base_dir: str


def _resolve_email_script(scraper: str, base_dir: str, logger: logging.Logger) -> tuple[str, str]:
    engine = (scraper or "camoufox").strip().lower()
    script = "email_scraper.py"
    if engine == "scrapy":
        script = "email_scraper_scrapy.py"
    script_path = os.path.join(base_dir, script)
    if not os.path.exists(script_path):
        logger.warning("Email scraper '%s' not found at %s; falling back to Camoufox engine.", engine, script_path)
        script_path = os.path.join(base_dir, "email_scraper.py")
        engine = "camoufox"
    return script_path, engine


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _normalize_email_policy(value: Any, default: str) -> str:
    policy = str(value or "").strip().lower()
    if policy in {"business_only", "business_or_public", "any_valid"}:
        return policy
    return default


def _estimate_batches(
    stats: Dict[str, Any],
    batch_size: int,
    cap: int = 0,
    multiplier: float = 1.0,
) -> int:
    if "contacts_without_email" not in stats:
        # If stats are temporarily unavailable, run a small probing pass instead of skipping.
        return 1 if cap <= 0 else max(1, min(1, int(cap)))
    pending = _coerce_int(stats.get("contacts_without_email", 0), 0)
    if pending <= 0:
        return 0
    ratio = pending / max(1, int(batch_size))
    mult = max(0.1, float(multiplier or 1.0))
    batches = int(math.ceil(ratio * mult))
    if cap > 0:
        batches = min(batches, int(cap))
    return max(1, batches)


def _run_subprocess_with_stop(
    args: list[str],
    cwd: str,
    logger: logging.Logger,
    should_stop: Callable[[], bool],
) -> int:
    proc = subprocess.Popen(args, cwd=cwd)
    try:
        while proc.poll() is None:
            if should_stop():
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
                return -15
            time.sleep(1.0)
    finally:
        if proc.poll() is None:
            proc.terminate()
    return int(proc.returncode or 0)


def _configure_maps_runtime(ctx: PipelineContext, logger: logging.Logger, campaign_scrape_mode: str = "") -> None:
    import maps_scraper

    max_concurrent = _coerce_int(ctx.maps_cfg.get("max_concurrent", 1), 1)
    detail_workers = _coerce_int(ctx.maps_cfg.get("detail_workers", 1), 1)
    configured_mode = str(ctx.maps_cfg.get("scrape_mode", "fast"))
    requested_mode = str(campaign_scrape_mode or "").strip().lower()
    scrape_mode = requested_mode if requested_mode in {"fast", "slow"} else configured_mode
    show_browser = bool(ctx.maps_cfg.get("show_browser", False))
    slow_min = _coerce_float(ctx.maps_cfg.get("slow_place_pause_min_s", 0.8), 0.8)
    slow_max = _coerce_float(ctx.maps_cfg.get("slow_place_pause_max_s", 1.8), 1.8)
    scroll_min = _coerce_float(ctx.maps_cfg.get("scroll_pause_min_s", 0.8), 0.8)
    scroll_max = _coerce_float(ctx.maps_cfg.get("scroll_pause_max_s", 0.8), 0.8)

    maps_scraper.MAX_CONCURRENT = max_concurrent
    maps_scraper.DEFAULT_DETAIL_WORKERS = maps_scraper.normalize_detail_workers(detail_workers)
    maps_scraper.DEFAULT_SCRAPE_MODE = maps_scraper.normalize_scrape_mode(scrape_mode)
    maps_scraper.DEFAULT_SHOW_BROWSER = maps_scraper.normalize_show_browser(show_browser)
    (
        maps_scraper.DEFAULT_SLOW_PLACE_PAUSE_MIN_S,
        maps_scraper.DEFAULT_SLOW_PLACE_PAUSE_MAX_S,
    ) = maps_scraper.normalize_pause_range(slow_min, slow_max)
    (
        maps_scraper.DEFAULT_SCROLL_PAUSE_MIN_S,
        maps_scraper.DEFAULT_SCROLL_PAUSE_MAX_S,
    ) = maps_scraper.normalize_scroll_pause_range(scroll_min, scroll_max)
    maps_scraper.DEFAULT_PROXY_URL = maps_scraper.normalize_maps_proxy_url(ctx.maps_cfg.get("proxy_url", ""))

    logger.info(
        "Maps runtime configured: mode=%s show_browser=%s workers=%s proxy=%s",
        maps_scraper.DEFAULT_SCRAPE_MODE,
        maps_scraper.DEFAULT_SHOW_BROWSER,
        maps_scraper.DEFAULT_DETAIL_WORKERS,
        "on" if maps_scraper.DEFAULT_PROXY_URL else "off",
    )


def _run_maps_stage(
    campaign_id: str,
    campaign_name: str,
    campaign_scrape_mode: str,
    ctx: PipelineContext,
    api: PipelineApiClient,
    logger: logging.Logger,
    should_stop: Callable[[], bool],
) -> None:
    import maps_scraper
    from maps_scraper import Campaign, CampaignProcessor, HttpClient, LeadsApiClient

    _configure_maps_runtime(ctx, logger, campaign_scrape_mode=campaign_scrape_mode)

    if not campaign_name:
        campaign_name = api.get_active_campaign_name(ctx.maps_base_url, campaign_id)
    if not campaign_name:
        campaign_name = str(campaign_id)

    class _PipelineMapsApiClient(LeadsApiClient):
        def complete_campaign(self, campaign_id: str) -> None:  # type: ignore[override]
            logger.info(
                "Skipping legacy complete_campaign(%s) during maps stage; finalize stage handles completion.",
                campaign_id,
            )

        def get_requests_for_campaign_name(self, campaign_name: str, include_inuse: bool = True):  # type: ignore[override]
            return super().get_requests_for_campaign_name(campaign_name, include_inuse=include_inuse)

    maps_api = _PipelineMapsApiClient(ctx.maps_base_url, HttpClient())
    processor = CampaignProcessor(
        maps_api,
        batch_size=_coerce_int(ctx.maps_cfg.get("batch_size", 20), 20),
        csv_dir=str(ctx.maps_cfg.get("csv_dir", "") or "") or None,
        scrape_mode=maps_scraper.DEFAULT_SCRAPE_MODE,
        show_browser=maps_scraper.DEFAULT_SHOW_BROWSER,
        slow_place_pause_min_s=maps_scraper.DEFAULT_SLOW_PLACE_PAUSE_MIN_S,
        slow_place_pause_max_s=maps_scraper.DEFAULT_SLOW_PLACE_PAUSE_MAX_S,
        scroll_pause_min_s=maps_scraper.DEFAULT_SCROLL_PAUSE_MIN_S,
        scroll_pause_max_s=maps_scraper.DEFAULT_SCROLL_PAUSE_MAX_S,
        detail_workers=maps_scraper.DEFAULT_DETAIL_WORKERS,
        proxy_url=maps_scraper.DEFAULT_PROXY_URL,
    )

    logger.info("Pipeline maps stage: processing campaign %s (%s)", campaign_id, campaign_name)
    if should_stop():
        raise RuntimeError("Stopping before maps stage")
    watcher_done = threading.Event()

    def _watch_stop() -> None:
        while not watcher_done.wait(1.0):
            if should_stop():
                processor.stop()
                return

    watcher = threading.Thread(target=_watch_stop, name="maps-stage-stop-watcher", daemon=True)
    watcher.start()
    try:
        processor.process_campaign(Campaign(id=str(campaign_id), name=str(campaign_name)))
    finally:
        watcher_done.set()
        watcher.join(timeout=2.0)


def _run_cleanup_stage(campaign_id: str, api: PipelineApiClient, logger: logging.Logger) -> None:
    resp = api.cleanup_contacts(campaign_id)
    if not resp or not bool(resp.get("_ok", True)):
        raise RuntimeError("Cleanup endpoint returned empty response")
    logger.info("Cleanup response for campaign %s: %s", campaign_id, resp)


def _build_email_args(
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
    email_policy: str,
) -> list[str]:
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
        "--email-policy",
        str(email_policy),
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
    return args


def _run_email_stage(
    stage: str,
    campaign_id: str,
    ctx: PipelineContext,
    api: PipelineApiClient,
    logger: logging.Logger,
    should_stop: Callable[[], bool],
) -> None:
    email_cfg = ctx.email_cfg
    pipeline_cfg = ctx.pipeline_cfg

    stats = api.get_stats(campaign_id)
    batch = max(1, _coerce_int(email_cfg.get("batch", 10), 10))

    if stage == "email_fast":
        scraper_key = str(pipeline_cfg.get("fast_scraper") or email_cfg.get("fast_scraper") or "scrapy").strip().lower()
        email_policy = _normalize_email_policy(pipeline_cfg.get("fast_email_policy", "business_only"), "business_only")
        max_cap = _coerce_int(pipeline_cfg.get("fast_max_batches_cap", 0), 0)
        fast_multiplier = _coerce_float(pipeline_cfg.get("fast_batches_multiplier", 1.1), 1.1)
        max_batches = _estimate_batches(stats, batch, max_cap, multiplier=fast_multiplier)
        if max_batches <= 0:
            logger.info("Email fast stage skipped for campaign %s: no contacts_without_email.", campaign_id)
            return
        max_batches_facebook = 0
        facebook = False
        concurrency = max(1, _coerce_int(pipeline_cfg.get("fast_concurrency", email_cfg.get("concurrency", 3)), 3))
    else:
        scraper_key = str(pipeline_cfg.get("fallback_scraper") or email_cfg.get("fallback_scraper") or "camoufox").strip().lower()
        email_policy = _normalize_email_policy(pipeline_cfg.get("fallback_email_policy", "business_or_public"), "business_or_public")
        max_regular_cap = _coerce_int(pipeline_cfg.get("fallback_max_batches", 0), 0)
        max_fb_cap = _coerce_int(pipeline_cfg.get("fallback_max_batches_facebook", 0), 0)
        fallback_multiplier = _coerce_float(pipeline_cfg.get("fallback_batches_multiplier", 1.0), 1.0)
        fallback_fb_multiplier = _coerce_float(pipeline_cfg.get("fallback_facebook_batches_multiplier", 1.0), 1.0)
        max_batches = _estimate_batches(stats, batch, max_regular_cap, multiplier=fallback_multiplier)
        max_batches_facebook = _estimate_batches(stats, batch, max_fb_cap, multiplier=fallback_fb_multiplier)
        if max_batches <= 0 and max_batches_facebook <= 0:
            logger.info("Email fallback stage skipped for campaign %s: no contacts_without_email.", campaign_id)
            return
        facebook = True
        concurrency = max(1, _coerce_int(pipeline_cfg.get("fallback_concurrency", 1), 1))

    script_path, resolved_engine = _resolve_email_script(scraper_key, ctx.base_dir, logger)

    args = _build_email_args(
        python_exe=os.environ.get("PYTHON", "python3"),
        script_path=script_path,
        campaign_id=campaign_id,
        base_url=ctx.email_base_url,
        batch=batch,
        max_batches=max_batches,
        max_batches_facebook=max_batches_facebook,
        concurrency=concurrency,
        timeout_s=_coerce_float(email_cfg.get("timeout_s", 8.0), 8.0),
        domain_timeout_s=_coerce_float(email_cfg.get("domain_timeout_s", 60.0), 60.0),
        links=max(1, _coerce_int(email_cfg.get("links", 5), 5)),
        min_domain_letters=max(1, _coerce_int(email_cfg.get("min_domain_letters", 2), 2)),
        facebook=facebook,
        facebook_engine=str(email_cfg.get("facebook_engine", "camoufox") or "camoufox"),
        facebook_proxy_url=str(email_cfg.get("facebook_proxy_url", "") or ""),
        same_domain_only=bool(email_cfg.get("same_domain_only", True)),
        email_policy=email_policy,
    )

    logger.info(
        "Pipeline %s stage: campaign=%s scraper=%s max_batches=%s max_batches_facebook=%s email_policy=%s",
        stage,
        campaign_id,
        resolved_engine,
        max_batches,
        max_batches_facebook,
        email_policy,
    )

    code = _run_subprocess_with_stop(args, cwd=ctx.base_dir, logger=logger, should_stop=should_stop)
    if code != 0:
        raise RuntimeError(f"Email scraper failed for stage {stage} with code={code}")


def _run_finalize_stage(campaign_id: str, ctx: PipelineContext, api: PipelineApiClient, logger: logging.Logger) -> None:
    from maps_scraper import HttpClient, LeadsApiClient

    maps_api = LeadsApiClient(ctx.maps_base_url, HttpClient())
    maps_api.complete_campaign(str(campaign_id))
    stats = api.get_stats(campaign_id)
    logger.info("Finalize stage stats for campaign %s: %s", campaign_id, stats)


def run_pipeline_worker(
    logger: logging.Logger,
    ctx: PipelineContext,
    worker_id: str,
    actor: str,
    claim_interval_s: float,
    lease_seconds: int,
    heartbeat_interval_s: float,
    should_stop: Callable[[], bool],
) -> None:
    pipeline_cfg = ctx.pipeline_cfg if isinstance(ctx.pipeline_cfg, dict) else {}
    api = PipelineApiClient(pipeline_cfg.get("base_url") or ctx.email_base_url, logger)
    machine_id = str(pipeline_cfg.get("machine_id") or "").strip() or default_machine_id()
    no_claim_counter = 0
    auto_start_enabled = bool(pipeline_cfg.get("auto_start_on_run_not_started", True))
    auto_start_cooldown_s = max(5.0, _coerce_float(pipeline_cfg.get("auto_start_cooldown_s", 30), 30.0))
    last_auto_start_ts = 0.0

    def _clean_scalar(value: Any) -> str:
        s = str(value or "").strip()
        if s.lower() in {"none", "null", "nil", "n/a"}:
            return ""
        return s

    logger.info(
        "Pipeline worker starting: worker_id=%s machine_id=%s actor=%s lease_seconds=%s claim_interval_s=%s",
        worker_id,
        machine_id,
        actor,
        lease_seconds,
        claim_interval_s,
    )

    def _auto_start_from_active() -> None:
        nonlocal last_auto_start_ts
        now = time.time()
        if (now - last_auto_start_ts) < auto_start_cooldown_s:
            return
        last_auto_start_ts = now

        active = api.list_active_campaigns()
        if not active:
            logger.info("Auto-start check: no active campaigns found.")
            return

        for camp in active:
            campaign_id = _clean_scalar(camp.get("id", ""))
            if not campaign_id:
                continue
            resp = api.start_pipeline(campaign_id=campaign_id, actor=actor, retry=False)
            if not resp or not bool(resp.get("_ok", True)):
                logger.warning("Auto-start pipeline failed for campaign=%s (empty response).", campaign_id)
                continue
            logger.info(
                "Auto-start pipeline: campaign=%s status=%s idempotent=%s run_id=%s stage=%s",
                campaign_id,
                str(resp.get("status", "none")),
                str(resp.get("idempotent", "none")),
                _clean_scalar(resp.get("run_id", "")) or "none",
                _clean_scalar(resp.get("current_stage", "")) or "none",
            )

    while not should_stop():
        claim = api.claim(worker_id=worker_id, machine_id=machine_id, actor=actor, lease_seconds=lease_seconds)
        run_obj = claim.get("run", {}) if isinstance(claim.get("run"), dict) else {}
        claim_run_id = (
            _clean_scalar(claim.get("run_id", ""))
            or _clean_scalar(run_obj.get("run_id", ""))
            or _clean_scalar(run_obj.get("id", ""))
        )
        claim_stage = (
            _clean_scalar(claim.get("stage", ""))
            or _clean_scalar(claim.get("current_stage", ""))
            or _clean_scalar(run_obj.get("stage", ""))
            or _clean_scalar(run_obj.get("current_stage", ""))
        )
        claim_campaign_id = (
            _clean_scalar(claim.get("campaign_id", ""))
            or _clean_scalar(run_obj.get("campaign_id", ""))
            or _clean_scalar(run_obj.get("campaign", ""))
        )
        claimed_flag = str(claim.get("claimed", "")).strip().lower()
        status_flag = str(claim.get("status", "")).strip().lower()
        identifiers_present = bool(claim_run_id and claim_campaign_id and claim_stage)
        explicit_claim_true = (
            bool(claim.get("claimed"))
            or claimed_flag in {"true", "1", "yes"}
            or status_flag == "claimed"
        )
        claimed = (
            (explicit_claim_true and identifiers_present)
            or ((not explicit_claim_true) and identifiers_present)
        )
        if explicit_claim_true and not identifiers_present:
            logger.warning(
                "Ignoring malformed claim response: explicit claim without identifiers (run_id=%s campaign_id=%s stage=%s keys=%s)",
                claim_run_id or "missing",
                claim_campaign_id or "missing",
                claim_stage or "missing",
                ",".join(sorted(claim.keys())),
            )
            claimed = False
        if not claimed:
            no_claim_counter += 1
            reason = str(claim.get("reason", "")).strip().lower()
            if auto_start_enabled and reason in {"run_not_started", "all_leased"}:
                _auto_start_from_active()
            if claim:
                # Emit every poll while idle so operator sees worker is alive and polling.
                reason = str(claim.get("reason", "")).strip() or "none"
                current_stage = str(claim.get("current_stage", "")).strip() or "none"
                pipeline_status = str(claim.get("pipeline_status", "")).strip() or "none"
                logger.info(
                    "No claimable run (reason=%s pipeline_status=%s current_stage=%s claimed=%s run_id=%s campaign_id=%s stage=%s poll=%s keys=%s).",
                    reason,
                    pipeline_status,
                    current_stage,
                    str(claim.get("claimed", "none")),
                    _clean_scalar(claim.get("run_id", "")) or "none",
                    _clean_scalar(claim.get("campaign_id", "")) or "none",
                    _clean_scalar(claim.get("stage", "")) or _clean_scalar(claim.get("current_stage", "")) or "none",
                    no_claim_counter,
                    ",".join(sorted(claim.keys())),
                )
            time.sleep(max(1.0, claim_interval_s))
            continue
        no_claim_counter = 0

        run_id = claim_run_id
        campaign_id = claim_campaign_id
        stage = claim_stage.strip().lower()
        campaign_name = (
            str(claim.get("campaign_name", "")).strip()
            or str(run_obj.get("campaign_name", "")).strip()
            or str(run_obj.get("name", "")).strip()
        )
        maps_scrape_mode = (
            _clean_scalar(claim.get("maps_scrape_mode", ""))
            or _clean_scalar(run_obj.get("maps_scrape_mode", ""))
            or "slow"
        )

        if not run_id or not campaign_id or not stage:
            logger.error("Invalid claim payload: %s", claim)
            time.sleep(max(1.0, claim_interval_s))
            continue

        logger.info(
            "Claimed pipeline run=%s campaign=%s campaign_name=%s stage=%s machine_id=%s maps_mode=%s",
            run_id,
            campaign_id,
            campaign_name or "-",
            stage,
            machine_id,
            maps_scrape_mode,
        )

        lease_lost = threading.Event()

        def _heartbeat_once() -> Dict[str, Any]:
            response = api.heartbeat(
                run_id=run_id,
                worker_id=worker_id,
                machine_id=machine_id,
                stage=stage,
                lease_seconds=lease_seconds,
            )
            status_code = int(response.get("_status", 200) or 200) if isinstance(response, dict) else 0
            if status_code == 409:
                lease_lost.set()
                raise RuntimeError(f"Pipeline lease lost for run={run_id} stage={stage}")
            return response if isinstance(response, dict) else {}

        stop_signal = lambda: should_stop() or lease_lost.is_set()

        beat = Heartbeater(
            interval_s=heartbeat_interval_s,
            beat_fn=_heartbeat_once,
            logger=logger,
        )
        beat.start()

        try:
            _heartbeat_once()

            if stage == "maps_scrape":
                _run_maps_stage(campaign_id, campaign_name, maps_scrape_mode, ctx, api, logger, stop_signal)
            elif stage == "cleanup_contacts":
                _run_cleanup_stage(campaign_id, api, logger)
            elif stage == "email_fast":
                _run_email_stage(stage, campaign_id, ctx, api, logger, stop_signal)
            elif stage == "email_fallback":
                _run_email_stage(stage, campaign_id, ctx, api, logger, stop_signal)
            elif stage == "finalize":
                _run_finalize_stage(campaign_id, ctx, api, logger)
            else:
                raise RuntimeError(f"Unsupported pipeline stage: {stage}")

            if lease_lost.is_set():
                raise RuntimeError(f"Pipeline lease lost before stage completion for run={run_id} stage={stage}")

            resp = api.stage_complete(run_id=run_id, worker_id=worker_id, machine_id=machine_id, stage=stage)
            if not bool(resp.get("_ok", True)):
                raise RuntimeError(f"Stage completion failed for run={run_id} stage={stage}: {resp}")
            if int(resp.get("_status", 200) or 200) == 409:
                raise RuntimeError(f"Stage completion rejected due lease conflict for run={run_id} stage={stage}")
            logger.info("Stage complete acknowledged: run=%s stage=%s resp=%s", run_id, stage, resp)
        except Exception as exc:
            tb = traceback.format_exc(limit=3)
            logger.exception("Stage failed: run=%s campaign=%s stage=%s", run_id, campaign_id, stage)
            fail_resp = api.fail(
                run_id=run_id,
                worker_id=worker_id,
                machine_id=machine_id,
                stage=stage,
                error=str(exc),
                error_payload={"trace": tb},
            )
            logger.error("Stage failure sent: run=%s stage=%s resp=%s", run_id, stage, fail_resp)
        finally:
            beat.stop()

    logger.info("Pipeline worker stopping")


def default_machine_id() -> str:
    env_machine_id = str(os.environ.get("PIPELINE_MACHINE_ID", "")).strip()
    if env_machine_id:
        return env_machine_id
    host = socket.gethostname().split(".")[0]
    return f"machine-{host}"


def default_worker_id(prefix: str = "daemon") -> str:
    host = socket.gethostname().split(".")[0]
    pid = os.getpid()
    return f"{prefix}-{host}-{pid}"
