from src.utils.sn import (
    extract_full_sn,
    is_full_sn_candidate,
    is_auto_sn_placeholder,
    model_key_from_sn,
    normalize_sn,
    same_sn_identity,
    sn_matches_text,
    sn_tail,
)


def test_auto_sn_placeholder_detection() -> None:
    assert is_auto_sn_placeholder("AUTOC0A800D6")
    assert is_auto_sn_placeholder("auto-c0a800d6")
    assert not is_auto_sn_placeholder("HB670EE07251E54E")


def test_full_sn_candidate_rejects_about_page_false_positive() -> None:
    assert is_full_sn_candidate("HB670EE00000001A")
    assert is_full_sn_candidate("EC752JJ172517046")
    assert not is_full_sn_candidate("ECLGGEDQ8TB")
    assert extract_full_sn("SN: ECLGGEDQ8TB") is None


def test_extract_full_sn_supports_official_broadcast_label() -> None:
    assert extract_full_sn("UGREEN broadcast\nSN=XY12345678F1F2\nMAC=AA:BB", "F1F2") == "XY12345678F1F2"
    assert extract_full_sn("SN=F1F2&MAC=", "F1F2") is None


def test_extract_full_sn_without_tail_requires_label() -> None:
    assert extract_full_sn("UGREEN broadcast\nSN=XY12345678F1F2\nMAC=AA:BB") == "XY12345678F1F2"
    assert extract_full_sn("cached XY12345678F1F2 value") is None


def test_sn_tail_accepts_full_or_last_four() -> None:
    assert sn_tail("HB67EE52241DFFD") == "DFFD"
    assert sn_tail("dffd") == "DFFD"


def test_sn_matches_login_device_label() -> None:
    assert sn_matches_text("HB67EE52241DFFD", '登录"DXP2800-DFFD"')
    assert sn_matches_text("DFFD", '登录"DXP2800-DFFD"')
    assert not sn_matches_text("AEF1", '登录"DXP2800-DFFD"')


def test_sn_matches_truncated_4800_plus_tail_label() -> None:
    assert sn_matches_text("EC752JJ172517046", "登录 DXP4800 Plus-704")
    assert sn_matches_text("7046", "登录 DXP4800 PLus-704")
    assert not sn_matches_text("8046", "登录 DXP4800 Plus-704")


def test_same_sn_identity_uses_tail() -> None:
    assert same_sn_identity("HB67EE52241DFFD", "DFFD")
    assert same_sn_identity("dffd", "HB67EE52241DFFD")
    assert not same_sn_identity("AEF1", "DFFD")
    assert not same_sn_identity("HB67EE52241DFFD", "EC752VV42251DFFD")
    assert same_sn_identity("EC752VV42251611A", "ec752vv42251611a")
    assert not same_sn_identity("AUTOC0A800D6", "00D6")


def test_normalize_sn_strips_scanner_noise() -> None:
    assert normalize_sn(" hb67-ee52241dffd\r\n") == "HB67EE52241DFFD"


def test_model_key_from_sn_prefix() -> None:
    assert model_key_from_sn("HB67EE52241DFFD") == "2800"
    assert model_key_from_sn("EC671JJ172517046") == "4800"
    assert model_key_from_sn("EC752JJ21251AA3F") == "4800Plus"
    assert model_key_from_sn("AA3F") is None
    assert model_key_from_sn("AUTOC0A800D6") is None


def test_extract_full_sn_supports_hb_and_ec_prefixes() -> None:
    assert extract_full_sn("序列号：HB67EE52241DFFD", "DFFD") == "HB67EE52241DFFD"
    assert extract_full_sn("序列号：EC4800ABCDEF7046", "7046") == "EC4800ABCDEF7046"


def test_extract_full_sn_ignores_model_tail_label() -> None:
    assert extract_full_sn("登录 DXP4800-7046", "7046") is None
    assert extract_full_sn("序列号：EC4800ABCDEF7046", "DFFD") is None
