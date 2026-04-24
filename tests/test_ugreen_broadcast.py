from src.discovery import ugreen_broadcast


def test_build_query_packet_matches_official_format() -> None:
    packet = ugreen_broadcast._build_query_packet("", "")

    assert packet[:2] == (100).to_bytes(2, "big")
    assert packet[2:4] == (8).to_bytes(2, "big")
    assert packet[4:] == b"SN=&MAC="


def test_decode_response_uses_remote_ip_and_pair_mac() -> None:
    payload = (
        b'{"error_code":0,"data":{"sn":"HB670EE02251AF1F2",'
        b'"pair":{"192.168.0.103":"AA:BB:CC:DD:EE:FF"}}}'
    )

    hit = ugreen_broadcast._decode_response(payload, ("192.168.0.103", 60000))

    assert hit is not None
    assert hit.address == "192.168.0.103"
    assert hit.sn == "HB670EE02251AF1F2"
    assert hit.mac == "AA:BB:CC:DD:EE:FF"
    assert hit.data["ip"] == "192.168.0.103"
