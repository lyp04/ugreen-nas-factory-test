from __future__ import annotations

import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..utils.logger import logger


def _probe(host: str, port: int, timeout: float) -> str | None:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return host
    except OSError:
        return None


def scan_subnet(subnet: str, port: int = 9999, timeout: float = 0.5, max_workers: int = 128) -> list[str]:
    net = ipaddress.ip_network(subnet, strict=False)
    hosts = [str(h) for h in net.hosts()]
    found: list[str] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_probe, h, port, timeout): h for h in hosts}
        for fut in as_completed(futures):
            result = fut.result()
            if result:
                found.append(result)
                logger.debug(f"Port scan hit: {result}:{port}")

    return found
