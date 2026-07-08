from __future__ import annotations

import io
from pathlib import Path

import pytest

from src.utils import label as label_util
from src.utils.label import (
    BARCODE_HEIGHT_MM,
    DEFAULT_DPI,
    LABEL_HEIGHT_MM,
    LABEL_WIDTH_MM,
    LEFT_MARGIN_MM,
    PRINT_AREA_WIDTH_MM,
    TEXT_PREFIX,
    TOP_MARGIN_MM,
    build_zpl,
    render_preview_png,
    _fit_code128_module_width,
    _mm_to_dots,
)


# ---------------------------------------------------------------------------
# build_zpl
# ---------------------------------------------------------------------------


def test_build_zpl_starts_and_ends_with_xa_xz() -> None:
    zpl = build_zpl("HB670EE00000001A").decode("utf-8")
    assert zpl.startswith("^XA")
    assert zpl.rstrip().endswith("^XZ")


def test_build_zpl_sets_label_dimensions_at_203dpi() -> None:
    zpl = build_zpl("HB670EE00000001A", dpi=203).decode("utf-8")
    # 42mm * 203dpi / 25.4 = 335.7 → 336
    assert "^PW336" in zpl
    # 25mm * 203dpi / 25.4 = 199.8 → 200
    assert "^LL200" in zpl


def test_build_zpl_sets_label_dimensions_at_300dpi() -> None:
    zpl = build_zpl("HB670EE00000001A", dpi=300).decode("utf-8")
    # 42mm * 300dpi / 25.4 = 496.06 → 496
    assert "^PW496" in zpl
    # 25mm * 300dpi / 25.4 = 295.27 → 295
    assert "^LL295" in zpl


def test_build_zpl_emits_two_gfa_bitmaps() -> None:
    sn = "HB670EE00000001A"
    zpl = build_zpl(sn).decode("utf-8")
    # Both barcode and SN text are now ^GFA bitmaps (barcode at spec 38mm
    # width, text in a bold TTF font that ZPL's built-in ^A0 cannot match).
    assert zpl.count("^GFA,") == 2
    # No native ^BC barcode or ^A0 text field.
    assert "^BCN" not in zpl
    assert "^A0N" not in zpl
    # No ^FD text data either — everything is bitmap.
    assert f"^FD{TEXT_PREFIX}{sn}^FS" not in zpl


def test_text_prefix_matches_spec() -> None:
    # Spec sample shows "SN：<value>"; we emit "SN: " (ASCII colon + space)
    # for printer-font compatibility — see TEXT_PREFIX comment.
    assert TEXT_PREFIX == "SN: "


def test_gfa_bytes_dont_print_row_padding_as_black() -> None:
    """Regression: bytes_per_row pads each row up to a byte boundary. After we
    invert PIL→ZPL polarity, the padding bits flip to 1 and would print as a
    thin black sliver at the right edge. The encoder must mask them back to 0.
    """
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("Pillow not installed")
    img = Image.new("1", (13, 4), 1)  # all white; 13 not multiple of 8
    zpl = label_util._image_to_gfa(img, origin_x=0, origin_y=0)
    # bytes_per_row = (13+7)//8 = 2; padding bits = 3 → mask = 0xE0
    # All-white PIL inverts to all-zero; padded bits stay 0 → every byte == 00
    import re
    m = re.search(r"\^GFA,\d+,\d+,(\d+),([0-9A-F]+)\^FS", zpl)
    assert m is not None
    bpr = int(m.group(1))
    data = m.group(2)
    assert bpr == 2
    # Every byte in the data should be "00" for an all-white image.
    assert set(data[i : i + 2] for i in range(0, len(data), 2)) == {"00"}


def test_build_zpl_rejects_empty_sn() -> None:
    with pytest.raises(ValueError):
        build_zpl("   ")
    with pytest.raises(ValueError):
        build_zpl("!!!")


def test_build_zpl_rejects_overlong_sn() -> None:
    with pytest.raises(ValueError):
        build_zpl("A" * 81)


