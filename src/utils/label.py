"""SN label generation for Zebra ZPL printers.

Spec (from 绿联DXP2800 整机标贴通用模版 模版二):
- Label: 42mm × 25mm, white background / black ink
- Barcode print area: 38mm × 14mm
- Margins: 2mm left/right, 5.5mm top, 4.5mm bottom
- Barcode: Code 128, height 12mm
- SN text: below barcode, ~5pt 汉仪康黑 (fallback to printer default font)

Public API:
- ``build_zpl(sn, dpi=203, ...) -> bytes``: pure ZPL II command bytes for raw send
- ``render_preview_png(sn, output_path, ...) -> Path``: PNG preview via python-barcode+Pillow
- ``list_windows_printers() -> list[dict]``: enumerate printer queues (Windows only)
- ``find_label_printer(...) -> str | None``: pick a Zebra-ish printer heuristically
- ``send_to_windows_printer(printer_name, data) -> None``: raw bytes to print queue
"""

from __future__ import annotations

import io
import sys
from dataclasses import dataclass
from pathlib import Path

from .logger import logger
from .sn import normalize_sn


# Label geometry (millimetres) — keep matching PDF spec 模版二.
LABEL_WIDTH_MM = 42.0
LABEL_HEIGHT_MM = 25.0
PRINT_AREA_WIDTH_MM = 38.0
PRINT_AREA_HEIGHT_MM = 14.0
LEFT_MARGIN_MM = 2.0
TOP_MARGIN_MM = 5.5
BARCODE_HEIGHT_MM = 12.0
TEXT_GAP_MM = 0.4  # gap between barcode bottom and SN text top
# Spec says 5pt (~1.76mm). Per factory readability feedback we ship at ~7pt
# (2.5mm em); the spec's 14mm "barcode print area" still holds for the bars,
# and the larger text extends into the 5.5mm bottom margin (~4mm to spare).
TEXT_HEIGHT_MM = 2.5
# Extra dots inserted between glyphs to loosen the SN's character cadence
# (~0.25mm at 203dpi). Purely a readability tweak.
TEXT_LETTER_SPACING_MM = 0.25
# Per spec sample (模版二): text below barcode is "SN：<sn value>".
# Spec image uses a full-width colon (U+FF1A); we emit an ASCII colon + space
# because the default Zebra A0 font cannot render CJK punctuation without
# uploading a CJK font to the printer first (^DU/^DG/^CW). Visually identical
# enough for the small printed size, and safe across all Zebra printers.
TEXT_PREFIX = "SN: "

DEFAULT_DPI = 203  # Most Zebra desktop printers (ZD220/ZD420/GK420t) are 203 dpi.

# ---------------------------------------------------------------------------
# 模版一 nameplate label spec (machine body sticker, 83.7×36.7mm)
# ---------------------------------------------------------------------------
NAMEPLATE_WIDTH_MM = 83.7
NAMEPLATE_HEIGHT_MM = 36.7
NAMEPLATE_DPI = 600  # Zebra ZT610 industrial 600dpi

# Two factory-print regions on the pre-printed supplier nameplate:
# - QR square: 22×22mm reserved with a 20×20mm dashed print area inside.
# - Barcode strip: 41×8.5mm reserved with a 38×6mm dashed print area (Code 128
#   barcode 4mm tall stacked on top of "P/N: ... SN: ..." text).
NAMEPLATE_QR_BOX_MM = 20.0           # red-dashed inner box for the QR
NAMEPLATE_STRIP_WIDTH_MM = 38.0      # red-dashed inner strip width
NAMEPLATE_STRIP_HEIGHT_MM = 6.0      # red-dashed inner strip height
NAMEPLATE_BARCODE_HEIGHT_MM = 4.0    # spec
NAMEPLATE_TEXT_HEIGHT_MM = 1.76      # 5pt 汉仪旗黑-55S spec

# Placement of the two reserved regions on the 83.7×36.7mm canvas.
# Second ZT610 trial: QR overshot, pulled back; strip's Y was right, X
# nudged right a touch; text moved to bold + 2.5mm em for legibility.
NAMEPLATE_QR_TOP_LEFT_MM = (65.0, 5.0)     # (x, y) of QR box top-left
NAMEPLATE_STRIP_TOP_LEFT_MM = (6.0, 25.0)  # (x, y) of strip top-left
NAMEPLATE_TEXT_EM_MM = 2.0  # text em-height; 2.5 overran the 38mm strip width

# QR payload template — SN gets substituted in. Spec sample literal:
# https://nas.ugreen.com/download?qr={"t":1,"data":{"sn":"DB670JJ00000001A"}}
NAMEPLATE_QR_URL_TEMPLATE = (
    'https://nas.ugreen.com/download?qr={{"t":1,"data":{{"sn":"{sn}"}}}}'
)

# ---------------------------------------------------------------------------
# 模版五 EAN-13 / "69 码" label spec (carton + middle-box, 50×30mm)
# ---------------------------------------------------------------------------
EAN13_WIDTH_MM = 50.0
EAN13_HEIGHT_MM = 30.0
EAN13_DPI = 203  # Deli DL-888T thermal printer is 203 dpi.

# Layout taken from "2800 69码 50x30mm用.ddl" (the supplier-shipped DLabel
# template). Three drawobjs, top to bottom: bold header, regular subtitle,
# EAN-13 barcode + human-readable interpretation line.
EAN13_HEADER_TOP_LEFT_MM = (4.44, 4.56)
EAN13_HEADER_SIZE_MM = (42.76, 3.60)
EAN13_HEADER_FONT_PT = 7  # bold

EAN13_SUBTITLE_TEXT = "NASync Network Attached Storage"
EAN13_SUBTITLE_TOP_LEFT_MM = (7.71, 8.01)
EAN13_SUBTITLE_SIZE_MM = (34.29, 2.96)
EAN13_SUBTITLE_FONT_PT = 6  # regular, centered

EAN13_BARCODE_TOP_LEFT_MM = (4.06, 11.61)
EAN13_BARCODE_SIZE_MM = (38.00, 16.00)  # combined barcode + HRI line
EAN13_BARCODE_BAR_HEIGHT_MM = 12.0     # bars only — HRI gets the remaining 4mm
EAN13_BARCODE_HRI_FONT_PT = 8

EAN13_HEADER_TEMPLATE = "Model: DXP {model_display} | P/N: {pn}"
EAN13_MODEL_DISPLAY = {
    "2800": "2800",
    "4800": "4800",
    "4800Plus": "4800 Plus",
}

# P/N lookup for the US-region refurbished SKU lineup (the only stock currently
# in factory test). A class = refurb level L0, B class = L1.
# 2800 is hardcoded — not in the 内部 NAS SKU 汇总 spreadsheet.
# 4800 / 4800Plus values match rows 67/68/97/98 of the 海外 sheet.
NAMEPLATE_PN_TABLE: dict[tuple[str, str], str] = {
    ("2800", "A"): "00000",
    ("2800", "B"): "00001",
    ("4800", "A"): "00002",
    ("4800", "B"): "00003",
    ("4800Plus", "A"): "00004",
    ("4800Plus", "B"): "00005",
}


def lookup_pn(model_key: str | None, grade: str | None) -> str | None:
    """Return the US-region refurb P/N for (model_key, grade), or None.

    ``model_key`` should be the value returned by ``model_key_from_sn``
    (e.g. "2800", "4800", "4800Plus"). ``grade`` is "A" or "B".
    """
    if not model_key or not grade:
        return None
    return NAMEPLATE_PN_TABLE.get((str(model_key), str(grade).strip().upper()))


# 13-digit EAN-13 (a.k.a. "69 码", as Chinese factory parlance reflects the
# 690-699 country code prefix) for the same SKU lineup. 4800 / 4800Plus
# sourced from the 海外 sheet column G; 2800 supplied directly by the
# factory (not in the spreadsheet).
NAMEPLATE_EAN13_TABLE: dict[tuple[str, str], str] = {
    ("2800", "A"): "6900000000000",
    ("2800", "B"): "6900000000001",
    ("4800", "A"): "6900000000002",
    ("4800", "B"): "6900000000003",
    ("4800Plus", "A"): "6900000000004",
    ("4800Plus", "B"): "6900000000005",
}


