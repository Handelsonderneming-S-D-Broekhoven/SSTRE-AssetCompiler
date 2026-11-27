# SPDX-FileCopyrightText: 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe
# SPDX-License-Identifier: GPL-3.0-only
"""Layered asset pipeline: build configuration -> resolved per-asset plan
(SSTRE ADR-0009).

Layers, in fixed order (a later layer overrides an earlier one only for the
assets it covers):

====  ===================  =================  =====================================
 #     id                   default            gate
====  ===================  =================  =====================================
 1     classic-base         on (mandatory)     always; source of truth and fallback
 2     hd-quality           on                 selector result ``hd`` (verified superior)
 3     hd-material-maps     on                 companion maps verified for the albedo
 4     hd-artwork           off                global opt-in AND per-asset approval
 5     hd-interface         off                own opt-in AND per-asset approval (UI)
 6     ai-enhancement       quality profile    processes the source chosen by 1-5
 7     overrides            off                curated / community packs, highest
====  ===================  =================  =====================================

Within a layer, conflicts are resolved by explicit priority (higher wins),
then by stable ID (lexicographically smaller wins). All inputs are sorted
before use, so input order never affects the plan; the plan and its
effective configuration are hashed with RFC 8785 canonical JSON (:mod:`jcs`).

Inputs are the source-selection records (``sstre-ac select-source``),
optional asset facts (e.g. the texture-set ``READY.json``), the build
configuration, an approvals file and asset packs (AI outputs, overrides).
Plans and records reference proprietary assets: keep them local (GATE-05).
"""

from __future__ import annotations

import json
from collections import Counter
from importlib import resources
from pathlib import Path

from . import GENERATOR_NAME, __version__
from . import platform_profiles as plat
from .jcs import content_hash, file_hash

CONFIG_FORMAT_VERSION = "0.1.0"
MANIFEST_FORMAT_VERSION = "0.1.0"
APPROVALS_FORMAT_VERSION = "0.1.0"
PACK_FORMAT_VERSION = "0.1.0"

LAYERS = ("classic-base", "hd-quality", "hd-material-maps", "hd-artwork", "hd-interface",
          "ai-enhancement", "overrides")
ORDER = {name: i + 1 for i, name in enumerate(LAYERS)}
SOURCE_LAYERS = ("classic-base", "hd-quality", "hd-artwork", "hd-interface", "overrides")
APPROVAL_LAYERS = ("hd-artwork", "hd-interface")
INTERFACE_CATEGORIES = frozenset({"ui"})  # menus, HUD, Netricsa (fonts are never replaced: R01)
LAYER_DEFAULTS = {
    "classic-base": {"enabled": True},
    "hd-quality": {"enabled": True},
    "hd-material-maps": {"enabled": True, "roles": list(plat.MAP_ROLES)},
    "hd-artwork": {"enabled": False},
    "hd-interface": {"enabled": False, "includeRelayout": False},
    "ai-enhancement": {"qualityProfile": "off", "packs": []},
    "overrides": {"enabled": False, "packs": []},
}
SCORE_KEYS = ("resolution_ratio", "psnr_db", "ssim", "layout_ssim", "delta_e", "hist_intersection",
              "artefact_index", "blockiness_excess", "sharpness_ratio", "detail_energy", "alpha_mae")
OVERRIDE_KIND_RANK = {"curated": 0, "community": 1}


class ConfigError(ValueError):
    pass


def _sha(x: str | None) -> str | None:
    if not x:
        return None
    return x if x.startswith("sha256:") else "sha256:" + x.lower()


def _json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ----------------------------------------------------------- built-ins ----

def builtin_quality_profiles() -> dict:
    text = resources.files("sstre_assetcompiler").joinpath(
        "data", "quality-profiles.json").read_text(encoding="utf-8")
    return json.loads(text)["profiles"]


# -------------------------------------------------------------- config ----

def _check_pack_ref(ref: dict, where: str) -> None:
    for k in ("packId", "version", "path", "priority", "kind"):
        if k not in ref:
            raise ConfigError(f"{where}: pack reference lacks '{k}'")
    if not isinstance(ref["priority"], int):
        raise ConfigError(f"{where}: priority of pack '{ref['packId']}' must be an integer")


def load_config(path: Path) -> tuple[dict, Path]:
    return _json(path), Path(path).parent


def normalize_config(raw: dict) -> dict:
    """Apply defaults and check the layer rules that a schema cannot express."""
    if raw.get("configFormatVersion", "").split(".")[0] != CONFIG_FORMAT_VERSION.split(".")[0]:
        raise ConfigError(f"unsupported configFormatVersion {raw.get('configFormatVersion')!r}")
    layers_in = raw.get("layers", {})
    unknown = sorted(set(layers_in) - set(LAYERS))
    if unknown:
        raise ConfigError(f"unknown layer(s): {', '.join(unknown)}")
    layers = {}
    for name in LAYERS:
        d = dict(LAYER_DEFAULTS[name])
        d.update(layers_in.get(name, {}))
        layers[name] = d
    if layers["classic-base"].get("enabled") is not True:
        raise ConfigError("classic-base cannot be disabled: it is the source of truth and fallback")
    for k in ("enabled",):
        for name in LAYERS:
            if name != "ai-enhancement" and not isinstance(layers[name].get(k), bool):
                raise ConfigError(f"layers.{name}.enabled must be true or false")
    bad_roles = sorted(set(layers["hd-material-maps"]["roles"]) - set(plat.MAP_ROLES))
    if bad_roles:
        raise ConfigError(f"unknown material-map role(s): {', '.join(bad_roles)}")
    qps = builtin_quality_profiles()
    qps.update(raw.get("qualityProfiles", {}))
    qp = layers["ai-enhancement"]["qualityProfile"]
    if qp not in qps:
        raise ConfigError(f"unknown quality profile '{qp}' (known: {', '.join(sorted(qps))})")
    seen = set()
    for slot, kinds in (("ai-enhancement", ("ai-enhancement",)), ("overrides", ("curated", "community"))):
        for ref in layers[slot]["packs"]:
            _check_pack_ref(ref, f"layers.{slot}")
            if ref["kind"] not in kinds:
                raise ConfigError(f"layers.{slot}: pack '{ref['packId']}' has kind '{ref['kind']}'"
                                  f" (allowed: {', '.join(kinds)})")
            if ref["packId"] in seen:
                raise ConfigError(f"duplicate packId '{ref['packId']}'")
            seen.add(ref["packId"])
    pp = raw.get("platformProfile", "pc-default")
    if isinstance(pp, str):
        profile = plat.load_builtin(pp)
    elif isinstance(pp, dict):
        plat.check_profile(pp)
        profile = pp
    else:
        raise ConfigError("platformProfile must be a built-in id or an inline profile object")
    return {"raw": raw, "layers": layers, "qualityProfileId": qp, "qualityProfile": qps[qp],
            "platformProfile": profile}


