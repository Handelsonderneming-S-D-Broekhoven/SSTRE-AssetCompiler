# SPDX-FileCopyrightText: 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe
# SPDX-License-Identifier: GPL-3.0-only
"""Layer rules, opt-in/approval gates, override precedence, AI layer interface
and determinism of the layered plan (ADR-0009). Synthetic data only."""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from layer_fixtures import (A_ART, A_CLASSIC, A_FONT, A_HD, A_MAPS, A_RELAYOUT, A_UI,  # noqa: E402
                            ai_entry, approvals_doc, approve, cand, config, entry, h,
                            make_workspace, pack_doc, plan, rec, standard_records, status,
                            write_json)

from sstre_assetcompiler import layers  # noqa: E402
from sstre_assetcompiler.layers import ConfigError  # noqa: E402


# ------------------------------------------------------------ defaults ----

def test_defaults_follow_adr_0009(tmp_path):
    m = plan(tmp_path, config())
    eff = {l["layer"]: l for l in m["effectiveConfig"]["layers"]}
    assert [l["layer"] for l in m["effectiveConfig"]["layers"]] == list(layers.LAYERS)
    assert [l["order"] for l in m["effectiveConfig"]["layers"]] == [1, 2, 3, 4, 5, 6, 7]
    assert eff["classic-base"]["enabled"] and eff["hd-quality"]["enabled"] and eff["hd-material-maps"]["enabled"]
    assert not eff["hd-artwork"]["enabled"] and not eff["hd-interface"]["enabled"]
    assert not eff["ai-enhancement"]["enabled"] and eff["ai-enhancement"]["qualityProfile"] == "off"
    assert not eff["overrides"]["enabled"]
    assert eff["hd-artwork"]["requiresApproval"] and eff["hd-interface"]["requiresApproval"]
    # Classic is the default experience
    assert entry(m, A_CLASSIC)["suppliedBy"] == "classic-base"
    assert entry(m, A_FONT)["suppliedBy"] == "classic-base"
    assert entry(m, A_ART)["suppliedBy"] == "classic-base"
    assert status(entry(m, A_ART), "hd-artwork") == "disabled"
    assert entry(m, A_UI)["suppliedBy"] == "classic-base"
    assert status(entry(m, A_UI), "hd-interface") == "disabled"
    assert status(entry(m, A_UI), "hd-artwork") == "not-applicable"
    # verified superior HD only
    e = entry(m, A_HD)
    assert e["suppliedBy"] == "hd-quality" and e["source"]["kind"] == "hd"
    assert e["source"]["sha256"] == "sha256:" + h("hd_tfe:syn/upscale")
    assert e["selection"]["ruleFired"] == "R08_upscale_with_detail"
    assert e["selection"]["scores"]["resolution_ratio"] == 16.0
    assert status(e, "classic-base") == "superseded"
    assert e["processedBy"] is None
    assert m["summary"]["suppliedBy"] == {"classic-base": 6, "hd-quality": 1}
    assert m["localOnly"] is True


def test_classic_base_cannot_be_disabled(tmp_path):
    with pytest.raises(ConfigError, match="classic-base"):
        plan(tmp_path, config(**{"classic-base": {"enabled": False}}))


def test_unknown_layer_and_profile_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown layer"):
        plan(tmp_path / "a", config(**{"hd-everything": {"enabled": True}}))
    with pytest.raises(ConfigError, match="quality profile"):
        plan(tmp_path / "b", config(**{"ai-enhancement": {"qualityProfile": "ultra"}}))
    with pytest.raises(ValueError, match="platform profile"):
        plan(tmp_path / "c", config(platformProfile="console-x"))
    with pytest.raises(ConfigError, match="configFormatVersion"):
        plan(tmp_path / "d", dict(config(), configFormatVersion="9.0.0"))


