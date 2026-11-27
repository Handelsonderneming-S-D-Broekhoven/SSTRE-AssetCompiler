# SPDX-FileCopyrightText: 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe
# SPDX-License-Identifier: GPL-3.0-only
"""Synthetic source-selection records, packs and approvals for the layer
tests. Every ID and hash is invented; no game data is used or derived."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sstre_assetcompiler import layers


def h(label: str) -> str:
    """Bare hex hash of a synthetic label (format of source-selection records)."""
    return hashlib.sha256(f"SSTRE synthetic fixture: {label}".encode()).hexdigest()


def cand(cid: str, px=(512, 512), classic_px=(256, 256), *, verified=True, related=False,
         method="exact_path", maps=None, ratio=None) -> dict:
    r = ratio if ratio is not None else (px[0] * px[1]) / (classic_px[0] * classic_px[1])
    return {"candidate_id": cid, "source_kind": "hd", "edition": "hd_tfe", "source_sha256": h(cid),
            "source_format": "bc1",
            "match": {"verified": verified, "related": related, "method": method},
            "companion_maps": maps or {},
            "metrics": {"classic_px": list(classic_px), "candidate_px": list(px), "resolution_ratio": r,
                        "aspect_match": True, "psnr_db": 33.0, "ssim": 0.95, "layout_ssim": 0.97,
                        "delta_e": 1.0, "detail_energy": 0.2, "artefact_index": 0.05}}


def rec(asset_id: str, result: str = "classic", rule: str = "R11_degraded_copy", *, category="architecture",
        cands=(), classic_px=(256, 256), alpha="opaque", selected=None, art=None, maps=None,
        maps_cand=None, art_status="pending_owner_opt_in", policy_version="0.1.0") -> dict:
    d = {"result": result, "base_source": "hd" if result == "hd" else "classic", "rule_fired": rule,
         "reason": "synthetic", "selected_candidate": selected, "requires_opt_in": result == "hd_art_candidate"}
    if art:
        d["hd_art"] = {"candidate_id": art, "status": art_status}
    if maps:
        d["companion_maps"] = list(maps)
        d["maps_candidate"] = maps_cand
    rid = "sel-" + hashlib.sha256(f"{asset_id}{policy_version}".encode()).hexdigest()[:16]
    return {"schema_version": "0.1.0", "record_id": rid, "asset_id": asset_id, "category": category,
            "generated_at": "2026-11-27T00:00:00Z", "generator": {"name": "synthetic", "version": "0"},
            "policy": {"policy_id": "sstre-source-selection", "version": policy_version,
                       "hd_art_opt_in": False, "thresholds": {"lossy_psnr_db": 40.0}},
            "classic": {"virtual_path": "x", "source_sha256": h(asset_id), "width_px": classic_px[0],
                        "height_px": classic_px[1], "mex_width": 4096, "mex_height": 4096,
                        "alpha_mode": alpha},
            "candidates": list(cands), "rules_evaluated": [{"rule_id": rule, "fired": True}],
            "decision": d, "local_only": True}


A_CLASSIC = "classic_tfe:textures/syn/plain.tex"
A_HD = "classic_tfe:textures/syn/upscale.tex"
A_MAPS = "classic_tfe:textures/syn/maps.tex"
A_ART = "classic_tfe:models/syn/statue.tex"
A_UI = "classic_tfe:textures/interface/syn_logo.tex"
A_RELAYOUT = "classic_tfe:textures/computer/syn_page.tex"
A_FONT = "classic_tfe:fonts/syn.tex"


def standard_records() -> list[dict]:
    return [
        rec(A_CLASSIC, cands=[cand("hd_tfe:syn/plain", (256, 256))]),
        rec(A_HD, "hd", "R08_upscale_with_detail", category="props",
            cands=[cand("hd_tfe:syn/upscale", (1024, 1024), maps={"normal": "n.png", "gloss": "g.png"})],
            selected="hd_tfe:syn/upscale"),
        rec(A_MAPS, "classic_with_hd_maps", "R11_degraded_copy",
            cands=[cand("hd_tfe:syn/maps", (512, 512), maps={"normal": "n.png", "ao": "a.png"})],
            maps=["ao", "normal"], maps_cand="hd_tfe:syn/maps"),
        rec(A_ART, "hd_art_candidate", "R07_art_difference", category="props",
            cands=[cand("hd_tfe:syn/statue", (512, 512))], art="hd_tfe:syn/statue"),
        rec(A_UI, "hd_art_candidate", "R07_art_difference", category="ui", alpha="blend",
            cands=[cand("hd_tfe:syn/logo", (512, 512))], art="hd_tfe:syn/logo"),
        rec(A_RELAYOUT, "classic_fallback", "R03_unverified", category="ui",
            cands=[cand("hd_tfe:syn/page", (1024, 768), verified=False, related=True, ratio=12.0)]),
        rec(A_FONT, "classic", "R01_never_replace_category", category="font"),
    ]


def config(**layers_cfg) -> dict:
    prof = layers_cfg.pop("platformProfile", "pc-default")
    extra = layers_cfg.pop("extra", {})
    return dict({"configFormatVersion": "0.1.0", "configId": "syn", "layers": layers_cfg,
                 "platformProfile": prof}, **extra)


def approvals_doc(*rows) -> dict:
    return {"approvalsFormatVersion": "0.1.0", "approvalsId": "syn", "approvals": list(rows)}


def approve(asset_id, layer, cid, decision="approved", **kw) -> dict:
    return dict({"assetId": asset_id, "layer": layer, "candidateId": cid, "decision": decision}, **kw)


def pack_doc(pack_id, kind, entries, version="1.0.0", quality=None) -> dict:
    d = {"packFormatVersion": "0.1.0", "packId": pack_id, "version": version, "kind": kind,
         "entries": entries}
    if quality:
        d["qualityProfile"] = quality
    return d


def ai_entry(asset_id, input_label_hex: str, px=(512, 512), passed=True, role="albedo") -> dict:
    return {"assetId": asset_id, "role": role, "file": "out.png", "sha256": "sha256:" + h("out" + asset_id + role),
            "px": list(px), "input": {"sourceSha256": "sha256:" + input_label_hex},
            "recipeHash": "sha256:" + h("recipe"), "qa": {"passed": passed}}


def write_json(path: Path, doc) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    return path


def make_workspace(tmp: Path, cfg: dict, records: list[dict], *, approvals=None, packs=()) -> Path:
    """Write config, records, approvals and packs; packs = (slot, ref_extra, doc)."""
    for r in records:
        write_json(tmp / "records" / f"{r['record_id']}.json", r)
    cfg = json.loads(json.dumps(cfg))
    if approvals is not None:
        write_json(tmp / "approvals.json", approvals)
        cfg["approvals"] = {"path": "approvals.json"}
    for slot, extra, doc in packs:
        p = write_json(tmp / "packs" / doc["packId"] / "pack.json", doc)
        ref = dict({"packId": doc["packId"], "version": doc["version"], "kind": doc["kind"],
                    "path": p.relative_to(tmp).as_posix(), "priority": 0}, **extra)
        L = cfg.setdefault("layers", {}).setdefault(slot, {})
        L.setdefault("packs", []).append(ref)
    return write_json(tmp / "build-config.json", cfg)


def plan(tmp: Path, cfg: dict, records: list[dict] | None = None, **kw) -> dict:
    p = make_workspace(tmp, cfg, standard_records() if records is None else records, **kw)
    return layers.build_plan(p, [tmp / "records"])


def entry(manifest: dict, asset_id: str) -> dict:
    return next(e for e in manifest["assets"] if e["assetId"] == asset_id)


def status(e: dict, layer: str) -> str:
    return next(s["status"] for s in e["layers"] if s["layer"] == layer)