def config_warnings(cfg: dict, approvals: list[dict]) -> list[str]:
    w = []
    L = cfg["layers"]
    for layer in APPROVAL_LAYERS:
        n = sum(1 for a in approvals if a["layer"] == layer and a["decision"] == "approved")
        if L[layer]["enabled"] and n == 0:
            w.append(f"{layer} is opted in but no asset is approved for it: it supplies nothing")
        if not L[layer]["enabled"] and n:
            w.append(f"{n} approval(s) for {layer} are ignored: the layer is not opted in")
    if L["overrides"]["packs"] and not L["overrides"]["enabled"]:
        w.append("override packs are listed but the overrides layer is disabled")
    if cfg["platformProfile"]["status"] == "hypothetical":
        w.append(f"platform profile '{cfg['platformProfile']['profileId']}' is hypothetical")
    return w


# -------------------------------------------------------------- hashes ----
# Hashes of JSON inputs are JCS hashes of the parsed document with its lists
# put in their canonical order, so that file formatting and list order (which
# carry no meaning) never change a pin or the plan hash (RD-004 section 5).

def approvals_hash(doc: dict) -> str:
    d = dict(doc, approvals=sorted(doc.get("approvals", []),
                                   key=lambda a: (a.get("assetId", ""), a.get("layer", ""),
                                                  a.get("candidateId", ""))))
    return content_hash(d)


def pack_hash(doc: dict) -> str:
    d = dict(doc, entries=sorted(doc.get("entries", []),
                                 key=lambda e: (e.get("assetId", ""), e.get("role", "albedo"))))
    return content_hash(d)


def config_hash(raw: dict) -> str:
    d = json.loads(json.dumps(raw))
    for slot in ("ai-enhancement", "overrides"):
        packs = d.get("layers", {}).get(slot, {}).get("packs")
        if packs:
            packs.sort(key=lambda r: (-int(r.get("priority", 0)), OVERRIDE_KIND_RANK.get(r.get("kind"), 0),
                                      r.get("packId", "")))
    return content_hash(d)


def facts_hash(facts: dict[str, dict]) -> str:
    return content_hash([facts[k] for k in sorted(facts)])


# ----------------------------------------------------------- approvals ----

def load_approvals(ref: dict | None, base: Path) -> tuple[list[dict], dict | None]:
    if not ref:
        return [], None
    doc = _json(base / ref["path"])
    actual = approvals_hash(doc)
    if ref.get("sha256") and _sha(ref["sha256"]) != actual:
        raise ConfigError(f"approvals file hash mismatch: expected {ref['sha256']}, got {actual}")
    if doc.get("approvalsFormatVersion", "").split(".")[0] != APPROVALS_FORMAT_VERSION.split(".")[0]:
        raise ConfigError("unsupported approvalsFormatVersion")
    keys = set()
    out = []
    for a in doc.get("approvals", []):
        for k in ("assetId", "layer", "candidateId", "decision"):
            if k not in a:
                raise ConfigError(f"approval lacks '{k}': {a}")
        if a["layer"] not in APPROVAL_LAYERS:
            raise ConfigError(f"approval for layer '{a['layer']}': only {', '.join(APPROVAL_LAYERS)} take approvals")
        if a["decision"] not in ("approved", "rejected"):
            raise ConfigError(f"approval decision must be approved or rejected: {a['decision']!r}")
        key = (a["assetId"], a["layer"], a["candidateId"])
        if key in keys:
            raise ConfigError(f"duplicate approval for {key}")
        keys.add(key)
        e = {k: a[k] for k in ("assetId", "layer", "candidateId", "decision")}
        if a.get("candidateSha256"):
            e["candidateSha256"] = _sha(a["candidateSha256"])
        e["priority"] = int(a.get("priority", 0))
        for k in ("approvedBy", "date"):
            if k in a:
                e[k] = a[k]
        out.append(e)
    out.sort(key=lambda e: (e["assetId"], e["layer"], e["candidateId"]))
    info = {"approvalsId": doc.get("approvalsId", ""), "sha256": actual,
            "count": len(out), "entries": out}
    return out, info


# --------------------------------------------------------------- packs ----