def lookup_ean13(model_key: str | None, grade: str | None) -> str | None:
    if not model_key or not grade:
        return None
    return NAMEPLATE_EAN13_TABLE.get((str(model_key), str(grade).strip().upper()))

# Heuristic substrings to recognize a Zebra/ZPL printer queue name on Windows.
_ZEBRA_NAME_HINTS = (
    "zebra",
    "zdesigner",
    "zpl",
    "gk420",
    "gx420",
    "zd220",
    "zd230",
    "zd420",
    "zd620",
    "lp 2824",
    "tlp 2824",
    "gt800",
    "gc420",
)


def _mm_to_dots(mm: float, dpi: int) -> int:
    return round(mm * dpi / 25.4)


def _fit_code128_module_width(sn_len: int, max_dots: int, prefer: int = 2) -> int:
    """Pick the largest module width in dots so the Code 128 stays ≤ ``max_dots``.

    Code 128 subset B encodes one character per 11 modules, plus start (11),
    checksum (11) and stop (13) modules — 35 overhead total.
    """
    modules = max(11 * sn_len + 35, 1)
    max_module = max_dots // modules
    if max_module < 1:
        logger.warning(
            f"SN of {sn_len} chars overflows the 38mm print area even at "
            f"1-dot modules; barcode will exceed spec width."
        )
        return 1
    return max(1, min(prefer, max_module))


def _render_code128_bitmap(sn: str, target_w: int, target_h: int):
    """Return a 1-bit Pillow Image of Code 128 sized exactly target_w × target_h.

    Crops out the padding rows python-barcode adds above/below the actual bars
    before downscaling, so the resulting bitmap has no stray horizontal lines.
    """
    import barcode  # type: ignore[import-not-found]
    from barcode.writer import ImageWriter  # type: ignore[import-not-found]
    from PIL import Image, ImageChops  # type: ignore[import-not-found]

    writer = ImageWriter()
    code = barcode.get("code128", sn, writer=writer)
    hi = code.render(
        writer_options={
            "module_height": 12.0,
            "module_width": 0.5,
            "quiet_zone": 0,
            "write_text": False,
            "background": "white",
            "foreground": "black",
            "dpi": 1200,
        }
    )
    gray = hi.convert("L")
    # Crop to the actual ink — ImageWriter still reserves header/footer
    # padding for the (suppressed) human-readable line, which would show up
    # as faint horizontal artifacts after threshold.
    inverted = ImageChops.invert(gray)
    bbox = inverted.getbbox()
    if bbox:
        gray = gray.crop(bbox)
    resized = gray.resize((target_w, target_h), Image.LANCZOS)
    return resized.point(lambda v: 0 if v < 128 else 255, mode="1")


def _image_to_gfa(img, origin_x: int, origin_y: int) -> str:
    """Encode a 1-bit Pillow Image as a Zebra ^GFA graphic field.

    Pillow's mode "1" packs ``1`` for white pixels and ``0`` for black; Zebra
    ^GFA does the opposite (``1`` prints a dot, ``0`` leaves the paper blank).
    We invert the byte stream so the visual intent of the Pillow image matches
    the printed result, then zero out the unused row-padding bits — otherwise
    those bits flip to ``1`` and print as a thin black sliver at the right
    edge of every row.
    """
    if img.mode != "1":
        img = img.convert("1")
    w, h = img.size
    bytes_per_row = (w + 7) // 8
    total = bytes_per_row * h
    padding_bits = bytes_per_row * 8 - w
    last_byte_mask = (0xFF << padding_bits) & 0xFF if padding_bits else 0xFF

    inverted = bytearray(b ^ 0xFF for b in img.tobytes())
    if padding_bits:
        for row in range(h):
            tail = (row + 1) * bytes_per_row - 1
            inverted[tail] &= last_byte_mask

    hex_data = bytes(inverted).hex().upper()
    return (
        f"^FO{origin_x},{origin_y}"
        f"^GFA,{total},{total},{bytes_per_row},{hex_data}^FS"
    )


def _render_barcode_gfa(
    sn: str,
    *,
    origin_x: int,
    origin_y: int,
    target_w_dots: int,
    target_h_dots: int,
) -> str:
    """Render Code 128 as a Zebra ^GFA bitmap exactly target_w × target_h dots."""
    try:
        img = _render_code128_bitmap(sn, target_w_dots, target_h_dots)
    except ImportError as exc:
        raise ImportError(
            "Spec-exact barcode (38mm fill) requires 'python-barcode' and "
            "'Pillow'. Install with: pip install python-barcode Pillow"
        ) from exc
    return _image_to_gfa(img, origin_x, origin_y)


def _render_text_gfa(
    text: str,
    *,
    origin_x: int,
    origin_y: int,
    max_w_dots: int,
    height_dots: int,
    letter_spacing_dots: int = 0,
    font_path: str | None = None,
) -> str:
    """Render bold SN text as a ^GFA bitmap using a bold TTF font."""
    try:
        from PIL import Image, ImageDraw  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            "Bold SN text requires 'Pillow'. Install with: pip install Pillow"
        ) from exc

    font = _load_bold_text_font(font_path, height_dots)
    # Measure once on a throwaway canvas to size the final bitmap.
    probe = Image.new("L", (max_w_dots, height_dots * 3), 255)
    pdraw = ImageDraw.Draw(probe)
    advances, ascent_offset, total_h = _measure_text_with_spacing(
        pdraw, text, font, letter_spacing_dots
    )
    total_w = sum(advances) if advances else 1
    canvas_w = min(max_w_dots, total_w + 2)
    canvas_h = total_h + 2

    img = Image.new("L", (canvas_w, canvas_h), 255)
    draw = ImageDraw.Draw(img)
    _draw_text_with_spacing(draw, text, font, advances, ascent_offset)
    one_bit = img.point(lambda v: 0 if v < 200 else 255, mode="1")
    return _image_to_gfa(one_bit, origin_x, origin_y)


def _measure_text_with_spacing(draw, text, font, letter_spacing_dots: int):
    """Return (per-glyph advance widths, top offset, total height).

    Per-glyph advance includes the glyph's natural advance plus the configured
    letter spacing. The top offset is the value we'll pass as ``-bbox[1]`` when
    drawing so glyphs sit flush to the top of the canvas.
    """
    if not text:
        return [], 0, 1
    advances: list[int] = []
    overall_top = None
    overall_bottom = 0
    for i, ch in enumerate(text):
        bbox = draw.textbbox((0, 0), ch, font=font)
        # bbox = (left, top, right, bottom); some glyphs have negative top.
        adv = (bbox[2] - bbox[0]) if bbox[2] > bbox[0] else font.getlength(ch)
        adv = int(round(adv)) + (letter_spacing_dots if i < len(text) - 1 else 0)
        advances.append(max(1, adv))
        overall_top = bbox[1] if overall_top is None else min(overall_top, bbox[1])
        overall_bottom = max(overall_bottom, bbox[3])
    ascent_offset = -(overall_top or 0)
    total_h = max(1, overall_bottom - (overall_top or 0))
    return advances, ascent_offset, total_h


def _draw_text_with_spacing(draw, text, font, advances, ascent_offset) -> None:
    x = 0
    for ch, adv in zip(text, advances):
        draw.text((x, ascent_offset), ch, fill=0, font=font)
        x += adv


