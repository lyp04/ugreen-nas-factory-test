from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src import form_entry


def test_form_data_includes_selected_code_from_unselected_group(monkeypatch, tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "forms.json").write_text(
        json.dumps(
            {
                "models": {"2800": "form_2800"},
                "forms": {
                    "form_2800": {
                        "model_key": "2800",
                        "default_grade": "A",
                        "template": {"id": 1, "warehouse_id": 6, "sku": "RV_TEST"},
                        "retread_results": {"A": {"field": "result", "value": "RV_A", "relations": []}},
                        "part_groups": [
                            {"field": "replace_parts", "title": "更换部件"},
                            {"field": "add_packaging", "title": "补充包材"},
                        ],
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (config / "materials.json").write_text(
        json.dumps(
            {
                "forms": {
                    "form_2800": {
                        "selected_material_groups": ["补充包材"],
                        "selected_material_codes": ["MR_THERMAL"],
                        "materials": [
                            {"code": "MR_THERMAL", "name": "导热硅胶", "group": "更换部件"},
                            {"code": "MR_OTHER", "name": "其他部件", "group": "更换部件"},
                            {"code": "MR_BOX", "name": "纸箱", "group": "补充包材"},
                        ],
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(form_entry, "autoupdate_root", lambda: tmp_path)

    form_data = form_entry.build_report_form_data({"sn": "SN123"}, tmp_path, model="2800")

    groups = {group["title"]: group["items"] for group in form_data["material_groups"]}
    assert [item["code"] for item in groups["更换部件"]] == ["MR_THERMAL"]
    assert [item["code"] for item in groups["补充包材"]] == ["MR_BOX"]


# ---------------------------------------------------------------------------
# list_replacement_part_candidates + toggle_material_deduction:
# the data layer behind the GUI's color-coded 物料 tab + double-click toggle.
# ---------------------------------------------------------------------------


def _seed_materials_fixture(tmp_path: Path) -> Path:
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    (config / "forms.json").write_text(
        json.dumps(
            {
                "models": {"2800": "form_2800"},
                "forms": {
                    "form_2800": {
                        "model_key": "2800",
                        "default_grade": "A",
                        "template": {"id": 1, "warehouse_id": 6, "sku": "RV_TEST"},
                        "retread_results": {"A": {"field": "result", "value": "RV_A", "relations": []}},
                        "part_groups": [
                            {"field": "replace_parts", "title": "更换部件"},
                            {"field": "add_packaging", "title": "补充包材"},
                        ],
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (config / "materials.json").write_text(
        json.dumps(
            {
                "forms": {
                    "form_2800": {
                        "selected_material_groups": ["补充包材"],
                        "selected_material_codes": ["MR_THERMAL"],
                        "materials": [
                            {"code": "MR_THERMAL", "name": "导热硅胶", "group": "更换部件"},
                            {"code": "MR_OTHER", "name": "其他部件", "group": "更换部件"},
                            {"code": "MR_BOX", "name": "纸箱", "group": "补充包材"},
                        ],
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return config


def test_list_replacement_part_candidates_flags_selection(monkeypatch, tmp_path: Path) -> None:
    _seed_materials_fixture(tmp_path)
    monkeypatch.setattr(form_entry, "autoupdate_root", lambda: tmp_path)

    groups = form_entry.list_replacement_part_candidates(project_root=tmp_path, model="2800")

    by_title = {group["title"]: group for group in groups}
    assert set(by_title) == {"更换部件", "补充包材"}
    # 更换部件 is a by-code group: every candidate shows, selected flag tracks
    # selected_material_codes membership.
    replace = {item["code"]: item for item in by_title["更换部件"]["items"]}
    assert by_title["更换部件"]["group_all_in"] is False
    assert replace["MR_THERMAL"]["selected"] is True
    assert replace["MR_OTHER"]["selected"] is False
    # 补充包材 is an all-in group: every candidate is selected by group membership.
    assert by_title["补充包材"]["group_all_in"] is True
    assert by_title["补充包材"]["items"][0]["selected"] is True


def test_toggle_material_deduction_adds_then_removes(monkeypatch, tmp_path: Path) -> None:
    config = _seed_materials_fixture(tmp_path)
    monkeypatch.setattr(form_entry, "autoupdate_root", lambda: tmp_path)

    # MR_OTHER starts unselected.
    materials_path = config / "materials.json"
    assert "MR_OTHER" not in json.loads(materials_path.read_text(encoding="utf-8"))["forms"]["form_2800"]["selected_material_codes"]

    # Add it.
    result = form_entry.toggle_material_deduction(
        project_root=tmp_path, model="2800", code="MR_OTHER", select=True
    )
    assert result is True
    codes = json.loads(materials_path.read_text(encoding="utf-8"))["forms"]["form_2800"]["selected_material_codes"]
    assert "MR_OTHER" in codes and "MR_THERMAL" in codes

    # Remove it via the implicit-flip path (no `select` arg).
    result = form_entry.toggle_material_deduction(
        project_root=tmp_path, model="2800", code="MR_OTHER"
    )
    assert result is False
    codes = json.loads(materials_path.read_text(encoding="utf-8"))["forms"]["form_2800"]["selected_material_codes"]
    assert "MR_OTHER" not in codes and "MR_THERMAL" in codes


def test_toggle_material_deduction_is_idempotent(monkeypatch, tmp_path: Path) -> None:
    config = _seed_materials_fixture(tmp_path)
    monkeypatch.setattr(form_entry, "autoupdate_root", lambda: tmp_path)

    # MR_THERMAL is already selected — selecting again should not duplicate.
    form_entry.toggle_material_deduction(
        project_root=tmp_path, model="2800", code="MR_THERMAL", select=True
    )
    codes = json.loads((config / "materials.json").read_text(encoding="utf-8"))["forms"]["form_2800"]["selected_material_codes"]
    assert codes.count("MR_THERMAL") == 1


def test_toggle_material_deduction_rejects_blank_code(monkeypatch, tmp_path: Path) -> None:
    _seed_materials_fixture(tmp_path)
    monkeypatch.setattr(form_entry, "autoupdate_root", lambda: tmp_path)

    assert form_entry.toggle_material_deduction(project_root=tmp_path, model="2800", code="") is False
    assert form_entry.toggle_material_deduction(project_root=tmp_path, model="2800", code="   ") is False


# ---------------------------------------------------------------------------
# sync_autoupdate_repo: never raise, no-op gracefully when preconditions fail.
# ---------------------------------------------------------------------------


def _git_required() -> str:
    git = shutil.which("git")
    if not git:
        pytest.skip("git not installed in test environment")
    return git


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "--quiet", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "tag.gpgsign", "false")


def test_sync_autoupdate_repo_skips_when_root_unresolved(monkeypatch) -> None:
    def boom() -> Path:
        raise form_entry.FormEntryError("nope")

    monkeypatch.setattr(form_entry, "autoupdate_root", boom)
    result = form_entry.sync_autoupdate_repo()
    assert result["status"] == "skipped"
    assert "nope" in result["reason"]


def test_sync_autoupdate_repo_skips_non_git_dir(monkeypatch, tmp_path: Path) -> None:
    # autoupdate_root resolves but the directory is just a plain folder.
    monkeypatch.setattr(form_entry, "autoupdate_root", lambda: tmp_path)
    result = form_entry.sync_autoupdate_repo()
    assert result["status"] == "skipped"
    assert "not a git work tree" in result["reason"]


def test_sync_autoupdate_repo_up_to_date_when_no_remote_commits(monkeypatch, tmp_path: Path) -> None:
    _git_required()
    upstream = tmp_path / "upstream"
    clone = tmp_path / "clone"
    _init_repo(upstream)
    (upstream / "README.md").write_text("hello\n")
    _git(upstream, "add", "README.md")
    _git(upstream, "commit", "--quiet", "-m", "init")
    # Bare upstream so the clone can push/fetch against it.
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "clone", "--bare", str(upstream), str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(bare), str(clone)], check=True, capture_output=True)

    monkeypatch.setattr(form_entry, "autoupdate_root", lambda: clone)
    result = form_entry.sync_autoupdate_repo()
    assert result["status"] == "up_to_date", result
    assert result["before"] == result["after"]


def test_sync_autoupdate_repo_fast_forwards_when_upstream_ahead(monkeypatch, tmp_path: Path) -> None:
    _git_required()
    upstream = tmp_path / "upstream"
    clone = tmp_path / "clone"
    _init_repo(upstream)
    (upstream / "README.md").write_text("hello\n")
    _git(upstream, "add", "README.md")
    _git(upstream, "commit", "--quiet", "-m", "init")
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "clone", "--bare", str(upstream), str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(bare), str(clone)], check=True, capture_output=True)

    # Push a new commit to the bare upstream from the source upstream working copy.
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=upstream, check=True, capture_output=True)
    (upstream / "materials.json").write_text('{"updated": true}\n')
    _git(upstream, "add", "materials.json")
    _git(upstream, "commit", "--quiet", "-m", "add materials")
    _git(upstream, "push", "--quiet", "origin", "main")

    # Sanity: clone hasn't fetched yet, so the new file isn't there.
    assert not (clone / "materials.json").exists()

    monkeypatch.setattr(form_entry, "autoupdate_root", lambda: clone)
    result = form_entry.sync_autoupdate_repo()
    assert result["status"] == "updated", result
    assert result["before"] != result["after"]
    assert (clone / "materials.json").read_text().strip() == '{"updated": true}'


def test_sync_autoupdate_repo_discards_dirty_materials_when_upstream_ahead(monkeypatch, tmp_path: Path) -> None:
    """内部系统 refresh writes materials.json on every startup; upstream may also
    advance materials.json (e.g. a new selected_material_codes opt-in). v0.1.14
    tried --autostash which left the tree conflicted in that case. The new
    behavior: discard the dirty edits with reset --hard, then let the 内部系统
    refresh that runs immediately after rewrite materials.json cleanly."""
    _git_required()
    upstream = tmp_path / "upstream"
    clone = tmp_path / "clone"
    _init_repo(upstream)
    (upstream / "materials.json").write_text('{"version": 1}\n')
    _git(upstream, "add", "materials.json")
    _git(upstream, "commit", "--quiet", "-m", "init")
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "clone", "--bare", str(upstream), str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(bare), str(clone)], check=True, capture_output=True)
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test")

    # Upstream advances materials.json (the realistic overlap case).
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=upstream, check=True, capture_output=True)
    (upstream / "materials.json").write_text('{"version": 2, "selected": ["NEW_CODE"]}\n')
    _git(upstream, "add", "materials.json")
    _git(upstream, "commit", "--quiet", "-m", "bump materials")
    _git(upstream, "push", "--quiet", "origin", "main")

    # Clone has dirty materials.json (内部系统-refresh-style local writes).
    (clone / "materials.json").write_text('{"version": 1, "last_refreshed_at": "2026-05-27"}\n')

    monkeypatch.setattr(form_entry, "autoupdate_root", lambda: clone)
    result = form_entry.sync_autoupdate_repo()
    assert result["status"] == "updated", result
    # Working tree is clean — no merge markers, no stash debris.
    status_proc = subprocess.run(["git", "status", "--porcelain"], cwd=clone, capture_output=True, text=True)
    assert status_proc.stdout == "", f"work tree not clean after sync: {status_proc.stdout!r}"
    # Upstream version present, local dirt is gone (内部系统 refresh will rewrite).
    assert (clone / "materials.json").read_text().strip() == '{"version": 2, "selected": ["NEW_CODE"]}'


def test_sync_autoupdate_repo_refuses_to_reset_when_local_ahead(monkeypatch, tmp_path: Path) -> None:
    """If the local branch has its own commits not yet pushed, the function
    must not reset them away — the operator presumably wants to keep them."""
    _git_required()
    upstream = tmp_path / "upstream"
    clone = tmp_path / "clone"
    _init_repo(upstream)
    (upstream / "README.md").write_text("init\n")
    _git(upstream, "add", "README.md")
    _git(upstream, "commit", "--quiet", "-m", "init")
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "clone", "--bare", str(upstream), str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(bare), str(clone)], check=True, capture_output=True)
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test")

    # Local commit not pushed to upstream — divergent.
    (clone / "local.txt").write_text("local only\n")
    _git(clone, "add", "local.txt")
    _git(clone, "commit", "--quiet", "-m", "local-only")
    before_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone, capture_output=True, text=True).stdout.strip()

    monkeypatch.setattr(form_entry, "autoupdate_root", lambda: clone)
    result = form_entry.sync_autoupdate_repo()
    assert result["status"] == "skipped", result
    assert "diverged" in result["reason"]
    after_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone, capture_output=True, text=True).stdout.strip()
    assert before_sha == after_sha  # untouched
    assert (clone / "local.txt").exists()
