from __future__ import annotations

import ipaddress
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Collection

from ..utils.logger import logger
from . import mdns_scanner, port_scanner, ugreen_broadcast


def find_nas(subnet: str, port: int, discovery_timeout: float, exclude: Collection[str] | None = None) -> str:
    candidates = find_nas_candidates(subnet, port, discovery_timeout, exclude=exclude)
    if candidates:
        return candidates[0]

    excluded = set(exclude or ())
    if excluded:
        raise RuntimeError(f"No unused UGOS NAS found on {subnet}:{port}; excluded active IPs: {sorted(excluded)}")
    raise RuntimeError(f"No UGOS NAS found on {subnet}:{port}")


def find_nas_candidates(
    subnet: str,
    port: int,
    discovery_timeout: float,
    exclude: Collection[str] | None = None,
) -> list[str]:
    excluded = set(exclude or ())
    candidates: list[str] = []

    logger.info("Trying UGREEN LAN broadcast discovery...")
    broadcast_hits = ugreen_broadcast.discover(timeout=min(discovery_timeout, 2.0))
    for hit in broadcast_hits:
        if hit.address in excluded:
            logger.info(f"Skipping UGOS already assigned to another active task: {hit.address}")
            continue
        logger.info(
            f"Found UGOS via UGREEN broadcast: {hit.address}"
            f"{f' SN={hit.sn}' if hit.sn else ''}"
            f"{f' MAC={hit.mac}' if hit.mac else ''}"
        )
        candidates.append(hit.address)

    logger.info("Trying mDNS discovery...")
    hits = mdns_scanner.discover(timeout=min(discovery_timeout, 5.0))
    for hit in hits:
        if hit.address in excluded:
            logger.info(f"Skipping UGOS already assigned to another active task: {hit.address}")
            continue
        if _looks_like_ugos(hit.address, port):
            logger.info(f"Found UGOS via mDNS: {hit.address}")
            candidates.append(hit.address)

    if candidates:
        logger.info(f"mDNS found {len(candidates)} usable candidate(s). Scanning {subnet}:{port} for any others...")
    else:
        logger.info(f"mDNS found nothing usable. Falling back to port scan on {subnet}:{port}...")

    for ip in _sort_ips(port_scanner.scan_subnet(subnet, port=port, timeout=0.5)):
        if ip in excluded:
            logger.info(f"Skipping UGOS already assigned to another active task: {ip}")
            continue
        if ip in candidates:
            continue
        if _looks_like_ugos(ip, port):
            logger.info(f"Found UGOS via port scan: {ip}")
            candidates.append(ip)

    candidates = _sort_ips(candidates)
    if candidates:
        return candidates

    if excluded:
        raise RuntimeError(f"No unused UGOS NAS found on {subnet}:{port}; excluded active IPs: {sorted(excluded)}")
    raise RuntimeError(f"No UGOS NAS found on {subnet}:{port}")


def _sort_ips(ips: list[str]) -> list[str]:
    def key(ip: str):
        try:
            return (0, ipaddress.ip_address(ip))
        except ValueError:
            return (1, ip)

    return sorted(ips, key=key)


def _looks_like_ugos(ip: str, port: int, timeout: float = 2.0) -> bool:
    url = f"http://{ip}:{port}/"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ugreen-factory-test"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return False


def wait_until_ready(ip: str, port: int, max_wait: float) -> None:
    logger.info(f"Waiting for UGOS at {ip}:{port} to be ready (max {max_wait}s)...")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if _looks_like_ugos(ip, port):
            logger.info("UGOS service is ready")
            return
        time.sleep(2)
    raise RuntimeError(f"UGOS at {ip}:{port} did not become ready within {max_wait}s")


def _tcp_state(ip: str, port: int, timeout: float) -> str:
    """'open' (port accepted), 'refused' (host up, nothing listening),
    'timeout' (no answer — host likely offline / wrong IP), or 'error'."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return "open"
    except ConnectionRefusedError:
        return "refused"
    except (socket.timeout, TimeoutError):
        return "timeout"
    except OSError:
        return "error"


def _http_status(ip: str, port: int, timeout: float) -> int | None:
    try:
        req = urllib.request.Request(f"http://{ip}:{port}/", headers={"User-Agent": "ugreen-factory-test"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:
        return None


def _ping_ok(ip: str, timeout: float) -> bool | None:
    """True/False if ping ran; None if ping itself could not be executed."""
    if sys.platform.startswith("win"):
        cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout))), ip]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout + 2,
        )
        return proc.returncode == 0
    except Exception:
        return None


def probe_reachability(ip: str, port: int, timeout: float = 3.0) -> dict:
    """Best-effort device/network probe used to explain a failure.

    Distinguishes 'NAS is up but UGOS/HTTP is stuck' from 'NAS is off the
    network' so the operator knows whether a reboot or a re-scan is the fix.
    Never raises — returns whatever it managed to collect.
    """
    result: dict = {"ip": ip, "port": port}
    try:
        result["tcp"] = _tcp_state(ip, port, timeout)
    except Exception:
        result["tcp"] = "error"
    try:
        result["http_status"] = _http_status(ip, port, timeout) if result.get("tcp") == "open" else None
    except Exception:
        result["http_status"] = None
    try:
        result["ping"] = _ping_ok(ip, timeout)
    except Exception:
        result["ping"] = None
    return result


def reachability_verdict(probe: dict) -> str:
    """把 probe_reachability 的结果翻成一句给 operator 看的判定：到底是 NAS 自己
    的问题（服务/页面层，重启设备多半能好）还是连接/网络问题（掉线 / IP 变了）。"""
    tcp = str(probe.get("tcp") or "")
    http = probe.get("http_status")
    ping = probe.get("ping")
    if tcp == "open":
        if isinstance(http, int) and 200 <= http < 400:
            return "设备在线、HTTP 正常——故障在页面 / UGOS 服务层，重启设备通常可恢复（详见失败截图）"
        return "设备在线但 HTTP 未就绪——UGOS 服务没起来，建议断电重启设备"
    if tcp == "refused":
        return "设备在线但 9999 端口未监听——UGOS 还没起来 / 端口有变，稍候重试或重启设备"
    if ping is True:
        return "设备在网但 9999 端口不通——可能正在重启 / 服务未起，稍候重试或重启设备"
    if ping is False:
        return "设备不在网——掉线 / 关机 / IP 变了，建议重启设备或重新发现"
    return "设备不可达——掉线 / 关机 / IP 变了，建议重启设备或重新发现"