def load_pack(ref: dict, base: Path) -> dict:
    doc = _json(base / ref["path"])
    actual = pack_hash(doc)
    if ref.get("sha256") and _sha(ref["sha256"]) != actual:
        raise ConfigError(f"pack '{ref['packId']}': index hash mismatch (expected {ref['sha256']}, got {actual})")
    if doc.get("packFormatVersion", "").split(".")[0] != PACK_FORMAT_VERSION.split(".")[0]:
        raise ConfigError(f"pack '{ref['packId']}': unsupported packFormatVersion")
    for k, want in (("packId", ref["packId"]), ("version", ref["version"]), ("kind", ref["kind"])):
        if doc.get(k) != want:
            raise ConfigError(f"pack '{ref['packId']}': index {k} {doc.get(k)!r} != config {want!r}")
    seen = set()
    entries = []
    for e in doc.get("entries", []):
        action = e.get("action", "replace")
        role = e.get("role", "albedo")
        key = (e["assetId"], role)
        if key in seen:
            raise ConfigError(f"pack '{ref['packId']}': duplicate entry for {key}")
        seen.add(key)
        if ref["kind"] == "ai-enhancement":
            if action != "replace":
                raise ConfigError(f"pack '{ref['packId']}': AI packs only supply outputs (action replace)")
            for k in ("sha256", "px", "input", "recipeHash", "qa"):
                if k not in e:
                    raise ConfigError(f"pack '{ref['packId']}': AI entry {key} lacks '{k}'")
        elif action == "replace":
            for k in ("sha256", "px"):
                if k not in e:
                    raise ConfigError(f"pack '{ref['packId']}': replace entry {key} lacks '{k}'")
        elif action not in ("use-classic", "skip-ai"):
            raise ConfigError(f"pack '{ref['packId']}': unknown action '{action}'")
        entries.append(dict(e, action=action, role=role))
    entries.sort(key=lambda e: (e["assetId"], e["role"]))
    return {"packId": ref["packId"], "version": ref["version"], "kind": ref["kind"],
            "priority": ref["priority"], "sha256": actual, "qualityProfile": doc.get("qualityProfile"),
            "entryCount": len(entries), "entries": entries}


def _pack_rank(p: dict):
    """Higher priority first, then curated before community, then packId."""
    return (-p["priority"], OVERRIDE_KIND_RANK.get(p["kind"], 0), p["packId"])


# ------------------------------------------------------------- records ----

def load_records(paths: list[Path]) -> list[dict]:
    recs = []
    for p in paths:
        p = Path(p)
        files = sorted(p.glob("sel-*.json")) if p.is_dir() else [p]
        for f in files:
            doc = _json(f)
            recs.extend(doc if isinstance(doc, list) else [doc])
    return recs


def record_hash(rec: dict) -> str:
    """Hash of a selection record without its run timestamp (cached on the
    record under a private key that is excluded from the hash)."""
    h = rec.get("_sstre_record_hash")
    if h is None:
        h = content_hash({k: v for k, v in rec.items() if k not in ("generated_at", "_sstre_record_hash")})
        rec["_sstre_record_hash"] = h
    return h


def load_asset_facts(path: Path | None) -> dict[str, dict]:
    """Asset facts by ID: a texture-set READY.json (with per-texture records)
    or a JSON list of objects with ``asset_id`` and optional ``category``,
    ``width_px``, ``height_px``, ``alpha_mode``, ``first_mip``,
    ``source_sha256``, ``image_sha256``."""
    if path is None:
        return {}
    path = Path(path)
    doc = _json(path)
    rows = doc["textures"] if isinstance(doc, dict) and "textures" in doc else doc
    out = {}
    for r in rows:
        f = dict(r)
        if "record" in r:
            rec = _json(path.parent / r["record"])
            tex = rec.get("texture", {})
            f.setdefault("first_mip", tex.get("first_mip"))
            f.setdefault("source_sha256", rec.get("source_sha256"))
            imgs = rec.get("images") or []
            if imgs:
                f.setdefault("image_sha256", imgs[0].get("sha256"))
        out[f["asset_id"]] = {k: f[k] for k in ("asset_id", "category", "width_px", "height_px",
                                                 "alpha_mode", "first_mip", "source_sha256",
                                                 "image_sha256") if f.get(k) is not None}
    return out


# ------------------------------------------------------------ resolver ----

def _cand(rec: dict, cid: str | None) -> dict | None:
    if not cid:
        return None
    return next((c for c in rec.get("candidates", []) if c["candidate_id"] == cid), None)


def _cand_px(c: dict | None) -> list[int] | None:
    m = (c or {}).get("metrics") or {}
    return list(m["candidate_px"]) if m.get("candidate_px") else None


def _scores(c: dict | None) -> dict:
    m = (c or {}).get("metrics") or {}
    return {k: m[k] for k in SCORE_KEYS if k in m}


def _relayout_candidate(rec: dict) -> dict | None:
    """EXP-012: UI images whose HD version is higher-resolution but re-laid-out
    (exact path, related, layout changed). 2D UI tolerates a mex change."""
    d = rec["decision"]
    if rec.get("category") not in INTERFACE_CATEGORIES:
        return None
    if d["result"] not in ("classic", "classic_fallback") or \
            d["rule_fired"] not in ("R03_unverified", "R04_uv_incompatible"):
        return None
    best = None
    for c in sorted(rec.get("candidates", []), key=lambda c: c["candidate_id"]):
        m = c.get("metrics") or {}
        mt = c.get("match", {})
        if m.get("resolution_ratio", 0) > 1 and (mt.get("method") == "exact_path" or mt.get("related")):
            if best is None or m["resolution_ratio"] > best["metrics"]["resolution_ratio"]:
                best = c
    return best