def test_hd_quality_disabled_and_unverified(tmp_path):
    m = plan(tmp_path / "a", config(**{"hd-quality": {"enabled": False}}))
    e = entry(m, A_HD)
    assert e["suppliedBy"] == "classic-base" and status(e, "hd-quality") == "disabled"
    bad = rec(A_HD, "hd", "R08_upscale_with_detail",
              cands=[cand("hd_tfe:x", (1024, 1024), verified=False)], selected="hd_tfe:x")
    m = plan(tmp_path / "b", config(), [bad])
    e = entry(m, A_HD)
    assert e["suppliedBy"] == "classic-base" and status(e, "hd-quality") == "blocked"


def test_degraded_or_equivalent_hd_never_replaces_classic(tmp_path):
    recs = [rec("classic_tfe:a.tex", "classic", "R11_degraded_copy", cands=[cand("hd:a", (256, 256))]),
            rec("classic_tfe:b.tex", "classic", "R06_equivalent", cands=[cand("hd:b", (256, 256))]),
            rec("classic_tfe:c.tex", "classic", "R09_upscale_without_detail", cands=[cand("hd:c")])]
    m = plan(tmp_path, config(**{"hd-artwork": {"enabled": True}, "hd-interface": {"enabled": True}}), recs)
    assert {e["suppliedBy"] for e in m["assets"]} == {"classic-base"}


# ------------------------------------------------- opt-in and approvals ----

def test_hd_artwork_needs_opt_in_and_approval(tmp_path):
    ok = approvals_doc(approve(A_ART, "hd-artwork", "hd_tfe:syn/statue", approvedBy="owner"))
    # approval without opt-in: ignored
    m = plan(tmp_path / "a", config(), approvals=ok)
    assert entry(m, A_ART)["suppliedBy"] == "classic-base"
    assert status(entry(m, A_ART), "hd-artwork") == "disabled"
    # opt-in without approval: awaiting
    m = plan(tmp_path / "b", config(**{"hd-artwork": {"enabled": True}}))
    assert entry(m, A_ART)["suppliedBy"] == "classic-base"
    assert status(entry(m, A_ART), "hd-artwork") == "awaiting-approval"
    # both: applied
    m = plan(tmp_path / "c", config(**{"hd-artwork": {"enabled": True}}), approvals=ok)
    e = entry(m, A_ART)
    assert e["suppliedBy"] == "hd-artwork" and e["source"]["id"] == "hd_tfe:syn/statue"
    assert "owner" in next(s["detail"] for s in e["layers"] if s["layer"] == "hd-artwork")
    assert m["effectiveConfig"]["approvals"]["count"] == 1
    # HD Artwork opt-in never touches interface art
    assert entry(m, A_UI)["suppliedBy"] == "classic-base"


@pytest.mark.parametrize("row,expect", [
    (approve(A_ART, "hd-artwork", "hd_tfe:syn/statue", "rejected"), "rejected"),
    (approve(A_ART, "hd-artwork", "hd_tfe:other"), "blocked"),
    (approve(A_ART, "hd-artwork", "hd_tfe:syn/statue", candidateSha256="sha256:" + "0" * 64), "blocked"),
    (approve(A_ART, "hd-artwork", "hd_tfe:syn/statue", candidateSha256=h("hd_tfe:syn/statue")), "applied"),
])
def test_approval_variants(tmp_path, row, expect):
    m = plan(tmp_path, config(**{"hd-artwork": {"enabled": True}}), approvals=approvals_doc(row))
    e = entry(m, A_ART)
    assert status(e, "hd-artwork") == expect
    assert e["suppliedBy"] == ("hd-artwork" if expect == "applied" else "classic-base")


def test_record_rejected_hd_art_is_not_applied(tmp_path):
    r = rec(A_ART, "hd_art_candidate", "R07_art_difference", category="props",
            cands=[cand("hd_tfe:syn/statue")], art="hd_tfe:syn/statue", art_status="rejected")
    m = plan(tmp_path, config(**{"hd-artwork": {"enabled": True}}), [r],
             approvals=approvals_doc(approve(A_ART, "hd-artwork", "hd_tfe:syn/statue")))
    assert status(entry(m, A_ART), "hd-artwork") == "rejected"


