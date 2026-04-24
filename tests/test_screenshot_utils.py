from src.utils.screenshot import relocate_session_dirs, session_dirs


def test_relocate_session_dirs_removes_tail_root_when_target_exists(tmp_path) -> None:
    output_root = tmp_path / "screenshot"
    full_sn = "EC752JJ172517046"

    session_dirs(output_root, full_sn)
    tail_dirs = session_dirs(output_root, "7046")
    marker = tail_dirs["screenshots"] / "tail-marker.txt"
    marker.write_text("created before full SN was known", encoding="utf-8")

    relocated = relocate_session_dirs(output_root, tail_dirs, full_sn)

    assert relocated["sn_root"] == output_root / full_sn
    assert not (output_root / "7046").exists()
    assert (output_root / full_sn / "图片" / "tail-marker.txt").read_text(encoding="utf-8")
