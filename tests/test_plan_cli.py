# SPDX-FileCopyrightText: 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe
# SPDX-License-Identifier: GPL-3.0-only
"""CLI for the layered plan and validation against the umbrella schemas
(skipped when the schemas are not available). Synthetic data only."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from conftest import REPO, sstre_schemas_dir  # noqa: E402
from layer_fixtures import (A_ART, A_CLASSIC, A_UI, ai_entry, approvals_doc, approve, config, h,  # noqa: E402
                            make_workspace, pack_doc, standard_records)

from sstre_assetcompiler import layers  # noqa: E402
from sstre_assetcompiler import platform_profiles as plat  # noqa: E402
from sstre_assetcompiler.cli import main  # noqa: E402

NEW_SCHEMAS = ("build-config", "build-manifest", "platform-profile", "layer-approvals", "asset-pack")


def _rich_workspace(tmp: Path) -> Path:
    cfg = config(**{"hd-artwork": {"enabled": True}, "hd-interface": {"enabled": True, "includeRelayout": True},
                    "overrides": {"enabled": True}, "ai-enhancement": {"qualityProfile": "conservative"}})
    packs = [("overrides", {"priority": 1}, pack_doc("cur", "curated", [
                {"assetId": A_UI, "action": "use-classic"},
                {"assetId": "classic_tfe:textures/syn/maps.tex", "file": "m.png", "sha256": "sha256:" + h("m"),
                 "px": [512, 512], "keepMaps": True}])),
             ("ai-enhancement", {}, pack_doc("ai", "ai-enhancement", [ai_entry(A_CLASSIC, h(A_CLASSIC))],
                                             quality="conservative"))]
    apr = approvals_doc(approve(A_ART, "hd-artwork", "hd_tfe:syn/statue", approvedBy="owner", date="2026-11-27"),
                        approve(A_UI, "hd-interface", "hd_tfe:syn/logo"))
    return make_workspace(tmp, cfg, standard_records(), approvals=apr, packs=packs)


def test_cli_plan_validate_profiles(tmp_path, capsys):
    p = _rich_workspace(tmp_path)
    assert main(["validate-config", "--config", str(p)]) == 0
    out = capsys.readouterr()
    assert "OK" in out.err and "pack:cur sha256:" in out.out and "approvals sha256:" in out.out
    out1, out2 = tmp_path / "plan1.json", tmp_path / "plan2.json"
    assert main(["plan", "--config", str(p), "--selection", str(tmp_path / "records"),
                 "--out", str(out1), "--summary", str(tmp_path / "s.json")]) == 0
    assert main(["plan", "--config", str(p), "--selection", str(tmp_path / "records"),
                 "--out", str(out2)]) == 0
    assert out1.read_bytes() == out2.read_bytes()  # no timestamps: byte-identical
    assert b"\r\n" not in out1.read_bytes()
    m = json.loads(out1.read_text(encoding="utf-8"))
    assert json.loads((tmp_path / "s.json").read_text())["planHash"] == m["planHash"]
    assert main(["plan", "--canonical", "--config", str(p), "--selection", str(tmp_path / "records"),
                 "--out", str(tmp_path / "c.json")]) == 0
    assert json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))["planHash"] == m["planHash"]
    assert main(["profiles"]) == 0
    assert "pc-low-memory" in capsys.readouterr().out
    assert main(["profiles", "--show", "legacy-constrained"]) == 0
    assert '"hypothetical"' in capsys.readouterr().out


def test_cli_errors(tmp_path, capsys):
    p = make_workspace(tmp_path, config(**{"classic-base": {"enabled": False}}), standard_records())
    assert main(["validate-config", "--config", str(p)]) == 1
    assert "classic-base" in capsys.readouterr().err
    assert main(["plan", "--config", str(p), "--selection", str(tmp_path / "records"),
                 "--out", str(tmp_path / "x.json")]) == 1


def test_examples_are_valid(tmp_path):
    for f in sorted((REPO / "examples" / "build-config").glob("*.json")):
        cfg = layers.normalize_config(json.loads(f.read_text(encoding="utf-8")))
        assert cfg["layers"]["classic-base"]["enabled"]


# ------------------------------------------------------------- schemas ----

@pytest.fixture(scope="module")
def schemas():
    d = sstre_schemas_dir()
    if d is None or not all((d / f"{n}.schema.json").is_file() for n in NEW_SCHEMAS):
        pytest.skip("build-config/build-manifest schemas not available (set SSTRE_SCHEMAS_DIR)")
    jsonschema = pytest.importorskip("jsonschema")
    from referencing import Registry, Resource
    docs = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in d.glob("*.schema.json")}
    reg = Registry().with_resources([(s["$id"], Resource.from_contents(s)) for s in docs.values()])

    def validator(name: str, pointer: str = ""):
        s = docs[f"{name}.schema.json"]
        schema = {"$ref": s["$id"] + pointer} if pointer else s
        cls = jsonschema.Draft202012Validator
        return cls(schema, registry=reg, format_checker=cls.FORMAT_CHECKER)
    return validator


def _ok(v, doc):
    errs = sorted(v.iter_errors(doc), key=lambda e: list(e.absolute_path))
    assert not errs, "; ".join(f"{list(e.absolute_path)}: {e.message}" for e in errs[:5])


def test_builtin_data_matches_schemas(schemas):
    v = schemas("platform-profile")
    for pid in plat.BUILTIN_PROFILES:
        _ok(v, plat.load_builtin(pid))
    q = schemas("build-config", "#/$defs/qualityProfile")
    for prof in layers.builtin_quality_profiles().values():
        _ok(q, prof)
    c = schemas("build-config")
    for f in sorted((REPO / "examples" / "build-config").glob("*.json")):
        _ok(c, json.loads(f.read_text(encoding="utf-8")))


def test_manifest_and_inputs_match_schemas(tmp_path, schemas):
    p = _rich_workspace(tmp_path)
    _ok(schemas("build-config"), json.loads(p.read_text()))
    _ok(schemas("layer-approvals"), json.loads((tmp_path / "approvals.json").read_text()))
    for pack in sorted((tmp_path / "packs").glob("*/pack.json")):
        _ok(schemas("asset-pack"), json.loads(pack.read_text()))
    m = layers.build_plan(p, [tmp_path / "records"])
    statuses = {s["status"] for e in m["assets"] for s in e["layers"]}
    assert {"applied", "superseded", "awaiting-approval", "not-eligible", "suppressed"} <= statuses
    _ok(schemas("build-manifest"), json.loads(json.dumps(m)))
    for prof in ("pc-low-memory", "legacy-constrained"):
        cfg = json.loads(p.read_text())
        cfg["platformProfile"] = prof
        p.write_text(json.dumps(cfg))
        _ok(schemas("build-manifest"), json.loads(json.dumps(layers.build_plan(p, [tmp_path / "records"]))))