def test_hd_interface_is_a_separate_opt_in(tmp_path):
    apr = approvals_doc(approve(A_UI, "hd-interface", "hd_tfe:syn/logo"),
                        approve(A_ART, "hd-artwork", "hd_tfe:syn/statue"))
    m = plan(tmp_path / "a", config(**{"hd-artwork": {"enabled": True}}), approvals=apr)
    assert entry(m, A_UI)["suppliedBy"] == "classic-base"
    assert entry(m, A_ART)["suppliedBy"] == "hd-artwork"
    m = plan(tmp_path / "b", config(**{"hd-interface": {"enabled": True}}), approvals=apr)
    assert entry(m, A_UI)["suppliedBy"] == "hd-interface"
    assert entry(m, A_ART)["suppliedBy"] == "classic-base"
    # an approval filed under the wrong layer does not count
    wrong = approvals_doc(approve(A_UI, "hd-artwork", "hd_tfe:syn/logo"))
    m = plan(tmp_path / "c", config(**{"hd-artwork": {"enabled": True}, "hd-interface": {"enabled": True}}),
             approvals=wrong)
    assert entry(m, A_UI)["suppliedBy"] == "classic-base"
    assert status(entry(m, A_UI), "hd-interface") == "awaiting-approval"


def test_interface_relayout_candidates(tmp_path):
    apr = approvals_doc(approve(A_RELAYOUT, "hd-interface", "hd_tfe:syn/page"))
    m = plan(tmp_path / "a", config(**{"hd-interface": {"enabled": True}}), approvals=apr)
    assert entry(m, A_RELAYOUT)["suppliedBy"] == "classic-base"  # includeRelayout off
    m = plan(tmp_path / "b", config(**{"hd-interface": {"enabled": True, "includeRelayout": True}}),
             approvals=apr)
    e = entry(m, A_RELAYOUT)
    assert e["suppliedBy"] == "hd-interface" and e["source"]["layoutChanged"] is True
    assert e["platform"]["textures"][0]["targetPx"] == [1024, 768]
    m = plan(tmp_path / "c", config(**{"hd-interface": {"enabled": True, "includeRelayout": True}}))
    assert status(entry(m, A_RELAYOUT), "hd-interface") == "awaiting-approval"


def test_approval_file_errors(tmp_path):
    dup = approvals_doc(approve(A_ART, "hd-artwork", "c"), approve(A_ART, "hd-artwork", "c"))
    with pytest.raises(ConfigError, match="duplicate approval"):
        plan(tmp_path / "a", config(), approvals=dup)
    with pytest.raises(ConfigError, match="only hd-artwork"):
        plan(tmp_path / "b", config(), approvals=approvals_doc(approve(A_ART, "hd-quality", "c")))
    with pytest.raises(ConfigError, match="approved or rejected"):
        plan(tmp_path / "c", config(), approvals=approvals_doc(approve(A_ART, "hd-artwork", "c", "maybe")))
    p = make_workspace(tmp_path / "d", config(), standard_records(), approvals=approvals_doc())
    cfg = json.loads(p.read_text())
    cfg["approvals"]["sha256"] = "sha256:" + "1" * 64
    p.write_text(json.dumps(cfg))
    with pytest.raises(ConfigError, match="hash mismatch"):
        layers.build_plan(p, [tmp_path / "d" / "records"])