def _load_bold_text_font(font_path: str | None, size_px: int, *, bold: bool = False):
    """Load a TrueType font, optionally bold-weight, from cross-platform paths.

    ``bold=False`` is the default and matches the SN sticker which the factory
    found too heavy in bold. ``bold=True`` is used by the nameplate.
    """
    from PIL import ImageFont  # type: ignore[import-not-found]

    candidates: list[str] = []
    if font_path:
        candidates.append(font_path)
    if bold:
        candidates.extend(
            [
                "HYKangHei45S-Bold.ttf",
                "HanyiKangHei45S-Bold.ttf",
                "C:/Windows/Fonts/msyhbd.ttc",  # Microsoft YaHei Bold
                "C:/Windows/Fonts/msyhbd.ttf",
                "C:/Windows/Fonts/simhei.ttf",
                "C:/Windows/Fonts/arialbd.ttf",  # Arial Bold
                # macOS CJK — PIL can't open PingFang.ttc, so prefer these for
                # Chinese glyphs (warehouse name etc.) in off-printer previews.
                "/System/Library/Fonts/STHeiti Medium.ttc",
                "/System/Library/Fonts/Hiragino Sans GB.ttc",
                "/System/Library/Fonts/PingFang.ttc",
                "/System/Library/Fonts/Helvetica.ttc",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            ]
        )
    else:
        candidates.extend(
            [
                "HYKangHei45S.ttf",
                "HanyiKangHei45S.ttf",
                "C:/Windows/Fonts/msyh.ttc",   # Microsoft YaHei Regular
                "C:/Windows/Fonts/msyh.ttf",
                "C:/Windows/Fonts/arial.ttf",  # Arial Regular
                "/System/Library/Fonts/STHeiti Light.ttc",
                "/System/Library/Fonts/Hiragino Sans GB.ttc",
                "/System/Library/Fonts/PingFang.ttc",
                "/System/Library/Fonts/Helvetica.ttc",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            ]
        )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size_px)
        except (OSError, ValueError):
            continue
    return ImageFont.load_default()


def _render_text_image(
    text: str,
    height_dots: int,
    *,
    bold: bool = False,
    letter_spacing_dots: int = 0,
    font_path: str | None = None,
):
    """Render ``text`` as a 1-bit Pillow Image. Caller positions it on the canvas."""
    from PIL import Image, ImageDraw  # type: ignore[import-not-found]

    font = _load_bold_text_font(font_path, height_dots, bold=bold)
    probe = Image.new("L", (max(4096, height_dots * len(text) * 2), height_dots * 3), 255)
    pdraw = ImageDraw.Draw(probe)
    advances, ascent_offset, total_h = _measure_text_with_spacing(
        pdraw, text, font, letter_spacing_dots
    )
    total_w = sum(advances) if advances else 1
    canvas_w = total_w + 2
    canvas_h = total_h + 2

    img = Image.new("L", (canvas_w, canvas_h), 255)
    draw = ImageDraw.Draw(img)
    _draw_text_with_spacing(draw, text, font, advances, ascent_offset)
    return img.point(lambda v: 0 if v < 200 else 255, mode="1")


def build_zpl(
    sn: str,
    dpi: int = DEFAULT_DPI,
    *,
    quantity: int = 1,
    darkness: int | None = None,
    text_height_mm: float = TEXT_HEIGHT_MM,
) -> bytes:
    """Generate ZPL II command bytes for one SN label.

    ``darkness`` (~MD) is optional override; leave ``None`` to use printer's
    configured default. ``quantity`` controls ^PQ copies.
    ``text_height_mm`` overrides the SN text em-height (default ~5pt).
    """
    normalized = normalize_sn(sn)
    if not normalized:
        raise ValueError(f"SN is empty after normalization: {sn!r}")
    if len(normalized) > 80:
        raise ValueError(f"SN too long for Code 128 label: {normalized!r}")

    label_w = _mm_to_dots(LABEL_WIDTH_MM, dpi)
    label_h = _mm_to_dots(LABEL_HEIGHT_MM, dpi)
    left = _mm_to_dots(LEFT_MARGIN_MM, dpi)
    top = _mm_to_dots(TOP_MARGIN_MM, dpi)
    bar_h = _mm_to_dots(BARCODE_HEIGHT_MM, dpi)
    text_y = top + bar_h + _mm_to_dots(TEXT_GAP_MM, dpi)
    text_h = max(_mm_to_dots(text_height_mm, dpi), 10)  # font dot height
    text_w = max(round(text_h * 0.55), 6)
    print_area_w = _mm_to_dots(PRINT_AREA_WIDTH_MM, dpi)

    parts: list[str] = [
        "^XA",
        "^CI28",  # UTF-8 encoding (future-proof, no-op for ASCII SN)
        f"^PW{label_w}",
        f"^LL{label_h}",
        "^LH0,0",
        "^LS0",
        "^PON",
    ]
    if darkness is not None:
        parts.append(f"~SD{max(0, min(30, int(darkness))):02d}")

    # Per 模版二 spec: barcode + SN row must fill exactly 38mm × 14mm.
    # Native ZPL ^BC at integer module width cannot hit 38mm for typical
    # 16-char SNs at 203 dpi (jumps from 26.4mm @ module=1 to 52.8mm @ module=2).
    # We render Code 128 as a high-resolution bitmap, downscale to exactly the
    # spec dimensions, and embed via ^GFA.
    parts.append(
        _render_barcode_gfa(
            normalized,
            origin_x=left,
            origin_y=top,
            target_w_dots=print_area_w,
            target_h_dots=bar_h,
        )
    )
    # SN text under the barcode. Per spec sample: "SN：<sn>", left-aligned,
    # inside the 38mm print area. We render via Pillow with a bold TTF font
    # so the stroke weight matches 汉仪康黑 45S better than ZPL's default A0.
    sn_text = f"{TEXT_PREFIX}{normalized}"
    parts.append(
        _render_text_gfa(
            sn_text,
            origin_x=left,
            origin_y=text_y,
            max_w_dots=print_area_w,
            height_dots=text_h,
            letter_spacing_dots=_mm_to_dots(TEXT_LETTER_SPACING_MM, dpi),
        )
    )
    parts.append(f"^PQ{max(1, int(quantity))},0,1,Y")
    parts.append("^XZ")

    return ("\n".join(parts) + "\n").encode("utf-8")


