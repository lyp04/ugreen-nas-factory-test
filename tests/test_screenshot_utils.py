from pathlib import Path

from src.utils import screenshot
from src.utils.screenshot import relocate_session_dirs, session_dirs


def test_capture_page_does_not_overwrite_same_second(tmp_path, monkeypatch) -> None:
    # _timestamp() is 1-second granular; two captures of the same page within the same
    # wall-clock second must NOT collide. The second gets a _2 suffix so the first
    # screenshot is never overwritten — a caller holding the first path keeps a real image.
    monkeypatch.setattr(screenshot, "_timestamp", lambda: "20260101_120000")

    class FakePage:
        def screenshot(self, path, **_kwargs):
            Path(path).write_bytes(b"png")

    page = FakePage()
    first = screenshot.capture_page(page, "SN123", "hdd_read", tmp_path)
    second = screenshot.capture_page(page, "SN123", "hdd_read", tmp_path)

    assert first != second
    assert first.exists() and second.exists()
    assert first.name == "SN123_hdd_read_20260101_120000.png"
    assert second.name == "SN123_hdd_read_20260101_120000_2.png"


def test_capture_page_cleans_partial_png_on_screenshot_failure(tmp_path) -> None:
    # If page.screenshot() raises after starting to write, capture_page must not leave a
    # partial/zero-byte PNG behind (it would later be mistaken for a valid existing capture).
    import pytest

    class BoomPage:
        def screenshot(self, path, **_kwargs):
            Path(path).write_bytes(b"partial")
            raise RuntimeError("screenshot failed mid-write")

    with pytest.raises(RuntimeError):
        screenshot.capture_page(BoomPage(), "SN123", "hdd_read", tmp_path)

    assert list(tmp_path.glob("*.png")) == []  # partial file cleaned up


def test_session_dirs_is_lazy_and_creates_nothing(tmp_path) -> None:
    # session_dirs must NOT create directories: a manual SN / SN-tail whose
    # device is never found should leave no empty folder behind. The folder is
    # materialized lazily by the first real artifact writer.
    output_root = tmp_path / "out"
    dirs = session_dirs(output_root, "ABCD1234")

    assert dirs["sn_root"] == output_root / "ABCD1234"
    assert dirs["base"] == output_root / "ABCD1234"
    assert dirs["screenshots"] == output_root / "ABCD1234" / "图片"
    assert not dirs["sn_root"].exists()
    assert not output_root.exists()


def test_relocate_session_dirs_removes_tail_root_when_target_exists(tmp_path) -> None:
    output_root = tmp_path / "screenshot"
    full_sn = "EC752JJ172517046"

    # session_dirs is lazy now; materialize the folders the way the real flow
    # would once artifacts actually land in them.
    full_dirs = session_dirs(output_root, full_sn)
    full_dirs["base"].mkdir(parents=True, exist_ok=True)
    tail_dirs = session_dirs(output_root, "7046")
    tail_dirs["screenshots"].mkdir(parents=True, exist_ok=True)
    marker = tail_dirs["screenshots"] / "tail-marker.txt"
    marker.write_text("created before full SN was known", encoding="utf-8")

    relocated = relocate_session_dirs(output_root, tail_dirs, full_sn)

    assert relocated["sn_root"] == output_root / full_sn
    assert not (output_root / "7046").exists()
    assert (output_root / full_sn / "图片" / "tail-marker.txt").read_text(encoding="utf-8")
