#!/usr/bin/env python3
"""Zero-daemon Ops Hub reporter. Requires only psutil and the standard library."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import signal
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    import psutil
except ImportError:
    print(
        "Missing dependency: psutil. Install it with: "
        "apt-get install -y python3-psutil",
        file=sys.stderr,
    )
    raise SystemExit(2)


ERROR_WORDS = ("error", "exception", "traceback", "fatal", "critical")
STOP = False


def _stop(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True


def _env_or_arg(args: argparse.Namespace, name: str, default: str = "") -> str:
    cli_name = name.lower()
    value = getattr(args, cli_name, None)
    if value not in (None, ""):
        return str(value)
    return os.getenv(name, default)


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]


def _local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return ""
    finally:
        sock.close()


def _port_status(port: int, timeout: float = 1.5) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return {
                "alive": True,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            }
    except OSError:
        return {
            "alive": False,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }


def _cpu_model() -> str:
    model = platform.processor()
    if model:
        return model
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


def _disk_inventory() -> list[dict[str, Any]]:
    disks: list[dict[str, Any]] = []
    seen_devices: set[str] = set()
    excluded_filesystems = {
        "squashfs",
        "tmpfs",
        "devtmpfs",
        "proc",
        "sysfs",
        "cgroup2",
    }
    try:
        partitions = psutil.disk_partitions(all=False)
    except Exception:
        partitions = []
    for partition in partitions:
        if (
            partition.fstype in excluded_filesystems
            or partition.device.startswith("/dev/loop")
            or partition.device in seen_devices
        ):
            continue
        seen_devices.add(partition.device)
        try:
            usage = psutil.disk_usage(partition.mountpoint)
        except (PermissionError, OSError):
            continue
        disks.append(
            {
                "mount": partition.mountpoint,
                "device": partition.device,
                "fstype": partition.fstype,
                "total_gb": round(usage.total / 1024**3, 2),
                "used_gb": round(usage.used / 1024**3, 2),
                "free_gb": round(usage.free / 1024**3, 2),
                "percent": usage.percent,
            }
        )
    return disks[:30]


def _gpu_inventory() -> list[dict[str, Any]]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return []
    try:
        result = subprocess.run(
            [
                executable,
                "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=4,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    gpus: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        values = [value.strip() for value in line.split(",")]
        if len(values) < 4:
            continue
        try:
            used = float(values[2])
            total = float(values[3])
            percent = float(values[1])
        except ValueError:
            continue
        gpus.append(
            {
                "name": values[0][:160],
                "utilization_percent": round(percent, 2),
                "memory_used_mb": round(used, 2),
                "memory_total_mb": round(total, 2),
            }
        )
    return gpus[:16]


class LogTracker:
    def __init__(self, path: str, initial_tail_bytes: int = 65536) -> None:
        self.path = Path(path).expanduser()
        self.identity: tuple[int, int] | None = None
        self.offset = 0
        self.first_read = True
        self.initial_tail_bytes = initial_tail_bytes
        try:
            stat = self.path.stat()
            self.identity = (stat.st_dev, stat.st_ino)
            self.offset = max(0, stat.st_size - self.initial_tail_bytes)
        except OSError:
            pass

    def new_errors(self) -> list[dict[str, str]]:
        findings: list[dict[str, str]] = []
        try:
            stat = self.path.stat()
        except OSError:
            return findings
        identity = (stat.st_dev, stat.st_ino)
        if self.identity != identity:
            self.identity = identity
            self.offset = 0
            self.first_read = True
        if stat.st_size < self.offset:
            self.offset = 0
        if stat.st_size == self.offset:
            return findings
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self.offset)
                lines = handle.readlines()
                self.offset = handle.tell()
        except OSError as exc:
            print(f"Cannot read log {self.path}: {exc}", file=sys.stderr)
            return findings
        seen: set[str] = set()
        for line in lines[-100:]:
            clean = line.strip()
            if not clean or not any(word in clean.lower() for word in ERROR_WORDS):
                continue
            digest = hashlib.sha1(clean.encode("utf-8", "replace")).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            findings.append(
                {
                    "message": clean[:4000],
                    "timestamp": time.strftime(
                        "%Y-%m-%dT%H:%M:%S%z", time.localtime()
                    ),
                }
            )
        self.first_read = False
        return findings


class SshLoginTracker:
    pattern = re.compile(
        r"Accepted\s+(password|publickey|keyboard-interactive/pam)\s+"
        r"for\s+(\S+)\s+from\s+(\S+)\s+port\s+(\d+)",
        re.IGNORECASE,
    )

    def __init__(self) -> None:
        self.since = time.time() - 300
        self.seen: set[str] = set()

    def new_logins(self) -> list[dict[str, str]]:
        now = time.time()
        try:
            result = subprocess.run(
                [
                    "journalctl",
                    "-u",
                    "ssh",
                    "-u",
                    "sshd",
                    "--since",
                    f"@{int(self.since)}",
                    "--no-pager",
                    "-o",
                    "cat",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            lines = result.stdout.splitlines()
        except (OSError, subprocess.SubprocessError):
            lines = []
        if not lines:
            for path in ("/var/log/auth.log", "/var/log/secure"):
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as handle:
                        lines.extend(handle.readlines()[-1000:])
                except OSError:
                    continue
        self.since = now - 2
        logins: list[dict[str, str]] = []
        for line in lines:
            match = self.pattern.search(line)
            if not match:
                continue
            method, username, source_ip, source_port = match.groups()
            digest = hashlib.sha1(line.strip().encode("utf-8", "replace")).hexdigest()
            if digest in self.seen:
                continue
            self.seen.add(digest)
            logins.append(
                {
                    "username": username,
                    "ip": source_ip,
                    "port": source_port,
                    "method": method,
                    "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                }
            )
        if len(self.seen) > 500:
            self.seen = set(list(self.seen)[-300:])
        return logins[-20:]


def build_payload(
    server_id: str,
    server_name: str,
    ports: list[int],
    trackers: list[LogTracker],
    last_report_latency_ms: float = 0,
    network_state: dict[str, Any] | None = None,
    ssh_login_tracker: SshLoginTracker | None = None,
) -> dict[str, Any]:
    memory = psutil.virtual_memory()
    root_disk = psutil.disk_usage("/")
    disks = _disk_inventory()
    unique_disks: dict[str, dict[str, Any]] = {}
    for item in disks:
        device = item.get("device") or item.get("mount")
        previous = unique_disks.get(device)
        if previous is None or float(item["total_gb"]) > float(previous["total_gb"]):
            unique_disks[device] = item
    if unique_disks:
        disk_total_gb = sum(float(item["total_gb"]) for item in unique_disks.values())
        disk_used_gb = sum(float(item["used_gb"]) for item in unique_disks.values())
        disk_free_gb = sum(float(item["free_gb"]) for item in unique_disks.values())
        disk_percent = (
            disk_used_gb / disk_total_gb * 100 if disk_total_gb else root_disk.percent
        )
    else:
        disk_total_gb = root_disk.total / 1024**3
        disk_used_gb = root_disk.used / 1024**3
        disk_free_gb = root_disk.free / 1024**3
        disk_percent = root_disk.percent
    swap = psutil.swap_memory()
    gpus = _gpu_inventory()
    network = psutil.net_io_counters()
    network_state = network_state if network_state is not None else {}
    current_monotonic = time.monotonic()
    previous_upload = network_state.get("upload_total_bytes")
    previous_download = network_state.get("download_total_bytes")
    previous_time = network_state.get("monotonic")
    elapsed = (
        current_monotonic - float(previous_time)
        if previous_time is not None
        else 0
    )
    upload_bps = 0.0
    download_bps = 0.0
    if (
        elapsed > 0
        and previous_upload is not None
        and previous_download is not None
        and network.bytes_sent >= int(previous_upload)
        and network.bytes_recv >= int(previous_download)
    ):
        upload_bps = (network.bytes_sent - int(previous_upload)) / elapsed
        download_bps = (network.bytes_recv - int(previous_download)) / elapsed
    network_state.update(
        {
            "upload_total_bytes": network.bytes_sent,
            "download_total_bytes": network.bytes_recv,
            "monotonic": current_monotonic,
        }
    )
    try:
        load1, load5, load15 = psutil.getloadavg()
    except (AttributeError, OSError):
        load1 = load5 = load15 = 0
    errors: list[dict[str, str]] = []
    for tracker in trackers:
        errors.extend(tracker.new_errors())
    ssh_logins = ssh_login_tracker.new_logins() if ssh_login_tracker else []
    return {
        "node_id": server_id,
        "hostname": server_name,
        "ip": _local_ip(),
        "cpu_percent": round(psutil.cpu_percent(interval=1.0), 2),
        "memory_percent": round(memory.percent, 2),
        "memory_total_mb": round(memory.total / 1024**2, 2),
        "memory_used_mb": round((memory.total - memory.available) / 1024**2, 2),
        "swap_total_mb": round(swap.total / 1024**2, 2),
        "swap_used_mb": round(swap.used / 1024**2, 2),
        "disk_percent": round(disk_percent, 2),
        "disk_total_gb": round(disk_total_gb, 2),
        "disk_used_gb": round(disk_used_gb, 2),
        "disk_free_gb": round(disk_free_gb, 2),
        "disks": disks,
        "gpu_available": bool(gpus),
        "gpu_percent": round(
            max((item["utilization_percent"] for item in gpus), default=0), 2
        ),
        "gpus": gpus,
        "network_upload_bps": round(max(0.0, upload_bps), 2),
        "network_download_bps": round(max(0.0, download_bps), 2),
        "network_upload_total_bytes": int(network.bytes_sent),
        "network_download_total_bytes": int(network.bytes_recv),
        "uptime_seconds": int(time.time() - psutil.boot_time()),
        "cpu_cores": psutil.cpu_count(logical=True) or 0,
        "cpu_model": _cpu_model(),
        "load1": round(load1, 2),
        "load5": round(load5, 2),
        "load15": round(load15, 2),
        "os_info": platform.platform(),
        "ssh_host": os.getenv("SSH_HOST", ""),
        "ssh_port": int(os.getenv("SSH_PORT", "22") or 22),
        "ssh_user": os.getenv("SSH_USER", "root"),
        "report_latency_ms": round(last_report_latency_ms, 2),
        "ports": {str(port): _port_status(port) for port in ports},
        "errors": errors[:50],
        "ssh_logins": ssh_logins,
        "agent_time": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
    }


def post_report(
    hub_url: str, agent_key: str, payload: dict[str, Any]
) -> float:
    started = time.perf_counter()
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        hub_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-Agent-Key": agent_key,
            "User-Agent": "OpsHub-Reporter/1.0",
        },
    )
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=20, context=context) as response:
        response.read(4096)
    return (time.perf_counter() - started) * 1000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ops Hub server reporter")
    parser.add_argument("--server-id", default="")
    parser.add_argument("--server-name", default="")
    parser.add_argument("--hub-url", default="")
    parser.add_argument("--agent-key", default="")
    parser.add_argument("--check-ports", default="")
    parser.add_argument("--log-files", default="")
    parser.add_argument("--interval", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    args = parse_args()
    server_name = _env_or_arg(args, "SERVER_NAME", socket.gethostname())
    server_id = _env_or_arg(
        args,
        "SERVER_ID",
        "".join(char if char.isalnum() or char in "-_." else "-" for char in server_name),
    )
    hub_url = _env_or_arg(args, "HUB_URL")
    agent_key = _env_or_arg(args, "AGENT_KEY")
    try:
        ports = [int(item) for item in _csv(_env_or_arg(args, "CHECK_PORTS"))]
    except ValueError:
        print("CHECK_PORTS must be comma-separated numbers", file=sys.stderr)
        return 2
    log_paths = _csv(_env_or_arg(args, "LOG_FILES"))
    try:
        interval = int(args.interval or _env_or_arg(args, "REPORT_INTERVAL", "60"))
    except ValueError:
        interval = 60
    interval = max(15, interval)
    if not agent_key:
        print("AGENT_KEY is required", file=sys.stderr)
        return 2
    if not hub_url:
        print("HUB_URL is required", file=sys.stderr)
        return 2

    trackers = [LogTracker(path) for path in log_paths]
    print(
        f"Reporter started: node={server_id} hub={hub_url} "
        f"ports={ports or 'none'} logs={len(trackers)} interval={interval}s",
        flush=True,
    )
    retry_delay = 5
    last_report_latency_ms = 0.0
    network_state: dict[str, Any] = {}
    ssh_login_tracker = SshLoginTracker()
    while not STOP:
        wait_seconds = interval
        try:
            payload = build_payload(
                server_id,
                server_name,
                ports,
                trackers,
                last_report_latency_ms,
                network_state,
                ssh_login_tracker,
            )
            last_report_latency_ms = post_report(hub_url, agent_key, payload)
            print(
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} report ok "
                f"cpu={payload['cpu_percent']} mem={payload['memory_percent']} "
                f"disk={payload['disk_percent']}",
                flush=True,
            )
            retry_delay = 5
        except urllib.error.HTTPError as exc:
            print(f"Report rejected: HTTP {exc.code}", file=sys.stderr, flush=True)
            wait_seconds = retry_delay
            retry_delay = min(60, retry_delay * 2)
        except Exception as exc:
            print(f"Report failed: {exc}", file=sys.stderr, flush=True)
            wait_seconds = retry_delay
            retry_delay = min(60, retry_delay * 2)
        deadline = time.time() + wait_seconds
        while not STOP and time.time() < deadline:
            time.sleep(min(1, max(0, deadline - time.time())))
    print("Reporter stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