def test_multiple_approved_candidates_use_priority_then_id(tmp_path):
    recs = [rec(A_ART, "hd_art_candidate", "R07_art_difference", category="props",
                cands=[cand("hd:b")], art="hd:b", policy_version="0.1.0"),
            rec(A_ART, "hd_art_candidate", "R07_art_difference", category="props",
                cands=[cand("hd:a")], art="hd:a", policy_version="0.2.0")]
    apr = approvals_doc(approve(A_ART, "hd-artwork", "hd:a"), approve(A_ART, "hd-artwork", "hd:b"))
    m = plan(tmp_path / "a", config(**{"hd-artwork": {"enabled": True}}), recs, approvals=apr)
    assert m["conflicts"] and m["conflicts"][0]["kind"] == "multiple-selection-records"
    chosen = m["conflicts"][0]["chosen"]
    assert entry(m, A_ART)["selection"]["recordId"] == chosen == min(r["record_id"] for r in recs)
    # restricting the policy selects the other record deterministically
    cfg = config(**{"hd-artwork": {"enabled": True}},
                 extra={"selection": {"policyId": "sstre-source-selection", "policyVersion": "0.2.0"}})
    m = plan(tmp_path / "b", cfg, recs, approvals=apr)
    assert entry(m, A_ART)["source"]["id"] == "hd:a" and not m["conflicts"]


# --------------------------------------------------------- material maps ----

def test_material_maps_rules(tmp_path):
    m = plan(tmp_path / "a", config())
    e = entry(m, A_MAPS)
    assert e["suppliedBy"] == "classic-base"
    assert [mm["role"] for mm in e["materialMaps"]] == ["ao", "normal"]
    assert {mm["layer"] for mm in e["materialMaps"]} == {"hd-material-maps"}
    assert status(e, "hd-material-maps") == "applied"
    # maps of the selected HD source
    e = entry(m, A_HD)
    assert [mm["role"] for mm in e["materialMaps"]] == ["gloss", "normal"]
    tex = {t["role"]: t for t in e["platform"]["textures"]}
    assert tex["normal"]["formats"] == {"raster": None, "rt": "bc5"}
    assert tex["gloss"]["formats"] == {"raster": None, "rt": "bc4"}
    # layer off
    m = plan(tmp_path / "b", config(**{"hd-material-maps": {"enabled": False}}))
    assert entry(m, A_MAPS)["materialMaps"] == [] and status(entry(m, A_MAPS), "hd-material-maps") == "disabled"
    # roles restricted by config, then by the platform profile
    m = plan(tmp_path / "c", config(**{"hd-material-maps": {"roles": ["ao"]}}))
    assert [mm["role"] for mm in entry(m, A_MAPS)["materialMaps"]] == ["ao"]
    m = plan(tmp_path / "d", config(platformProfile="pc-low-memory"))
    assert [mm["role"] for mm in entry(m, A_MAPS)["materialMaps"]] == ["normal"]
    m = plan(tmp_path / "e", config(platformProfile="legacy-constrained"))
    assert entry(m, A_MAPS)["materialMaps"] == []
    assert status(entry(m, A_MAPS), "hd-material-maps") == "dropped"
    with pytest.raises(ConfigError, match="material-map role"):
        plan(tmp_path / "f", config(**{"hd-material-maps": {"roles": ["specular"]}}))


# ------------------------------------------------------------ overrides ----

def _ov(pack_id, kind, entries, priority=0, version="1.0.0"):
    return ("overrides", {"priority": priority}, pack_doc(pack_id, kind, entries, version))


def _repl(asset_id, label, px=(512, 512), **kw):
    return dict({"assetId": asset_id, "file": f"{label}.png", "sha256": "sha256:" + h(label), "px": list(px)}, **kw)


def test_overrides_disabled_by_default(tmp_path):
    m = plan(tmp_path, config(), packs=[_ov("cur", "curated", [_repl(A_HD, "x")])])
    e = entry(m, A_HD)
    assert e["suppliedBy"] == "hd-quality" and status(e, "overrides") == "disabled"


