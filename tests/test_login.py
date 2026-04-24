from src.flows import login


def test_password_error_maps_to_unflashed_message() -> None:
    assert login._body_has_password_error("用户名或密码错误")
    assert login.UNFLASHED_MESSAGE == "未刷机，请先刷机"


def test_non_password_error_does_not_map_to_unflashed_message() -> None:
    assert not login._body_has_password_error("服务启动中，请稍后刷新")