class _Ctx:
    def __init__(self, cfg, approvals, override_packs, ai_packs):
        self.cfg = cfg
        self.L = cfg["layers"]
        self.qp = cfg["qualityProfile"]
        self.qp_id = cfg["qualityProfileId"]
        self.profile = cfg["platformProfile"]
        self.approvals: dict[tuple, list[dict]] = {}
        for a in approvals:
            self.approvals.setdefault((a["assetId"], a["layer"]), []).append(a)
        self.override_packs = sorted(override_packs, key=_pack_rank)
        self.ai_packs = sorted(ai_packs, key=_pack_rank)
        self.ov_index = self._index(self.override_packs)
        self.ai_index = self._index(self.ai_packs)

    @staticmethod
    def _index(packs):
        idx: dict[str, list[tuple[dict, dict]]] = {}
        for p in packs:  # already in precedence order
            for e in p["entries"]:
                idx.setdefault(e["assetId"], []).append((p, e))
        for v in idx.values():  # explicit entry priority inside the pack order
            v.sort(key=lambda pe: (_pack_rank(pe[0]), -int(pe[1].get("priority", 0)), pe[1]["role"]))
        return idx


def _gate_approval(ctx: _Ctx, asset_id: str, layer: str, cand: dict) -> tuple[str, str]:
    """(status, detail) for an approval-gated candidate."""
    if not ctx.L[layer]["enabled"]:
        return "disabled", "layer not opted in"
    approvals = sorted(ctx.approvals.get((asset_id, layer), []),
                       key=lambda a: (-a["priority"], a["candidateId"]))
    mine = [a for a in approvals if a["candidateId"] == cand["candidate_id"]]
    if not mine:
        if approvals:
            return "blocked", f"approval names another candidate ({approvals[0]['candidateId']})"
        return "awaiting-approval", "no per-asset approval"
    a = mine[0]
    if a["decision"] == "rejected":
        return "rejected", "rejected in approvals"
    want = a.get("candidateSha256")
    if want and want != _sha(cand.get("source_sha256")):
        return "blocked", "stale approval: candidate hash changed"
    return "applied", f"approved{(' by ' + a['approvedBy']) if a.get('approvedBy') else ''}"


