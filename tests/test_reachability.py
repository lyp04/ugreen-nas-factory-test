from src.discovery.discover import reachability_verdict


def test_verdict_device_online_http_ok() -> None:
    # TCP open + HTTP 2xx → device is fine at the network/HTTP layer; the failure
    # is in the page / UGOS service, so a reboot usually clears it.
    v = reachability_verdict({"tcp": "open", "http_status": 200, "ping": True})
    assert "设备在线" in v
    assert "服务层" in v


def test_verdict_device_online_http_stuck() -> None:
    v = reachability_verdict({"tcp": "open", "http_status": None, "ping": True})
    assert "HTTP 未就绪" in v
    assert "重启" in v


def test_verdict_port_refused() -> None:
    v = reachability_verdict({"tcp": "refused", "http_status": None, "ping": True})
    assert "端口未监听" in v


def test_verdict_port_unreachable_but_pingable() -> None:
    v = reachability_verdict({"tcp": "timeout", "http_status": None, "ping": True})
    assert "端口不通" in v


def test_verdict_offline_ping_fail() -> None:
    # TCP timeout + ping fail → genuinely off the network (powered off / IP moved).
    v = reachability_verdict({"tcp": "timeout", "http_status": None, "ping": False})
    assert "不在网" in v
    assert "重新发现" in v


def test_verdict_unreachable_ping_unknown() -> None:
    v = reachability_verdict({"tcp": "timeout", "http_status": None, "ping": None})
    assert "不可达" in v
