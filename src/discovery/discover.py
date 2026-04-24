from __future__ import annotations

import ipaddress
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
