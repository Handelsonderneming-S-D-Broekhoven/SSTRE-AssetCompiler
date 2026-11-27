# SPDX-FileCopyrightText: 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe
# SPDX-License-Identifier: GPL-3.0-only
"""Declarative platform (target) profiles for textures (ADR-0009 rule 5).

A profile is data (umbrella ``schemas/platform-profile.schema.json``). It sets
texture size limits, the containers and pixel/compression formats per render
target, the mip policy, a memory budget and the preprocessing steps. Profiles
are independent of the asset layers: the same layer selection can be served
to different targets by switching the profile.

This module applies a profile to planned textures: it clamps sizes
(deterministic power-of-two halving or exact fit), picks a format per target
and texture role, estimates memory and enforces the budget. Every change is
recorded as an action so the build manifest can explain the final size.

Built-in profiles live in ``data/platform-profiles/``. ``pc-default`` carries
the limit validated in EXP-012 (2048x4096 = 8.4 Mpx loaded; 4096x4096 crashed
the Classic-VK runtime on Vulkan and OpenGL). ``legacy-constrained`` is a
**hypothetical** illustration and contains no platform-SDK information.
"""

from __future__ import annotations

import heapq
import json
from importlib import resources

PROFILE_FORMAT_VERSION = "0.1.0"
BUILTIN_PROFILES = ("pc-default", "pc-low-memory", "legacy-constrained")

# Bits per pixel of the formats a profile may name (block formats: block bits
# / block pixels). Palettised: 8-bit indices; the palette is not counted.
FORMAT_BPP = {
    "rgba8": 32.0, "rgb8": 24.0, "rgba4444": 16.0, "rgba5551": 16.0, "rgb565": 16.0,
    "bc1": 4.0, "bc3": 8.0, "bc4": 4.0, "bc5": 8.0, "bc7": 8.0,
    "astc-4x4": 8.0, "astc-6x6": 128.0 / 36.0, "astc-8x8": 2.0,
    "etc2-rgb8": 4.0, "etc2-rgba8": 8.0, "eac-r11": 4.0, "eac-rg11": 8.0,
    "pal8": 8.0, "l8": 8.0, "la8": 16.0,
}
ROLE_SLOTS = ("color-opaque", "color-alpha", "normal", "mask")
MAP_ROLES = ("normal", "gloss", "ao", "height")


class ProfileError(ValueError):
    pass


def load_builtin(profile_id: str) -> dict:
    if profile_id not in BUILTIN_PROFILES:
        raise ProfileError(f"unknown platform profile '{profile_id}' "
                           f"(built in: {', '.join(BUILTIN_PROFILES)})")
    text = resources.files("sstre_assetcompiler").joinpath(
        "data", "platform-profiles", f"{profile_id}.json").read_text(encoding="utf-8")
    prof = json.loads(text)
    check_profile(prof)
    return prof


def check_profile(p: dict) -> None:
    """Semantic checks beyond the JSON schema (kept dependency-free)."""
    for key in ("profileFormatVersion", "profileId", "status", "textureLimits", "targets",
                "mipPolicy", "memoryBudget", "preprocessing"):
        if key not in p:
            raise ProfileError(f"platform profile lacks '{key}'")
    lim = p["textureLimits"]
    for key in ("maxWidth", "maxHeight", "maxPixels"):
        if not isinstance(lim.get(key), int) or lim[key] < 1:
            raise ProfileError(f"textureLimits.{key} must be a positive integer")
    if not p["targets"]:
        raise ProfileError("a platform profile needs at least one target")
    ids = [t["targetId"] for t in p["targets"]]
    if len(set(ids)) != len(ids):
        raise ProfileError("duplicate targetId in platform profile")
    for t in p["targets"]:
        for slot, fmt in t.get("formats", {}).items():
            if slot not in ROLE_SLOTS:
                raise ProfileError(f"target {t['targetId']}: unknown role slot '{slot}'")
            if fmt not in FORMAT_BPP:
                raise ProfileError(f"target {t['targetId']}: unknown format '{fmt}'")
        if "color-opaque" not in t.get("formats", {}):
            raise ProfileError(f"target {t['targetId']}: 'color-opaque' format required")
    acc = p["memoryBudget"].get("accountingTarget")
    if acc not in ids:
        raise ProfileError(f"memoryBudget.accountingTarget '{acc}' is not a target of the profile")
    if p["preprocessing"]["downscale"]["policy"] not in ("halve-until-fit", "fit-exact"):
        raise ProfileError("preprocessing.downscale.policy must be halve-until-fit or fit-exact")


# ------------------------------------------------------------ size limits --

