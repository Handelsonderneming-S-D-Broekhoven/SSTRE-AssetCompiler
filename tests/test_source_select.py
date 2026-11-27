# SPDX-FileCopyrightText: 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe
# SPDX-License-Identifier: GPL-3.0-only
"""Synthetic tests for the source selector. All images are generated here."""

from __future__ import annotations

import json

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("PIL")

from sstre_assetcompiler import source_select as ss  # noqa: E402
from sstre_assetcompiler import texio  # noqa: E402

from conftest import sstre_schemas_dir, validator  # noqa: E402


def _art(h, w, seed=0, freq=6.0):
    """Smooth synthetic 'artwork' (sum of sinusoids) plus fine noise."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w] / max(h, w)
    img = np.zeros((h, w, 3))
    for c in range(3):
        a, b, p = rng.uniform(1, freq, 3)
        img[..., c] = 0.5 + 0.25 * np.sin(2 * np.pi * (a * x + b * y) + p) \
            + 0.15 * np.sin(2 * np.pi * (b * 3 * x - a * 2 * y))
    img += rng.normal(0, 0.03, img.shape)
    rgba = np.concatenate([np.clip(img, 0, 1) * 255, np.full((h, w, 1), 255)], -1)
    return rgba.astype(np.uint8)


def _down(a, w, h):
    return np.clip(ss.resize(a.astype(np.float32), w, h, down=True), 0, 255).round().astype(np.uint8)


def _blockify(a, keep=0.6):
    """Crude BC-like degradation: flatten each 4x4 block towards its mean
    (colour is preserved, block edges appear)."""
    out = a.astype(np.float32)
    h, w = a.shape[:2]
    for by in range(0, h, 4):
        for bx in range(0, w, 4):
            blk = out[by:by + 4, bx:bx + 4, :3]
            m = blk.reshape(-1, 3).mean(0)
            out[by:by + 4, bx:bx + 4, :3] = m + (blk - m) * keep
    return np.clip(out, 0, 255).round().astype(np.uint8)


def _req(cat="architecture", verified=True, related=False, maps=None, alpha="opaque"):
    c = {"candidate_id": "hd_tfe:x", "source_kind": "hd", "edition": "hd_tfe",
         "match": {"verified": verified, "related": related, "method": "exact_path"}}
    if maps:
        c["companion_maps"] = maps
    return {"asset_id": "classic_tfe:textures/x.tex", "category": cat,
            "classic": {"width_px": 64, "height_px": 64, "alpha_mode": alpha}, "candidates": [c]}


def _run(classic, cand, **kw):
    req = _req(**kw)
    m = {"hd_tfe:x": ss.compare(classic, cand, classic_alpha_mode=req["classic"]["alpha_mode"])}
    return ss.decide(req, m, ss.Policy())


def test_metrics_basic_properties():
    a = _art(64, 64)
    m = ss.compare(a, a, classic_alpha_mode="opaque")
    assert m["bit_identical"] and m["psnr_db"] == 99.0 and m["ssim"] > 0.999
    assert m["delta_e"] == 0 and m["resolution_ratio"] == 1.0
    b = ss.compare(a, _blockify(a), classic_alpha_mode="opaque")
    assert b["blockiness_excess"] > 0.06 and b["psnr_db"] < 40


def test_identical_hd_is_no_gain():
    a = _art(64, 64)
    rec = _run(a, a.copy())
    assert rec["decision"]["result"] == "classic"
    assert rec["decision"]["rule_fired"] == "R06_equivalent"


def test_lossy_same_res_copy_prefers_classic():
    a = _art(64, 64)
    rec = _run(a, _blockify(a))
    assert rec["decision"]["result"] == "classic"
    assert rec["decision"]["rule_fired"] == "R11_degraded_copy"


def test_same_res_is_not_rejected_categorically():
    """A same-size HD that is sharper and not blockier may win (R10)."""
    hi = _art(128, 128, freq=9)
    soft = np.clip(ss.resize(ss.resize(hi.astype(np.float32), 32, 32, down=True), 64, 64,
                             down=False), 0, 255).astype(np.uint8)   # blurry Classic
    sharp = _down(hi, 64, 64)                                        # crisper same-size HD
    rec = _run(soft, sharp)
    assert rec["decision"]["result"] == "hd", rec["rules_evaluated"]
    assert rec["decision"]["rule_fired"] == "R10_same_res_cleaner"


def test_true_upscale_with_detail_selects_hd():
    hi = _art(256, 256, seed=3, freq=12)
    classic = _down(hi, 64, 64)
    rec = _run(classic, hi)
    m = rec["candidates"][0]["metrics"]
    assert m["resolution_ratio"] == 16.0 and m["detail_energy"] >= 0.10
    assert rec["decision"]["result"] == "hd" and rec["decision"]["selected_candidate"] == "hd_tfe:x"


def test_interpolated_upscale_is_rejected():
    classic = _art(64, 64, seed=4)
    up = np.clip(ss.resize(classic.astype(np.float32), 256, 256, down=False), 0, 255).astype(np.uint8)
    rec = _run(classic, up)
    assert rec["decision"]["rule_fired"] == "R09_upscale_without_detail"
    assert rec["decision"]["base_source"] == "classic"


def test_recoloured_art_goes_to_opt_in_layer():
    a = _art(64, 64, seed=5)
    recol = a.copy()
    recol[..., 0] = np.clip(recol[..., 0].astype(int) + 70, 0, 255)
    recol[..., 2] = (recol[..., 2] * 0.5).astype(np.uint8)
    rec = _run(a, recol)
    d = rec["decision"]
    assert d["result"] == "hd_art_candidate" and d["requires_opt_in"] and d["base_source"] == "classic"
    assert d["hd_art"]["status"] == "pending_owner_opt_in"


def test_layout_mismatch_and_unverified_and_categories():
    a = _art(64, 64, seed=6)
    other = _art(64, 64, seed=99)
    assert _run(a, other)["decision"]["rule_fired"] == "R04_uv_incompatible"
    assert _run(a, _art(64, 32, seed=6))["decision"]["rule_fired"] == "R04_uv_incompatible"
    assert _run(a, a, verified=False)["decision"]["result"] == "classic_fallback"
    for cat in ("font", "technical"):
        assert _run(a, a, cat=cat)["decision"]["rule_fired"] == "R01_never_replace_category"
    req = _req()
    req["candidates"] = []
    assert ss.decide(req, {}, ss.Policy())["decision"]["result"] == "classic_fallback"


def test_companion_maps_modifier_and_alpha_rule():
    a = _art(64, 64, seed=7)
    rec = _run(a, _blockify(a), maps={"normal": "n.png"})
    assert rec["decision"]["result"] == "classic_with_hd_maps"
    assert rec["decision"]["companion_maps"] == ["normal"]
    cut = a.copy()
    cut[:16, :16, 3] = 0
    rec = _run(cut, a.copy(), alpha="cutout")  # HD lost the alpha channel
    assert rec["decision"]["rule_fired"] == "R05_alpha_mismatch"


def test_cli_end_to_end_and_schema(tmp_path):
    a = _art(64, 64, seed=8)
    texio.save_png(a, tmp_path / "c.png")
    texio.save_png(_blockify(a), tmp_path / "h.png")
    req = _req()
    req["classic"]["image"] = "c.png"
    req["candidates"][0]["image"] = "h.png"
    (tmp_path / "req.json").write_text(json.dumps([req]))
    out = tmp_path / "out"
    assert ss.run_cli(tmp_path / "req.json", None, out) == 0
    recs = [p for p in out.glob("sel-*.json")]
    assert len(recs) == 1
    rec = json.loads(recs[0].read_text())
    assert rec["decision"]["rule_fired"] == "R11_degraded_copy"
    assert rec["policy"]["thresholds"]["lossy_psnr_db"] == 40.0
    summary = json.loads((out / "summary.json").read_text())
    assert summary["by_category"]["architecture"] == {"classic:R11_degraded_copy": 1}
    # determinism: same inputs -> same decision and metrics
    again = ss.evaluate_request(json.loads((tmp_path / "req.json").read_text())[0], ss.Policy(), tmp_path)
    assert again["candidates"] == rec["candidates"] and again["decision"] == rec["decision"]
    schemas = sstre_schemas_dir()
    if schemas is None or not (schemas / "source-selection.schema.json").is_file():
        pytest.skip("source-selection schema not available (set SSTRE_SCHEMAS_DIR)")
    v = validator(schemas / "source-selection.schema.json")
    for r in (rec, again):
        errs = list(v.iter_errors(r))
        assert not errs, errs[0].message