def resolve_asset(asset_id: str, rec: dict | None, facts: dict | None, ctx: _Ctx) -> dict:
    facts = facts or {}
    L = ctx.L
    cl = (rec or {}).get("classic", {})
    category = (rec or {}).get("category") or facts.get("category") or "other"
    cpx = None
    w, h = cl.get("width_px", facts.get("width_px")), cl.get("height_px", facts.get("height_px"))
    if w and h:
        cpx = [int(w), int(h)]
    alpha = cl.get("alpha_mode", facts.get("alpha_mode"))
    classic = {"sha256": _sha(cl.get("source_sha256") or facts.get("source_sha256")), "px": cpx,
               "alphaMode": alpha}
    if facts.get("image_sha256") or cl.get("image_sha256"):
        classic["imageSha256"] = _sha(cl.get("image_sha256") or facts.get("image_sha256"))
    if facts.get("first_mip") is not None:
        classic["firstMip"] = int(facts["first_mip"])
    classic = {k: v for k, v in classic.items() if v is not None}

    status: dict[str, dict] = {n: {"status": "not-applicable"} for n in LAYERS}
    status["classic-base"] = {"status": "applied"}
    # source contributions: (layer order, priority, stable id, source dict)
    contrib: list[tuple[int, int, str, dict]] = []
    selection = None
    dec = None
    if rec is not None:
        dec = rec["decision"]
        focus = _cand(rec, dec.get("selected_candidate") or (dec.get("hd_art") or {}).get("candidate_id")
                      or dec.get("maps_candidate"))
        if focus is None and rec.get("candidates"):
            focus = sorted(rec["candidates"], key=lambda c: c["candidate_id"])[0]
        selection = {"recordId": rec["record_id"], "recordSha256": record_hash(rec),
                     "policyId": rec["policy"]["policy_id"], "policyVersion": rec["policy"]["version"],
                     "result": dec["result"], "ruleFired": dec["rule_fired"],
                     "candidateId": focus["candidate_id"] if focus else None, "scores": _scores(focus)}

        # layer 2: HD Quality Improvements (verified superior only)
        if dec["result"] == "hd":
            c = _cand(rec, dec["selected_candidate"])
            if c is None or not c.get("match", {}).get("verified"):
                status["hd-quality"] = {"status": "blocked", "detail": "selected candidate not verified"}
            elif not L["hd-quality"]["enabled"]:
                status["hd-quality"] = {"status": "disabled", "detail": dec["rule_fired"]}
            else:
                status["hd-quality"] = {"status": "applied", "detail": dec["rule_fired"]}
                contrib.append((ORDER["hd-quality"], 0, c["candidate_id"], _hd_source("hd-quality", c)))

        # layers 4/5: HD Artwork / HD Interface (opt-in + approval)
        arts = []
        if dec["result"] == "hd_art_candidate":
            c = _cand(rec, dec["hd_art"]["candidate_id"])
            layer = "hd-interface" if category in INTERFACE_CATEGORIES else "hd-artwork"
            if c is not None:
                arts.append((layer, c, False, dec["hd_art"]["status"] == "rejected"))
        if L["hd-interface"].get("includeRelayout"):
            c = _relayout_candidate(rec)
            if c is not None:
                arts.append(("hd-interface", c, True, False))
        for layer, c, relayout, rejected in arts:
            if rejected:
                st, det = "rejected", "rejected in the selection record"
            else:
                st, det = _gate_approval(ctx, asset_id, layer, c)
            if relayout:
                det += "; layout changes (2D UI only, mex may change)"
            status[layer] = {"status": st, "detail": det}
            if st == "applied":
                pri = max([a["priority"] for a in ctx.approvals.get((asset_id, layer), [])
                           if a["candidateId"] == c["candidate_id"]] or [0])
                src = _hd_source(layer, c)
                if relayout:
                    src["layoutChanged"] = True
                contrib.append((ORDER[layer], pri, c["candidate_id"], src))

    # layer 7: overrides
    ov_entries = ctx.ov_index.get(asset_id, []) if L["overrides"]["enabled"] else []
    ov_albedo = next(((p, e) for p, e in ov_entries if e["role"] == "albedo"), None)
    skip_ai = False
    forced_classic = False
    if ctx.ov_index.get(asset_id) and not L["overrides"]["enabled"]:
        status["overrides"] = {"status": "disabled"}
    if ov_albedo:
        p, e = ov_albedo
        others = [f"{q['packId']}" for q, f in ov_entries if f["role"] == "albedo" and q is not p]
        det = f"{p['packId']}@{p['version']} {e['action']}" + (f"; superseding {', '.join(others)}" if others else "")
        status["overrides"] = {"status": "applied", "detail": det}
        if e["action"] == "replace":
            contrib.append((ORDER["overrides"], int(e.get("priority", 0)), p["packId"],
                            {"layer": "overrides", "kind": "override", "id": f"{p['packId']}:{asset_id}",
                             "sha256": _sha(e["sha256"]), "px": list(e["px"]),
                             "pack": {"packId": p["packId"], "version": p["version"], "kind": p["kind"]},
                             "keepMaps": bool(e.get("keepMaps", False))}))
        elif e["action"] == "use-classic":
            forced_classic = True
        elif e["action"] == "skip-ai":
            skip_ai = True

    # winner: highest layer, then explicit priority, then stable id
    classic_src = {"layer": "classic-base", "kind": "classic", "id": asset_id,
                   "sha256": classic.get("sha256"), "px": cpx}
    if classic.get("imageSha256"):
        classic_src["imageSha256"] = classic["imageSha256"]
    if forced_classic:
        for _, _, _, s in contrib:
            status[s["layer"]] = {"status": "superseded", "detail": "overrides: use-classic"}
        contrib = []
        source = dict(classic_src, decidedBy="overrides")
    elif contrib:
        contrib.sort(key=lambda t: (-t[0], -t[1], t[2]))
        source = contrib[0][3]
        for _, _, _, s in contrib[1:]:
            if s["layer"] != source["layer"]:
                status[s["layer"]] = {"status": "superseded", "detail": f"by {source['layer']}"}
        status["classic-base"] = {"status": "superseded", "detail": f"by {source['layer']}"}
        source = dict(source, decidedBy=source["layer"])
    else:
        source = dict(classic_src, decidedBy="classic-base")
    source = {k: v for k, v in source.items() if v is not None}

    # layer 3: material maps compatible with the final albedo
    maps = _material_maps(rec, dec, source, ctx, status)

    # layer 6: AI enhancement
    processing, ai_maps = _ai(asset_id, category, source, cpx, classic.get("firstMip"), ctx, status,
                              suppressed=skip_ai or source["layer"] == "overrides")
    have = {m["role"] for m in maps}
    for m in ai_maps:
        if m["role"] not in have:
            maps.append(m)
    # map overrides from packs
    for p, e in ov_entries:
        if e["role"] != "albedo" and e["action"] == "replace":
            maps = [m for m in maps if m["role"] != e["role"]]
            maps.append({"role": e["role"], "layer": "overrides", "id": f"{p['packId']}:{asset_id}",
                         "sha256": _sha(e["sha256"]), "px": list(e["px"])})
    maps.sort(key=lambda m: m["role"])

    entry = {
        "assetId": asset_id,
        "category": category,
        "classic": classic,
        "selection": selection,
        "source": source,
        "suppliedBy": source["layer"],
        "processedBy": "ai-enhancement" if processing["status"] == "applied" else None,
        "materialMaps": maps,
        "processing": processing,
        "layers": [dict({"layer": n, "order": ORDER[n]}, **status[n]) for n in LAYERS],
    }
    entry["platform"] = _platform(entry, ctx)
    return entry


def _hd_source(layer: str, c: dict) -> dict:
    s = {"layer": layer, "kind": "hd", "id": c["candidate_id"], "sha256": _sha(c.get("source_sha256")),
         "px": _cand_px(c)}
    if c.get("image_sha256"):
        s["imageSha256"] = _sha(c["image_sha256"])
    if c.get("source_format"):
        s["format"] = c["source_format"]
    return s


