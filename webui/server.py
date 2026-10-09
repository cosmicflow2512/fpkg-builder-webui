#!/usr/bin/env python3
"""Small web UI wrapper for fPKG Builder."""

from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from mimetypes import guess_type
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(os.environ.get("FPKG_REPO_ROOT", Path(__file__).resolve().parents[1])).resolve()
WEB_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = WEB_ROOT / "static"
HOST = os.environ.get("FPKG_WEB_HOST", "0.0.0.0")
PORT = int(os.environ.get("FPKG_WEB_PORT", "8080"))
POWERSHELL = os.environ.get("FPKG_POWERSHELL") or shutil.which("pwsh") or shutil.which("powershell.exe")
PYTHON = os.environ.get("FPKG_PYTHON") or shutil.which("python3") or shutil.which("python") or "python"
MAX_LOG_LINES = int(os.environ.get("FPKG_MAX_LOG_LINES", "4000"))
BROWSE_ROOTS = [
    Path(path).resolve()
    for path in os.environ.get(
        "FPKG_BROWSE_ROOTS",
        "/mnt/user:/mnt/cache/appdata:/app/fpkg-builder",
    ).split(":")
    if path
]


@dataclass
class Job:
    id: str
    args: list[str]
    created_at: float = field(default_factory=time.time)
    status: str = "queued"
    return_code: Optional[int] = None
    finished_at: Optional[float] = None
    log: list[str] = field(default_factory=list)
    process: Optional[subprocess.Popen[str]] = None

    def append(self, line: str) -> None:
        self.log.append(line.rstrip("\n"))
        if len(self.log) > MAX_LOG_LINES:
            del self.log[: len(self.log) - MAX_LOG_LINES]


jobs: dict[str, Job] = {}
jobs_lock = threading.Lock()
active_job_id: Optional[str] = None


