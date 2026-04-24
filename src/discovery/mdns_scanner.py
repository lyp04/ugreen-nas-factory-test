from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import Iterable

from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf

from ..utils.logger import logger

DEFAULT_SERVICE_TYPES = ("_http._tcp.local.", "_ugreen._tcp.local.")


@dataclass
class MdnsHit:
    address: str
    name: str
    port: int


def discover(service_types: Iterable[str] = DEFAULT_SERVICE_TYPES, timeout: float = 5.0) -> list[MdnsHit]:
    hits: list[MdnsHit] = []

    def on_change(
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
        **_: object,
    ) -> None:
        if state_change is not ServiceStateChange.Added:
            return
        info = zeroconf.get_service_info(service_type, name, timeout=int(timeout * 1000))
        if not info or not info.addresses:
            return
        for raw in info.addresses:
            try:
                ip = socket.inet_ntoa(raw)
            except OSError:
                continue
            hits.append(MdnsHit(address=ip, name=name, port=info.port or 0))
            logger.debug(f"mDNS hit: {name} @ {ip}:{info.port}")

    zc = Zeroconf()
    try:
        browsers = [ServiceBrowser(zc, st, handlers=[on_change]) for st in service_types]
        import time
        time.sleep(timeout)
        for b in browsers:
            b.cancel()
    finally:
        zc.close()

    return hits