def test_override_precedence(tmp_path):
    packs = [_ov("community-b", "community", [_repl(A_HD, "cb"), _repl(A_ART, "cb2")], priority=5),
             _ov("curated-a", "curated", [_repl(A_HD, "ca")], priority=5),
             _ov("community-a", "community", [_repl(A_HD, "caa"), _repl(A_ART, "caa2")], priority=5),
             _ov("low", "curated", [_repl(A_CLASSIC, "low"), _repl(A_HD, "low2")], priority=1)]
    cfg = config(**{"overrides": {"enabled": True}, "hd-artwork": {"enabled": True}})
    m = plan(tmp_path, cfg, packs=packs,
             approvals=approvals_doc(approve(A_ART, "hd-artwork", "hd_tfe:syn/statue")))
    e = entry(m, A_HD)  # same priority: curated before community
    assert e["suppliedBy"] == "overrides" and e["source"]["pack"]["packId"] == "curated-a"
    assert status(e, "hd-quality") == "superseded" and e["materialMaps"] == []
    e = entry(m, A_ART)  # same priority and kind: packId order; overrides beat HD Artwork
    assert e["source"]["pack"]["packId"] == "community-a" and status(e, "hd-artwork") == "superseded"
    assert entry(m, A_CLASSIC)["source"]["pack"]["packId"] == "low"
    assert [p["packId"] for p in m["effectiveConfig"]["overridePacks"]] == \
        ["curated-a", "community-a", "community-b", "low"]


def test_override_actions(tmp_path):
    packs = [_ov("cur", "curated", [{"assetId": A_HD, "action": "use-classic"},
                                    _repl(A_MAPS, "m", keepMaps=True),
                                    {"assetId": A_CLASSIC, "action": "skip-ai"}])]
    cfg = config(**{"overrides": {"enabled": True}, "ai-enhancement": {"qualityProfile": "conservative"}})
    m = plan(tmp_path, cfg, packs=packs)
    e = entry(m, A_HD)
    assert e["suppliedBy"] == "classic-base" and e["source"]["decidedBy"] == "overrides"
    assert status(e, "hd-quality") == "superseded"
    e = entry(m, A_MAPS)
    assert e["suppliedBy"] == "overrides" and [mm["role"] for mm in e["materialMaps"]] == ["ao", "normal"]
    assert e["processing"]["status"] == "suppressed"
    assert entry(m, A_CLASSIC)["processing"]["status"] == "suppressed"


def test_override_map_replacement(tmp_path):
    packs = [_ov("cur", "curated", [_repl(A_MAPS, "nm", role="normal")])]
    m = plan(tmp_path, config(**{"overrides": {"enabled": True}}), packs=packs)
    maps = {mm["role"]: mm for mm in entry(m, A_MAPS)["materialMaps"]}
    assert maps["normal"]["layer"] == "overrides" and maps["ao"]["layer"] == "hd-material-maps"


def test_pack_errors(tmp_path):
    with pytest.raises(ConfigError, match="duplicate entry"):
        plan(tmp_path / "a", config(), packs=[_ov("p", "curated", [_repl(A_HD, "a"), _repl(A_HD, "b")])])
    with pytest.raises(ConfigError, match="lacks 'px'"):
        plan(tmp_path / "b", config(), packs=[_ov("p", "curated", [{"assetId": A_HD, "sha256": "sha256:" + h("x")}])])
    with pytest.raises(ConfigError, match="unknown action"):
        plan(tmp_path / "c", config(), packs=[_ov("p", "curated", [{"assetId": A_HD, "action": "delete"}])])
    with pytest.raises(ConfigError, match="kind"):
        plan(tmp_path / "d", config(), packs=[("overrides", {}, pack_doc("p", "ai-enhancement", []))])
    with pytest.raises(ConfigError, match="duplicate packId"):
        plan(tmp_path / "e", config(), packs=[_ov("p", "curated", []), _ov("p", "community", [])])
    p = make_workspace(tmp_path / "f", config(), standard_records(), packs=[_ov("p", "curated", [])])
    cfg = json.loads(p.read_text())
    cfg["layers"]["overrides"]["packs"][0]["version"] = "2.0.0"
    p.write_text(json.dumps(cfg))
    with pytest.raises(ConfigError, match="version"):
        layers.build_plan(p, [tmp_path / "f" / "records"])
    cfg["layers"]["overrides"]["packs"][0].update(version="1.0.0", sha256="sha256:" + "2" * 64)
    p.write_text(json.dumps(cfg))
    with pytest.raises(ConfigError, match="hash mismatch"):
        layers.build_plan(p, [tmp_path / "f" / "records"])
    with pytest.raises(ConfigError, match="integer"):
        plan(tmp_path / "g", config(), packs=[("overrides", {"priority": "high"}, pack_doc("q", "curated", []))])