def test_build_zpl_barcode_height_matches_12mm_spec() -> None:
    zpl = build_zpl("HB670EE00000001A", dpi=203).decode("utf-8")
    # 12mm * 8 dpmm = 96 dots. The ^GFA bytes-per-row * rows = 96 rows worth of
    # data. ^GFA syntax: ^GFa,b,c,d where d = bytes-per-row, b = total bytes.
    expected_h = _mm_to_dots(BARCODE_HEIGHT_MM, 203)
    expected_w = _mm_to_dots(PRINT_AREA_WIDTH_MM, 203)
    expected_bpr = (expected_w + 7) // 8
    expected_total = expected_bpr * expected_h
    assert expected_h == 96
    assert expected_w == 304
    assert f"^GFA,{expected_total},{expected_total},{expected_bpr}," in zpl


def test_build_zpl_quantity_emits_pq() -> None:
    zpl = build_zpl("HB670EE00000001A", quantity=3).decode("utf-8")
    assert "^PQ3,0,1,Y" in zpl


def test_build_zpl_default_dpi_constant() -> None:
    # Sanity: spec defaults to 203dpi for Zebra desktop label printers.
    assert DEFAULT_DPI == 203


# ---------------------------------------------------------------------------
# Geometry primitives
# ---------------------------------------------------------------------------


def test_mm_to_dots_matches_known_values() -> None:
    # 42mm at 203dpi = 336 dots
    assert _mm_to_dots(LABEL_WIDTH_MM, 203) == 336
    # 25mm at 203dpi = 200 dots
    assert _mm_to_dots(LABEL_HEIGHT_MM, 203) == 200
    # 2mm at 203dpi = 16 dots
    assert _mm_to_dots(LEFT_MARGIN_MM, 203) == 16
    # 5.5mm at 203dpi = 44 dots
    assert _mm_to_dots(TOP_MARGIN_MM, 203) == 44


def test_module_width_fits_within_print_area() -> None:
    # Helper still exists for callers/tooling that want native-^BC sizing;
    # build_zpl no longer uses it (bitmap renders to exact spec width).
    print_area_dots = _mm_to_dots(PRINT_AREA_WIDTH_MM, 203)  # 304
    assert _fit_code128_module_width(16, print_area_dots) == 1
    assert _fit_code128_module_width(10, print_area_dots) == 2
    assert _fit_code128_module_width(4, print_area_dots) == 2


def test_barcode_origin_is_left_2mm_top_5_5mm() -> None:
    # Bitmap is placed at the 2mm left × 5.5mm top corner — no centering
    # offsets, because the bitmap itself fills the full 38mm width.
    zpl = build_zpl("HB670EE00000001A", dpi=203).decode("utf-8")
    assert "^FO16,44^GFA," in zpl
    zpl_short = build_zpl("ABCDEFGHIJ", dpi=203).decode("utf-8")
    assert "^FO16,44^GFA," in zpl_short


# ---------------------------------------------------------------------------
# Preview PNG (skipped if optional deps missing)
# ---------------------------------------------------------------------------


def test_render_preview_png_writes_correctly_sized_file(tmp_path: Path) -> None:
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("Pillow not installed")
    try:
        import barcode  # noqa: F401  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("python-barcode not installed")

    out = tmp_path / "preview.png"
    result = render_preview_png("HB670EE00000001A", out)
    assert result == out
    assert out.exists() and out.stat().st_size > 0

    with Image.open(out) as img:
        # 42 x 25 mm at default 300 dpi preview = 496 x 295 px.
        expected_w = round(LABEL_WIDTH_MM * 300 / 25.4)
        expected_h = round(LABEL_HEIGHT_MM * 300 / 25.4)
        assert img.size == (expected_w, expected_h)


def test_render_preview_png_normalizes_sn(tmp_path: Path) -> None:
    try:
        from PIL import Image  # noqa: F401  # type: ignore[import-not-found]
        import barcode  # noqa: F401  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("python-barcode/Pillow not installed")

    out = tmp_path / "norm.png"
    # Empty SN must raise.
    with pytest.raises(ValueError):
        render_preview_png("   ", out)


# ---------------------------------------------------------------------------
# Printer discovery — non-Windows behavior only (Windows path tested manually)
# ---------------------------------------------------------------------------


def test_list_printers_returns_empty_on_non_windows(monkeypatch) -> None:
    monkeypatch.setattr(label_util, "is_windows", lambda: False)
    assert label_util.list_windows_printers() == []


