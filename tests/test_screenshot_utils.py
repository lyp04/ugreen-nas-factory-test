from src.utils.screenshot import relocate_session_dirs, session_dirs


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