# ------------------------------------------------------- AI enhancement ----

def _ai(entries, quality="conservative", version="0.1.0", priority=0, pack_id="ai"):
    return ("ai-enhancement", {"priority": priority},
            pack_doc(pack_id, "ai-enhancement", entries, version=version, quality=quality))


def test_ai_off_and_planned(tmp_path):
    m = plan(tmp_path / "a", config())
    assert {e["processing"]["status"] for e in m["assets"]} == {"off"}
    m = plan(tmp_path / "b", config(**{"ai-enhancement": {"qualityProfile": "conservative"}}))
    p = entry(m, A_CLASSIC)["processing"]
    assert p["status"] == "awaiting-output" and p["mode"] == "sr" and p["scale"] == 2
    assert p["plannedPx"] == [512, 512]
    assert entry(m, A_UI)["processing"]["status"] == "not-eligible"      # UI excluded
    assert entry(m, A_FONT)["processing"]["status"] == "not-eligible"    # fonts excluded
    assert entry(m, A_HD)["processing"]["status"] == "source-sufficient"  # HD already 4x
    assert entry(m, A_CLASSIC)["processedBy"] is None


def test_ai_output_matching(tmp_path):
    good = ai_entry(A_CLASSIC, h(A_CLASSIC))
    m = plan(tmp_path / "a", config(**{"ai-enhancement": {"qualityProfile": "conservative"}}),
             packs=[_ai([good, ai_entry(A_CLASSIC, h(A_CLASSIC), (512, 512), role="normal")])])
    e = entry(m, A_CLASSIC)
    assert e["processing"]["status"] == "applied" and e["processedBy"] == "ai-enhancement"
    assert e["suppliedBy"] == "classic-base"
    assert e["processing"]["output"]["recipeHash"] == "sha256:" + h("recipe")
    assert e["platform"]["textures"][0]["inputPx"] == [512, 512]
    assert [mm for mm in e["materialMaps"] if mm["layer"] == "ai-enhancement"][0]["inferred"] is True
    # rejected outputs stay visible
    for sub, ent, quality, why in [
        ("b", ai_entry(A_CLASSIC, h("something else")), "conservative", "input hash"),
        ("c", ai_entry(A_CLASSIC, h(A_CLASSIC), passed=False), "conservative", "QA failed"),
        ("d", ai_entry(A_CLASSIC, h(A_CLASSIC)), "standard", "profile standard"),
        ("e", ai_entry(A_CLASSIC, h(A_CLASSIC), (512, 256)), "conservative", "aspect"),
        ("f", ai_entry(A_CLASSIC, h(A_CLASSIC), (128, 128)), "conservative", "smaller"),
    ]:
        m = plan(tmp_path / sub, config(**{"ai-enhancement": {"qualityProfile": "conservative"}}),
                 packs=[_ai([ent], quality=quality)])
        p = entry(m, A_CLASSIC)["processing"]
        assert p["status"] == "awaiting-output", sub
        assert why in " ".join(p["rejectedOutputs"]), sub