def test_find_label_printer_returns_preferred_on_non_windows(monkeypatch) -> None:
    monkeypatch.setattr(label_util, "is_windows", lambda: False)
    # On non-Windows, find_label_printer is a no-op that just returns the hint.
    assert label_util.find_label_printer("ZDesigner GK420t") == "ZDesigner GK420t"
    assert label_util.find_label_printer(None) is None


def test_find_label_printer_strict_exact_match_when_preferred(monkeypatch) -> None:
    monkeypatch.setattr(label_util, "is_windows", lambda: True)
    queues = [
        {"name": "ZDesigner ZD888-203dpi ZPL", "driver": "ZDesigner ZD888", "port": "USB002"},
        {"name": "ZDesigner ZD888-203dpi ZPL (副本 1)", "driver": "ZDesigner ZD888", "port": "USB004"},
        {"name": "DeliDL-888T", "driver": "Deli", "port": "USB003"},
    ]
    monkeypatch.setattr(label_util, "list_windows_printers", lambda: queues)
    # Exact match wins, including the parenthesised副本 suffix that previously
    # confused the substring heuristic.
    assert (
        label_util.find_label_printer("ZDesigner ZD888-203dpi ZPL (副本 1)")
        == "ZDesigner ZD888-203dpi ZPL (副本 1)"
    )
    # No fuzzy match — wrong queue name returns None, never "close enough".
    assert label_util.find_label_printer("ZDesigner ZD888") is None
    assert label_util.find_label_printer("DeliDL-999X") is None
    # Case-insensitive exact still OK.
    assert label_util.find_label_printer("delidl-888t") == "DeliDL-888T"


def test_find_label_printer_heuristic_only_when_preferred_is_empty(monkeypatch) -> None:
    monkeypatch.setattr(label_util, "is_windows", lambda: True)
    queues = [
        {"name": "OneNote", "driver": "Microsoft", "port": "nul:"},
        {"name": "ZDesigner ZT610-600dpi ZPL", "driver": "ZDesigner", "port": "USB001"},
    ]
    monkeypatch.setattr(label_util, "list_windows_printers", lambda: queues)
    assert label_util.find_label_printer(None) == "ZDesigner ZT610-600dpi ZPL"
    assert label_util.find_label_printer("") == "ZDesigner ZT610-600dpi ZPL"


def test_send_to_windows_printer_refuses_on_non_windows(monkeypatch) -> None:
    monkeypatch.setattr(label_util, "is_windows", lambda: False)
    with pytest.raises(RuntimeError, match="requires Windows"):
        label_util.send_to_windows_printer("anything", b"^XA^XZ")


def test_write_zpl_file_roundtrips(tmp_path: Path) -> None:
    payload = build_zpl("HB670EE00000001A")
    out = label_util.write_zpl_file(tmp_path / "sub" / "label.zpl", payload)
    assert out.read_bytes() == payload


# ---------------------------------------------------------------------------
# Nameplate (模版一) P/N lookup
# ---------------------------------------------------------------------------


def test_lookup_pn_table_covers_all_us_refurb_sku(monkeypatch) -> None:
    # NAMEPLATE_PN_TABLE ships empty in the public repo (real SKU data is
    # internal/proprietary); install a synthetic table here to exercise the
    # lookup logic itself.
    monkeypatch.setitem(label_util.NAMEPLATE_PN_TABLE, ("2800", "A"), "00000")
    monkeypatch.setitem(label_util.NAMEPLATE_PN_TABLE, ("2800", "B"), "00001")
    monkeypatch.setitem(label_util.NAMEPLATE_PN_TABLE, ("4800", "A"), "00002")
    monkeypatch.setitem(label_util.NAMEPLATE_PN_TABLE, ("4800", "B"), "00003")
    monkeypatch.setitem(label_util.NAMEPLATE_PN_TABLE, ("4800Plus", "A"), "00004")
    monkeypatch.setitem(label_util.NAMEPLATE_PN_TABLE, ("4800Plus", "B"), "00005")
    assert label_util.lookup_pn("2800", "A") == "00000"
    assert label_util.lookup_pn("2800", "B") == "00001"
    assert label_util.lookup_pn("4800", "A") == "00002"
    assert label_util.lookup_pn("4800", "B") == "00003"
    assert label_util.lookup_pn("4800Plus", "A") == "00004"
    assert label_util.lookup_pn("4800Plus", "B") == "00005"