def _material_maps(rec, dec, source, ctx: _Ctx, status) -> list[dict]:
    if rec is None:
        return []
    L = ctx.L
    roles_cfg = set(L["hd-material-maps"]["roles"])
    prof_maps = ctx.profile.get("materialMaps", {"enabled": True, "roles": list(plat.MAP_ROLES)})
    roles_prof = set(prof_maps.get("roles", [])) if prof_maps.get("enabled", True) else set()
    m01 = dec["result"] == "classic_with_hd_maps"
    if source["layer"] in ("hd-quality", "hd-artwork", "hd-interface"):
        cand = _cand(rec, source["id"])
        roles = sorted(r for r, p in (cand or {}).get("companion_maps", {}).items() if p)
        why = "maps of the selected HD source"
    elif m01 and source["layer"] == "classic-base":
        cand = _cand(rec, dec["maps_candidate"])
        roles = sorted(dec.get("companion_maps", []))
        why = "verified for the Classic albedo (M01)"
    elif m01 and source["layer"] == "overrides" and source.get("keepMaps"):
        cand = _cand(rec, dec["maps_candidate"])
        roles = sorted(dec.get("companion_maps", []))
        why = "M01 maps kept by the override (keepMaps)"
    else:
        if m01:
            status["hd-material-maps"] = {"status": "dropped",
                                          "detail": f"not verified for the {source['layer']} albedo"}
        return []
    if not roles or cand is None:
        return []
    if not L["hd-material-maps"]["enabled"]:
        status["hd-material-maps"] = {"status": "disabled", "detail": ",".join(roles)}
        return []
    use = [r for r in roles if r in roles_cfg and r in roles_prof]
    dropped = [r for r in roles if r not in use]
    if not use:
        status["hd-material-maps"] = {"status": "dropped",
                                      "detail": f"roles {','.join(roles)} excluded by config/profile"}
        return []
    det = why + (f"; dropped {','.join(dropped)} (config/profile)" if dropped else "")
    status["hd-material-maps"] = {"status": "applied", "detail": det}
    px = _cand_px(cand)
    out = []
    for r in use:
        m = {"role": r, "layer": "hd-material-maps", "id": f"{cand['candidate_id']}#{r}"}
        if px:
            m["px"] = px
            m["pxAssumed"] = True  # companion maps share the candidate size [ASM]
        out.append(m)
    return out


def _ai(asset_id, category, source, cpx, first_mip, ctx: _Ctx, status, *, suppressed: bool):
    qp = ctx.qp
    base = {"layer": "ai-enhancement", "qualityProfile": ctx.qp_id}
    if not qp.get("enabled"):
        status["ai-enhancement"] = {"status": "off", "detail": f"quality profile {ctx.qp_id}"}
        return dict(base, status="off"), []
    cls = qp.get("classes", {}).get(category, {"mode": qp.get("defaultMode", "skip")})
    if cls.get("mode", "skip") == "skip":
        status["ai-enhancement"] = {"status": "not-eligible", "detail": f"category {category}"}
        return dict(base, status="not-eligible"), []
    if suppressed:
        status["ai-enhancement"] = {"status": "suppressed", "detail": "overrides"}
        return dict(base, status="suppressed"), []
    if source["layer"] not in qp.get("processSources", ["classic-base"]):
        status["ai-enhancement"] = {"status": "not-eligible", "detail": f"source layer {source['layer']}"}
        return dict(base, status="not-eligible"), []
    scale = int(cls.get("scale", 2))
    out = dict(base, mode=cls["mode"], scale=scale)
    spx = source.get("px")
    if cpx:
        want = [cpx[0] * scale, cpx[1] * scale]
        if spx and spx[0] * spx[1] >= want[0] * want[1]:
            status["ai-enhancement"] = {"status": "source-sufficient",
                                        "detail": f"source {spx[0]}x{spx[1]} >= x{scale}"}
            return dict(out, status="source-sufficient"), []
        (tw, th), acts = plat.clamp_size(want[0], want[1], ctx.profile, classic_px=cpx,
                                         first_mip=first_mip,
                                         keeps_mex=not source.get("layoutChanged", False))
        if spx and tw * th <= spx[0] * spx[1]:
            why = ",".join(a["action"] for a in acts) or "limits"
            status["ai-enhancement"] = {"status": "platform-capped",
                                        "detail": f"x{scale} wanted {want[0]}x{want[1]}, profile allows "
                                                  f"{tw}x{th} ({why})"}
            return dict(out, status="platform-capped", plannedClampedFrom=want), []
        out["plannedPx"] = [tw, th]
        if acts:
            out["plannedClampedFrom"] = want
    # find a matching output in the AI packs
    want_hashes = {h for h in (source.get("sha256"), source.get("imageSha256")) if h}
    rejected = []
    maps = []
    chosen = None
    for p, e in ctx.ai_index.get(asset_id, []):
        if p.get("qualityProfile") not in (None, ctx.qp_id):
            rejected.append(f"{p['packId']}: profile {p.get('qualityProfile')}")
            continue
        inp = _sha(e["input"].get("sourceSha256"))
        if inp not in want_hashes:
            rejected.append(f"{p['packId']}: input hash does not match the selected source")
            continue
        if not e["qa"].get("passed"):
            rejected.append(f"{p['packId']}: QA failed")
            continue
        if e["role"] == "albedo":
            if chosen is not None:
                continue
            opx = list(e["px"])
            if spx and opx[0] * spx[1] != opx[1] * spx[0]:
                rejected.append(f"{p['packId']}: aspect differs from source")
                continue
            if spx and (opx[0] < spx[0] or opx[1] < spx[1]):
                rejected.append(f"{p['packId']}: output smaller than source")
                continue
            chosen = (p, e)
        else:
            maps.append({"role": e["role"], "layer": "ai-enhancement", "id": f"{p['packId']}:{asset_id}",
                         "sha256": _sha(e["sha256"]), "px": list(e["px"]), "inferred": True})
    if chosen:
        p, e = chosen
        out["status"] = "applied"
        out["output"] = {"packId": p["packId"], "version": p["version"], "sha256": _sha(e["sha256"]),
                         "px": list(e["px"]), "recipeHash": _sha(e["recipeHash"])}
        if e.get("provenanceSha256"):
            out["output"]["provenanceSha256"] = _sha(e["provenanceSha256"])
        status["ai-enhancement"] = {"status": "applied", "detail": f"{p['packId']}@{p['version']}"}
    else:
        out["status"] = "awaiting-output"
        det = f"{cls['mode']} x{scale}"
        if rejected:
            det += "; rejected: " + "; ".join(sorted(rejected))
            out["rejectedOutputs"] = sorted(rejected)
        status["ai-enhancement"] = {"status": "awaiting-output", "detail": det}
    return out, maps