def json_response(handler: SimpleHTTPRequestHandler, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
    body = json.dumps(payload, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler: SimpleHTTPRequestHandler, text: str, status: HTTPStatus = HTTPStatus.OK) -> None:
    body = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def read_json(handler: SimpleHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        return {}
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def status_payload() -> dict[str, Any]:
    scripts = {
        "build": ROOT / "build-fpkg.ps1",
        "gui": ROOT / "fpkg-builder-gui.ps1",
        "tool": ROOT / "toolchain" / "prospero-pub-cmd.exe",
    }
    return {
        "root": str(ROOT),
        "platform": platform.platform(),
        "powershell": POWERSHELL,
        "python": PYTHON,
        "scripts": {name: {"path": str(path), "exists": path.exists()} for name, path in scripts.items()},
        "canStart": bool(POWERSHELL) and scripts["build"].exists(),
        "browseRoots": [str(path) for path in BROWSE_ROOTS],
        "notes": [
            "The upstream builder is Windows/PowerShell oriented and ships Windows .exe publisher tools.",
            "On Linux/Unraid, the WebUI can launch the configured PowerShell command, but the publisher toolchain still needs a compatible runtime.",
        ],
    }


def publisher_path() -> Path:
    return ROOT / "toolchain" / "prospero-pub-cmd.exe"


def pkg_info(package: str) -> dict[str, Any] | None:
    info_script = ROOT / "scripts" / "pkg-info.py"
    if not info_script.exists():
        return None
    try:
        raw = subprocess.check_output(
            [PYTHON, str(info_script), package],
            cwd=str(ROOT),
            text=True,
            stderr=subprocess.STDOUT,
        )
        return json.loads(raw[raw.index("{") :])
    except Exception:
        return None


def path_allowed(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except FileNotFoundError:
        resolved = path.absolute()
    for root in BROWSE_ROOTS:
        if resolved == root or root in resolved.parents:
            return True
    return False


def browse_payload(raw_path: str, want: str) -> dict[str, Any]:
    path = Path(raw_path or str(BROWSE_ROOTS[0])).expanduser()
    if not path_allowed(path):
        raise ValueError(f"Path is outside allowed browse roots: {path}")
    if not path.exists():
        raise ValueError(f"Path does not exist: {path}")
    if path.is_file():
        path = path.parent
    entries = []
    for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        if child.name.startswith("."):
            continue
        if not path_allowed(child):
            continue
        is_dir = child.is_dir()
        if want == "dir" and not is_dir:
            continue
        entries.append(
            {
                "name": child.name,
                "path": str(child),
                "type": "dir" if is_dir else "file",
                "size": child.stat().st_size if child.is_file() else None,
            }
        )
    return {
        "path": str(path),
        "parent": str(path.parent) if path.parent != path and path_allowed(path.parent) else None,
        "roots": [str(root) for root in BROWSE_ROOTS],
        "entries": entries,
    }


def bool_arg(payload: dict[str, Any], key: str, flag: str, args: list[str]) -> None:
    if payload.get(key):
        args.append(flag)


def value_arg(payload: dict[str, Any], key: str, flag: str, args: list[str]) -> None:
    value = str(payload.get(key, "")).strip()
    if value:
        args.extend([flag, value])


def build_args(payload: dict[str, Any]) -> list[str]:
    if not POWERSHELL:
        raise ValueError("No PowerShell executable found. Set FPKG_POWERSHELL.")
    script = ROOT / "build-fpkg.ps1"
    if not script.exists():
        raise ValueError(f"Missing build script: {script}")

    output_folder = str(payload.get("outputFolder", "")).strip()
    if not output_folder:
        raise ValueError("Output folder is required.")

    args = [
        POWERSHELL,
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-OutputFolder",
        output_folder,
        "-Python",
        PYTHON,
    ]
    value_arg(payload, "gameFolder", "-GameFolder", args)
    value_arg(payload, "backportFolder", "-BackportFolder", args)
    value_arg(payload, "name", "-Name", args)
    value_arg(payload, "contentVersion", "-ContentVersion", args)
    value_arg(payload, "workFolder", "-WorkFolder", args)
    value_arg(payload, "toolkitRoot", "-ToolkitRoot", args)
    value_arg(payload, "temporaryDirectory", "-TemporaryDirectory", args)

    compression = str(payload.get("compressionLevel", "7")).strip()
    try:
        level = int(compression)
    except ValueError as exc:
        raise ValueError("Compression level must be a number from -4 to 9.") from exc
    if level < -4 or level > 9:
        raise ValueError("Compression level must be from -4 to 9.")
    args.extend(["-CompressionLevel", str(level)])

    bool_arg(payload, "rebuildBase", "-RebuildBase", args)
    bool_arg(payload, "keepWork", "-KeepWork", args)
    bool_arg(payload, "force", "-Force", args)
    return args


def extract_args(payload: dict[str, Any]) -> list[str]:
    publisher = publisher_path()
    if not publisher.exists():
        raise ValueError(f"Missing publisher tool: {publisher}")
    package = str(payload.get("package", "")).strip()
    destination = str(payload.get("destination", "")).strip()
    if not package:
        raise ValueError("Package is required.")
    if not Path(package).is_file():
        raise ValueError(f"Package does not exist or is not a file: {package}")
    if not destination:
        raise ValueError("Destination folder is required.")
    destination_path = Path(destination)
    if destination_path.exists() and destination_path.is_file():
        raise ValueError(f"Destination is a file, not a folder: {destination}")
    destination_path.mkdir(parents=True, exist_ok=True)

    info = pkg_info(package)
    is_patch = bool(info and info.get("kind") == "ps5-delta")
    passcode_args = ["--no_passcode"] if is_patch else ["--passcode", "0" * 32]
    return [str(publisher), "img_extract"] + passcode_args + ["--no_progress_bar", package, destination]


def run_job(job: Job) -> None:
    global active_job_id
    with jobs_lock:
        active_job_id = job.id
        job.status = "running"
    job.append(f"Starting job {job.id}")
    job.append("Command: " + " ".join(job.args))

    try:
        process = subprocess.Popen(
            job.args,
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            start_new_session=True,
        )
        job.process = process
        assert process.stdout is not None
        for line in process.stdout:
            job.append(line)
        job.return_code = process.wait()
        job.status = "succeeded" if job.return_code == 0 else "failed"
        job.append(f"Finished with exit code {job.return_code}")
    except Exception as exc:
        job.status = "failed"
        job.append(f"WebUI runner error: {exc}")
    finally:
        job.finished_at = time.time()
        with jobs_lock:
            if active_job_id == job.id:
                active_job_id = None


def job_summary(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "status": job.status,
        "createdAt": job.created_at,
        "finishedAt": job.finished_at,
        "returnCode": job.return_code,
        "logLines": len(job.log),
    }


class Handler(SimpleHTTPRequestHandler):
    server_version = "FpkgBuilderWebUI/1.0"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/status":
            json_response(self, status_payload())
            return
        if path == "/api/browse":
            query = parse_qs(urlparse(self.path).query)
            try:
                payload = browse_payload(query.get("path", [""])[0], query.get("type", ["any"])[0])
            except Exception as exc:
                json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            json_response(self, payload)
            return
        if path == "/api/jobs":
            with jobs_lock:
                payload = {"activeJobId": active_job_id, "jobs": [job_summary(job) for job in jobs.values()]}
            json_response(self, payload)
            return
        if path.startswith("/api/jobs/"):
            parts = [unquote(part) for part in path.split("/") if part]
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "jobs" and parts[3] == "log":
                job_id = parts[2]
                job = jobs.get(job_id)
                if not job:
                    json_response(self, {"error": "Job not found."}, HTTPStatus.NOT_FOUND)
                    return
                json_response(self, {"job": job_summary(job), "log": job.log})
                return
        return self.serve_static(path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/jobs":
            try:
                payload = read_json(self)
                kind = str(payload.get("kind", "build")).strip()
                args = extract_args(payload) if kind == "extract" else build_args(payload)
            except Exception as exc:
                json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            with jobs_lock:
                if active_job_id:
                    json_response(self, {"error": f"Job already running: {active_job_id}"}, HTTPStatus.CONFLICT)
                    return
                job = Job(id=uuid.uuid4().hex[:12], args=args)
                jobs[job.id] = job
            threading.Thread(target=run_job, args=(job,), daemon=True).start()
            json_response(self, {"job": job_summary(job)}, HTTPStatus.CREATED)
            return

        if path.startswith("/api/jobs/") and path.endswith("/cancel"):
            job_id = path.split("/")[3]
            job = jobs.get(job_id)
            if not job:
                json_response(self, {"error": "Job not found."}, HTTPStatus.NOT_FOUND)
                return
            if job.process and job.process.poll() is None:
                job.status = "cancelled"
                job.append("Cancelling job.")
                try:
                    os.killpg(job.process.pid, signal.SIGTERM)
                except Exception:
                    job.process.terminate()
            json_response(self, {"job": job_summary(job)})
            return

        json_response(self, {"error": "Not found."}, HTTPStatus.NOT_FOUND)

    def serve_static(self, path: str) -> None:
        if path == "/":
            target = WEB_ROOT / "index.html"
        elif path.startswith("/static/"):
            target = STATIC_ROOT / path.removeprefix("/static/")
        else:
            text_response(self, "Not found.", HTTPStatus.NOT_FOUND)
            return

        target = target.resolve()
        if WEB_ROOT not in target.parents and target != WEB_ROOT:
            text_response(self, "Not found.", HTTPStatus.NOT_FOUND)
            return
        if not target.exists() or not target.is_file():
            text_response(self, "Not found.", HTTPStatus.NOT_FOUND)
            return
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", guess_type(str(target))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        print("%s - %s" % (self.address_string(), format % args))


def main() -> None:
    print(f"fPKG Builder WebUI serving {ROOT} on http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