def _is_pow2(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


def _floor_pow2(n: int) -> int:
    return 1 << (max(1, n).bit_length() - 1)


def violations(w: int, h: int, lim: dict) -> list[str]:
    out = []
    if w > lim["maxWidth"]:
        out.append("max-width")
    if h > lim["maxHeight"]:
        out.append("max-height")
    if "maxLongEdge" in lim and max(w, h) > lim["maxLongEdge"]:
        out.append("max-long-edge")
    if "maxShortEdge" in lim and min(w, h) > lim["maxShortEdge"]:
        out.append("max-short-edge")
    if w * h > lim["maxPixels"]:
        out.append("max-pixels")
    if lim.get("requirePowerOfTwo") and not (_is_pow2(w) and _is_pow2(h)):
        out.append("power-of-two")
    return out


def clamp_size(w: int, h: int, profile: dict, *, classic_px: tuple[int, int] | None = None,
               first_mip: int | None = None, keeps_mex: bool = True) -> tuple[tuple[int, int], list[dict]]:
    """Clamp a desired size to the profile limits. Returns ((w, h), actions)."""
    lim = profile["textureLimits"]
    policy = profile["preprocessing"]["downscale"]["policy"]
    actions: list[dict] = []
    req = (w, h)
    # EXP-012: with identical mex the raster path supports density <= 2^firstMip.
    if (lim.get("respectFirstMipDensity") and keeps_mex and first_mip is not None
            and classic_px is not None):
        cw, ch = classic_px[0] << first_mip, classic_px[1] << first_mip
        if w > cw or h > ch:
            nw, nh = w, h
            while nw > cw or nh > ch:
                nw, nh = max(1, nw // 2), max(1, nh // 2)
            actions.append({"action": "first-mip-density-cap", "from": [w, h], "to": [nw, nh],
                            "detail": f"firstMip={first_mip}"})
            w, h = nw, nh
    if lim.get("requirePowerOfTwo") and not (_is_pow2(w) and _is_pow2(h)):
        nw, nh = _floor_pow2(w), _floor_pow2(h)
        actions.append({"action": "round-down-power-of-two", "from": [w, h], "to": [nw, nh]})
        w, h = nw, nh
    v = violations(w, h, lim)
    if not v:
        return (w, h), actions + (_below_classic(w, h, classic_px, req) if (w, h) != req else [])
    ow, oh = w, h
    if policy == "halve-until-fit":
        steps = 0
        while violations(w, h, lim):
            if w == 1 and h == 1:
                break
            w, h = max(1, w // 2), max(1, h // 2)
            steps += 1
        actions.append({"action": "downscale", "from": [ow, oh], "to": [w, h],
                        "detail": f"halve x{steps}; limits: {','.join(v)}"})
    else:  # fit-exact: largest same-aspect size within all limits
        s = min(lim["maxWidth"] / w, lim["maxHeight"] / h, (lim["maxPixels"] / (w * h)) ** 0.5)
        if "maxLongEdge" in lim:
            s = min(s, lim["maxLongEdge"] / max(w, h))
        if "maxShortEdge" in lim:
            s = min(s, lim["maxShortEdge"] / min(w, h))
        w, h = max(1, int(w * s)), max(1, int(h * s))
        while violations(w, h, lim) and (w > 1 or h > 1):  # integer rounding guard
            w, h = max(1, w - 1), max(1, h - 1)
        actions.append({"action": "downscale", "from": [ow, oh], "to": [w, h],
                        "detail": f"fit-exact; limits: {','.join(v)}"})
    return (w, h), actions + _below_classic(w, h, classic_px, req)


def _below_classic(w: int, h: int, classic_px, req: tuple[int, int]) -> list[dict]:
    if classic_px is not None and (w < classic_px[0] or h < classic_px[1]):
        return [{"action": "below-classic-resolution", "from": list(req), "to": [w, h],
                 "detail": f"Classic {classic_px[0]}x{classic_px[1]} exceeds the profile limits"}]
    return []


# ---------------------------------------------------------------- formats --

def slot_for(role: str, alpha_mode: str | None) -> str:
    if role == "albedo":
        return "color-alpha" if alpha_mode in ("cutout", "blend") else "color-opaque"
    if role == "normal":
        return "normal"
    return "mask"


def formats_for(profile: dict, role: str, alpha_mode: str | None) -> dict:
    """Format per target for one texture role; None when the target cannot
    take the role (e.g. a raster target without material maps)."""
    slot = slot_for(role, alpha_mode)
    out = {}
    for t in sorted(profile["targets"], key=lambda t: t["targetId"]):
        f = t.get("formats", {})
        # A missing slot means the target cannot take this role; alpha is never
        # silently dropped into an opaque format.
        out[t["targetId"]] = f.get(slot)
    return out


def texture_bytes(w: int, h: int, fmt: str | None, include_mips: bool) -> int:
    if fmt is None:
        return 0
    bpp = FORMAT_BPP[fmt]
    total = 0.0
    while True:
        if fmt.startswith(("bc", "etc2", "eac", "astc")):
            bw = 4 if not fmt.startswith("astc") else int(fmt.split("-")[1].split("x")[0])
            bx, by = -(-w // bw), -(-h // bw)
            total += bx * by * bw * bw * bpp / 8.0
        else:
            total += w * h * bpp / 8.0
        if not include_mips or (w == 1 and h == 1):
            break
        w, h = max(1, w // 2), max(1, h // 2)
    return int(round(total))


def _budget_px(t: dict) -> list[int]:
    """Size that counts for the budget: the planned AI size where an output
    is still awaited, else the target size."""
    return t.get("plannedPx") or t["targetPx"]


def asset_bytes(textures: list[dict], profile: dict, *, planned: bool = True) -> int:
    mb = profile["memoryBudget"]
    target = mb["accountingTarget"]
    inc = mb.get("includeMips", True)
    total = 0
    for t in textures:
        w, h = _budget_px(t) if planned else t["targetPx"]
        total += texture_bytes(w, h, t["formats"].get(target), inc)
    return total


def enforce_budget(entries: list[dict], profile: dict) -> dict:
    """Apply the memory budget over all planned assets (in place).

    ``entries`` are plan entries with ``platform.textures`` and
    ``platform.classicPx``. Policy ``report`` only measures; ``reduce-largest``
    halves the currently largest asset (ties: asset ID) until the total fits,
    never below the Classic resolution when ``neverBelowClassic`` is set."""
    mb = profile["memoryBudget"]
    budget = mb.get("bytes")
    never_below = profile["preprocessing"]["downscale"].get("neverBelowClassic", True)
    sizes = {e["assetId"]: asset_bytes(e["platform"]["textures"], profile) for e in entries}
    total = sum(sizes.values())
    before = total
    halvings = 0
    if budget is not None and total > budget and mb.get("policy") == "reduce-largest":
        by_id = {e["assetId"]: e for e in entries}
        heap = [(-b, aid) for aid, b in sizes.items() if b > 0]
        heapq.heapify(heap)
        while total > budget and heap:
            nb, aid = heapq.heappop(heap)
            e = by_id[aid]
            tex = e["platform"]["textures"]
            alb = next((t for t in tex if t["role"] == "albedo"), tex[0])
            w, h = _budget_px(alb)
            cpx = e["platform"].get("classicPx")
            if w == 1 and h == 1:
                continue
            if never_below and cpx and (w // 2 < cpx[0] or h // 2 < cpx[1]):
                continue
            for t in tex:
                key = "plannedPx" if "plannedPx" in t else "targetPx"
                tw, th = t[key]
                t[key] = [max(1, tw // 2), max(1, th // 2)]
                if key == "plannedPx" and t["plannedPx"][0] * t["plannedPx"][1] <= t["targetPx"][0] * t["targetPx"][1]:
                    t["targetPx"] = [min(t["targetPx"][0], t["plannedPx"][0]), min(t["targetPx"][1], t["plannedPx"][1])]
                    del t["plannedPx"]
            e["platform"]["actions"].append({"action": "budget-halve", "from": [w, h],
                                             "to": [max(1, w // 2), max(1, h // 2)]})
            new = asset_bytes(tex, profile)
            total += new - sizes[aid]
            sizes[aid] = new
            halvings += 1
            heapq.heappush(heap, (-new, aid))
    for e in entries:
        e["platform"]["estimatedBytes"] = sizes[e["assetId"]]
    current = sum(asset_bytes(e["platform"]["textures"], profile, planned=False) for e in entries)
    return {"budgetBytes": budget, "policy": mb.get("policy"), "accountingTarget": mb["accountingTarget"],
            "estimatedBytesBefore": before, "estimatedBytes": total, "budgetHalvings": halvings,
            "estimatedBytesWithoutPendingAi": current,
            "withinBudget": budget is None or total <= budget}


def preprocessing_steps(profile: dict, role: str, alpha_mode: str | None, resized: bool) -> list[str]:
    """Ordered preprocessing step ids for one texture (recorded, not executed)."""
    pp = profile["preprocessing"]
    steps = []
    cs = pp.get("colorSpace", {})
    if role == "albedo":
        steps.append(f"colour-space:{cs.get('color', 'srgb')}")
    else:
        steps.append(f"colour-space:{cs.get('data', 'linear')}")
    al = pp.get("alpha", {})
    if role == "albedo" and alpha_mode in ("cutout", "blend"):
        steps.append(f"alpha:{al.get('mode', 'preserve')}")
        if al.get("dilateEdges"):
            steps.append("alpha:dilate-edges")
        if alpha_mode == "cutout" and al.get("cutoutCoverage"):
            steps.append("alpha:preserve-coverage")
    if resized:
        d = pp["downscale"]
        steps.append(f"downscale:{d['policy']}:{d.get('filter', 'box')}"
                     + (":linear-light" if cs.get("resampleInLinear") and role == "albedo" else ""))
        if role == "normal" and pp.get("normals", {}).get("renormalizeAfterResample", True):
            steps.append("normals:renormalize")
    steps.append(f"mips:{profile['mipPolicy'].get('filter', 'box')}")
    return steps