def test_ai_pack_priority_and_hd_source(tmp_path):
    a = ai_entry(A_CLASSIC, h(A_CLASSIC))
    b = dict(ai_entry(A_CLASSIC, h(A_CLASSIC)), sha256="sha256:" + h("b-output"))
    m = plan(tmp_path / "a", config(**{"ai-enhancement": {"qualityProfile": "conservative"}}),
             packs=[_ai([a], pack_id="ai-a", priority=1), _ai([b], pack_id="ai-b", priority=2)])
    assert entry(m, A_CLASSIC)["processing"]["output"]["packId"] == "ai-b"
    # the AI layer processes the *selected* source: an output made from Classic
    # pixels is not used when HD Artwork supplies the source
    cfg = config(**{"ai-enhancement": {"qualityProfile": "standard"}, "hd-artwork": {"enabled": True}})
    m = plan(tmp_path / "b", cfg, approvals=approvals_doc(approve(A_ART, "hd-artwork", "hd_tfe:syn/statue")),
             packs=[_ai([ai_entry(A_ART, h(A_ART), (1024, 1024))], quality="standard")])
    p = entry(m, A_ART)["processing"]
    assert p["status"] == "awaiting-output" and "input hash" in p["rejectedOutputs"][0]
    m = plan(tmp_path / "c", cfg, approvals=approvals_doc(approve(A_ART, "hd-artwork", "hd_tfe:syn/statue")),
             packs=[_ai([ai_entry(A_ART, h("hd_tfe:syn/statue"), (1024, 1024))], quality="standard")])
    assert entry(m, A_ART)["processing"]["status"] == "applied"


def test_ai_pack_entry_validation(tmp_path):
    bad = ai_entry(A_CLASSIC, h(A_CLASSIC))
    del bad["recipeHash"]
    with pytest.raises(ConfigError, match="recipeHash"):
        plan(tmp_path / "a", config(), packs=[_ai([bad])])
    with pytest.raises(ConfigError, match="action replace"):
        plan(tmp_path / "b", config(), packs=[_ai([dict(ai_entry(A_CLASSIC, "00"), action="skip-ai")])])


def test_custom_quality_profile(tmp_path):
    qp = {"mine": {"enabled": True, "defaultMode": "sr", "processSources": ["classic-base"],
                   "classes": {"ui": {"mode": "skip"}}}}
    m = plan(tmp_path, config(**{"ai-enhancement": {"qualityProfile": "mine"}}, extra={"qualityProfiles": qp}))
    assert entry(m, A_FONT)["processing"]["status"] == "awaiting-output"
    assert entry(m, A_FONT)["processing"]["scale"] == 2
    assert entry(m, A_UI)["processing"]["status"] == "not-eligible"
    assert entry(m, A_HD)["processing"]["status"] == "not-eligible"  # HD source not in processSources


# ---------------------------------------------------------- determinism ----

def _shuffled_workspace(tmp, seed):
    rnd = random.Random(seed)
    recs = standard_records()
    rnd.shuffle(recs)
    for r in recs:
        r["generated_at"] = f"2026-11-{rnd.randint(1, 28):02d}T00:00:00Z"
    rows = [approve(A_ART, "hd-artwork", "hd_tfe:syn/statue"), approve(A_UI, "hd-interface", "hd_tfe:syn/logo"),
            approve(A_RELAYOUT, "hd-interface", "hd_tfe:syn/page", "rejected")]
    rnd.shuffle(rows)
    packs = [_ov("cur", "curated", [_repl(A_CLASSIC, "c"), _repl(A_MAPS, "m")], priority=3),
             _ov("com", "community", [_repl(A_CLASSIC, "d")], priority=3),
             _ai([ai_entry(A_CLASSIC, h(A_CLASSIC))], pack_id="ai-1"),
             _ai([ai_entry(A_CLASSIC, h(A_CLASSIC))], pack_id="ai-0")]
    rnd.shuffle(packs)
    for _, _, doc in packs:
        rnd.shuffle(doc["entries"])
    cfg = config(**{"hd-artwork": {"enabled": True}, "hd-interface": {"enabled": True, "includeRelayout": True},
                    "overrides": {"enabled": True}, "ai-enhancement": {"qualityProfile": "conservative"}},
                 platformProfile="pc-low-memory")
    return plan(tmp, cfg, recs, approvals=approvals_doc(*rows), packs=packs)