def build_nameplate_zpl(
    sn: str,
    pn: str,
    *,
    dpi: int = NAMEPLATE_DPI,
    quantity: int = 1,
    darkness: int | None = None,
    qr_top_left_mm: tuple[float, float] = NAMEPLATE_QR_TOP_LEFT_MM,
    strip_top_left_mm: tuple[float, float] = NAMEPLATE_STRIP_TOP_LEFT_MM,
) -> bytes:
    """Generate ZPL for the 模版一 nameplate (machine body sticker).

    The nameplate stock is pre-printed by the supplier with the UGREEN logo,
    compliance text, FCC/TUV/HDMI marks and two reserved white regions. We
    only fill those two regions:

    - QR code in a 22×22mm reserved square (20×20mm inner print area),
      payload: ``https://nas.ugreen.com/download?qr={"t":1,"data":{"sn":"<SN>"}}``
    - Code 128 barcode (height 4mm, encodes SN only) + text line
      ``P/N: <pn>    SN: <sn>`` in a 41×8.5mm reserved strip (38×6mm inner).

    ``qr_top_left_mm`` and ``strip_top_left_mm`` let callers nudge each
    region's position without recompiling; useful for matching different
    supplier stock revisions.
    """
    normalized = normalize_sn(sn)
    if not normalized:
        raise ValueError(f"SN is empty after normalization: {sn!r}")
    if len(normalized) > 80:
        raise ValueError(f"SN too long for Code 128 label: {normalized!r}")

    pn_clean = (pn or "").strip()
    if not pn_clean:
        raise ValueError("P/N must be supplied (use a placeholder for layout tests)")

    label_w = _mm_to_dots(NAMEPLATE_WIDTH_MM, dpi)
    label_h = _mm_to_dots(NAMEPLATE_HEIGHT_MM, dpi)

    parts: list[str] = [
        "^XA",
        "^CI28",
        f"^PW{label_w}",
        f"^LL{label_h}",
        "^LH0,0",
        "^LS0",
        "^PON",
    ]
    if darkness is not None:
        parts.append(f"~SD{max(0, min(30, int(darkness))):02d}")

    # --- QR code in the 20×20mm inner print area --------------------------
    qr_x_mm, qr_y_mm = qr_top_left_mm
    qr_payload = NAMEPLATE_QR_URL_TEMPLATE.format(sn=normalized)
    qr_box_dots = _mm_to_dots(NAMEPLATE_QR_BOX_MM, dpi)
    # Estimate QR module count for our URL length: 71-ish chars in byte mode
    # → version 4 (33×33). Magnification = floor(box_dots / 33).
    qr_modules = 33
    qr_mag = max(1, min(10, qr_box_dots // qr_modules))
    parts.extend(
        [
            f"^FO{_mm_to_dots(qr_x_mm, dpi)},{_mm_to_dots(qr_y_mm, dpi)}",
            # ^BQa,b,c,d,e: orientation N, model 2, magnification, error Q, mask 7
            f"^BQN,2,{qr_mag},Q,7",
            # ^FDQA,<text>  Q = error correction Q, A = automatic encoding mode
            f"^FDQA,{qr_payload}^FS",
        ]
    )

    # --- Barcode + P/N+SN text strip in the 38×6mm inner area --------------
    strip_x_mm, strip_y_mm = strip_top_left_mm
    strip_x_dots = _mm_to_dots(strip_x_mm, dpi)
    strip_y_dots = _mm_to_dots(strip_y_mm, dpi)
    strip_w_dots = _mm_to_dots(NAMEPLATE_STRIP_WIDTH_MM, dpi)
    bar_h_dots = _mm_to_dots(NAMEPLATE_BARCODE_HEIGHT_MM, dpi)
    text_h_dots = max(_mm_to_dots(NAMEPLATE_TEXT_EM_MM, dpi), 12)

    # Code 128 barcode (encodes SN only).
    parts.append(
        _render_barcode_gfa(
            normalized,
            origin_x=strip_x_dots,
            origin_y=strip_y_dots,
            target_w_dots=strip_w_dots,
            target_h_dots=bar_h_dots,
        )
    )
    # Bold P/N flush left + SN flush right, rendered as bitmaps so we control
    # font weight/spacing precisely. Spec font 汉仪旗黑-55S isn't on the printer
    # so we fall back to MS YaHei Bold on Windows.
    text_y_dots = strip_y_dots + bar_h_dots + _mm_to_dots(0.3, dpi)
    pn_text = f"P/N: {pn_clean}"
    sn_text = f"SN: {normalized}"
    pn_img = _render_text_image(pn_text, text_h_dots, bold=True)
    sn_img = _render_text_image(sn_text, text_h_dots, bold=True)
    parts.append(_image_to_gfa(pn_img, strip_x_dots, text_y_dots))
    sn_right_edge = strip_x_dots + strip_w_dots
    sn_x_dots = max(strip_x_dots, sn_right_edge - sn_img.width)
    parts.append(_image_to_gfa(sn_img, sn_x_dots, text_y_dots))

    parts.append(f"^PQ{max(1, int(quantity))},0,1,Y")
    parts.append("^XZ")

    return ("\n".join(parts) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# 模版六 周转箱标贴 (turnover-box / carton label) — 102×152mm, ZD888 ZPL.
# One label per box of 4 machines (printed in 2 copies). Layout per spec page 06:
#   PID (=P/N) big, QR top-right, QTY 4 PCS, PO, production date, a 2×2 grid of
#   four Code-128 SN barcodes, the receiving warehouse, and a remarks line.
# The QR payload is  SN*{PID}*{QTY}*{PO}*{DATE}*{seq}  — it does NOT contain the
# serial numbers (those live only in the four Code-128 barcodes). {seq} is a
# per-day running counter ("一天出了多少张单"), 3-digit zero-padded, reset daily.
# ---------------------------------------------------------------------------
CARTON_WIDTH_MM = 102.0
CARTON_HEIGHT_MM = 152.0
CARTON_DPI = 203
CARTON_QTY = 4                       # machines per box (fixed per spec)
CARTON_SN_COUNT = 4
CARTON_QR_MM = 20.0
CARTON_PO_DEFAULT = "XXXXXXXXXXX"    # 11-X placeholder until real POs are wired
CARTON_WAREHOUSE_DEFAULT = "收料仓"
CARTON_REMARK_LABEL = "备注:"
# Operators paste 4 SNs into the SN box separated by any of these (EN/CN comma,
# EN/CN semicolon) or whitespace.
CARTON_SN_SEPARATORS = ",;，；、 \t\r\n"


def carton_qr_payload(
    pid: str, po: str, prod_date: str, seq: int, *, qty: int = CARTON_QTY
) -> str:
    """QR data string for 模版六: ``SN*{PID}*{QTY}*{PO}*{DATE}*{seq:03d}``.

    Note ``SN`` here is a literal tag, not a serial number — the QR carries no
    serials. ``seq`` is the day's running count, zero-padded to 3 digits.
    """
    return f"SN*{pid}*{int(qty)}*{po}*{prod_date}*{int(seq):03d}"


def split_carton_sns(raw: str) -> list[str]:
    """Split a free-form SN string (EN/CN comma/semicolon/whitespace) into a
    normalized, de-duplicated list, order preserved."""
    import re

    out: list[str] = []
    for tok in re.split(f"[{re.escape(CARTON_SN_SEPARATORS)}]+", str(raw or "")):
        sn = normalize_sn(tok)
        if sn and sn not in out:
            out.append(sn)
    return out


def _carton_state_paths(data_dir):
    """Return (seq_file, log_file) under ``data_dir``, creating the dir."""
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d / "carton_seq.json", d / "carton_log.jsonl"


def peek_carton_seq(data_dir, *, today: str | None = None) -> int:
    """Return what the next turnover-box seq would be today (1-based) without
    committing it. Resets when the local date rolls over."""
    import json
    from datetime import date

    today = today or date.today().isoformat()
    seq_path, _ = _carton_state_paths(data_dir)
    try:
        st = json.loads(seq_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    base = int(st.get("count", 0)) if st.get("date") == today else 0
    return base + 1


def commit_carton_print(data_dir, record: dict, *, today: str | None = None) -> int:
    """Bump and persist today's count, append a台账 line to the log, return the
    committed seq. Call this only after a successful print so failures don't
    burn a number."""
    import json
    from datetime import date, datetime

    today = today or date.today().isoformat()
    seq_path, log_path = _carton_state_paths(data_dir)
    try:
        st = json.loads(seq_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    count = int(st.get("count", 0)) if st.get("date") == today else 0
    count += 1
    seq_path.write_text(
        json.dumps({"date": today, "count": count}, ensure_ascii=False),
        encoding="utf-8",
    )
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "date": today,
        "seq": count,
        **record,
    }
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return count


def build_turnover_box_zpl(
    model_key: str | None,
    grade: str | None,
    sns: list[str],
    *,
    seq: int,
    pid: str | None = None,
    po: str | None = None,
    prod_date: str | None = None,
    warehouse: str | None = None,
    dpi: int = CARTON_DPI,
    quantity: int = 2,
    darkness: int | None = None,
) -> bytes:
    """Generate ZPL for the 模版六 周转箱 (turnover-box) label, 102×152mm.

    ``pid`` defaults to ``lookup_pn(model_key, grade)`` so the PID, the QR and
    the model never drift; ``po`` / ``prod_date`` / ``warehouse`` default to the
    11-X placeholder, today, and 收料仓. ``seq`` is the day's running count
    (the caller obtains it via :func:`peek_carton_seq` and persists it with
    :func:`commit_carton_print` only after a successful print).
    """
    from datetime import date

    sns = [normalize_sn(s) for s in (sns or []) if normalize_sn(s)]
    if len(sns) != CARTON_SN_COUNT:
        raise ValueError(
            f"turnover box label needs exactly {CARTON_SN_COUNT} SNs; got {len(sns)}"
        )
    pid = (pid or lookup_pn(model_key, grade) or "").strip()
    if not pid:
        raise ValueError(
            f"could not resolve PID/PN for model={model_key!r} grade={grade!r}"
        )
    po = (po if po is not None else CARTON_PO_DEFAULT).strip() or CARTON_PO_DEFAULT
    prod_date = prod_date or date.today().isoformat()
    warehouse = (warehouse or CARTON_WAREHOUSE_DEFAULT).strip() or CARTON_WAREHOUSE_DEFAULT
    payload = carton_qr_payload(pid, po, prod_date, seq)

    def mm(v: float) -> int:
        return _mm_to_dots(v, dpi)

    label_w = mm(CARTON_WIDTH_MM)
    label_h = mm(CARTON_HEIGHT_MM)

    parts: list[str] = [
        "^XA",
        "^CI28",
        f"^PW{label_w}",
        f"^LL{label_h}",
        "^LH0,0",
        "^LS0",
        "^PON",
    ]
    if darkness is not None:
        parts.append(f"~SD{max(0, min(30, int(darkness))):02d}")

    # --- table geometry (mm), measured from BarTender's official render ------
    L, R = 5.0, 97.0
    y_top = 1.8
    y_pid_qty = 24.9      # PID | QTY
    y_qty_po = 50.1       # QTY | PO  (also QR cell bottom)
    y_po_date = 60.6      # PO | DATE   (PO/DATE are thin rows)
    y_date_sn = 71.1      # DATE | SN
    y_sn_wh = 106.7       # SN | 收料仓  (SN block is one open cell — no inner grid)
    y_wh_rm = 123.5       # 收料仓 | 备注
    y_bottom = 148.1      # 备注 is a tall row
    x_qr_split = 60.0     # PID/QTY block | QR cell
    x_mid = (L + R) / 2.0
    sn_row_mid = (y_date_sn + y_sn_wh) / 2.0
    th_frame = max(8, mm(1.3))      # bold outer frame
    th = max(6, mm(0.9))            # internal dividers: slightly thinner than the frame

    def hline(y: float) -> None:
        parts.append(f"^FO{mm(L)},{mm(y)}^GB{mm(R - L)},{th},{th}^FS")

    def place(img, x: int, y: int) -> None:
        parts.append(_image_to_gfa(img, max(0, int(x)), max(0, int(y))))

    def row_label_value(label, value, y1, y2, label_h_mm, value_h_mm, suffix=None):
        # Vertically center the small label and the larger value on the row's
        # mid-line, flush left (matches the BarTender original).
        lab = _render_text_image(label, mm(label_h_mm), bold=True)
        val = _render_text_image(str(value), mm(value_h_mm), bold=True)
        cy = mm((y1 + y2) / 2.0)
        x = mm(L) + mm(3.0)
        place(lab, x, cy - lab.height // 2)
        x += lab.width + mm(2.5)
        place(val, x, cy - val.height // 2)
        if suffix:
            suf = _render_text_image(suffix, mm(label_h_mm), bold=True)
            place(suf, x + val.width + mm(2.0), cy - suf.height // 2)

    # bold outer frame
    parts.append(
        f"^FO{mm(L)},{mm(y_top)}^GB{mm(R - L)},{mm(y_bottom - y_top)},{th_frame}^FS"
    )
    # full-width horizontal dividers (from QTY|PO downward)
    for y in (y_qty_po, y_po_date, y_date_sn, y_sn_wh, y_wh_rm):
        hline(y)
    # PID|QTY divider spans ONLY the left block — the QR is one tall open cell,
    # so this line must NOT cross into it (that was the stray box under the QR).
    parts.append(
        f"^FO{mm(L)},{mm(y_pid_qty)}^GB{mm(x_qr_split - L)},{th},{th}^FS"
    )
    # vertical divider: PID/QTY block | QR cell
    parts.append(
        f"^FO{mm(x_qr_split)},{mm(y_top)}^GB{th},{mm(y_qty_po - y_top)},{th}^FS"
    )

    # --- PID / QTY / PO / DATE rows -----------------------------------------
    row_label_value("PID:", pid, y_top, y_pid_qty, 5.5, 9.0)
    row_label_value("QTY:", CARTON_QTY, y_pid_qty, y_qty_po, 5.5, 9.0, suffix="PCS")
    row_label_value("PO:", po, y_qty_po, y_po_date, 5.0, 6.0)
    row_label_value("DATE:", prod_date, y_po_date, y_date_sn, 5.0, 6.0)

    # --- QR (top-right, one open cell) + wrapped payload text under it -------
    qr_cell_w = mm(R - x_qr_split)
    qr_mag = 8                                       # QR magnification (~29mm)
    # The fixed 37-char payload encodes to a version-3 (29-module) QR; use that
    # exact width so the symbol centers in its cell.
    qr_px = 29 * qr_mag
    qr_x = mm(x_qr_split) + max(0, (qr_cell_w - qr_px) // 2)
    qr_y = mm(y_top) + mm(3.0)
    parts.extend(
        [
            f"^FO{qr_x},{qr_y}",
            f"^BQN,2,{qr_mag},Q,7",
            f"^FDQA,{payload}^FS",
        ]
    )
    # human-readable payload under the QR — bigger, wrapped to fit the cell width
    ds_h = max(16, mm(2.4))
    ds_max_w = qr_cell_w - mm(3.0)
    probe = _render_text_image(payload, ds_h, bold=False)
    if probe.width <= ds_max_w:
        ds_lines = [payload]
    else:
        n_lines = (probe.width + ds_max_w - 1) // ds_max_w
        per = (len(payload) + n_lines - 1) // n_lines
        ds_lines = [payload[i:i + per] for i in range(0, len(payload), per)]
    ty = qr_y + qr_px + mm(2.0)
    for ln in ds_lines:
        li = _render_text_image(ln, ds_h, bold=False)
        place(li, mm(x_qr_split) + max(0, (qr_cell_w - li.width) // 2), ty)
        ty += li.height + mm(0.4)

    # --- four Code-128 SN barcodes (open 2×2, no internal grid) -------------
    bar_w = mm(33.0)                # slightly narrower (per feedback)
    bar_h = mm(9.0)                 # slightly taller (per feedback)
    sn_text_h = max(13, mm(2.0))
    sn_cells = [
        (L, x_mid, y_date_sn, sn_row_mid),
        (x_mid, R, y_date_sn, sn_row_mid),
        (L, x_mid, sn_row_mid, y_sn_wh),
        (x_mid, R, sn_row_mid, y_sn_wh),
    ]
    for sn, (cx1, cx2, cy1, cy2) in zip(sns, sn_cells):
        center_x = mm((cx1 + cx2) / 2.0)
        bx = center_x - bar_w // 2
        by = mm(cy1) + mm(2.0)
        parts.append(
            _render_barcode_gfa(
                sn, origin_x=bx, origin_y=by, target_w_dots=bar_w, target_h_dots=bar_h
            )
        )
        txt = _render_text_image(f"S/N: {sn}", sn_text_h, bold=False)
        place(txt, center_x - txt.width // 2, by + bar_h + mm(0.6))

    # --- warehouse (centered) + remarks (centered, near top of the tall row) -
    wh_img = _render_text_image(warehouse, mm(7.0), bold=True)
    place(
        wh_img,
        mm(x_mid) - wh_img.width // 2,
        mm((y_sn_wh + y_wh_rm) / 2.0) - wh_img.height // 2,
    )
    rm_img = _render_text_image(CARTON_REMARK_LABEL, mm(5.0), bold=True)
    place(rm_img, mm(x_mid) - rm_img.width // 2, mm(y_wh_rm) + mm(3.5))

    parts.append(f"^PQ{max(1, int(quantity))},0,1,Y")
    parts.append("^XZ")
    return ("\n".join(parts) + "\n").encode("utf-8")


def build_ean13_zpl(
    model_key: str,
    pn: str,
    ean13: str,
    *,
    dpi: int = EAN13_DPI,
    quantity: int = 2,
    darkness: int | None = None,
) -> bytes:
    """Generate ZPL for the 模版五 EAN-13 (69 码) carton/middle-box label.

    50×30mm. Layout follows the supplier-shipped ``2800 69码 50x30mm用.ddl``
    DLabel template: bold header line on top, regular subtitle below it, and
    a Code EAN-13 barcode with HRI line at the bottom.
    """
    if not pn or not ean13:
        raise ValueError("build_ean13_zpl requires both pn and ean13")
    digits = "".join(ch for ch in str(ean13) if ch.isdigit())
    if len(digits) not in (12, 13):
        raise ValueError(
            f"EAN-13 input must be 12 or 13 digits; got {len(digits)} from {ean13!r}"
        )
    # ZPL's ^BE will recompute the check digit; pass the leading 12 digits.
    bar_data = digits[:12]
    model_display = EAN13_MODEL_DISPLAY.get(str(model_key), str(model_key))
    header_text = EAN13_HEADER_TEMPLATE.format(model_display=model_display, pn=pn)

    label_w = _mm_to_dots(EAN13_WIDTH_MM, dpi)
    label_h = _mm_to_dots(EAN13_HEIGHT_MM, dpi)

    parts: list[str] = [
        "^XA",
        "^CI28",
        f"^PW{label_w}",
        f"^LL{label_h}",
        "^LH0,0",
        "^LS0",
        "^PON",
    ]
    if darkness is not None:
        parts.append(f"~SD{max(0, min(30, int(darkness))):02d}")

    # Header — bold, left-aligned, rendered as a bitmap so the spec's bold
    # weight survives on printers without an uploaded CJK/Latin bold font.
    header_h_dots = max(_mm_to_dots(EAN13_HEADER_FONT_PT / 72 * 25.4 + 0.3, dpi), 12)
    header_img = _render_text_image(header_text, header_h_dots, bold=True)
    header_origin = (
        _mm_to_dots(EAN13_HEADER_TOP_LEFT_MM[0], dpi),
        _mm_to_dots(EAN13_HEADER_TOP_LEFT_MM[1], dpi),
    )
    parts.append(_image_to_gfa(header_img, header_origin[0], header_origin[1]))

    # Subtitle — regular weight, horizontally centered within the spec box.
    sub_h_dots = max(_mm_to_dots(EAN13_SUBTITLE_FONT_PT / 72 * 25.4 + 0.3, dpi), 10)
    sub_img = _render_text_image(EAN13_SUBTITLE_TEXT, sub_h_dots, bold=False)
    sub_box_left = _mm_to_dots(EAN13_SUBTITLE_TOP_LEFT_MM[0], dpi)
    sub_box_top = _mm_to_dots(EAN13_SUBTITLE_TOP_LEFT_MM[1], dpi)
    sub_box_w = _mm_to_dots(EAN13_SUBTITLE_SIZE_MM[0], dpi)
    sub_x = sub_box_left + max(0, (sub_box_w - sub_img.width) // 2)
    parts.append(_image_to_gfa(sub_img, sub_x, sub_box_top))

    # EAN-13 barcode + interpretation line. Module width is auto-fit so the
    # bars stay within the 38mm spec box. EAN-13 has a fixed 95 modules.
    barcode_x = _mm_to_dots(EAN13_BARCODE_TOP_LEFT_MM[0], dpi)
    barcode_y = _mm_to_dots(EAN13_BARCODE_TOP_LEFT_MM[1], dpi)
    barcode_w_dots = _mm_to_dots(EAN13_BARCODE_SIZE_MM[0], dpi)
    bar_h_dots = _mm_to_dots(EAN13_BARCODE_BAR_HEIGHT_MM, dpi)
    module = max(1, barcode_w_dots // 113)  # 95 bars + 18-module quiet zones
    parts.extend(
        [
            f"^FO{barcode_x},{barcode_y}",
            f"^BY{module},3,{bar_h_dots}",
            # ^BEorientation,height,print_interp_line,interp_line_above
            f"^BEN,{bar_h_dots},Y,N",
            f"^FD{bar_data}^FS",
        ]
    )

    parts.append(f"^PQ{max(1, int(quantity))},0,1,Y")
    parts.append("^XZ")
    return ("\n".join(parts) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# 模版五 EAN-13 (69 码) — Deli DL-888T (TSC engine, native TSPL, NOT ZPL)
# ---------------------------------------------------------------------------
# The Deli DL-888T uses Seagull's TSC driver; its firmware speaks TSPL, so raw
# ZPL (build_ean13_zpl) does not print on it. We drive it with TSPL instead:
# native ``BARCODE EAN13`` for scannable bars, and PIL-rendered ``BITMAP`` text
# for the header / subtitle / human-readable line — which lets us hit arbitrary
# font sizes and true bold (the built-in TSPL bitmap fonts can do neither).
#
# Tuned on a real Deli DL-888T @203dpi against factory feedback: header bigger +
# bold, subtitle slightly bigger, HRI digits bold, all horizontally centered.
EAN13_TSPL_TOP_MARGIN_MM = 2.7
EAN13_TSPL_HEADER_H_MM = 2.7        # bold
EAN13_TSPL_HEADER_SUB_GAP_MM = 0.8
EAN13_TSPL_SUBTITLE_H_MM = 2.0      # regular
EAN13_TSPL_SUB_BAR_GAP_MM = 1.3
EAN13_TSPL_BAR_H_MM = 11.3          # data-bar height (guards protrude below this)
EAN13_TSPL_HRI_H_MM = 2.5           # bold HRI digits, nestled under the guards
EAN13_TSPL_GUARD_EXT_EXTRA_MM = 0.4 # guard protrusion beyond the HRI digits
EAN13_TSPL_MODULE_MM = 0.375        # EAN-13 narrow module width

# EAN-13 quiet zones (in narrow modules); the left zone holds the first HRI digit.
EAN13_QUIET_LEFT_MODULES = 11
EAN13_QUIET_RIGHT_MODULES = 7

# TSPL ``BITMAP`` polarity is the OPPOSITE of ZPL ^GF: a 0 bit prints black, a 1
# bit leaves the paper white. Pillow mode "1" already packs 0=black/1=white, so
# bytes map directly (no inversion). Flip this only if a print comes out
# inverted (white-on-black).
_TSPL_BITMAP_BLACK_IS_ZERO = True


def _ean13_check_digit(digits12: str) -> str:
    """Standard EAN-13 modulo-10 check digit for the leading 12 digits."""
    total = sum((3 if i % 2 else 1) * int(c) for i, c in enumerate(digits12))
    return str((10 - total % 10) % 10)


# EAN-13 module encoding (95 modules total). The first digit is never drawn as
# bars; it selects the L/G parity pattern of the six left-hand digits. Right-hand
# digits always use the R (C) set. The three guard patterns (start 101, center
# 0 1010, end 101) are printed taller so they protrude below the data bars and
# frame the human-readable digits — the classic retail look.
_EAN13_L = {
    "0": "0001101", "1": "0011001", "2": "0010011", "3": "0111101", "4": "0100011",
    "5": "0110001", "6": "0101111", "7": "0111011", "8": "0110111", "9": "0001011",
}
_EAN13_G = {
    "0": "0100111", "1": "0110011", "2": "0011011", "3": "0100001", "4": "0011101",
    "5": "0111001", "6": "0000101", "7": "0010001", "8": "0001001", "9": "0010111",
}
_EAN13_R = {
    "0": "1110010", "1": "1100110", "2": "1101100", "3": "1000010", "4": "1011100",
    "5": "1001110", "6": "1010000", "7": "1000100", "8": "1001000", "9": "1110100",
}
_EAN13_PARITY = {
    "0": "LLLLLL", "1": "LLGLGG", "2": "LLGGLG", "3": "LLGGGL", "4": "LGLLGG",
    "5": "LGGLLG", "6": "LGGGLL", "7": "LGLGLG", "8": "LGLGGL", "9": "LGGLGL",
}
# Module index ranges occupied by the start/center/end guard bars.
_EAN13_GUARD_MODULES = set(range(0, 3)) | set(range(45, 50)) | set(range(92, 95))


def _ean13_binary(full13: str) -> str:
    """Return the 95-module 0/1 string for a full 13-digit EAN-13 number."""
    parity = _EAN13_PARITY[full13[0]]
    bits = "101"  # start guard
    for i, ch in enumerate(full13[1:7]):
        bits += (_EAN13_L if parity[i] == "L" else _EAN13_G)[ch]
    bits += "01010"  # center guard
    for ch in full13[7:13]:
        bits += _EAN13_R[ch]
    bits += "101"  # end guard
    return bits


def _render_ean13_full_bitmap(
    full13: str,
    narrow: int,
    bar_h: int,
    hri_h: int,
    guard_ext: int,
    *,
    font_path: str | None = None,
):
    """Render a complete EAN-13 symbol as a 1-bit Pillow Image.

    Drawn at exactly ``narrow`` device dots per module with no resampling, so the
    bars stay crisp and scannable. Includes the protruding start/center/end guard
    bars and the bold human-readable digits in the standard retail 1 + 6 + 6
    layout, plus the left (11-module) and right (7-module) quiet zones.
    Pillow mode "1": ``1`` = white, ``0`` = black.
    """
    from PIL import Image, ImageDraw  # type: ignore[import-not-found]

    bits = _ean13_binary(full13)
    ql, qr = EAN13_QUIET_LEFT_MODULES, EAN13_QUIET_RIGHT_MODULES
    width = (ql + 95 + qr) * narrow
    height = bar_h + guard_ext
    img = Image.new("1", (width, height), 1)  # white background
    draw = ImageDraw.Draw(img)

    x0 = ql * narrow  # x of module 0 (first bar)
    for m, bit in enumerate(bits):
        if bit == "1":
            x = x0 + m * narrow
            blen = (bar_h + guard_ext) if m in _EAN13_GUARD_MODULES else bar_h
            draw.rectangle([x, 0, x + narrow - 1, blen - 1], fill=0)  # black bar

    font = _load_bold_text_font(font_path, hri_h, bold=True)

    def place(ch: str, center_x: float) -> None:
        bbox = draw.textbbox((0, 0), ch, font=font)
        gw, gh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        tx = center_x - gw / 2 - bbox[0]
        ty = bar_h + (guard_ext - gh) / 2 - bbox[1]  # centered in the protrusion band
        draw.text((tx, ty), ch, font=font, fill=0)

    # First digit sits in the left quiet zone, just left of the start guard.
    fb = draw.textbbox((0, 0), full13[0], font=font)
    fx = max(0, x0 - narrow - (fb[2] - fb[0]) - fb[0])
    fy = bar_h + (guard_ext - (fb[3] - fb[1])) / 2 - fb[1]
    draw.text((fx, fy), full13[0], font=font, fill=0)

    # Left group between start/center guards; right group between center/end
    # guards. Each digit centered in its 7-module cell.
    for i, ch in enumerate(full13[1:7]):
        place(ch, x0 + (3 + i * 7 + 3.5) * narrow)
    for i, ch in enumerate(full13[7:13]):
        place(ch, x0 + (50 + i * 7 + 3.5) * narrow)

    return img


def _image_to_tspl_bitmap(img, origin_x: int, origin_y: int, *, mode: int = 0) -> bytes:
    """Encode a 1-bit Pillow Image as a TSPL ``BITMAP`` command (header + binary).

    Returns raw bytes (ASCII command prefix + packed bitmap + CRLF) ready to be
    concatenated into a TSPL job. ``mode`` 0 = OVERWRITE.
    """
    if img.mode != "1":
        img = img.convert("1")
    w, h = img.size
    bytes_per_row = (w + 7) // 8
    padding_bits = bytes_per_row * 8 - w
    data = bytearray(img.tobytes())  # Pillow "1": 1=white, 0=black, MSB-first

    if _TSPL_BITMAP_BLACK_IS_ZERO:
        # Already matches TSPL (0=black). Pad bits are the LOW bits of the last
        # byte of each row; Pillow leaves them 0 (=black) so force them to 1
        # (=white) or they print a black sliver down the right edge.
        if padding_bits:
            pad_low = (1 << padding_bits) - 1
            for row in range(h):
                data[(row + 1) * bytes_per_row - 1] |= pad_low
    else:
        data = bytearray(b ^ 0xFF for b in data)
        if padding_bits:
            keep = (0xFF << padding_bits) & 0xFF
            for row in range(h):
                data[(row + 1) * bytes_per_row - 1] &= keep

    prefix = f"BITMAP {origin_x},{origin_y},{bytes_per_row},{h},{mode},".encode("ascii")
    return prefix + bytes(data) + b"\r\n"


def _ean13_layout(model_key: str, pn: str, ean13: str, dpi: int) -> dict:
    """Shared geometry + rendered text bitmaps for the EAN-13 label.

    Used by both ``build_ean13_tspl`` (print) and ``render_ean13_preview_png``
    (off-printer verification) so the two never drift.
    """
    if not pn or not ean13:
        raise ValueError("EAN-13 label requires both pn and ean13")
    digits = "".join(c for c in str(ean13) if c.isdigit())
    if len(digits) not in (12, 13):
        raise ValueError(
            f"EAN-13 input must be 12 or 13 digits; got {len(digits)} from {ean13!r}"
        )
    d12 = digits[:12]
    full13 = d12 + _ean13_check_digit(d12)
    model_display = EAN13_MODEL_DISPLAY.get(str(model_key), str(model_key))
    header_text = EAN13_HEADER_TEMPLATE.format(model_display=model_display, pn=pn)

    label_w = _mm_to_dots(EAN13_WIDTH_MM, dpi)
    label_h = _mm_to_dots(EAN13_HEIGHT_MM, dpi)
    header_h = max(_mm_to_dots(EAN13_TSPL_HEADER_H_MM, dpi), 12)
    sub_h = max(_mm_to_dots(EAN13_TSPL_SUBTITLE_H_MM, dpi), 10)
    hri_h = max(_mm_to_dots(EAN13_TSPL_HRI_H_MM, dpi), 12)
    bar_h = _mm_to_dots(EAN13_TSPL_BAR_H_MM, dpi)
    narrow = max(2, _mm_to_dots(EAN13_TSPL_MODULE_MM, dpi))
    guard_ext = hri_h + _mm_to_dots(EAN13_TSPL_GUARD_EXT_EXTRA_MM, dpi)

    header_img = _render_text_image(header_text, header_h, bold=True)
    sub_img = _render_text_image(EAN13_SUBTITLE_TEXT, sub_h, bold=False)
    barcode_img = _render_ean13_full_bitmap(full13, narrow, bar_h, hri_h, guard_ext)

    header_y = _mm_to_dots(EAN13_TSPL_TOP_MARGIN_MM, dpi)
    sub_y = header_y + header_h + _mm_to_dots(EAN13_TSPL_HEADER_SUB_GAP_MM, dpi)
    bar_y = sub_y + sub_h + _mm_to_dots(EAN13_TSPL_SUB_BAR_GAP_MM, dpi)

    center = lambda iw: max(0, (label_w - iw) // 2)  # noqa: E731
    # Center the bar region (ignoring the asymmetric quiet zones) so the bars sit
    # under the centered header/subtitle exactly as before.
    bar_region_w = 95 * narrow
    barcode_x = max(0, (label_w - bar_region_w) // 2 - EAN13_QUIET_LEFT_MODULES * narrow)

    return {
        "full13": full13,
        "label_w": label_w,
        "label_h": label_h,
        "header_img": header_img,
        "sub_img": sub_img,
        "barcode_img": barcode_img,
        "header_xy": (center(header_img.width), header_y),
        "sub_xy": (center(sub_img.width), sub_y),
        "barcode_xy": (barcode_x, bar_y),
    }


def build_ean13_tspl(
    model_key: str,
    pn: str,
    ean13: str,
    *,
    dpi: int = EAN13_DPI,
    quantity: int = 2,
    density: int | None = None,
    speed: int | None = None,
) -> bytes:
    """Generate TSPL command bytes for the 模版五 EAN-13 (69 码) label.

    Targets the Deli DL-888T (TSC engine). 50×30mm. Three PIL-rendered ``BITMAP``
    blocks: bold header, regular subtitle, and the complete EAN-13 symbol (bars
    with protruding guard bars + bold human-readable digits). The barcode is
    rendered as a bitmap rather than via the native ``BARCODE`` command so the
    guard extensions and bold digits are fully under our control.

    ``density`` (0-15) and ``speed`` (inches/sec) override print darkness; leave
    both ``None`` to use the printer's own configured defaults (same as the SN
    label, which does not force darkness).
    """
    lay = _ean13_layout(model_key, pn, ean13, dpi)

    out = bytearray()

    def cmd(s: str) -> None:
        out.extend(s.encode("ascii"))
        out.extend(b"\r\n")

    cmd(f"SIZE {EAN13_WIDTH_MM:g} mm,{EAN13_HEIGHT_MM:g} mm")
    cmd("GAP 2 mm,0 mm")
    if speed is not None:
        cmd(f"SPEED {int(speed)}")
    if density is not None:
        cmd(f"DENSITY {int(density)}")
    cmd("DIRECTION 1")
    cmd("REFERENCE 0,0")
    cmd("CLS")
    out.extend(_image_to_tspl_bitmap(lay["header_img"], *lay["header_xy"]))
    out.extend(_image_to_tspl_bitmap(lay["sub_img"], *lay["sub_xy"]))
    out.extend(_image_to_tspl_bitmap(lay["barcode_img"], *lay["barcode_xy"]))
    cmd(f"PRINT 1,{max(1, int(quantity))}")
    return bytes(out)


def render_ean13_preview_png(
    model_key: str,
    pn: str,
    ean13: str,
    output_path: str | Path,
    *,
    dpi: int = EAN13_DPI,
    scale: int = 4,
) -> Path:
    """Compose the EAN-13 label exactly as ``build_ean13_tspl`` lays it out and
    save it as a (scaled-up) PNG for visual review without a printer.

    Uses the very same ``_ean13_layout`` bitmaps that get sent to the printer, so
    the preview is pixel-identical to the printed label.
    """
    from PIL import Image  # type: ignore[import-not-found]

    lay = _ean13_layout(model_key, pn, ean13, dpi)
    canvas = Image.new("1", (lay["label_w"], lay["label_h"]), 1)  # 1 = white
    canvas.paste(lay["header_img"], lay["header_xy"])
    canvas.paste(lay["sub_img"], lay["sub_xy"])
    canvas.paste(lay["barcode_img"], lay["barcode_xy"])

    big = canvas.resize((lay["label_w"] * scale, lay["label_h"] * scale), Image.NEAREST)
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    big.convert("RGB").save(out_path, format="PNG")
    return out_path


# ---------------------------------------------------------------------------
# PNG preview (cross-platform, for off-printer verification)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PreviewOptions:
    dpi: int = 300  # screen-friendly preview dpi, independent of printer dpi
    background: str = "white"
    foreground: str = "black"
    font_path: str | None = None
    text_height_mm: float = TEXT_HEIGHT_MM


def render_preview_png(
    sn: str,
    output_path: str | Path,
    options: PreviewOptions | None = None,
) -> Path:
    """Render a 42×25mm preview PNG of the label using python-barcode + Pillow.

    Raises ``ImportError`` if python-barcode / Pillow are not installed.
    """
    try:
        import barcode  # type: ignore[import-not-found]
        from barcode.writer import ImageWriter  # type: ignore[import-not-found]
        from PIL import Image, ImageDraw, ImageFont  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            "Preview requires 'python-barcode' and 'Pillow'. "
            "Install via: pip install python-barcode Pillow"
        ) from exc

    opts = options or PreviewOptions()
    normalized = normalize_sn(sn)
    if not normalized:
        raise ValueError(f"SN is empty after normalization: {sn!r}")

    dpi = opts.dpi
    px = lambda mm: max(1, round(mm * dpi / 25.4))  # noqa: E731

    canvas_w = px(LABEL_WIDTH_MM)
    canvas_h = px(LABEL_HEIGHT_MM)
    canvas = Image.new("RGB", (canvas_w, canvas_h), opts.background)
    draw = ImageDraw.Draw(canvas)

    # Mirror ZPL: bitmap-rendered barcode at exactly 38mm × 12mm, with the
    # surrounding bbox cropped so no padding lines appear above/below.
    print_area_w_px = px(PRINT_AREA_WIDTH_MM)
    bar_h_px = px(BARCODE_HEIGHT_MM)
    bw_barcode = _render_code128_bitmap(normalized, print_area_w_px, bar_h_px)
    barcode_img = bw_barcode.convert("RGB")
    bar_x = px(LEFT_MARGIN_MM)
    bar_y = px(TOP_MARGIN_MM)
    canvas.paste(barcode_img, (bar_x, bar_y))

    # Mirror ZPL: bold SN text, left-aligned within the 38mm print area,
    # with the same per-glyph letter-spacing as the printed bitmap.
    font = _load_bold_text_font(opts.font_path, px(opts.text_height_mm))
    text_y = bar_y + bar_h_px + px(TEXT_GAP_MM)
    sn_text = f"{TEXT_PREFIX}{normalized}"
    text_x = px(LEFT_MARGIN_MM)
    advances, ascent_offset, _ = _measure_text_with_spacing(
        draw, sn_text, font, px(TEXT_LETTER_SPACING_MM)
    )
    cur_x = text_x
    for ch, adv in zip(sn_text, advances):
        draw.text((cur_x, text_y + ascent_offset), ch, fill=opts.foreground, font=font)
        cur_x += adv

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, format="PNG", dpi=(dpi, dpi))
    return out_path


def _load_preview_font(font_path: str | None, size_px: int):
    from PIL import ImageFont  # type: ignore[import-not-found]

    candidates: list[str] = []
    if font_path:
        candidates.append(font_path)
    # Spec font + reasonable fallbacks across macOS / Windows / Linux.
    candidates.extend(
        [
            "HYKangHei45S.ttf",
            "HanyiKangHei45S.ttf",
            "C:/Windows/Fonts/msyh.ttc",  # Microsoft YaHei
            "C:/Windows/Fonts/simhei.ttf",
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/Supplemental/Songti.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size_px)
        except (OSError, ValueError):
            continue
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Windows raw printing
# ---------------------------------------------------------------------------


def is_windows() -> bool:
    return sys.platform.startswith("win")


def list_windows_printers() -> list[dict]:
    """List installed printer queues on Windows. Returns [] on non-Windows."""
    if not is_windows():
        return []
    try:
        import win32print  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("pywin32 is not installed; cannot enumerate printers")
        return []

    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    rows = win32print.EnumPrinters(flags, None, 2)
    out: list[dict] = []
    for row in rows:
        # Level-2 PRINTER_INFO_2 rows are dict-like in pywin32.
        if isinstance(row, dict):
            name = str(row.get("pPrinterName") or "")
            port = str(row.get("pPortName") or "")
            driver = str(row.get("pDriverName") or "")
        else:
            # Level-1 fallback: (flags, description, name, comment)
            name = str(row[2]) if len(row) > 2 else ""
            port = ""
            driver = str(row[1]) if len(row) > 1 else ""
        if name:
            out.append({"name": name, "port": port, "driver": driver})
    return out


def find_label_printer(preferred: str | None = None) -> str | None:
    """Resolve which printer queue to send to.

    When ``preferred`` is supplied (i.e. the caller has a config-driven name),
    the name is *strict*: we only return a queue whose name matches exactly
    (case-insensitive). No substring matches, no Zebra-hint fallback. The
    factory machine has multiple Zebra/Deli queues, including orphan ones,
    and silently picking a different one shipped to the wrong printer once
    already — config is law.

    Only when ``preferred`` is empty/None do we fall back to the legacy
    Zebra-hint heuristic, which exists for first-time setup before someone
    has filled in ``<x>_printer.name`` in config.yml.
    """
    if not is_windows():
        return preferred

    printers = list_windows_printers()
    if not printers:
        return None

    if preferred:
        pref = preferred.lower()
        for p in printers:
            if p["name"].lower() == pref:
                return p["name"]
        return None  # strict — no fuzzy fallback when config gave us a name

    # Legacy first-run fallback only. Real factory configs should never
    # land here because the *_printer.name keys are always set.
    for p in printers:
        bag = f"{p['name']} {p['driver']}".lower()
        if any(hint in bag for hint in _ZEBRA_NAME_HINTS):
            return p["name"]
    try:
        import win32print  # type: ignore[import-not-found]

        default_name = win32print.GetDefaultPrinter()
    except (ImportError, Exception):  # noqa: BLE001
        default_name = ""
    if default_name and any(hint in default_name.lower() for hint in _ZEBRA_NAME_HINTS):
        return default_name
    return None


def send_to_windows_printer(printer_name: str, data: bytes, *, job_name: str = "SN Label") -> None:
    """Send ``data`` (raw ZPL bytes) directly to the named printer queue."""
    if not is_windows():
        raise RuntimeError("send_to_windows_printer requires Windows")
    try:
        import win32print  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "pywin32 is not installed. Run: pip install pywin32"
        ) from exc

    handle = win32print.OpenPrinter(printer_name)
    try:
        job = win32print.StartDocPrinter(handle, 1, (job_name, None, "RAW"))
        try:
            win32print.StartPagePrinter(handle)
            win32print.WritePrinter(handle, data)
            win32print.EndPagePrinter(handle)
        finally:
            win32print.EndDocPrinter(handle)
        logger.info(f"Submitted label job '{job_name}' to printer '{printer_name}' (id={job})")
    finally:
        win32print.ClosePrinter(handle)


def write_zpl_file(path: str | Path, data: bytes) -> Path:
    """Write ZPL bytes to a file (for inspection or for printers that read from
    a hotfolder). Returns the resolved path."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return out
