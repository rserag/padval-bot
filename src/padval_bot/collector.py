"""Read-only host, service, network, HTTP, Docker, and RAID collection."""

from __future__ import annotations

import base64
import datetime as dt
import html
import json
import os
import shlex
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any


HOST_PROBE = r'''
import json
import os
import shutil
import subprocess
import sys

config = json.loads(sys.argv[1])

def run(args, timeout=8):
    try:
        p = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=timeout,
                           check=False)
        return p.returncode, p.stdout.strip()
    except Exception:
        return 124, ""

def mem_percent():
    values = {}
    with open("/proc/meminfo", encoding="ascii") as handle:
        for line in handle:
            key, value = line.split(":", 1)
            values[key] = int(value.strip().split()[0]) * 1024
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    return round((total - available) * 100 / total) if total else 0

def disk_percent(path):
    usage = shutil.disk_usage(path)
    return round(usage.used * 100 / usage.total) if usage.total else 0

def active(unit):
    return run(["systemctl", "is-active", unit], 4)[1] == "active"

result = {
    "name": config["name"],
    "address": config.get("address", "local"),
    "reachable": True,
    "uptime": int(float(open("/proc/uptime", encoding="ascii").read().split()[0])),
    "load": round(os.getloadavg()[0], 2),
    "memory_pct": mem_percent(),
    "filesystems": {},
    "services": {},
    "containers": [],
    "compose_projects": [],
    "expected_stopped": config.get("docker", {}).get("expected_stopped", []),
    "expected_dormant_projects": config.get("docker", {}).get("expected_dormant_projects", []),
    "process_memory": {},
    "notes": config.get("notes", []),
}

for item in config.get("filesystems", [{"name": "root", "path": "/"}]):
    try:
        result["filesystems"][item["name"]] = disk_percent(item["path"])
    except OSError:
        result["filesystems"][item["name"]] = None

for unit in config.get("services", []):
    result["services"][unit] = active(unit)

docker = config.get("docker", {})
if docker.get("enabled"):
    rc, output = run(["docker", "ps", "-a", "--format",
                      "{{.Names}}|{{.State}}|{{.Status}}"], 10)
    if rc == 0 and output:
        result["containers"] = output.splitlines()
    rc, output = run(["docker", "compose", "ls", "--all", "--format", "json"], 10)
    if rc == 0 and output:
        try:
            projects = json.loads(output)
            if isinstance(projects, list):
                result["compose_projects"] = projects
        except json.JSONDecodeError:
            pass

for item in config.get("process_memory", []):
    rc, value = run(["systemctl", "show", item["unit"], "-p",
                     "MemoryCurrent", "--value"], 5)
    try:
        result["process_memory"][item["name"]] = int(value)
    except ValueError:
        result["process_memory"][item["name"]] = None

raid = config.get("raid")
if raid:
    try:
        mdstat = open("/proc/mdstat", encoding="ascii").read()
    except OSError:
        mdstat = ""
    device = raid.get("device", "md0")
    required = raid.get("required_members", "[UU]")
    result["raid"] = {
        "device": device,
        "members_ok": required in mdstat,
        "members_label": required,
        "spare_ok": "(S)" in mdstat if raid.get("require_spare") else None,
        "syncing": any(marker in mdstat for marker in ("resync", "recovery", "check =")),
        "mismatch": None,
    }
    try:
        mismatch_path = f"/sys/block/{device}/md/mismatch_cnt"
        result["raid"]["mismatch"] = int(open(mismatch_path, encoding="ascii").read().strip())
    except (OSError, ValueError):
        pass

print(json.dumps(result, separators=(",", ":")))
'''


