# SPDX-FileCopyrightText: 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe
# SPDX-License-Identifier: GPL-3.0-only
"""Platform profiles: limits, clamping, formats, memory budget. Synthetic."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from layer_fixtures import A_CLASSIC, config, entry, plan, rec, cand  # noqa: E402

from sstre_assetcompiler import platform_profiles as plat  # noqa: E402


@pytest.fixture(params=plat.BUILTIN_PROFILES)
def profile(request):
    return plat.load_builtin(request.param)


def test_builtin_profiles_load(profile):
    assert profile["profileId"] in plat.BUILTIN_PROFILES
    plat.check_profile(profile)


def test_pc_default_is_the_exp012_limit():
    p = plat.load_builtin("pc-default")
    lim = p["textureLimits"]
    assert lim["maxPixels"] == 2048 * 4096
    assert plat.violations(2048, 4096, lim) == []        # loaded in EXP-012
    assert plat.violations(2048, 2048, lim) == []
    assert "max-pixels" in plat.violations(4096, 4096, lim)  # crashed in EXP-012
    assert plat.clamp_size(4096, 4096, p)[0] == (2048, 2048)
    assert plat.clamp_size(8192, 4096, p)[0] == (4096, 2048)
    leg = plat.load_builtin("legacy-constrained")
    assert leg["status"] == "hypothetical"
    assert all(t["status"] == "not-validated" for t in leg["targets"])
    assert {"astc", "etc2", "palettised"} <= {t["targetId"] for t in leg["targets"]}


def test_clamp_policies_and_first_mip():
    p = plat.load_builtin("pc-default")
    (w, h), acts = plat.clamp_size(2048, 2048, p, classic_px=(256, 256), first_mip=1)
    assert (w, h) == (512, 512) and acts[0]["action"] == "first-mip-density-cap"
    (w, h), acts = plat.clamp_size(2048, 2048, p, classic_px=(256, 256), first_mip=1, keeps_mex=False)
    assert (w, h) == (2048, 2048) and acts == []
    fit = copy.deepcopy(p)
    fit["preprocessing"]["downscale"]["policy"] = "fit-exact"
    (w, h), acts = plat.clamp_size(3000, 3000, fit)
    assert w * h <= p["textureLimits"]["maxPixels"] and w == h == 2048
    assert acts[0]["detail"].startswith("fit-exact")
    leg = plat.load_builtin("legacy-constrained")
    (w, h), acts = plat.clamp_size(1000, 600, leg, classic_px=(1000, 600))  # per-axis power of two
    assert (w, h) == (512, 512)
    assert [a["action"] for a in acts] == ["round-down-power-of-two", "below-classic-resolution"]
    (w, h), acts = plat.clamp_size(2048, 1024, leg, classic_px=(2048, 1024))
    assert (w, h) == (512, 256)
    assert [a["action"] for a in acts] == ["downscale", "below-classic-resolution"]


def test_formats_per_role():
    p = plat.load_builtin("pc-default")
    assert plat.formats_for(p, "albedo", "opaque") == {"raster": "rgb8", "rt": "bc7"}
    assert plat.formats_for(p, "albedo", "blend") == {"raster": "rgba8", "rt": "bc7"}
    assert plat.formats_for(p, "normal", None) == {"raster": None, "rt": "bc5"}
    low = plat.load_builtin("pc-low-memory")
    assert plat.formats_for(low, "albedo", "opaque")["rt"] == "bc1"
    assert plat.texture_bytes(4, 4, "bc1", False) == 8
    assert plat.texture_bytes(5, 5, "bc7", False) == 64  # padded to 8x8 blocks
    assert plat.texture_bytes(2, 2, "rgba8", True) == 16 + 4
    assert plat.texture_bytes(8, 8, None, True) == 0
    assert plat.texture_bytes(6, 6, "astc-6x6", False) == 16


def test_profile_errors():
    p = plat.load_builtin("pc-default")
    for mutate, msg in [
        (lambda q: q.pop("targets"), "lacks 'targets'"),
        (lambda q: q["textureLimits"].update(maxPixels=0), "maxPixels"),
        (lambda q: q["targets"][0]["formats"].update(mask="bc9"), "unknown format"),
        (lambda q: q["targets"][0]["formats"].update(specular="bc4"), "role slot"),
        (lambda q: q["targets"][0]["formats"].pop("color-opaque"), "color-opaque"),
        (lambda q: q["targets"].append(dict(q["targets"][0])), "duplicate targetId"),
        (lambda q: q["memoryBudget"].update(accountingTarget="gpu"), "accountingTarget"),
        (lambda q: q["preprocessing"]["downscale"].update(policy="crop"), "downscale.policy"),
        (lambda q: q.__setitem__("targets", []), "at least one target"),
    ]:
        q = copy.deepcopy(p)
        mutate(q)
        with pytest.raises(plat.ProfileError, match=msg):
            plat.check_profile(q)
    with pytest.raises(plat.ProfileError, match="unknown platform profile"):
        plat.load_builtin("console-x")


def test_profile_clamping_in_plan(tmp_path):
    recs = [rec("classic_tfe:big.tex", "hd", "R08_upscale_with_detail", classic_px=(1024, 1024),
                cands=[cand("hd:big", (4096, 4096), (1024, 1024))], selected="hd:big"),
            rec(A_CLASSIC, classic_px=(1024, 512))]
    m = plan(tmp_path / "a", config(), recs)
    e = entry(m, "classic_tfe:big.tex")
    assert e["platform"]["textures"][0]["targetPx"] == [2048, 2048]
    assert e["platform"]["actions"][0]["action"] == "downscale"
    assert "downscale:halve-until-fit:lanczos3:linear-light" in e["platform"]["textures"][0]["preprocessing"]
    m = plan(tmp_path / "b", config(**{"ai-enhancement": {"qualityProfile": "standard"}},
                                    platformProfile="pc-low-memory"), recs)
    p = entry(m, A_CLASSIC)["processing"]
    assert p["plannedClampedFrom"] == [4096, 2048] and p["plannedPx"] == [2048, 1024]
    assert entry(m, "classic_tfe:big.tex")["platform"]["textures"][0]["targetPx"] == [2048, 2048]
    assert m["effectiveConfig"]["platformProfile"]["id"] == "pc-low-memory"


def test_inline_profile_and_budget(tmp_path):
    prof = plat.load_builtin("pc-default")
    prof = dict(prof, profileId="tiny-budget",
                memoryBudget={"bytes": 300_000, "accountingTarget": "raster", "includeMips": False,
                              "policy": "reduce-largest"})
    prof["preprocessing"]["downscale"]["neverBelowClassic"] = True
    recs = [rec(f"classic_tfe:t{i}.tex", "hd", "R08_upscale_with_detail", classic_px=(64, 64),
                cands=[cand(f"hd:t{i}", (512, 512), (64, 64))], selected=f"hd:t{i}") for i in range(3)]
    m = plan(tmp_path / "a", config(platformProfile=prof), recs)
    b = m["summary"]["budget"]
    assert b["estimatedBytesBefore"] == 3 * 512 * 512 * 3
    assert b["withinBudget"] and b["estimatedBytes"] <= 300_000 and b["budgetHalvings"] > 0
    sizes = [entry(m, f"classic_tfe:t{i}.tex")["platform"]["textures"][0]["targetPx"] for i in range(3)]
    # deterministic order: largest first, ties by asset ID
    assert sizes == [[128, 128], [128, 128], [256, 256]]
    assert b["estimatedBytes"] == 2 * 128 * 128 * 3 + 256 * 256 * 3
    # never below Classic: an impossible budget stays over budget
    prof2 = dict(prof, memoryBudget=dict(prof["memoryBudget"], bytes=10))
    m = plan(tmp_path / "b", config(platformProfile=prof2), recs)
    assert not m["summary"]["budget"]["withinBudget"]
    assert all(entry(m, f"classic_tfe:t{i}.tex")["platform"]["textures"][0]["targetPx"] == [64, 64]
               for i in range(3))
    report = dict(prof, memoryBudget=dict(prof["memoryBudget"], policy="report"))
    m = plan(tmp_path / "c", config(platformProfile=report), recs)
    assert m["summary"]["budget"]["budgetHalvings"] == 0 and not m["summary"]["budget"]["withinBudget"]


def test_ai_target_respects_first_mip_and_budget(tmp_path):
    from layer_fixtures import make_workspace, standard_records, write_json
    from sstre_assetcompiler import layers
    p = make_workspace(tmp_path / "a", config(**{"ai-enhancement": {"qualityProfile": "standard"}}),
                       standard_records())
    facts = write_json(tmp_path / "a" / "facts.json", [{"asset_id": A_CLASSIC, "first_mip": 1}])
    m = layers.build_plan(p, [tmp_path / "a" / "records"], facts)
    proc = entry(m, A_CLASSIC)["processing"]
    assert proc["plannedPx"] == [512, 512] and proc["plannedClampedFrom"] == [1024, 1024]
    write_json(facts, [{"asset_id": A_CLASSIC, "first_mip": 0}])
    m = layers.build_plan(p, [tmp_path / "a" / "records"], facts)
    e = entry(m, A_CLASSIC)
    assert e["processing"]["status"] == "platform-capped"
    assert next(s for s in e["layers"] if s["layer"] == "ai-enhancement")["status"] == "platform-capped"
    # the memory budget counts planned AI sizes and reduces them first
    prof = plat.load_builtin("pc-default")
    prof["memoryBudget"] = {"bytes": 3 * 128 * 128 * 3, "accountingTarget": "raster", "includeMips": False,
                            "policy": "reduce-largest"}
    recs = [rec(f"classic_tfe:s{i}.tex", classic_px=(64, 64)) for i in range(3)]
    m = plan(tmp_path / "b", config(**{"ai-enhancement": {"qualityProfile": "standard"}}, platformProfile=prof), recs)
    b = m["summary"]["budget"]
    assert b["estimatedBytesBefore"] == 3 * 256 * 256 * 3 and b["estimatedBytesWithoutPendingAi"] == 3 * 64 * 64 * 3
    assert b["withinBudget"]
    assert [entry(m, f"classic_tfe:s{i}.tex")["processing"]["plannedPx"] for i in range(3)] == [[128, 128]] * 3
    prof["memoryBudget"]["bytes"] = 3 * 64 * 64 * 3
    m = plan(tmp_path / "c", config(**{"ai-enhancement": {"qualityProfile": "standard"}}, platformProfile=prof), recs)
    assert {entry(m, f"classic_tfe:s{i}.tex")["processing"]["status"] for i in range(3)} == {"platform-capped"}
