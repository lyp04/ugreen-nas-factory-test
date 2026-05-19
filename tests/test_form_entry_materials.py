from __future__ import annotations

import json
from pathlib import Path

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