def run(args: list[str], timeout: int = 10, input_text: str | None = None) -> tuple[int, str]:
    try:
        process = subprocess.run(
            args,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        return process.returncode, process.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return 124, ""


def format_duration(seconds: int) -> str:
    days, remainder = divmod(max(0, seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _probe_command(host: dict[str, Any]) -> list[str]:
    encoded_probe = base64.b64encode(HOST_PROBE.encode("utf-8")).decode("ascii")
    encoded_config = base64.b64encode(json.dumps(host).encode("utf-8")).decode("ascii")
    python_code = (
        "import base64,sys;"
        f"sys.argv=['probe',base64.b64decode({encoded_config!r}).decode()];"
        f"exec(base64.b64decode({encoded_probe!r}))"
    )
    if host["mode"] == "local":
        return ["python3", "-c", python_code]

    ssh = host["ssh"]
    destination = f"{ssh['user']}@{ssh['host']}"
    remote = f"python3 -c {shlex.quote(python_code)}"
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={ssh['known_hosts_file']}",
        "-i", ssh["identity_file"],
        destination,
        remote,
    ]


def collect_host(host: dict[str, Any]) -> dict[str, Any]:
    rc, output = run(_probe_command(host), timeout=int(host.get("timeout", 15)))
    if rc != 0:
        return {
            "name": host["name"],
            "address": host.get("address", host.get("ssh", {}).get("host", "?")),
            "reachable": False,
            "notes": host.get("notes", []),
        }
    try:
        result = json.loads(output)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass
    return {
        "name": host["name"],
        "address": host.get("address", "?"),
        "reachable": False,
        "notes": ["host probe returned invalid data"],
    }


def ping(host: str) -> bool:
    return run(["ping", "-c", "1", "-W", "2", host], timeout=4)[0] == 0


def tcp_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def collect_network(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for check in checks:
        kind = check.get("type", "icmp")
        if kind == "tcp":
            healthy = tcp_open(check["host"], int(check["port"]))
        elif kind == "dns":
            try:
                socket.getaddrinfo(check["host"], int(check.get("port", 443)))
                healthy = True
            except OSError:
                healthy = False
        else:
            healthy = ping(check["host"])
        results.append({**check, "healthy": healthy})
    return results


def collect_http(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for check in checks:
        minimum = int(check.get("healthy_status_min", 200))
        maximum = int(check.get("healthy_status_max", 499))
        rc, value = run(
            [
                "curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
                "--max-time", str(int(check.get("timeout", 8))), check["url"],
            ],
            timeout=int(check.get("timeout", 8)) + 2,
        )
        status = int(value) if rc == 0 and value.isdigit() else None
        results.append({
            **check,
            "status": status,
            "healthy": status is not None and minimum <= status <= maximum,
        })
    return results


def collect_routeros(config: dict[str, Any] | None) -> dict[str, Any] | None:
    if not config or not config.get("enabled"):
        return None
    command = (
        ':put ("uptime=" . [/system/resource/get uptime]); '
        ':put ("version=" . [/system/resource/get version]); '
        ':put ("cpu=" . [/system/resource/get cpu-load]); '
        ':put ("free=" . [/system/resource/get free-memory]); '
        ':put ("total=" . [/system/resource/get total-memory]); '
        f':put ("wg=" . [/interface/wireguard/get [find where name="{config.get("wireguard_interface", "wg1")}"] running]); '
        f':put ("peers=" . [:len [/interface/wireguard/peers/find where interface="{config.get("wireguard_interface", "wg1")}"]])'
    )
    rc, output = run(
        [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={config['known_hosts_file']}",
            "-i", config["identity_file"],
            f"{config['user']}@{config['host']}", command,
        ],
        timeout=int(config.get("timeout", 10)),
    )
    if rc != 0:
        return {"healthy": False}
    values: dict[str, Any] = {"healthy": True}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.strip().split("=", 1)
            values[key] = value
    return values


def collect_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated_at": dt.datetime.now().astimezone(),
        "network": collect_network(config.get("network_checks", [])),
        "routeros": collect_routeros(config.get("routeros")),
        "hosts": [collect_host(host) for host in config["hosts"]],
        "http": collect_http(config.get("http_checks", [])),
    }


def _host_issues(host: dict[str, Any]) -> list[str]:
    if not host.get("reachable"):
        return [f"{host['name']} unreachable"]
    issues = [f"{host['name']} {unit} inactive" for unit, ok in host.get("services", {}).items() if not ok]
    expected_stopped = set(host.get("expected_stopped", []))
    for row in host.get("containers", []):
        parts = str(row).split("|", 2)
        if not parts or parts[0] in expected_stopped:
            continue
        if len(parts) < 2 or parts[1] != "running":
            issues.append(f"{host['name']} container {parts[0]} is not running")
        elif len(parts) == 3 and "unhealthy" in parts[2].lower():
            issues.append(f"{host['name']} container {parts[0]} unhealthy")
    expected_dormant = set(host.get("expected_dormant_projects", []))
    for project in host.get("compose_projects", []):
        name = str(project.get("Name", "unknown"))
        status = str(project.get("Status", "unknown"))
        if name not in expected_dormant and not status.startswith("running"):
            issues.append(f"{host['name']} Compose project {name} is {status}")
    raid = host.get("raid")
    if raid:
        if not raid.get("members_ok"):
            issues.append(f"{host['name']} RAID members degraded")
        if raid.get("spare_ok") is False:
            issues.append(f"{host['name']} RAID spare missing")
        if isinstance(raid.get("mismatch"), int) and raid["mismatch"] > 0:
            issues.append(f"{host['name']} RAID mismatch {raid['mismatch']}")
    return issues


def _finish_html(lines: list[str], limit: int = 4000) -> str:
    """Keep Telegram HTML valid by truncating only between complete lines."""
    result: list[str] = []
    for line in lines:
        candidate = "\n".join([*result, line])
        if len(candidate) > limit - 40:
            result.append("<i>…report truncated</i>")
            break
        result.append(line)
    return "\n".join(result)


def render_report(snapshot: dict[str, Any], title: str = "SYSTEM STATUS") -> str:
    issues: list[str] = []
    for item in snapshot.get("network", []):
        if not item["healthy"]:
            issues.append(f"network check failed: {item['name']}")
    router = snapshot.get("routeros")
    if router is not None and not router.get("healthy"):
        issues.append("RouterOS detail check failed")
    for host in snapshot.get("hosts", []):
        issues.extend(_host_issues(host))
    for item in snapshot.get("http", []):
        if not item["healthy"]:
            issues.append(f"{item['name']} returned {item.get('status') or 'no response'}")

    generated = snapshot.get("generated_at")
    stamp = generated.strftime("%Y-%m-%d %H:%M %Z") if hasattr(generated, "strftime") else str(generated)
    overall = "All monitored systems healthy" if not issues else f"{len(issues)} issue{'s' if len(issues) != 1 else ''} detected"
    lines = [
        f"{'🟢' if not issues else '🟠'} <b>{html.escape(title)}</b>",
        f"<b>{html.escape(overall)}</b>",
        f"<code>{html.escape(stamp)}</code>",
    ]

    network = snapshot.get("network", [])
    if network:
        lines.extend(["", "<b>NETWORK</b>"])
        lines.append("  ".join(f"{'✅' if x['healthy'] else '❌'} {html.escape(str(x['name']))}" for x in network))
    if router and router.get("healthy"):
        lines.append(
            f"RouterOS <b>{html.escape(str(router.get('version', '?')))}</b> · "
            f"up {html.escape(str(router.get('uptime', '?')))} · "
            f"CPU {html.escape(str(router.get('cpu', '?')))}% · "
            f"WG {html.escape(str(router.get('wg', '?')))} ({html.escape(str(router.get('peers', '?')))} peers)"
        )

    for host in snapshot.get("hosts", []):
        lines.extend([
            "",
            f"<b>{html.escape(str(host['name']).upper())}</b>  <code>{html.escape(str(host.get('address', '?')))}</code>",
        ])
        if not host.get("reachable"):
            lines.append("❌ Host unreachable")
            continue
        lines.append(
            f"⏱ {format_duration(int(host.get('uptime', 0)))} · "
            f"Load {html.escape(str(host.get('load', '?')))}"
        )
        storage = [f"RAM {html.escape(str(host.get('memory_pct', '?')))}%"]
        storage.extend(
            f"{html.escape(str(name))} {html.escape(str(value if value is not None else '?'))}%"
            for name, value in host.get("filesystems", {}).items()
        )
        lines.append("💾 " + " · ".join(storage))
        services = host.get("services", {})
        if services:
            healthy_services = sum(bool(value) for value in services.values())
            lines.append(
                f"{'✅' if healthy_services == len(services) else '❌'} Core services  "
                f"<b>{healthy_services}/{len(services)}</b>"
            )
        projects = host.get("compose_projects", [])
        if projects:
            dormant = set(host.get("expected_dormant_projects", []))
            monitored = [project for project in projects if project.get("Name") not in dormant]
            dormant_present = [project for project in projects if project.get("Name") in dormant]
            active = sum(str(project.get("Status", "")).startswith("running") for project in monitored)
            lines.append(
                f"{'✅' if active == len(monitored) else '❌'} Apps  <b>{active}/{len(monitored)} active</b>"
            )
            if dormant_present:
                lines.append(
                    f"⏸ {len(dormant_present)} dormant project{'s' if len(dormant_present) != 1 else ''}"
                )
        containers = host.get("containers", [])
        if containers:
            expected_stopped = set(host.get("expected_stopped", []))
            monitored_rows = [str(row).split("|", 2) for row in containers if str(row).split("|", 1)[0] not in expected_stopped]
            healthy_containers = sum(
                len(parts) >= 2 and parts[1] == "running" and (len(parts) < 3 or "unhealthy" not in parts[2].lower())
                for parts in monitored_rows
            )
            lines.append(
                f"{'✅' if healthy_containers == len(monitored_rows) else '❌'} Containers  "
                f"<b>{healthy_containers}/{len(monitored_rows)} healthy</b>"
            )
        raid = host.get("raid")
        if raid:
            spare = " · spare " + ("yes" if raid.get("spare_ok") else "no") if raid.get("spare_ok") is not None else ""
            lines.append(
                f"{'✅' if raid.get('members_ok') and raid.get('spare_ok') is not False and raid.get('mismatch') in (0, None) else '❌'} "
                f"RAID <b>{html.escape(str(raid.get('device', '?')))}</b> · "
                f"<code>{html.escape(str(raid.get('members_label', '?')))}</code>"
                f"{spare} · mismatch {html.escape(str(raid.get('mismatch', '?')))}"
            )
        for name, value in host.get("process_memory", {}).items():
            if isinstance(value, int):
                lines.append(f"ℹ️ {html.escape(str(name))} memory  <b>{value / (1024 ** 3):.1f} GiB</b>")
        lines.extend(f"ℹ️ {html.escape(str(note))}" for note in host.get("notes", []))

    http = snapshot.get("http", [])
    if http:
        healthy_http = sum(bool(item["healthy"]) for item in http)
        lines.extend([
            "",
            "<b>HTTP</b>",
            f"{'✅' if healthy_http == len(http) else '❌'} Endpoints  <b>{healthy_http}/{len(http)} responding</b>",
        ])

    if issues:
        lines.extend(["", "<b>ATTENTION</b>"])
        lines.extend(f"❌ {html.escape(issue)}" for issue in issues[:12])
        if len(issues) > 12:
            lines.append(f"<i>+{len(issues) - 12} more</i>")

    return _finish_html(lines)
