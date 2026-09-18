"""Contact-scoped source extraction for explicitly streaming pipeline runs.

The main API owns readiness. Claim returns committed source_email tasks as
{tasks: [{id, lease_token, contact: {id, campaign_id, domain, email, ...}}]}.
The campaign_id may instead be present on the task itself.

POST /api/streaming/source-tasks/claim takes worker_id, optional campaign_id
and limit=1. POST /{id}/heartbeat takes lease_token and returns {active: bool}.
POST /{id}/complete takes lease_token, status ('completed' or 'failed'), optional
email, error and result. Completion atomically writes the email and readiness;
omitting email also completes extraction. Leases last 180s with 30s heartbeats.
Inactive heartbeats or 409 revoke ownership. Failure or expiry can requeue work
with a new token; stale tokens must never write contacts.

Source-only and HTTP rows become ready on commit. Browser rows needing website
extraction stay gated until completion here. This module never calls global
cleanup or writes through the unleased email_update endpoint on the main API.

Deployment: scraper modules prepend Daemon/.deps to sys.path. Bundled macOS
wheels (for example lxml/etree.cpython-312-darwin.so) cannot run on Linux, even
inside a Python 3.11 environment. Rebuild those dependencies for the target
platform/interpreter and set PYTHON to that interpreter for scraper children.
Runtime imports and unit tests alone do not verify browser installation.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from uuid import uuid4

if __package__:
    from .pipeline_runtime import DaemonMetricSampler, PipelineApiClient, _coerce_int, _run_email_stage
else:
    from pipeline_runtime import DaemonMetricSampler, PipelineApiClient, _coerce_int, _run_email_stage


LEASE_SECONDS = 180
HEARTBEAT_SECONDS = 10
POLL_SECONDS = 1.0
SOURCE_TASKS_PATH = "/api/streaming/source-tasks"


def _ok(response):
    return (
        isinstance(response, dict) and bool(response)
        and response.get("_ok", True) is not False
        and response.get("accepted", True) is not False
        and response.get("active", True) is not False
        and 200 <= int(response.get("_status", 200)) < 300
        and not response.get("error")
    )


class ScopedContactApi:
    """Local compatibility API for one contact; scraper writes stay in memory."""

    def __init__(self, campaign_id, contact, should_stop):
        self.campaign_id = str(campaign_id)
        self.contact = dict(contact)
        self.should_stop = should_stop
        self.updates = []
        self.pulls = 0
        self.error = ""
        self._lock = threading.Lock()
        self._prefix = f"/{uuid4().hex}/api/campaign/{self.campaign_id}"
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.respond("GET")

            def do_POST(self):
                self.respond("POST")

            def respond(self, method):
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if size < 0 or size > 65536:
                        raise ValueError("Invalid scoped request size")
                    payload = json.loads(self.rfile.read(size)) if size else None
                    with owner._lock:
                        status, result = owner.handle(method, urlsplit(self.path).path, payload)
                except (ValueError, TypeError, AttributeError) as exc:
                    owner.error = str(exc)
                    status, result = 400, {"error": str(exc)}
                body = json.dumps(result).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_port}{self._prefix.split('/api/')[0]}"
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.05), name="scoped-contact-api", daemon=True,
        )

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_args):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def handle(self, method, path, payload):
        if self.should_stop():
            return 409, {"error": "Source task stopped or lease lost"}
        if method == "GET" and path == self._prefix + "/stats":
            return 200, {"contacts_without_email": 0 if self.contact.get("email") else 1}
        if method == "GET" and path == self._prefix + "/nomail":
            self.pulls += 1
            contacts = [] if self.contact.get("email") else [dict(self.contact)]
            return 200, {"contacts": contacts, "count": len(contacts)}
        if method == "POST" and path == self._prefix + "/email_update":
            updates = payload.get("contacts", [payload])
            if not isinstance(updates, list) or not updates:
                raise ValueError("Empty scoped email update")
            for update in updates:
                if str(update.get("id")) != str(self.contact["id"]) or not str(update.get("email") or "").strip():
                    raise ValueError("Email update outside claimed contact or missing email")
            self.contact["email"] = str(updates[-1]["email"]).strip()
            self.updates = [{"id": str(self.contact["id"]), "email": self.contact["email"]}]
            return 200, {"status": "buffered", "updated_count": len(updates)}
        self.error = "Scraper requested an endpoint outside its contact scope"
        return 404, {"error": self.error}


def process_contact(task, ctx, logger, should_stop):
    """Run the existing fast and browser/Facebook passes as a one-row batch."""
    contact = task["contact"]
    if should_stop():
        raise RuntimeError("Source email task stopped")
    if contact.get("email") or not any(str(contact.get(key) or "").strip() for key in ("domain", "website", "url")):
        return []
    with ScopedContactApi(task["campaign_id"], contact, should_stop) as scoped:
        scoped_ctx = replace(
            ctx,
            email_base_url=scoped.base_url,
            email_cfg={**ctx.email_cfg, "batch": 1},
            pipeline_cfg={
                **ctx.pipeline_cfg,
                "fast_max_batches_cap": 1, "fallback_max_batches": 1,
                "fallback_max_batches_facebook": 1, "fast_concurrency": 1, "fallback_concurrency": 1,
            },
        )
        local_api = PipelineApiClient(scoped.base_url, logger)
        for stage in ("email_fast", "email_fallback"):
            if should_stop():
                raise RuntimeError("Source email task stopped")
            if scoped.contact.get("email"):
                break
            pulls_before = scoped.pulls
            _run_email_stage(stage, str(task["campaign_id"]), scoped_ctx, local_api, logger, should_stop, scoped=True)
            if scoped.error or scoped.pulls == pulls_before:
                raise RuntimeError(scoped.error or f"{stage} exited without fetching its claimed contact")
        if should_stop():
            raise RuntimeError("Source email task stopped")
        return scoped.updates


class TaskLease:
    def __init__(self, api, task, should_stop, logger, identity=None):
        self.api, self.task = api, task
        self.should_stop, self.logger = should_stop, logger
        self.identity = dict(identity or {})
        self.lost = threading.Event()
        self.controlled_stop = False
        self._done = threading.Event()
        self.deadline = time.monotonic() + LEASE_SECONDS
        self._thread = threading.Thread(target=self._heartbeat, name="source-task-heartbeat", daemon=True)

    def stopped(self):
        if time.monotonic() >= self.deadline:
            self.lost.set()
        return self.should_stop() or self.lost.is_set()

    def action(self, action, **payload):
        response = self.api._request_json(
            "POST", f"{SOURCE_TASKS_PATH}/{self.task['id']}/{action}",
            {**self.identity, "lease_token": self.task["lease_token"], **payload}, timeout_s=10.0,
        )
        if str(response.get("daemon_state") or "").strip().lower() == "stopped":
            self.controlled_stop = True
            self.lost.set()
        if response.get("active") is False or int(response.get("_status", 200)) in {403, 404, 409, 410}:
            self.lost.set()
        return response

    def renew(self):
        started = time.monotonic()
        response = self.action("heartbeat")
        if _ok(response) and response.get("active") is True:
            self.deadline = started + LEASE_SECONDS
            return True
        return False

    def _heartbeat(self):
        while not self._done.wait(HEARTBEAT_SECONDS):
            if self.stopped():
                return
            try:
                self.renew()
            except Exception:
                self.logger.exception("Source task heartbeat failed: task=%s", self.task["id"])

    def start(self):
        if self.stopped() or not self.renew():
            raise RuntimeError("Cannot confirm source task lease")
        self._thread.start()

    def close(self):
        self._done.set()
        if self._thread.ident is not None:
            self._thread.join()


class SourceEmailWorker:
    def __init__(self, api, ctx, logger, worker_id, should_stop, machine_id=None, worker_kind="worker"):
        self.api, self.ctx, self.logger = api, ctx, logger
        self.managed = machine_id is not None
        self.worker_id = f"{worker_id}-source-email" if self.managed else worker_id
        self.machine_id = str(machine_id or worker_id).strip()
        self.worker_kind = worker_kind
        self.metrics = DaemonMetricSampler("source_email")
        self.should_stop = should_stop
        self.concurrency = min(8, max(1, _coerce_int(ctx.pipeline_cfg.get("streaming_concurrency", 2), 2)))

    def _identity(self):
        if not self.managed:
            return {"worker_id": self.worker_id}
        return {
            "worker_id": self.worker_id,
            "machine_id": self.machine_id,
            "worker_metadata": self.metrics.snapshot(),
        }

    def _lease_identity(self):
        return self._identity() if self.managed else {}

    def _process_task(self, task, should_stop):
        lease = TaskLease(self.api, task, should_stop, self.logger, self._lease_identity())
        completing = False
        try:
            lease.start()
            updates = process_contact(task, self.ctx, self.logger, lease.stopped)
            if lease.stopped():
                return
            completing = True
            payload = {"status": "completed", "result": {"email_found": bool(updates or task["contact"].get("email"))}}
            if updates:
                payload["email"] = updates[-1]["email"]
            # Retry the same token: a timed-out completion may already be committed.
            for _ in range(3):
                if lease.stopped():
                    return
                response = lease.action("complete", **payload)
                if _ok(response):
                    self.logger.info("Source email complete: task=%s contact=%s", task["id"], task["contact"]["id"])
                    return
                if not lease.stopped():
                    time.sleep(0.2)
            raise RuntimeError("Source task completion was not acknowledged")
        except Exception as exc:
            self.logger.warning("Source email task %s failed: %s", task["id"], exc)
            if not completing and not lease.stopped():
                lease.action("complete", status="failed", error=str(exc)[:2000])
        finally:
            if lease.controlled_stop:
                try:
                    response = lease.action("release")
                    self.logger.info("Daemon stop released source task=%s response=%s", task["id"], response)
                except Exception:
                    self.logger.exception("Could not release stopped source task=%s", task["id"])
            lease.close()

    @staticmethod
    def _validate_tasks(tasks, campaign_id, limit, active):
        if not isinstance(tasks, list) or len(tasks) > limit:
            raise RuntimeError("Source task claim exceeded capacity or omitted tasks")
        seen = set(active)
        for task in tasks:
            if not isinstance(task, dict) or any(not task.get(key) for key in ("id", "lease_token")):
                raise RuntimeError("Source task claim missing identity or lease token")
            if task.get("step_type", "source_email") != "source_email":
                raise RuntimeError("Source task claim has unexpected step type")
            contact = task.get("contact")
            if not isinstance(contact, dict) or not str(contact.get("id", "")).isdigit():
                raise RuntimeError("Source task claim missing an explicit contact")
            task["campaign_id"] = task.get("campaign_id") or contact.get("campaign_id")
            if not str(task["campaign_id"]).isdigit():
                raise RuntimeError("Source task claim missing campaign_id")
            if campaign_id is not None and str(task["campaign_id"]) != str(campaign_id):
                raise RuntimeError("Source task claim escaped requested campaign")
            if contact.get("campaign_id") is not None and str(contact["campaign_id"]) != str(task["campaign_id"]):
                raise RuntimeError("Source task contact belongs to another campaign")
            key = (str(task["campaign_id"]), str(contact["id"]))
            if key in seen:
                raise RuntimeError("Source task claim repeated a contact already in flight")
            seen.add(key)

    def run(self, campaign_id=None):
        abort = threading.Event()
        stop = lambda: self.should_stop() or abort.is_set()
        futures = {}
        next_poll = 0.0
        with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="source-email") as pool:
            try:
                while not stop():
                    for future in list(futures):
                        if future.done():
                            del futures[future]
                            try:
                                future.result()
                            except Exception:
                                self.logger.exception("Source task failed; its lease remains recoverable.")
                            next_poll = 0.0
                    if len(futures) < self.concurrency and time.monotonic() >= next_poll:
                        payload = {**self._identity(), "limit": 1}
                        if campaign_id is not None:
                            payload["campaign_id"] = str(campaign_id)
                        try:
                            response = self.api._request_json("POST", SOURCE_TASKS_PATH + "/claim", payload)
                            if int(response.get("_status", 200)) in {404, 405}:
                                next_poll = time.monotonic() + 60.0
                            else:
                                if not _ok(response):
                                    raise RuntimeError("Source task claim failed")
                                tasks = response.get("tasks")
                                self._validate_tasks(tasks, campaign_id, 1, futures.values())
                                for task in tasks:
                                    future = pool.submit(self._process_task, task, stop)
                                    futures[future] = (str(task["campaign_id"]), str(task["contact"]["id"]))
                                next_poll = time.monotonic() + (0.0 if tasks else POLL_SECONDS)
                        except Exception:
                            self.logger.exception("Source task claim failed; retrying without advancing readiness.")
                            next_poll = time.monotonic() + 5.0
                    if futures:
                        wait(futures, timeout=0.1, return_when=FIRST_COMPLETED)
                    else:
                        abort.wait(0.1)
            finally:
                abort.set()
