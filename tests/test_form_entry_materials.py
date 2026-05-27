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


def test_sync_autoupdate_repo_autostash_keeps_local_edits(monkeypatch, tmp_path: Path) -> None:
    """内部系统 refresh writes materials.json on every startup; the merge must not
    abort on that uncommitted dirt."""
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

    # Upstream advances README; clone has dirty materials.json the way a
    # post-内部系统-refresh factory machine would.
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=upstream, check=True, capture_output=True)
    (upstream / "README.md").write_text("new\n")
    _git(upstream, "add", "README.md")
    _git(upstream, "commit", "--quiet", "-m", "add readme")
    _git(upstream, "push", "--quiet", "origin", "main")

    (clone / "materials.json").write_text('{"version": 1, "last_refreshed_at": "2026-05-27"}\n')

    monkeypatch.setattr(form_entry, "autoupdate_root", lambda: clone)
    result = form_entry.sync_autoupdate_repo()
    assert result["status"] == "updated", result
    # Dirty edit survived the autostash round-trip.
    assert "last_refreshed_at" in (clone / "materials.json").read_text()
    # Upstream change is now present too.
    assert (clone / "README.md").read_text() == "new\n"
