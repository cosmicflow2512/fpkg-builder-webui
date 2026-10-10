#!/usr/bin/env python3
"""Small web UI wrapper for fPKG Builder."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import signal
import subprocess
import tarfile
import threading
import time
import uuid
import zipfile
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
CONFIG_ROOT = Path(os.environ.get("FPKG_CONFIG_ROOT", "/config")).resolve()
SETTINGS_FILE = CONFIG_ROOT / "settings.json"
BROWSE_ROOTS = [
    Path(path).resolve()
    for path in os.environ.get(
        "FPKG_BROWSE_ROOTS",
        "/mnt/user:/config:/app/fpkg-builder",
    ).split(":")
    if path
]


@dataclass
class Job:
    id: str
    args: list[str]
    kind: str = "command"
    payload: dict[str, Any] = field(default_factory=dict)
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


DEFAULT_SETTINGS = {
    "defaultOutput": os.environ.get("FPKG_DEFAULT_OUTPUT", "/mnt/user/fpkg-output"),
    "defaultWork": os.environ.get("FPKG_DEFAULT_WORK", "/config/work"),
    "archivePasswords": [],
    "overlapPrepare": True,
    "verifyChecksums": True,
}


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


def load_settings() -> dict[str, Any]:
    if not SETTINGS_FILE.exists():
        return DEFAULT_SETTINGS.copy()
    try:
        with SETTINGS_FILE.open("r", encoding="utf-8") as handle:
            stored = json.load(handle)
    except Exception:
        return DEFAULT_SETTINGS.copy()
    settings = DEFAULT_SETTINGS.copy()
    settings.update({key: value for key, value in stored.items() if key in DEFAULT_SETTINGS})
    if not isinstance(settings.get("archivePasswords"), list):
        settings["archivePasswords"] = []
    return settings


def save_settings(payload: dict[str, Any]) -> dict[str, Any]:
    settings = DEFAULT_SETTINGS.copy()
    settings.update({key: payload.get(key, settings[key]) for key in DEFAULT_SETTINGS})
    passwords = settings.get("archivePasswords") or []
    settings["archivePasswords"] = [str(item) for item in passwords if str(item).strip()]
    settings["overlapPrepare"] = bool(settings.get("overlapPrepare"))
    settings["verifyChecksums"] = bool(settings.get("verifyChecksums"))
    CONFIG_ROOT.mkdir(parents=True, exist_ok=True)
    with SETTINGS_FILE.open("w", encoding="utf-8") as handle:
        json.dump(settings, handle, indent=2)
    return settings


def human_size(size: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


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


def packages_payload(raw_path: str) -> dict[str, Any]:
    settings = load_settings()
    folder = Path(raw_path or settings["defaultOutput"]).expanduser()
    if not path_allowed(folder):
        raise ValueError(f"Path is outside allowed browse roots: {folder}")
    if not folder.exists():
        return {"path": str(folder), "packages": []}
    if not folder.is_dir():
        raise ValueError(f"Path is not a folder: {folder}")
    packages = []
    for package in sorted(folder.glob("*.pkg"), key=lambda item: item.stat().st_mtime, reverse=True):
        stat = package.stat()
        info = pkg_info(str(package))
        param = (info or {}).get("param") or {}
        packages.append(
            {
                "name": package.name,
                "path": str(package),
                "size": stat.st_size,
                "sizeHuman": human_size(stat.st_size),
                "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                "titleId": param.get("titleId"),
                "contentId": param.get("contentId"),
                "version": param.get("contentVersion") or param.get("version"),
            }
        )
    return {"path": str(folder), "packages": packages}


def diagnostics_payload() -> dict[str, Any]:
    settings = load_settings()
    tools = {
        "powershell": POWERSHELL,
        "python": PYTHON,
        "7z": shutil.which("7z") or shutil.which("7za"),
        "unar": shutil.which("unar"),
        "publisher": str(publisher_path()),
    }
    roots = []
    for root in BROWSE_ROOTS:
        entry: dict[str, Any] = {"path": str(root), "exists": root.exists()}
        if root.exists():
            usage = shutil.disk_usage(root)
            entry["free"] = usage.free
            entry["freeHuman"] = human_size(usage.free)
        roots.append(entry)
    safe_settings = settings.copy()
    safe_settings["archivePasswords"] = ["***" for _ in safe_settings.get("archivePasswords", [])]
    return {
        "system": platform.platform(),
        "root": str(ROOT),
        "configRoot": str(CONFIG_ROOT),
        "tools": tools,
        "browseRoots": roots,
        "settings": safe_settings,
        "jobs": [job_summary(job) for job in jobs.values()],
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
    value_arg(payload, "referencePackage", "-ReferencePackage", args)
    value_arg(payload, "toolkitRoot", "-ToolkitRoot", args)
    value_arg(payload, "temporaryDirectory", "-TemporaryDirectory", args)

    compression = str(payload.get("compressionLevel", "4")).strip()
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


def effective_work_folder(value: str = "") -> str:
    work_folder = str(value or "").strip() or str(load_settings().get("defaultWork", "")).strip()
    return work_folder or "/config/work"


def import_root() -> Path:
    return Path(os.environ.get("FPKG_IMPORT_ROOT", "/config/imports"))


def archive_stem(path: Path) -> str:
    name = path.name
    for suffix in (".tar.gz", ".tar.xz", ".tar.bz2", ".tgz", ".tbz2", ".txz"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def split_archive_start(path: Path) -> Path:
    name = path.name
    lower = name.lower()
    directory = path.parent

    rar_part = re.match(r"^(?P<prefix>.+)\.part(?P<number>\d+)\.rar$", name, re.IGNORECASE)
    if rar_part:
        number = int(rar_part.group("number"))
        width = len(rar_part.group("number"))
        if number != 1:
            first = directory / f"{rar_part.group('prefix')}.part{1:0{width}d}.rar"
            if first.exists():
                return first
        return path

    rar_volume = re.match(r"^(?P<prefix>.+)\.r\d{2,3}$", name, re.IGNORECASE)
    if rar_volume:
        first = directory / f"{rar_volume.group('prefix')}.rar"
        if first.exists():
            return first

    zip_volume = re.match(r"^(?P<prefix>.+)\.z\d{2,3}$", name, re.IGNORECASE)
    if zip_volume:
        first = directory / f"{zip_volume.group('prefix')}.zip"
        if first.exists():
            return first

    numeric_volume = re.match(r"^(?P<prefix>.+)\.(?P<number>\d{3})$", name, re.IGNORECASE)
    if numeric_volume and numeric_volume.group("number") != "001":
        first = directory / f"{numeric_volume.group('prefix')}.001"
        if first.exists():
            return first

    return path


def has_split_zip_parts(path: Path) -> bool:
    return any(path.parent.glob(f"{path.stem}.z[0-9][0-9]")) or any(path.parent.glob(f"{path.stem}.z[0-9][0-9][0-9]"))


def is_archive_file(path: Path) -> bool:
    lower = path.name.lower()
    return lower.endswith((".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2")) or bool(
        re.search(r"\.(part\d+\.rar|r\d{2,3}|z\d{2,3}|\d{3})$", lower)
    )


def safe_extract_zip(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as handle:
        for member in handle.infolist():
            target = (destination / member.filename).resolve()
            if destination.resolve() not in target.parents and target != destination.resolve():
                raise ValueError(f"Archive entry escapes destination: {member.filename}")
        handle.extractall(destination)


def safe_extract_tar(archive: Path, destination: Path) -> None:
    with tarfile.open(archive) as handle:
        for member in handle.getmembers():
            target = (destination / member.name).resolve()
            if destination.resolve() not in target.parents and target != destination.resolve():
                raise ValueError(f"Archive entry escapes destination: {member.name}")
        handle.extractall(destination)


def extract_archive(archive: Path, destination: Path, job: Job) -> None:
    original_archive = archive
    archive = split_archive_start(archive)
    if archive != original_archive:
        job.append(f"Using first archive volume: {archive}")
    destination.mkdir(parents=True, exist_ok=True)
    lower = archive.name.lower()
    seven_zip = shutil.which("7z") or shutil.which("7za")
    unar = shutil.which("unar")
    if lower.endswith(".zip") and has_split_zip_parts(archive) and seven_zip:
        job.append(f"Split ZIP detected; extracting with {seven_zip}.")
        run_process(job, [seven_zip, "x", "-y", f"-o{destination}", str(archive)])
        if job.return_code not in (None, 0):
            raise RuntimeError(f"Archive extraction failed with exit code {job.return_code}")
        job.return_code = None
        return
    if lower.endswith(".zip"):
        job.append("Extracting ZIP archive.")
        safe_extract_zip(archive, destination)
        return
    if lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2")):
        job.append("Extracting TAR archive.")
        safe_extract_tar(archive, destination)
        return
    if lower.endswith(".rar") and unar:
        job.append(f"Extracting RAR archive with {unar}.")
        run_process(job, [unar, "-f", "-o", str(destination), str(archive)])
        if job.return_code in (None, 0):
            job.return_code = None
            return
        job.append(f"unar failed with exit code {job.return_code}; trying 7z fallback.")
        shutil.rmtree(destination, ignore_errors=True)
        destination.mkdir(parents=True, exist_ok=True)
        job.return_code = None
    if seven_zip:
        job.append(f"Extracting archive with {seven_zip}.")
        run_process(job, [seven_zip, "x", "-y", f"-o{destination}", str(archive)])
        if job.return_code not in (None, 0):
            raise RuntimeError(f"Archive extraction failed with exit code {job.return_code}")
        job.return_code = None
        return
    raise ValueError("Unsupported archive type without 7z/7za installed.")


def single_child_or_self(path: Path) -> Path:
    children = [child for child in path.iterdir() if not child.name.startswith(".")]
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return path


def nested_archive_or_none(path: Path) -> Path | None:
    current = single_child_or_self(path)
    if not current.is_dir():
        return None
    children = [child for child in current.iterdir() if not child.name.startswith(".")]
    if len(children) == 1 and children[0].is_file() and is_archive_file(children[0]):
        return children[0]
    return None


def contains_game_dump(path: Path) -> bool:
    return (path / "sce_sys" / "param.json").is_file()


def archive_build_payload(payload: dict[str, Any]) -> dict[str, Any]:
    archive = Path(str(payload.get("archive", "")).strip())
    if not archive.is_file():
        raise ValueError(f"Archive does not exist or is not a file: {archive}")
    output_folder = str(payload.get("outputFolder", "")).strip() or load_settings()["defaultOutput"]
    return {
        "archive": str(archive),
        "outputFolder": output_folder,
        "gameFolder": str(payload.get("gameFolder", "")).strip(),
        "gameArchive": str(payload.get("gameArchive", "")).strip(),
        "basePackage": str(payload.get("basePackage", "")).strip(),
        "name": str(payload.get("name", "")).strip(),
        "force": bool(payload.get("force")),
        "keepWork": bool(payload.get("keepWork")),
        "rebuildBase": bool(payload.get("rebuildBase")),
        "compressionLevel": str(payload.get("compressionLevel", "4")).strip() or "4",
        "workFolder": str(payload.get("workFolder", "")).strip(),
    }


def folder_backport_payload(payload: dict[str, Any]) -> dict[str, Any]:
    backport_folder = Path(str(payload.get("backportFolder", "")).strip())
    if not backport_folder.is_dir():
        raise ValueError(f"Backport folder does not exist or is not a folder: {backport_folder}")
    return {
        "backportFolder": str(backport_folder),
        "outputFolder": str(payload.get("outputFolder", "")).strip() or load_settings()["defaultOutput"],
        "gameFolder": str(payload.get("gameFolder", "")).strip(),
        "basePackage": str(payload.get("basePackage", "")).strip(),
        "name": str(payload.get("name", "")).strip(),
        "force": bool(payload.get("force")),
        "keepWork": bool(payload.get("keepWork")),
        "compressionLevel": str(payload.get("compressionLevel", "4")).strip() or "4",
        "workFolder": str(payload.get("workFolder", "")).strip(),
    }


def extract_to_import(archive: Path, job: Job, label: str) -> Path:
    destination = import_root() / f"{label}-{archive_stem(archive)}-{job.id}"
    job.append(f"Import archive: {archive}")
    job.append(f"Extract into: {destination}")
    extract_archive(archive, destination, job)
    source = single_child_or_self(destination)
    for depth in range(1, 4):
        nested = nested_archive_or_none(source)
        if not nested:
            break
        nested_destination = import_root() / f"{label}-nested{depth}-{archive_stem(nested)}-{job.id}"
        job.append(f"Nested archive detected: {nested}")
        job.append(f"Extract nested archive into: {nested_destination}")
        extract_archive(nested, nested_destination, job)
        source = single_child_or_self(nested_destination)
    return source


def title_id_from_pkg(package: Path) -> str | None:
    info = pkg_info(str(package))
    param = (info or {}).get("param") or {}
    title_id = param.get("titleId")
    return str(title_id).strip() if title_id else None


def resolve_base_package(path_text: str, preferred_name: str = "") -> Path:
    base_path = Path(path_text)
    if base_path.is_file():
        if base_path.suffix.lower() != ".pkg":
            raise ValueError(f"Base package is not a .pkg file: {base_path}")
        return base_path
    if not base_path.is_dir():
        raise ValueError(f"Base PKG path does not exist: {base_path}")

    packages = sorted(base_path.glob("*.pkg"))
    if not packages:
        packages = sorted(base_path.rglob("*.pkg"))
    if not packages:
        raise ValueError(f"No .pkg file found in base folder: {base_path}")

    preferred = preferred_name.strip().lower()
    if preferred:
        matches = [package for package in packages if preferred in package.stem.lower()]
        if len(matches) == 1:
            return matches[0]

    if len(packages) == 1:
        return packages[0]

    examples = ", ".join(package.name for package in packages[:5])
    raise ValueError(f"Base folder contains multiple .pkg files ({examples}). Select the exact Base PKG file.")


def apply_base_source(payload: dict[str, Any], build_payload: dict[str, Any], job: Job) -> None:
    base_source = str(payload.get("basePackage", "")).strip()
    game_archive = str(payload.get("gameArchive", "")).strip()
    game_folder = str(payload.get("gameFolder", "")).strip()

    if base_source:
        base_path = Path(base_source)
        if base_path.is_file() and base_path.suffix.lower() != ".pkg":
            job.append(f"Base archive: {base_path}")
            extracted_base = extract_to_import(base_path, job, "base")
            if contains_game_dump(extracted_base):
                job.append(f"Base archive looks like a game dump: {extracted_base}")
                build_payload["gameFolder"] = str(extracted_base)
                return
            base_path = resolve_base_package(str(extracted_base), str(build_payload.get("name", "")))
        else:
            base_path = resolve_base_package(base_source, str(build_payload.get("name", "")))

        job.append(f"Base package: {base_path}")
        name = build_payload.get("name") or title_id_from_pkg(base_path) or base_path.stem
        build_payload["name"] = name
        build_payload["referencePackage"] = str(base_path)
        job.append("Using base package in place; it will not be copied to the output folder.")
        return

    if game_archive:
        game_archive_path = Path(game_archive)
        if not game_archive_path.is_file():
            raise ValueError(f"Game archive does not exist or is not a file: {game_archive_path}")
        game_source = extract_to_import(game_archive_path, job, "game")
        if not contains_game_dump(game_source):
            raise ValueError(f"Game archive did not unpack to a game dump with sce_sys/param.json: {game_source}")
        build_payload["gameFolder"] = str(game_source)
        return

    if game_folder:
        build_payload["gameFolder"] = game_folder


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


def run_process(job: Job, args: list[str]) -> None:
    job.append("Command: " + " ".join(args))
    process = subprocess.Popen(
        args,
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


def run_archive_build(job: Job) -> None:
    payload = job.payload
    archive = Path(payload["archive"])
    source = extract_to_import(archive, job, "main")
    build_payload: dict[str, Any] = {
        "outputFolder": payload["outputFolder"],
        "compressionLevel": payload.get("compressionLevel", "4"),
        "workFolder": effective_work_folder(payload.get("workFolder", "")),
        "force": payload.get("force", False),
        "keepWork": payload.get("keepWork", False),
    }
    if payload.get("name"):
        build_payload["name"] = payload["name"]

    has_base_source = bool(payload.get("basePackage") or payload.get("gameArchive") or payload.get("gameFolder"))
    if contains_game_dump(source) and not has_base_source:
        job.append(f"Archive looks like a game dump: {source}")
        build_payload["gameFolder"] = str(source)
    else:
        job.append(f"Archive looks like backport files: {source}")
        build_payload["backportFolder"] = str(source)
        apply_base_source(payload, build_payload, job)
        if not has_base_source and not payload.get("name"):
            raise ValueError(
                "Backport-only archive needs a Base PKG, Game archive, Game folder, or a Name matching an existing base PKG in the output folder."
            )

    args = build_args(build_payload)
    run_process(job, args)


def run_folder_backport(job: Job) -> None:
    payload = job.payload
    build_payload: dict[str, Any] = {
        "backportFolder": payload["backportFolder"],
        "outputFolder": payload["outputFolder"],
        "compressionLevel": payload.get("compressionLevel", "4"),
        "workFolder": effective_work_folder(payload.get("workFolder", "")),
        "force": payload.get("force", False),
        "keepWork": payload.get("keepWork", False),
    }
    for key in ("gameFolder", "name"):
        if payload.get(key):
            build_payload[key] = payload[key]
    base_package = payload.get("basePackage", "")
    if base_package:
        apply_base_source(payload, build_payload, job)
    args = build_args(build_payload)
    run_process(job, args)


def pair_build_payload(payload: dict[str, Any]) -> dict[str, Any]:
    game_source = str(payload.get("gameSource", "")).strip()
    backport_source = str(payload.get("backportSource", "")).strip()
    if not game_source:
        raise ValueError("Game/RAW source is required.")
    if not backport_source:
        raise ValueError("Backport source is required.")
    game_path = Path(game_source)
    backport_path = Path(backport_source)
    if not game_path.exists():
        raise ValueError(f"Game/RAW source does not exist: {game_path}")
    if not backport_path.exists():
        raise ValueError(f"Backport source does not exist: {backport_path}")
    return {
        "archive": str(game_path),
        "backportSource": str(backport_path),
        "outputFolder": str(payload.get("outputFolder", "")).strip() or load_settings()["defaultOutput"],
        "name": str(payload.get("name", "")).strip(),
        "force": bool(payload.get("force")),
        "keepWork": bool(payload.get("keepWork")),
        "rebuildBase": bool(payload.get("rebuildBase")),
        "compressionLevel": str(payload.get("compressionLevel", "4")).strip() or "4",
        "workFolder": str(payload.get("workFolder", "")).strip(),
    }


def run_pair_build(job: Job) -> None:
    payload = job.payload
    game_source = Path(payload["archive"])
    if game_source.is_file():
        game_folder = extract_to_import(game_source, job, "game")
    else:
        game_folder = game_source
    if not contains_game_dump(game_folder):
        raise ValueError(f"Game source did not resolve to a game dump with sce_sys/param.json: {game_folder}")

    backport_source = Path(str(payload.get("backportSource", "")))
    if backport_source.is_file():
        backport_folder = extract_to_import(backport_source, job, "backport")
    else:
        backport_folder = backport_source
    if not backport_folder.is_dir():
        raise ValueError(f"Backport source did not resolve to a folder: {backport_folder}")

    build_payload = {
        "outputFolder": payload["outputFolder"],
        "gameFolder": str(game_folder),
        "backportFolder": str(backport_folder),
        "name": payload.get("name", ""),
        "compressionLevel": payload.get("compressionLevel", "4"),
        "workFolder": effective_work_folder(payload.get("workFolder", "")),
        "force": payload.get("force", False),
        "keepWork": payload.get("keepWork", False),
        "rebuildBase": payload.get("rebuildBase", False),
    }
    args = build_args(build_payload)
    run_process(job, args)


def run_job(job: Job) -> None:
    global active_job_id
    with jobs_lock:
        active_job_id = job.id
        job.status = "running"
    job.append(f"Starting job {job.id}")

    try:
        if job.kind == "archive_build":
            run_archive_build(job)
        elif job.kind == "pair_build":
            run_pair_build(job)
        elif job.kind == "folder_backport":
            run_folder_backport(job)
        else:
            run_process(job, job.args)
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
        if path == "/api/packages":
            query = parse_qs(urlparse(self.path).query)
            try:
                payload = packages_payload(query.get("path", [""])[0])
            except Exception as exc:
                json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            json_response(self, payload)
            return
        if path == "/api/settings":
            json_response(self, load_settings())
            return
        if path == "/api/diagnostics":
            json_response(self, diagnostics_payload())
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
                if kind == "extract":
                    args = extract_args(payload)
                    job_payload = {}
                elif kind == "archive_build":
                    args = []
                    job_payload = archive_build_payload(payload)
                elif kind == "pair_build":
                    args = []
                    job_payload = pair_build_payload(payload)
                elif kind == "folder_backport":
                    args = []
                    job_payload = folder_backport_payload(payload)
                else:
                    args = build_args(payload)
                    job_payload = {}
            except Exception as exc:
                json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            with jobs_lock:
                if active_job_id:
                    json_response(self, {"error": f"Job already running: {active_job_id}"}, HTTPStatus.CONFLICT)
                    return
                job = Job(id=uuid.uuid4().hex[:12], args=args, kind=kind, payload=job_payload)
                jobs[job.id] = job
            threading.Thread(target=run_job, args=(job,), daemon=True).start()
            json_response(self, {"job": job_summary(job)}, HTTPStatus.CREATED)
            return

        if path == "/api/settings":
            try:
                settings = save_settings(read_json(self))
            except Exception as exc:
                json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            json_response(self, settings)
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
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        print("%s - %s" % (self.address_string(), format % args))


def main() -> None:
    print(f"fPKG Builder WebUI serving {ROOT} on http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