def _platform(entry: dict, ctx: _Ctx) -> dict:
    prof = ctx.profile
    cpx = entry["classic"].get("px")
    fm = entry["classic"].get("firstMip")
    src = entry["source"]
    proc = entry["processing"]
    in_px = proc["output"]["px"] if proc.get("status") == "applied" else src.get("px")
    keeps_mex = not src.get("layoutChanged", False)
    actions: list[dict] = []
    textures = []
    alpha = entry["classic"].get("alphaMode")
    if in_px:
        (tw, th), acts = plat.clamp_size(in_px[0], in_px[1], prof, classic_px=cpx, first_mip=fm,
                                         keeps_mex=keeps_mex)
        actions += [dict(a, role="albedo") for a in acts]
        tex = {"role": "albedo", "inputPx": list(in_px), "targetPx": [tw, th],
               "formats": plat.formats_for(prof, "albedo", alpha)}
        if proc.get("status") == "awaiting-output" and proc.get("plannedPx"):
            tex["plannedPx"] = list(proc["plannedPx"])  # size once the AI output exists
        textures.append(tex)
    for m in entry["materialMaps"]:
        if not m.get("px"):
            continue
        (tw, th), acts = plat.clamp_size(m["px"][0], m["px"][1], prof, classic_px=cpx,
                                         first_mip=fm, keeps_mex=keeps_mex)
        actions += [dict(a, role=m["role"]) for a in acts]
        textures.append({"role": m["role"], "inputPx": list(m["px"]), "targetPx": [tw, th],
                         "formats": plat.formats_for(prof, m["role"], None)})
    out = {"profileId": prof["profileId"], "textures": textures, "actions": actions}
    if cpx:
        out["classicPx"] = cpx
    if not in_px:
        out["actions"].append({"action": "size-unknown", "detail": "no pixel size recorded"})
    return out


def _finish_platform(entries: list[dict], profile: dict) -> None:
    for e in entries:
        alpha = e["classic"].get("alphaMode")
        for t in e["platform"]["textures"]:
            t["preprocessing"] = plat.preprocessing_steps(profile, t["role"], alpha,
                                                          t["targetPx"] != t["inputPx"])


# ---------------------------------------------------------------- plan ----

def build_plan(config_path: Path, selection: list[Path], assets: Path | None = None,
               only_listed: bool = False) -> dict:
    raw, base = load_config(config_path)
    cfg = normalize_config(raw)
    approvals, approvals_info = load_approvals(raw.get("approvals"), base)
    L = cfg["layers"]
    ov_packs = [load_pack(r, base) for r in L["overrides"]["packs"]]
    ai_packs = [load_pack(r, base) for r in L["ai-enhancement"]["packs"]]
    records = load_records(selection)
    facts = load_asset_facts(assets)
    return resolve(cfg, approvals, approvals_info, ov_packs, ai_packs, records, facts,
                   only_listed=only_listed, assets_hash=facts_hash(facts) if assets else None)


def resolve(cfg: dict, approvals: list[dict], approvals_info: dict | None, ov_packs: list[dict],
            ai_packs: list[dict], records: list[dict], facts: dict[str, dict], *,
            only_listed: bool = False, assets_hash: str | None = None) -> dict:
    ctx = _Ctx(cfg, approvals, ov_packs, ai_packs)
    by_asset: dict[str, list[dict]] = {}
    for r in records:
        by_asset.setdefault(r["asset_id"], []).append(r)
    conflicts = []
    want_policy = cfg["raw"].get("selection", {})
    chosen: dict[str, dict] = {}
    for aid in sorted(by_asset):
        rs = by_asset[aid]
        if want_policy.get("policyId"):
            rs = [r for r in rs if r["policy"]["policy_id"] == want_policy["policyId"]
                  and (not want_policy.get("policyVersion")
                       or r["policy"]["version"] == want_policy["policyVersion"])] or []
            if not rs:
                continue
        # explicit priority (none in records yet: 0), then stable record id
        rs = sorted(rs, key=lambda r: (-int(r.get("priority", 0)), r["record_id"]))
        if len(rs) > 1:
            conflicts.append({"assetId": aid, "kind": "multiple-selection-records",
                              "chosen": rs[0]["record_id"], "others": [r["record_id"] for r in rs[1:]]})
        chosen[aid] = rs[0]
    ids = set(facts) if only_listed else set(chosen) | set(facts)
    entries = [resolve_asset(aid, chosen.get(aid), facts.get(aid), ctx) for aid in sorted(ids)]
    budget = plat.enforce_budget(entries, cfg["platformProfile"])
    for e in entries:  # keep the AI target in step with budget reductions
        proc = e["processing"]
        alb = next((t for t in e["platform"]["textures"] if t["role"] == "albedo"), None)
        if alb is None or proc["status"] != "awaiting-output" or "plannedPx" not in proc:
            continue
        if "plannedPx" in alb:
            proc["plannedPx"] = list(alb["plannedPx"])
        else:  # the budget removed the whole gain
            proc["plannedClampedFrom"] = proc.pop("plannedPx")
            proc["status"] = "platform-capped"
            for s in e["layers"]:
                if s["layer"] == "ai-enhancement":
                    s.update(status="platform-capped", detail="memory budget")
    _finish_platform(entries, cfg["platformProfile"])
    effective = effective_config(cfg, approvals_info, ov_packs, ai_packs, list(chosen.values()),
                                 assets_hash, only_listed)
    body = {"effectiveConfig": effective, "assets": entries}
    manifest = {
        "manifestFormatVersion": MANIFEST_FORMAT_VERSION,
        "kind": "sstre-build-manifest",
        "generator": {"name": GENERATOR_NAME, "version": __version__},
        "layerOrder": list(LAYERS),
        "effectiveConfig": effective,
        "effectiveConfigHash": content_hash(effective),
        "assets": entries,
        "conflicts": conflicts,
        "summary": summarize(entries, budget),
        "planHash": content_hash(body),
        "localOnly": True,
    }
    return manifest