def test_lookup_pn_normalizes_grade_case(monkeypatch) -> None:
    monkeypatch.setitem(label_util.NAMEPLATE_PN_TABLE, ("2800", "A"), "00000")
    monkeypatch.setitem(label_util.NAMEPLATE_PN_TABLE, ("2800", "B"), "00001")
    assert label_util.lookup_pn("2800", "a") == "00000"
    assert label_util.lookup_pn("2800", " b ") == "00001"


def test_lookup_pn_returns_none_when_anything_missing() -> None:
    assert label_util.lookup_pn(None, "A") is None
    assert label_util.lookup_pn("2800", None) is None
    assert label_util.lookup_pn("9999", "A") is None
    assert label_util.lookup_pn("2800", "C") is None


def test_lookup_ean13_table_covers_us_refurb_skus(monkeypatch) -> None:
    # NAMEPLATE_EAN13_TABLE ships empty in the public repo; install a
    # synthetic table here to exercise the lookup logic itself.
    monkeypatch.setitem(label_util.NAMEPLATE_EAN13_TABLE, ("2800", "A"), "6900000000000")
    monkeypatch.setitem(label_util.NAMEPLATE_EAN13_TABLE, ("2800", "B"), "6900000000001")
    monkeypatch.setitem(label_util.NAMEPLATE_EAN13_TABLE, ("4800", "A"), "6900000000002")
    monkeypatch.setitem(label_util.NAMEPLATE_EAN13_TABLE, ("4800", "B"), "6900000000003")
    monkeypatch.setitem(label_util.NAMEPLATE_EAN13_TABLE, ("4800Plus", "A"), "6900000000004")
    monkeypatch.setitem(label_util.NAMEPLATE_EAN13_TABLE, ("4800Plus", "B"), "6900000000005")
    assert label_util.lookup_ean13("2800", "A") == "6900000000000"
    assert label_util.lookup_ean13("2800", "B") == "6900000000001"
    assert label_util.lookup_ean13("4800", "A") == "6900000000002"
    assert label_util.lookup_ean13("4800", "B") == "6900000000003"
    assert label_util.lookup_ean13("4800Plus", "A") == "6900000000004"
    assert label_util.lookup_ean13("4800Plus", "B") == "6900000000005"


def test_lookup_ean13_returns_none_for_unknown_inputs() -> None:
    assert label_util.lookup_ean13("9999", "B") is None
    assert label_util.lookup_ean13(None, "A") is None
    assert label_util.lookup_ean13("2800", None) is None
    assert label_util.lookup_ean13("2800", "C") is None


def test_build_ean13_zpl_emits_native_ean_and_label_dims() -> None:
    zpl = label_util.build_ean13_zpl(
        "4800Plus", "00004", "6900000000004", dpi=203, quantity=2
    ).decode("utf-8")
    # Canvas: 50×30mm at 203dpi → 400×240 dots
    assert "^PW400" in zpl
    assert "^LL240" in zpl
    # Native ^BE barcode; ZPL recomputes the check digit so we strip the 13th.
    assert "^BEN," in zpl
    assert "^FD690000000000^FS" in zpl
    # Two ^GFA bitmaps for the header + subtitle.
    assert zpl.count("^GFA,") == 2
    assert "^PQ2,0,1,Y" in zpl


def test_build_ean13_zpl_rejects_bad_ean_length() -> None:
    with pytest.raises(ValueError, match="12 or 13 digits"):
        label_util.build_ean13_zpl("4800", "00002", "1234567")
    with pytest.raises(ValueError, match="requires both pn and ean13"):
        label_util.build_ean13_zpl("4800", "", "6900000000002")


def test_build_nameplate_zpl_emits_qr_and_two_text_blocks() -> None:
    zpl = label_util.build_nameplate_zpl(
        "DB670JJ00000001A", "000000", dpi=600
    ).decode("utf-8")
    # QR encoded via native ^BQ
    assert "^BQN,2," in zpl
    # P/N and SN rendered as separate bitmaps for left/right alignment
    assert zpl.count("^GFA,") >= 3  # 1 barcode + 2 text bitmaps
    # The encoded QR payload follows the URL template
    assert 'QA,https://nas.ugreen.com/download?qr={"t":1,"data":{"sn":"DB670JJ00000001A"}}' in zpl
