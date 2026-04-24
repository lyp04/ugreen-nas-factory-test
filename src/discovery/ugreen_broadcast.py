from __future__ import annotations

import json
import select
import socket
import time
from dataclasses import dataclass, field

from ..utils.logger import logger


BROADCAST_PORT = 60000
BROADCAST_MAGIC = 100
BROADCAST_ADDRESS = "255.255.255.255"


@dataclass(frozen=True)
class UgreenBroadcastHit:
    address: str
    sn: str = ""
    mac: str = ""
    data: dict = field(default_factory=dict)


def discover(sn: str = "", mac: str = "", timeout: float = 1.5) -> list[UgreenBroadcastHit]:
    """Discover UGREEN NAS devices with the same UDP packet used by the PC app."""
    packet = _build_query_packet(sn, mac)
    sockets = _open_broadcast_sockets()
    if not sockets:
        return []

    hits: dict[str, UgreenBroadcastHit] = {}
    deadline = time.monotonic() + max(timeout, 0.1)
    try:
        for sock in sockets:
            local = _socket_name(sock)
            try:
                sock.sendto(packet, (BROADCAST_ADDRESS, BROADCAST_PORT))
                logger.debug(f"UGREEN broadcast sent from {local}")
            except OSError as exc:
                logger.debug(f"UGREEN broadcast send failed from {local}: {exc}")

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            readable, _, _ = select.select(sockets, [], [], remaining)
            if not readable:
                break
            for sock in readable:
                try:
                    payload, remote = sock.recvfrom(8192)
                except OSError:
                    continue
                hit = _decode_response(payload, remote)
                if hit is None:
                    continue
                if sn and hit.sn != sn:
                    continue
                hits[hit.address] = hit
    finally:
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass

    return list(hits.values())


def _build_query_packet(sn: str, mac: str) -> bytes:
    body = f"SN={sn or ''}&MAC={mac or ''}".encode("utf-8")
    return BROADCAST_MAGIC.to_bytes(2, "big") + len(body).to_bytes(2, "big") + body


def _decode_response(payload: bytes, remote: tuple[str, int]) -> UgreenBroadcastHit | None:
    try:
        message = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    if message.get("error_code") != 0:
        return None

    data = message.get("data") or {}
    if not isinstance(data, dict):
        return None

    address = str(remote[0])
    sn = str(data.get("sn") or "").strip()
    mac = str(data.get("mac") or "").strip()
    pair = data.get("pair")
    if isinstance(pair, dict):
        mac = str(pair.get(address) or mac).strip()

    data = dict(data)
    data["ip"] = address
    if mac:
        data["mac"] = mac
    return UgreenBroadcastHit(address=address, sn=sn, mac=mac, data=data)


def _open_broadcast_sockets() -> list[socket.socket]:
    sockets: list[socket.socket] = []
    for address in _local_ipv4_addresses():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, 128)
            sock.bind((address, 0))
            sockets.append(sock)
        except OSError as exc:
            logger.debug(f"Could not bind UGREEN broadcast socket on {address or '0.0.0.0'}: {exc}")
            try:
                sock.close()
            except OSError:
                pass

    return sockets


def _local_ipv4_addresses() -> list[str]:
    addresses: list[str] = []
    try:
        for _, _, _, _, sockaddr in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = sockaddr[0]
            if address and not address.startswith("127.") and address not in addresses:
                addresses.append(address)
    except OSError:
        pass

    addresses.append("")
    return addresses


def _socket_name(sock: socket.socket) -> str:
    try:
        host, port = sock.getsockname()
        return f"{host or '0.0.0.0'}:{port}"
    except OSError:
        return "<closed>"