def effective_config(cfg, approvals_info, ov_packs, ai_packs, records, assets_hash, only_listed) -> dict:
    L = cfg["layers"]
    layers = []
    for n in LAYERS:
        d = {"layer": n, "order": ORDER[n]}
        if n == "ai-enhancement":
            d["enabled"] = bool(cfg["qualityProfile"].get("enabled"))
            d["qualityProfile"] = cfg["qualityProfileId"]
        else:
            d["enabled"] = L[n]["enabled"]
        if n == "hd-material-maps":
            d["roles"] = sorted(L[n]["roles"])
        if n == "hd-interface":
            d["includeRelayout"] = bool(L[n].get("includeRelayout"))
        if n in APPROVAL_LAYERS:
            d["requiresApproval"] = True
        layers.append(d)
    pols: dict[tuple, dict] = {}
    for r in records:
        k = (r["policy"]["policy_id"], r["policy"]["version"], content_hash(r["policy"]["thresholds"]))
        pols.setdefault(k, {"policyId": k[0], "version": k[1], "thresholdsHash": k[2], "records": 0})
        pols[k]["records"] += 1
    rec_hashes = sorted(record_hash(r) for r in records)
    qp = cfg["qualityProfile"]
    prof = cfg["platformProfile"]

    def pack_info(p):
        d = {k: p[k] for k in ("packId", "version", "kind", "priority", "sha256", "entryCount")}
        if p.get("qualityProfile"):
            d["qualityProfile"] = p["qualityProfile"]
        return d
    eff = {
        "configId": cfg["raw"].get("configId", ""),
        "configFormatVersion": cfg["raw"]["configFormatVersion"],
        "configHash": config_hash(cfg["raw"]),
        "layers": layers,
        "qualityProfile": {"id": cfg["qualityProfileId"], "hash": content_hash(qp), "definition": qp},
        "platformProfile": {"id": prof["profileId"], "status": prof["status"], "hash": content_hash(prof),
                            "profile": prof},
        "approvals": approvals_info,
        "overridePacks": [pack_info(p) for p in sorted(ov_packs, key=_pack_rank)],
        "aiPacks": [pack_info(p) for p in sorted(ai_packs, key=_pack_rank)],
        "selection": {"recordCount": len(records), "recordSetHash": content_hash(rec_hashes),
                      "policies": [pols[k] for k in sorted(pols)]},
        "assetFacts": {"sha256": assets_hash, "onlyListed": bool(only_listed)} if assets_hash else None,
    }
    return eff


def summarize(entries: list[dict], budget: dict) -> dict:
    sup = Counter(e["suppliedBy"] for e in entries)
    decided = Counter(e["source"]["decidedBy"] for e in entries)
    ai = Counter(e["processing"]["status"] for e in entries)
    maps = Counter(m["layer"] for e in entries for m in e["materialMaps"])
    assets_with_maps = sum(1 for e in entries if e["materialMaps"])
    layer_status: dict[str, Counter] = {n: Counter() for n in LAYERS}
    for e in entries:
        for s in e["layers"]:
            layer_status[s["layer"]][s["status"]] += 1
    acts = Counter(a["action"] for e in entries for a in e["platform"]["actions"])
    return {
        "assets": len(entries),
        "suppliedBy": {k: sup[k] for k in sorted(sup)},
        "decidedBy": {k: decided[k] for k in sorted(decided)},
        "aiEnhancement": {k: ai[k] for k in sorted(ai)},
        "materialMaps": {"assets": assets_with_maps, "byLayer": {k: maps[k] for k in sorted(maps)}},
        "layerStatus": {n: {k: c[k] for k in sorted(c)} for n, c in layer_status.items()},
        "platformActions": {k: acts[k] for k in sorted(acts)},
        "budget": budget,
    }


def validate_config(config_path: Path, *, verify_files: bool = False) -> tuple[dict, list[str]]:
    """Load and check a build configuration, its approvals and packs.
    Returns (normalized config, warnings); raises ConfigError on errors.
    ``cfg["pins"]`` lists the hashes to pin in a release config."""
    raw, base = load_config(config_path)
    cfg = normalize_config(raw)
    approvals, ainfo = load_approvals(raw.get("approvals"), base)
    cfg["pins"] = {"config": config_hash(raw)}
    if ainfo:
        cfg["pins"]["approvals"] = ainfo["sha256"]
    L = cfg["layers"]
    packs = []
    for slot in ("overrides", "ai-enhancement"):
        for ref in L[slot]["packs"]:
            p = load_pack(ref, base)
            packs.append(p)
            cfg["pins"][f"pack:{p['packId']}"] = p["sha256"]
            if verify_files:
                pdir = (base / ref["path"]).parent
                for e in p["entries"]:
                    if e.get("file"):
                        got = file_hash(pdir / e["file"])
                        if got != _sha(e["sha256"]):
                            raise ConfigError(f"pack '{p['packId']}': {e['file']} hash mismatch")
    warnings = config_warnings(cfg, approvals)
    for p in packs:
        if p["kind"] == "ai-enhancement" and p["qualityProfile"] not in (None, cfg["qualityProfileId"]):
            warnings.append(f"AI pack '{p['packId']}' was made for quality profile "
                            f"'{p['qualityProfile']}', config uses '{cfg['qualityProfileId']}': its outputs are not used")
    return cfg, warnings
