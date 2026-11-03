"""A local stand-in for the Gemini Batch API (Gemini Developer API), so the agent and the UI can
be tested end to end WITHOUT any real LLM call and without any cost.

It speaks the same HTTP endpoints the google-genai SDK uses, so no application code has to
change: set GEMINI_EMULATOR_URL (see agent.py / sectionizer/batch_sync.py) and the real SDK talks to
this server instead of Google. Standard library only; no API key needed.

    python gemini_batch_emulator.py                       # http://127.0.0.1:8099, jobs take 20 s
    python gemini_batch_emulator.py --delay 5 --scenario mixed

What it does
  * file upload (resumable protocol), batches.create, batches.get / list / cancel / delete,
    files.get / delete, files.download
  * validates the input JSONL the way the real service does and answers 400 INVALID_ARGUMENT
  * a job is PENDING, then RUNNING, then finishes after --delay seconds
  * writes one fake result per request, in the real result-line format (including fields such
    as usageMetadata.serviceTier and thoughtSignature). Category ids are taken from the enum in the
    request's own response schema, exactly as a model constrained by that schema would.
    Titles and text are prefixed "[EMULATED]".

Scenarios (set with --scenario, or at run time with POST /_emulator/scenario)
  success     every request succeeds
  mixed       requests cycle through: ok, blocked, invalid output, request error
  job_failed  the whole job ends FAILED

Control endpoints
  GET  /_emulator/state                  jobs and files
  POST /_emulator/scenario               {"scenario": "mixed"}  (applies to jobs created afterwards)
  POST /_emulator/jobs/<id>/finish       finish a job now instead of waiting for the delay

Not emulated: inline requests, embeddings, Vertex AI, authentication, rate limits.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SCENARIOS = ("success", "mixed", "job_failed")
TYPE_BATCH = "type.googleapis.com/google.ai.generativelanguage.v1main.GenerateContentBatch"
SAFETY = [{"category": c, "probability": "NEGLIGIBLE"} for c in (
    "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")]


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class ApiError(Exception):
    def __init__(self, code: int, status: str, message: str):
        self.code, self.status, self.message = code, status, message


# ── fake model output ────────────────────────────────────────────────────────

def _get(d: dict, *names):
    for n in names:
        if d.get(n) is not None:
            return d[n]
    return None


def validate_input(data: bytes) -> list[dict]:
    """The checks the real service applies to a batch input file."""
    lines = []
    for number, raw in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            raise ApiError(400, "INVALID_ARGUMENT", f"Invalid JSON on line {number} of the input file.")
        request = obj.get("request") if isinstance(obj, dict) else None
        if not isinstance(request, dict) or not request.get("contents"):
            raise ApiError(400, "INVALID_ARGUMENT", f"Line {number}: 'request.contents' is required.")
        lines.append(obj)
    if not lines:
        raise ApiError(400, "INVALID_ARGUMENT", "The input file contains no requests.")
    keys = [str(l.get("key", "")) for l in lines if l.get("key") is not None]
    if len(keys) != len(set(keys)):
        raise ApiError(400, "INVALID_ARGUMENT", "Duplicate keys in the input file.")
    return lines


def fake_line(obj: dict, index: int, scenario: str) -> dict:
    key = obj.get("key", f"request-{index}")
    req = obj["request"]
    cfg = _get(req, "generationConfig", "generation_config") or {}
    schema = _get(cfg, "responseJsonSchema", "response_json_schema") or {}
    enum = (((schema.get("properties") or {}).get("category_ids") or {}).get("items") or {}).get("enum") or []
    system = _get(req, "systemInstruction", "system_instruction") or {}
    sys_text = "".join(p.get("text", "") for p in system.get("parts", []))
    user = "".join(p.get("text", "") for c in req["contents"] for p in c.get("parts", []))
    h = int(hashlib.sha256(str(key).encode()).hexdigest(), 16)
    # about chars/4, 0-8% lower, so estimate and actual differ the way they do for real
    prompt_tokens = max(1, int((len(sys_text) + len(user)) / 4 * (0.92 + (h % 9) / 100)))

    kind = "ok" if scenario != "mixed" else ("ok", "blocked", "invalid", "error")[index % 4]
    if kind == "error":
        return {"key": key, "error": {"code": 500, "message": "Internal error (emulated)"}}
    if kind == "blocked":
        return {"key": key, "response": {
            "promptFeedback": {"blockReason": "SAFETY"},
            "usageMetadata": {"promptTokenCount": prompt_tokens, "totalTokenCount": prompt_tokens}}}

    title = re.search(r"<title>(.*?)</title>", user, re.S)
    body = re.search(r"<text>(.*?)</text>", user, re.S)
    text = " ".join((body.group(1) if body else user).split())
    headline = " ".join((title.group(1) if title else " ".join(text.split()[:9])).split())[:90]
    ids = []
    if enum:
        ids.append(enum[(h >> 8) % len(enum)])
        if len(enum) > 1 and h % 3 == 0:
            ids.append(enum[((h >> 8) + 1) % len(enum)])
    payload = json.dumps({
        "category_ids": ids,
        "content_type": "opinion" if "views expressed" in text.lower() else ("news", "analysis", "news")[h % 3],
        "confidence": round(0.55 + (h % 40) / 100, 2),
        "new_title": f"[EMULATED] {headline}",
        "new_text": "[EMULATED] " + " ".join(text.split()[:60]),
    })
    thoughts = 300 + h % 500
    if kind == "invalid":
        payload, finish, cand = payload[: len(payload) // 2], "MAX_TOKENS", 2000
    else:
        finish, cand = "STOP", max(1, len(payload) // 4)
    return {"key": key, "response": {
        "candidates": [{"content": {"parts": [{"text": payload, "thoughtSignature": "RW11bGF0b3I="}], "role": "model"},
                        "finishReason": finish, "index": 0, "safetyRatings": SAFETY}],
        "usageMetadata": {
            "promptTokenCount": prompt_tokens, "candidatesTokenCount": cand,
            "totalTokenCount": prompt_tokens + cand + thoughts,
            "promptTokensDetails": [{"modality": "TEXT", "tokenCount": prompt_tokens}],
            "thoughtsTokenCount": thoughts, "serviceTier": "SERVICE_TIER_STANDARD"},
        "modelVersion": "gemini-emulator", "responseId": secrets.token_hex(11)}}


# ── state ────────────────────────────────────────────────────────────────────

class Store:
    def __init__(self, root: Path, delay: float, scenario: str):
        self.root, self.delay, self.scenario = root, delay, scenario
        (root / "files").mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.uploads: dict[str, dict] = {}
        self.files: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        state = root / "state.json"
        if state.exists():
            saved = json.loads(state.read_text())
            self.files, self.jobs = saved.get("files", {}), saved.get("jobs", {})

    def save(self):
        (self.root / "state.json").write_text(json.dumps({"files": self.files, "jobs": self.jobs}, indent=1))

    def path(self, file_id: str) -> Path:
        return self.root / "files" / f"{file_id}.bin"

    def file_json(self, file_id: str, base: str) -> dict:
        m = self.files[file_id]
        return {"name": f"files/{file_id}", "displayName": m.get("display_name"), "mimeType": m.get("mime_type"),
                "sizeBytes": str(m["size"]), "createTime": m["created"], "updateTime": m["created"],
                "expirationTime": iso(time.time() + 48 * 3600), "sha256Hash": m["sha256"],
                "uri": f"{base}/v1beta/files/{file_id}", "state": "ACTIVE", "source": "UPLOADED"}

    # -- jobs
    def state_of(self, job: dict) -> str:
        if job.get("cancelled"):
            return "BATCH_STATE_CANCELLED"
        elapsed = time.time() - job["created"]
        if elapsed < job["delay"] * 0.25:
            return "BATCH_STATE_PENDING"
        if elapsed < job["delay"]:
            return "BATCH_STATE_RUNNING"
        return "BATCH_STATE_FAILED" if job["scenario"] == "job_failed" else "BATCH_STATE_SUCCEEDED"

    def settle(self, job_id: str) -> dict:
        """Create the results file the first time a job is seen finished."""
        job = self.jobs[job_id]
        state = self.state_of(job)
        if state == "BATCH_STATE_SUCCEEDED" and not job.get("output_file"):
            lines = validate_input(self.path(job["src_file"]).read_bytes())
            out = "\n".join(json.dumps(fake_line(l, i, job["scenario"])) for i, l in enumerate(lines)) + "\n"
            file_id = f"emu-out-{secrets.token_hex(5)}"
            self.path(file_id).write_bytes(out.encode())
            self.files[file_id] = {"display_name": f"{job_id}-results.jsonl", "mime_type": "application/jsonl",
                                   "size": len(out), "created": iso(time.time()), "sha256": hashlib.sha256(out.encode()).hexdigest()}
            job["output_file"] = file_id
            job["request_count"] = len(lines)
        if state in ("BATCH_STATE_SUCCEEDED", "BATCH_STATE_FAILED") and not job.get("end"):
            job["end"] = job["created"] + job["delay"]
        self.save()
        return job

    def operation(self, job_id: str) -> dict:
        job = self.settle(job_id)
        state = self.state_of(job)
        name = f"batches/{job_id}"
        meta = {"@type": TYPE_BATCH, "model": f"models/{job['model']}", "displayName": job["display_name"],
                "createTime": iso(job["created"]), "updateTime": iso(time.time()), "state": state, "name": name}
        op = {"name": name, "metadata": meta}
        if state in ("BATCH_STATE_SUCCEEDED", "BATCH_STATE_FAILED", "BATCH_STATE_CANCELLED"):
            meta["endTime"] = iso(job.get("end", time.time()))
            op["done"] = True
        if state == "BATCH_STATE_SUCCEEDED":
            meta["output"] = {"responsesFile": f"files/{job['output_file']}"}
            meta["batchStats"] = {"requestCount": str(job["request_count"])}
            op["response"] = {"@type": TYPE_BATCH.replace("GenerateContentBatch", "GenerateContentBatchOutput"),
                              "responsesFile": f"files/{job['output_file']}"}
        if state == "BATCH_STATE_FAILED":
            op["error"] = {"code": 8, "message": "The batch job failed (emulated)."}
        return op


# ── HTTP ─────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    store: Store
    quiet = False
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if not self.quiet:
            print(f"[emulator] {self.address_string()} {fmt % args}", flush=True)

    @property
    def base(self) -> str:
        return f"http://{self.headers.get('Host') or '127.0.0.1'}"

    def _send(self, code: int, body=None, headers: dict | None = None, raw: bytes | None = None, ctype="application/json"):
        data = raw if raw is not None else json.dumps(body if body is not None else {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _json(self) -> dict:
        try:
            return json.loads(self._body() or b"{}")
        except ValueError:
            raise ApiError(400, "INVALID_ARGUMENT", "Invalid JSON payload received.")

    def handle_any(self, method: str):
        url = urlparse(self.path)
        path = url.path
        try:
            with self.store.lock:
                self.route(method, path, parse_qs(url.query))
        except ApiError as e:
            self._send(e.code, {"error": {"code": e.code, "message": e.message, "status": e.status}})
        except Exception as e:   # a bug in the emulator should be visible, not hidden
            self._send(500, {"error": {"code": 500, "message": f"emulator error: {e}", "status": "INTERNAL"}})

    def do_GET(self): self.handle_any("GET")
    def do_POST(self): self.handle_any("POST")
    def do_DELETE(self): self.handle_any("DELETE")

    def route(self, method: str, path: str, query: dict):
        s = self.store
        # ---- control
        if path == "/_emulator/state" and method == "GET":
            return self._send(200, {"scenario": s.scenario, "delay": s.delay,
                                    "jobs": {j: {**v, "state": s.state_of(v)} for j, v in s.jobs.items()}, "files": s.files})
        if path == "/_emulator/scenario" and method == "POST":
            scenario = self._json().get("scenario")
            if scenario not in SCENARIOS:
                raise ApiError(400, "INVALID_ARGUMENT", f"scenario must be one of {SCENARIOS}")
            s.scenario = scenario
            return self._send(200, {"scenario": scenario})
        m = re.fullmatch(r"/_emulator/jobs/([^/]+)/finish", path)
        if m and method == "POST":
            job = s.jobs.get(m.group(1)) or self._missing(m.group(1))
            job["created"] -= job["delay"] + 1
            return self._send(200, s.operation(m.group(1)))

        # ---- file upload (resumable): start, then upload chunks to the returned URL
        if path == "/upload/v1beta/files" and method == "POST":
            meta = (self._json().get("file") or {})
            upload_id = secrets.token_hex(8)
            s.uploads[upload_id] = {"display_name": meta.get("displayName") or meta.get("display_name"),
                                    "mime_type": self.headers.get("X-Goog-Upload-Header-Content-Type") or meta.get("mimeType"),
                                    "data": bytearray()}
            return self._send(200, {}, {"x-goog-upload-status": "active", "x-goog-upload-chunk-granularity": "8388608",
                                        "x-goog-upload-url": f"{self.base}/upload/v1beta/files/{upload_id}"})
        m = re.fullmatch(r"/upload/v1beta/files/([^/]+)", path)
        if m and method == "POST":
            up = s.uploads.get(m.group(1)) or self._missing("upload")
            command = (self.headers.get("X-Goog-Upload-Command") or "").lower()
            up["data"].extend(self._body())
            if "finalize" not in command:
                return self._send(200, {}, {"x-goog-upload-status": "active"})
            data = bytes(up["data"])
            file_id = m.group(1)
            s.files[file_id] = {"display_name": up["display_name"], "mime_type": up["mime_type"], "size": len(data),
                                "created": iso(time.time()), "sha256": hashlib.sha256(data).hexdigest()}
            s.path(file_id).write_bytes(data)
            del s.uploads[m.group(1)]
            s.save()
            return self._send(200, {"file": s.file_json(file_id, self.base)}, {"x-goog-upload-status": "final"})

        # ---- files
        m = re.fullmatch(r"/(?:download/)?v1beta/files/([^/:]+?)(:download)?", path)
        if m and method == "GET":
            file_id = m.group(1)
            if file_id not in s.files:
                self._missing(f"files/{file_id}")
            if m.group(2) or query.get("alt") == ["media"]:
                return self._send(200, raw=s.path(file_id).read_bytes(), ctype="application/octet-stream")
            return self._send(200, s.file_json(file_id, self.base))
        if m and method == "DELETE":
            s.files.pop(m.group(1), None)
            s.path(m.group(1)).unlink(missing_ok=True)
            s.save()
            return self._send(200, {})

        # ---- batches
        m = re.fullmatch(r"/v1beta/models/([^/:]+):batchGenerateContent", path)
        if m and method == "POST":
            batch = self._json().get("batch") or {}
            file_name = (batch.get("inputConfig") or {}).get("fileName")
            if not file_name:
                raise ApiError(400, "INVALID_ARGUMENT", "batch.input_config.file_name is required (inline requests are not emulated).")
            file_id = file_name.removeprefix("files/")
            if file_id not in s.files:
                raise ApiError(404, "NOT_FOUND", f"File {file_name} was not found.")
            validate_input(s.path(file_id).read_bytes())
            job_id = secrets.token_hex(18)
            s.jobs[job_id] = {"display_name": batch.get("displayName") or f"batch-{job_id[:8]}", "model": m.group(1),
                              "src_file": file_id, "created": time.time(), "delay": s.delay, "scenario": s.scenario}
            s.save()
            return self._send(200, s.operation(job_id))
        if path == "/v1beta/batches" and method == "GET":
            return self._send(200, {"operations": [s.operation(j) for j in s.jobs]})
        m = re.fullmatch(r"/v1beta/batches/([^/:]+?)(:cancel)?", path)
        if m:
            job_id = m.group(1)
            if job_id not in s.jobs:
                self._missing(f"batches/{job_id}")
            if method == "POST" and m.group(2):
                s.jobs[job_id]["cancelled"] = True
                s.save()
                return self._send(200, {})
            if method == "DELETE":
                s.jobs.pop(job_id)
                s.save()
                return self._send(200, {})
            if method == "GET":
                return self._send(200, s.operation(job_id))
        raise ApiError(404, "NOT_FOUND", f"{method} {path} is not emulated.")

    def _missing(self, what: str):
        raise ApiError(404, "NOT_FOUND", f"{what} was not found.")


def serve(host: str, port: int, root: Path, delay: float, scenario: str, quiet: bool = False) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"store": Store(root, delay, scenario), "quiet": quiet})
    return ThreadingHTTPServer((host, port), handler)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Local Gemini Batch API emulator (no real LLM calls, no cost).")
    ap.add_argument("--host", default=os.getenv("EMULATOR_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("EMULATOR_PORT", "8099")))
    ap.add_argument("--delay", type=float, default=float(os.getenv("EMULATOR_DELAY", "20")),
                    help="seconds before a job finishes (default 20)")
    ap.add_argument("--scenario", choices=SCENARIOS, default=os.getenv("EMULATOR_SCENARIO", "success"))
    ap.add_argument("--dir", default=os.getenv("EMULATOR_DIR", ".output/gemini_emulator"), help="where files and jobs are kept")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    server = serve(a.host, a.port, Path(a.dir), a.delay, a.scenario, a.quiet)
    print(f"Gemini Batch API emulator on http://{a.host}:{a.port}  (jobs finish after {a.delay:g}s, scenario '{a.scenario}', data in {a.dir})\n"
          f"Point the app at it:  GEMINI_EMULATOR_URL=http://{a.host}:{a.port}   (no real LLM call is made)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
