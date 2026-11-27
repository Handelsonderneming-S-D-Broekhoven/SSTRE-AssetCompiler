# SPDX-FileCopyrightText: 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe
# SPDX-License-Identifier: GPL-3.0-only
"""Per-asset source selection between Classic, HD (verified match) and the
optional HD-art layer.

Owner policy (2026-11-27): do not reject same-resolution HD categorically;
select per asset on effective image quality, resolution, compression
artefacts, extra material maps, artwork difference, UV compatibility and
preservation of the original art direction; prefer Classic where HD is merely
a degraded/lossy copy. Every decision is written as a record that validates
against the umbrella ``schemas/source-selection.schema.json``.

This module never decodes HD formats. It consumes *decoded images plus
metadata* through a file interface (see ``docs/source-selection.md``): any
external, local decoder may produce the PNGs. Requests and records contain
local paths and hashes of game assets and stay local (GATE-05).
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import GENERATOR_NAME, __version__

RECORD_SCHEMA_VERSION = "0.1.0"
POLICY_ID = "sstre-source-selection"
POLICY_VERSION = "0.1.0"

# Categories whose Classic texture is never replaced (RD-002 s16.4).
NEVER_REPLACE = frozenset({"font", "technical", "procedural_effect"})

DEFAULT_THRESHOLDS = {
    # same content / layout (EXP-008 calibration, see docs)
    "layout_ssim_min": 0.55,        # SSIM at 64 px: below -> different layout / not UV compatible
    "same_art_ssim_min": 0.80,      # SSIM at Classic resolution: below -> redrawn ("HD-art")
    "drift_delta_e_max": 6.0,       # mean CIE76 dE at Classic resolution: above -> art drift
    "hist_intersection_min": 0.80,  # luminance histogram intersection
    # equivalence / degradation (same resolution)
    "equivalent_psnr_db": 48.0,     # >= : HD adds nothing over the lossless Classic original
    "lossy_psnr_db": 40.0,          # <  : HD is a lossy copy at this resolution
    "blockiness_excess_max": 0.06,  # 4x4 block-boundary gradient excess vs Classic (same res)
    "upscale_artefact_max": 0.20,   # blockiness excess / linear scale (blocks shrink on screen)
    "same_res_sharpness_gain_min": 1.15,
    "same_res_min_classic_sharpness": 0.001,  # below: Classic is smooth (glows), ratio meaningless
    # upscale value
    "resolution_ratio_min": 2.0,    # pixel-count ratio counted as a resolution gain
    "detail_energy_min": 0.10,      # share of HD signal above the Classic Nyquist band
    "alpha_mae_max": 0.08,          # mean |alpha diff| (0..1) for alpha-carrying Classic
}


# ---------------------------------------------------------------- image ops --

def _pil():
    from PIL import Image
    return Image


def resize(a: np.ndarray, w: int, h: int, *, down: bool) -> np.ndarray:
    """Float32 resize per channel (BOX for downsampling, BICUBIC for up)."""
    Image = _pil()
    filt = Image.BOX if down else Image.BICUBIC
    chans = [np.asarray(Image.fromarray(np.ascontiguousarray(a[..., c]), "F").resize((w, h), filt))
             for c in range(a.shape[2])]
    return np.stack(chans, -1).astype(np.float32)


def to_float(rgba: np.ndarray) -> np.ndarray:
    return rgba.astype(np.float32) / 255.0


def composite_grey(f: np.ndarray) -> np.ndarray:
    a = f[..., 3:4]
    return f[..., :3] * a + 0.5 * (1.0 - a)


def luminance(rgb: np.ndarray) -> np.ndarray:
    return rgb @ np.array([0.2126, 0.7152, 0.0722], np.float32)


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(np.mean((a - b) ** 2))
    return 99.0 if mse <= 1e-12 else min(99.0, 10.0 * math.log10(1.0 / mse))


def _gauss_kernel(sigma: float = 1.5, radius: int = 3) -> np.ndarray:
    x = np.arange(-radius, radius + 1, dtype=np.float32)
    k = np.exp(-(x * x) / (2 * sigma * sigma))
    return k / k.sum()


def _blur(y: np.ndarray, k: np.ndarray) -> np.ndarray:
    r = len(k) // 2
    p = np.pad(y, r, mode="reflect") if min(y.shape) > r else np.pad(y, r, mode="edge")
    # separable convolution
    h = np.zeros((p.shape[0], y.shape[1]), np.float32)
    for i, kv in enumerate(k):
        h += kv * p[:, i:i + y.shape[1]]
    v = np.zeros(y.shape, np.float32)
    for i, kv in enumerate(k):
        v += kv * h[i:i + y.shape[0], :]
    return v


def ssim(a: np.ndarray, b: np.ndarray) -> float:
    """Mean SSIM on luminance (Wang et al. 2004 constants, Gaussian 7x7)."""
    x, y = luminance(a), luminance(b)
    k = _gauss_kernel()
    c1, c2 = 0.01 ** 2, 0.03 ** 2
    mx, my = _blur(x, k), _blur(y, k)
    sxx = _blur(x * x, k) - mx * mx
    syy = _blur(y * y, k) - my * my
    sxy = _blur(x * y, k) - mx * my
    s = ((2 * mx * my + c1) * (2 * sxy + c2)) / ((mx * mx + my * my + c1) * (sxx + syy + c2))
    return float(np.mean(s))


def _srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    c = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    m = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722],
                  [0.0193, 0.1192, 0.9505]], np.float32)
    xyz = c @ m.T / np.array([0.95047, 1.0, 1.08883], np.float32)
    f = np.where(xyz > 216 / 24389, np.cbrt(xyz), (24389 / 27 * xyz + 16) / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]),
                     200 * (f[..., 1] - f[..., 2])], -1)


def delta_e(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.mean(np.linalg.norm(_srgb_to_lab(a) - _srgb_to_lab(b), axis=-1)))


def hist_intersection(a: np.ndarray, b: np.ndarray, bins: int = 64) -> float:
    ha, _ = np.histogram(luminance(a), bins=bins, range=(0, 1))
    hb, _ = np.histogram(luminance(b), bins=bins, range=(0, 1))
    return float(np.minimum(ha / ha.sum(), hb / hb.sum()).sum())


def blockiness(rgb: np.ndarray, block: int = 4) -> float:
    """Ratio of mean |gradient| across ``block``-pixel boundaries to the mean
    elsewhere (1.0 = no block structure). Detects BCn block artefacts."""
    y = luminance(rgb)
    vals = []
    for g in (np.abs(np.diff(y, axis=1)), np.abs(np.diff(y, axis=0)).T):
        if g.shape[1] < 2 * block:
            continue
        idx = np.arange(g.shape[1])
        on = (idx % block) == block - 1
        inner = float(g[:, ~on].mean())
        if inner > 1e-6:
            vals.append(float(g[:, on].mean()) / inner)
    return float(np.mean(vals)) if vals else 1.0


def sharpness(rgb: np.ndarray) -> float:
    y = luminance(rgb)
    if min(y.shape) < 3:
        return 0.0
    lap = (-4 * y[1:-1, 1:-1] + y[:-2, 1:-1] + y[2:, 1:-1] + y[1:-1, :-2] + y[1:-1, 2:])
    return float(lap.var())


def detail_energy(hd: np.ndarray, w: int, h: int) -> float:
    """Share of HD signal (RMS) above the Classic Nyquist band: HD minus its
    own down-then-up resampling to the Classic size. An HD image that is only
    an interpolated Classic scores near 0."""
    H, W = hd.shape[:2]
    if (W, H) == (w, h):
        return 0.0
    lo = resize(resize(hd, w, h, down=True), W, H, down=False)
    hf = luminance(hd) - luminance(lo)
    total = luminance(hd) - luminance(hd).mean()
    den = float(np.sqrt(np.mean(total ** 2)))
    return 0.0 if den < 1e-6 else float(np.sqrt(np.mean(hf ** 2))) / den


def _low_size(w: int, h: int, side: int = 64) -> tuple[int, int]:
    s = min(1.0, side / max(h, w))
    return max(1, round(w * s)), max(1, round(h * s))


def _low(f: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    if f.shape[1] == size[0] and f.shape[0] == size[1]:
        return f
    return resize(f, size[0], size[1], down=True)


# --------------------------------------------------------------- metrics ----

def compare(classic_rgba: np.ndarray, cand_rgba: np.ndarray, *, classic_alpha_mode: str) -> dict:
    """All pixel metrics for one Classic/candidate pair (deterministic)."""
    c = to_float(classic_rgba)
    d = to_float(cand_rgba)
    h, w = c.shape[:2]
    H, W = d.shape[:2]
    m: dict = {"classic_px": [w, h], "candidate_px": [W, H],
               "resolution_ratio": round((W * H) / (w * h), 4)}
    aspect_ok = abs(W * h - H * w) <= max(W, w) // 64
    m["aspect_match"] = bool(aspect_ok)
    if not aspect_ok:
        return m
    d_at_c = resize(d, w, h, down=True) if (W, H) != (w, h) else d
    cg, dg = composite_grey(c), composite_grey(d_at_c)
    m["bit_identical"] = bool((W, H) == (w, h) and np.array_equal(classic_rgba, cand_rgba))
    m["psnr_db"] = round(psnr(cg, dg), 3)
    m["ssim"] = round(ssim(cg, dg), 4)
    ls = _low_size(w, h, 32)
    lc, ld = composite_grey(_low(c, ls)), composite_grey(_low(d, ls))
    m["layout_ssim"] = round(ssim(lc, ld), 4) if min(lc.shape[:2]) >= 7 else m["ssim"]
    # colour / tone drift is measured on the low-pass (layout) images so that
    # detail differences (sharpening, noise, compression) do not count as drift
    ds = _low_size(w, h, max(4, min(32, max(w, h) // 8)))
    m["delta_e"] = round(delta_e(composite_grey(_low(c, ds)), composite_grey(_low(d, ds))), 3)
    m["hist_intersection"] = round(hist_intersection(cg, dg), 4)
    m["blockiness_classic"] = round(blockiness(cg), 4)
    m["blockiness_candidate"] = round(blockiness(composite_grey(d)), 4)
    m["blockiness_excess"] = round(m["blockiness_candidate"] - m["blockiness_classic"], 4)
    m["artefact_index"] = round(m["blockiness_excess"] / math.sqrt(max(1.0, m["resolution_ratio"])), 4)
    sc = sharpness(cg)
    sd = sharpness(dg)
    m["sharpness_classic"] = round(sc, 6)
    m["sharpness_ratio"] = round(sd / sc, 4) if sc > 1e-9 else 1.0
    m["detail_energy"] = round(detail_energy(composite_grey(d), w, h), 4)
    if classic_alpha_mode in ("cutout", "blend"):
        m["alpha_mae"] = round(float(np.mean(np.abs(c[..., 3] - d_at_c[..., 3]))), 4)
        m["candidate_alpha_present"] = bool(cand_rgba[..., 3].min() < 255)
    return m


# ---------------------------------------------------------------- policy ----

@dataclass
class Policy:
    thresholds: dict = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    policy_id: str = POLICY_ID
    version: str = POLICY_VERSION
    hd_art_opt_in: bool = False  # owner opt-in for the HD-art layer (default off)

    @classmethod
    def load(cls, path: Path | None) -> "Policy":
        if path is None:
            return cls()
        doc = json.loads(path.read_text(encoding="utf-8"))
        th = dict(DEFAULT_THRESHOLDS)
        th.update(doc.get("thresholds", {}))
        return cls(th, doc.get("policy_id", POLICY_ID), doc.get("version", POLICY_VERSION),
                   bool(doc.get("hd_art_opt_in", False)))

    def as_record(self) -> dict:
        return {"policy_id": self.policy_id, "version": self.version,
                "hd_art_opt_in": self.hd_art_opt_in, "thresholds": dict(self.thresholds)}


RULES = [
    ("R01_never_replace_category", "Category is never replaced (fonts, technical maps, procedural effects)"),
    ("R02_no_candidate", "No HD candidate"),
    ("R03_unverified", "Best candidate not verified as same content"),
    ("R04_uv_incompatible", "Aspect or layout differs: not a drop-in replacement"),
    ("R05_alpha_mismatch", "Classic alpha not preserved by candidate"),
    ("R06_equivalent", "HD equals Classic (bit-identical or above equivalence PSNR): no gain"),
    ("R07_art_difference", "Same layout but redrawn/recoloured artwork (HD-art)"),
    ("R08_upscale_with_detail", "More pixels with real detail above Classic Nyquist, artefacts and drift within limits"),
    ("R09_upscale_without_detail", "More pixels but no real detail (interpolated/soft) or block artefacts dominate"),
    ("R10_same_res_cleaner", "Same resolution, sharper and not blockier, art preserved"),
    ("R11_degraded_copy", "Lossy/blocky copy of the Classic art"),
    ("R12_default_classic", "No rule found a benefit"),
]
_RULE_TEXT = dict(RULES)


def _verified(c: dict) -> bool:
    return bool(c.get("match", {}).get("verified"))


def decide(request: dict, metrics: dict[str, dict], policy: Policy) -> dict:
    """Apply the ordered rules to one asset. ``metrics`` maps candidate_id ->
    output of :func:`compare` (missing for candidates without an image)."""
    t = policy.thresholds
    cat = request.get("category", "other")
    cands = request.get("candidates", [])
    trace: list[dict] = []

    def ev(rule: str, fired: bool, detail: str = "") -> bool:
        trace.append({"rule_id": rule, "fired": bool(fired), **({"detail": detail} if detail else {})})
        return fired

    def result(res: str, rule: str, cand: dict | None = None, **extra) -> dict:
        base = "hd" if res == "hd" else "classic"
        out = {"result": res, "base_source": base, "rule_fired": rule,
               "reason": _RULE_TEXT[rule], "selected_candidate": cand["candidate_id"] if cand and res == "hd" else None,
               "requires_opt_in": False}
        out.update(extra)
        return out

    if ev("R01_never_replace_category", cat in NEVER_REPLACE or request.get("classic", {}).get("procedural_effect", False), cat):
        return _finish(request, metrics, policy, trace, result("classic", "R01_never_replace_category"))
    usable = [c for c in cands if c["candidate_id"] in metrics]
    if ev("R02_no_candidate", not usable):
        return _finish(request, metrics, policy, trace, result("classic_fallback", "R02_no_candidate"))

    # best candidate: verified first, then highest layout similarity, then resolution
    def key(c):
        m = metrics[c["candidate_id"]]
        return (_verified(c), m.get("aspect_match", False), m.get("layout_ssim", -1), m["resolution_ratio"])
    best = max(usable, key=key)
    m = metrics[best["candidate_id"]]
    maps = [r for r in ("normal", "gloss", "ao", "height") if best.get("companion_maps", {}).get(r)]

    def with_maps(res: dict) -> dict:
        """Classic albedo + HD companion maps when the layout is unchanged."""
        if res["base_source"] == "classic" and maps and _verified(best) and m.get("aspect_match") \
                and m.get("layout_ssim", 0) >= t["layout_ssim_min"] and res["result"] != "classic_fallback":
            trace.append({"rule_id": "M01_companion_maps", "fired": True, "detail": ",".join(maps)})
            res = dict(res, result="classic_with_hd_maps", companion_maps=maps,
                       maps_candidate=best["candidate_id"])
        return res

    art_ok = (m.get("aspect_match") and m.get("layout_ssim", 0) >= t["layout_ssim_min"])
    if not _verified(best):
        ev("R03_unverified", True, best.get("match", {}).get("method", ""))
        # related artwork with the same layout can still be an HD-art candidate
        differs = m.get("ssim", 1.0) < t["same_art_ssim_min"] or             m.get("delta_e", 0.0) > t["drift_delta_e_max"]
        if art_ok and differs and best.get("match", {}).get("related", False):
            trace.append({"rule_id": "R07_art_difference", "fired": True,
                          "detail": f"unverified related candidate; ssim={m.get('ssim')} dE={m.get('delta_e')}"})
            return _finish(request, metrics, policy, trace, _art(result, best, policy))
        return _finish(request, metrics, policy, trace, result("classic_fallback", "R03_unverified"))
    ev("R03_unverified", False)
    if ev("R04_uv_incompatible", not art_ok,
          f"aspect_match={m.get('aspect_match')} layout_ssim={m.get('layout_ssim')}"):
        return _finish(request, metrics, policy, trace, result("classic", "R04_uv_incompatible"))
    if "alpha_mae" in m and ev("R05_alpha_mismatch",
                               (not m.get("candidate_alpha_present")) or m["alpha_mae"] > t["alpha_mae_max"],
                               f"alpha_mae={m['alpha_mae']}"):
        return _finish(request, metrics, policy, trace, with_maps(result("classic", "R05_alpha_mismatch")))
    same_res = m["resolution_ratio"] <= 1.0
    if ev("R06_equivalent", same_res and (m["bit_identical"] or m["psnr_db"] >= t["equivalent_psnr_db"]),
          f"psnr={m['psnr_db']}"):
        return _finish(request, metrics, policy, trace, with_maps(result("classic", "R06_equivalent")))
    drift = m["delta_e"] > t["drift_delta_e_max"] or m["hist_intersection"] < t["hist_intersection_min"]
    if ev("R07_art_difference", m["ssim"] < t["same_art_ssim_min"] or drift,
          f"ssim={m['ssim']} dE={m['delta_e']} hist={m['hist_intersection']}"):
        return _finish(request, metrics, policy, trace, _art(result, best, policy))
    blocky = m["blockiness_excess"] > t["blockiness_excess_max"]
    if m["resolution_ratio"] >= t["resolution_ratio_min"]:
        if ev("R08_upscale_with_detail", m["detail_energy"] >= t["detail_energy_min"]
              and m["artefact_index"] <= t["upscale_artefact_max"],
              f"ratio={m['resolution_ratio']} detail={m['detail_energy']} art={m['artefact_index']}"):
            return _finish(request, metrics, policy, trace, result("hd", "R08_upscale_with_detail", best))
        ev("R09_upscale_without_detail", True, f"detail={m['detail_energy']} art={m['artefact_index']}")
        return _finish(request, metrics, policy, trace, with_maps(result("classic", "R09_upscale_without_detail")))
    if ev("R10_same_res_cleaner", same_res and not blocky
          and m["sharpness_classic"] >= t["same_res_min_classic_sharpness"]
          and m["sharpness_ratio"] >= t["same_res_sharpness_gain_min"],
          f"sharp={m['sharpness_ratio']} blk={m['blockiness_excess']} psnr={m['psnr_db']}"):
        return _finish(request, metrics, policy, trace, result("hd", "R10_same_res_cleaner", best))
    if ev("R11_degraded_copy", m["psnr_db"] < t["lossy_psnr_db"] or blocky,
          f"psnr={m['psnr_db']} blk={m['blockiness_excess']}"):
        return _finish(request, metrics, policy, trace, with_maps(result("classic", "R11_degraded_copy")))
    ev("R12_default_classic", True)
    return _finish(request, metrics, policy, trace, with_maps(result("classic", "R12_default_classic")))


def _art(result, cand: dict, policy: Policy) -> dict:
    res = result("hd_art_candidate", "R07_art_difference")
    res["hd_art"] = {"candidate_id": cand["candidate_id"],
                     "status": "opted_in" if policy.hd_art_opt_in else "pending_owner_opt_in"}
    res["requires_opt_in"] = True
    return res


def _finish(request: dict, metrics: dict, policy: Policy, trace: list, decision: dict) -> dict:
    cands = []
    for c in request.get("candidates", []):
        e = {k: c[k] for k in ("candidate_id", "source_kind", "edition", "virtual_path",
                               "source_sha256", "source_format", "match", "companion_maps",
                               "image_sha256") if k in c}
        if c["candidate_id"] in metrics:
            e["metrics"] = metrics[c["candidate_id"]]
        else:
            e["metrics"] = None
        cands.append(e)
    cl = request.get("classic", {})
    rec = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "record_id": "sel-" + hashlib.sha256(
            json.dumps([request.get("asset_id"), policy.as_record()], sort_keys=True).encode()
        ).hexdigest()[:16],
        "asset_id": request["asset_id"],
        "category": request.get("category", "other"),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "generator": {"name": GENERATOR_NAME, "version": __version__},
        "policy": policy.as_record(),
        "classic": {k: cl[k] for k in ("virtual_path", "source_sha256", "width_px", "height_px",
                                       "mex_width", "mex_height", "alpha_mode",
                                       "procedural_effect", "image_sha256") if k in cl},
        "candidates": cands,
        "rules_evaluated": trace,
        "decision": decision,
        "local_only": True,
    }
    return rec


# ------------------------------------------------------------------- CLI ----

def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def evaluate_request(request: dict, policy: Policy, base_dir: Path) -> dict:
    from .texio import load_png
    cl = request.get("classic", {})
    metrics: dict[str, dict] = {}
    if cl.get("image"):
        cp = base_dir / cl["image"]
        classic = load_png(cp)
        cl.setdefault("image_sha256", _sha(cp))
        for c in request.get("candidates", []):
            if not c.get("image"):
                continue
            p = base_dir / c["image"]
            c.setdefault("image_sha256", _sha(p))
            metrics[c["candidate_id"]] = compare(classic, load_png(p),
                                                 classic_alpha_mode=cl.get("alpha_mode", "opaque"))
    return decide(request, metrics, policy)


def run_cli(requests_path: Path, policy_path: Path | None, out_dir: Path) -> int:
    policy = Policy.load(policy_path)
    reqs = json.loads(requests_path.read_text(encoding="utf-8"))
    out_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, dict[str, int]] = {}
    for req in reqs:
        rec = evaluate_request(req, policy, requests_path.parent)
        name = rec["record_id"] + ".json"
        (out_dir / name).write_text(json.dumps(rec, indent=1) + "\n", encoding="utf-8")
        row = summary.setdefault(rec["category"], {})
        key = f"{rec['decision']['result']}:{rec['decision']['rule_fired']}"
        row[key] = row.get(key, 0) + 1
    (out_dir / "summary.json").write_text(json.dumps(
        {"policy": policy.as_record(), "records": len(reqs), "by_category": summary},
        indent=1) + "\n", encoding="utf-8")
    return 0
