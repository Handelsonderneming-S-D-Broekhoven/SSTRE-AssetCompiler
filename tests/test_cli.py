# SPDX-FileCopyrightText: 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe
# SPDX-License-Identifier: GPL-3.0-only
"""End-to-end CLI runs on synthetic installs, validated against the SSTRE schemas."""

import json

import pytest

from conftest import make_zip, sstre_schemas_dir, validator
from sstre_assetcompiler.cli import main
from sstre_assetcompiler.inventory import normalized_id_path


def _schemas():
    d = sstre_schemas_dir()
    if d is None or not (d / "game-edition-report.schema.json").is_file():
        pytest.skip("SSTRE schemas not found (set SSTRE_SCHEMAS_DIR)")
    return d


@pytest.fixture
def fps(classic_like, hd_like, tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    assert main(["fingerprint", str(classic_like), "--edition", "classic_tfe",
                 "--profile-id", "tfe", "--include", "*.gro", "-o", str(a)]) == 0
    assert main(["fingerprint", str(hd_like), "--edition", "fusion", "--profile-id", "fus",
                 "--file-edition", "content/gamehd/*=fusion_hd_tfe", "-o", str(b)]) == 0
    return a, b


def test_detect_report_validates(classic_like, hd_like, fps, tmp_path):
    out = tmp_path / "report.json"
    unknown = tmp_path / "empty"
    unknown.mkdir()
    rc = main(["detect", str(classic_like), str(hd_like), str(unknown),
               "--fingerprints", str(fps[0]), "--fingerprints", str(fps[1]), "-o", str(out)])
    assert rc == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    validator(_schemas() / "game-edition-report.schema.json").validate(report)
    assert [i["status"] for i in report["installs"]] == ["exact", "exact", "unknown"]
    assert [i["detected_edition"] for i in report["installs"]] == ["classic_tfe", "fusion", "unknown"]
    assert report["installs"][1]["archives"][0]["edition"] == "fusion_hd_tfe"


def test_report_schema_examples_validate():
    path = _schemas() / "game-edition-report.schema.json"
    v = validator(path)
    for ex in json.loads(path.read_text(encoding="utf-8"))["examples"]:
        v.validate(ex)


def test_inventory_validates_and_resolves_overrides(classic_like, hd_like, fps, tmp_path):
    out, summ = tmp_path / "inv.json", tmp_path / "sum.json"
    rc = main(["inventory", str(classic_like), str(hd_like), "--fingerprints", str(fps[0]),
               "--fingerprints", str(fps[1]), "-o", str(out), "--summary", str(summ),
               "--jobs", "2"])
    assert rc == 0
    inv = json.loads(out.read_text(encoding="utf-8"))
    validator(_schemas() / "asset-inventory.schema.json").validate(inv)
    by_id = {a["asset_id"]: a for a in inv["assets"]}

    wall = by_id["classic_tfe:textures/wall.tex"]
    assert wall["source_id"] == "classic_tfe.patch.gro"  # later archive wins (fallback order)
    assert wall["shadowed_sources"] == ["classic_tfe.base.gro"] and "fallback" in wall["notes"]
    assert wall["texture"]["width_px"] == 128 and wall["file_format"] == "se1_tex_v4"
    old = by_id["classic_tfe:textures/old.tex"]
    assert old["file_format"] == "se1_tex_v3" and old["texture"]["width_px"] == 32
    assert by_id["classic_tfe:textures/fire.tex"]["texture"]["procedural_effect"] is True
    assert by_id["classic_tfe:models/thing.mdl"]["asset_type"] == "model"
    assert "classic_tfe:text/hello_world.txt" in by_id
    assert not any("mod" == a["virtual_path"].split("/")[0].lower() for a in inv["assets"])

    # Content/<game>/ prefix stripped; a colliding Content/Shared path is disambiguated
    hd = [a for a in inv["assets"] if a["edition"] == "fusion_hd_tfe"]
    assert {a["file_format"] for a in hd} == {"se3_meta_bin"}
    assert "fusion_hd_tfe:textures/wall.tex" in by_id and "fusion_hd_tfe:levels/a.wld" in by_id
    assert any(a["asset_id"].startswith("fusion_hd_tfe:textures/wall.tex-") for a in hd)
    assert by_id["fusion_hd_tfe:textures/wall.tex"]["texture"] == {}

    s = json.loads(summ.read_text(encoding="utf-8"))
    assert s["assets"] == len(inv["assets"]) == 9
    assert s["shadowed_assets"] == 1 and s["asset_id_collisions"] == 1
    assert s["classic_tex"]["classic_tfe"]["count"] == 3


def test_inventory_without_profile_uses_unknown(tmp_path):
    root = tmp_path / "loose"
    make_zip(root / "x.gro", {"a.txt": b"1"})
    out = tmp_path / "inv.json"
    assert main(["inventory", str(root), "-o", str(out)]) == 0
    inv = json.loads(out.read_text(encoding="utf-8"))
    assert inv["assets"][0]["asset_id"] == "unknown:a.txt"


def test_bad_fingerprint_file_is_an_error(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    (tmp_path / "r").mkdir()
    assert main(["detect", str(tmp_path / "r"), "--fingerprints", str(bad)]) == 1
    assert "not valid JSON" in capsys.readouterr().err


@pytest.mark.parametrize("vp,expected", [
    ("Textures/A B.tex", "textures/a_b.tex"),
    ("Content/GameHD/Models/X.mdl", "models/x.mdl"),
    ("Content/x.txt", "content/x.txt"),
    ("Data\\Win.tex", "data/win.tex"),
])
def test_normalized_id_path(vp, expected):
    assert normalized_id_path(vp) == expected