def test_shuffled_inputs_give_identical_plan(tmp_path):
    hashes = {(m["planHash"], m["effectiveConfigHash"])
              for m in (_shuffled_workspace(tmp_path / str(s), s) for s in range(6))}
    assert len(hashes) == 1


def test_plan_hash_tracks_inputs(tmp_path):
    base = plan(tmp_path / "a", config())
    again = plan(tmp_path / "b", config())
    assert base["planHash"] == again["planHash"]
    recs = standard_records()
    recs[0]["policy"]["thresholds"]["lossy_psnr_db"] = 41.0
    changed = plan(tmp_path / "c", config(), recs)
    assert changed["planHash"] != base["planHash"]
    assert changed["effectiveConfig"]["selection"]["recordSetHash"] != \
        base["effectiveConfig"]["selection"]["recordSetHash"]
    other = plan(tmp_path / "d", config(platformProfile="pc-low-memory"))
    assert other["effectiveConfigHash"] != base["effectiveConfigHash"]


def test_asset_facts_and_only_listed(tmp_path):
    p = make_workspace(tmp_path, config(**{"ai-enhancement": {"qualityProfile": "standard"}}), standard_records())
    facts = write_json(tmp_path / "facts.json", [
        {"asset_id": A_CLASSIC, "first_mip": 1, "image_sha256": h("png")},
        {"asset_id": "classic_tfe:textures/syn/no_record.tex", "category": "terrain", "width_px": 64,
         "height_px": 64, "alpha_mode": "opaque"}])
    m = layers.build_plan(p, [tmp_path / "records"], facts)
    e = entry(m, A_CLASSIC)
    assert e["classic"]["firstMip"] == 1 and e["source"]["imageSha256"] == "sha256:" + h("png")
    n = entry(m, "classic_tfe:textures/syn/no_record.tex")
    assert n["selection"] is None and n["suppliedBy"] == "classic-base"
    assert len(m["assets"]) == 8
    m = layers.build_plan(p, [tmp_path / "records"], facts, only_listed=True)
    assert [e["assetId"] for e in m["assets"]] == sorted([A_CLASSIC, "classic_tfe:textures/syn/no_record.tex"])
    assert m["effectiveConfig"]["assetFacts"]["onlyListed"] is True


def test_warnings(tmp_path):
    p = make_workspace(tmp_path, config(**{"hd-artwork": {"enabled": True}}, platformProfile="legacy-constrained"),
                       standard_records(), approvals=approvals_doc(approve(A_UI, "hd-interface", "x")),
                       packs=[_ov("p", "curated", []), _ai([], quality="standard")])
    _, w = layers.validate_config(p)
    text = "\n".join(w)
    assert "hd-artwork is opted in but no asset is approved" in text
    assert "approval(s) for hd-interface are ignored" in text
    assert "overrides layer is disabled" in text
    assert "hypothetical" in text
    assert "quality profile 'standard'" in text


def test_validate_config_verify_files(tmp_path):
    p = make_workspace(tmp_path, config(**{"overrides": {"enabled": True}}), standard_records(),
                       packs=[_ov("p", "curated", [_repl(A_HD, "x")])])
    (tmp_path / "packs" / "p" / "x.png").write_bytes(b"not the fixture")
    layers.validate_config(p)
    with pytest.raises(ConfigError, match="hash mismatch"):
        layers.validate_config(p, verify_files=True)
