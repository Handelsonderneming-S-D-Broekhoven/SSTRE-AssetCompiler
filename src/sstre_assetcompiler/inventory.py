# SPDX-FileCopyrightText: 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe
# SPDX-License-Identifier: GPL-3.0-only
"""Build ``asset-inventory.schema.json`` documents from detected installs."""

from __future__ import annotations

import hashlib
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import GENERATOR_NAME, __version__
from .archives import ArchiveScan, scan_archive
from .editions import HashCache, Profile, default_source_edition, scan_files
from .texheader import (HEADER_PROBE_BYTES, TexHeaderError, parse_classic_tex_header,
                        sniff_format, texture_info)

INVENTORY_SCHEMA_VERSION = "0.1.0"

# File extension -> asset_type. Extensions are format knowledge (RD-002 s.5),
# not game content.
ASSET_TYPES = {
    ".tex": "texture", ".mdl": "model", ".bm": "mesh", ".bmf": "mesh",
    ".bs": "skeleton", ".skl": "skeleton", ".ba": "animation", ".ans": "animation",
    ".anh": "animation", ".amc": "animation", ".wld": "world", ".mtr": "material",
    ".mat": "material", ".sha": "shader", ".shc": "shader", ".fnt": "font",
    ".ttf": "font", ".wav": "sound", ".ogg": "sound", ".mp3": "music",
    ".ecl": "entity_class", ".es": "script", ".lua": "script", ".pfx": "particle",
    ".txt": "text", ".des": "text",
}

_ID_UNSAFE = re.compile(r"[^a-z0-9_./-]")
_SOURCE_ID_UNSAFE = re.compile(r"[^a-z0-9_.-]")


def asset_type_for(name: str) -> str:
    return ASSET_TYPES.get(PurePosixPath(name.casefold()).suffix, "other")


def group_key(virtual_path: str) -> str:
    return virtual_path.replace("\\", "/").casefold()


def normalized_id_path(virtual_path: str) -> str:
    """Lowercase, '/'-separated, without a leading ``Content/<game>/`` prefix."""
    parts = [p for p in group_key(virtual_path).split("/") if p]
    if len(parts) >= 3 and parts[0] == "content":
        parts = parts[2:]
    return _ID_UNSAFE.sub("_", "/".join(parts))


def source_id_for(edition: str, relpath: str) -> str:
    return _SOURCE_ID_UNSAFE.sub("_", f"{edition}.{relpath.casefold().replace('/', '_')}")


@dataclass
class SourcePlan:
    relpath: str
    edition: str
    load_priority: int | None
    steam: dict | None


def plan_sources(root: Path, profile: Profile | None, *, all_archives: bool,
                 edition_override: str | None, include_mods: bool, steam: dict | None,
                 warnings: list[str]) -> list[SourcePlan]:
    plans: dict[str, SourcePlan] = {}
    if profile:
        for f in profile.files:
            if f.role != "archive":
                continue
            if not (root / f.relpath).is_file():
                warnings.append(f"listed archive missing: {f.relpath}")
                continue
            ed = edition_override or profile.source_edition(f)
            plans[f.relpath.casefold()] = SourcePlan(f.relpath, ed, f.load_priority, steam)
    if all_archives or not profile:
        rels, _ = scan_files(root, include_mods=include_mods)
        ed = edition_override or default_source_edition(profile.edition if profile else None)
        for rel in rels:
            if rel.casefold().endswith(".gro") and rel.casefold() not in plans:
                plans[rel.casefold()] = SourcePlan(rel, ed, None, steam)
    return sorted(plans.values(), key=lambda s: s.relpath.casefold())


def build_inventory(installs: list[tuple[Path, Profile | None, dict | None]], *,
                    jobs: int = 4, all_archives: bool = False,
                    edition_override: str | None = None, include_mods: bool = False,
                    cache: HashCache | None = None) -> tuple[dict, dict]:
    """Return (inventory document, summary) for one or more installs.

    ``installs`` holds (root, matched profile or None, steam metadata or None).
    """
    t0 = time.perf_counter()
    cache = cache or HashCache()
    warnings: list[str] = []
    jobs_list = []  # (root, plan)
    for root, profile, steam in installs:
        for plan in plan_sources(root, profile, all_archives=all_archives,
                                 edition_override=edition_override,
                                 include_mods=include_mods, steam=steam, warnings=warnings):
            jobs_list.append((root, plan))

    def work(item):
        root, plan = item
        path = root / plan.relpath
        sha = cache.get(path)
        scan = scan_archive(path, head_bytes=HEADER_PROBE_BYTES)  # head: format sniffing
        return root, plan, sha, path.stat().st_size, scan

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        results = list(pool.map(work, jobs_list))

    sources = []
    groups: dict[tuple[str, str], list] = {}
    summary_sources = []
    for root, plan, sha, size, scan in results:
        sid = source_id_for(plan.edition, plan.relpath)
        if any(s["source_id"] == sid for s in sources):
            sid = f"{sid}.{hashlib.sha1(str(root).encode()).hexdigest()[:8]}"
        src = {"source_id": sid, "edition": plan.edition, "relpath": plan.relpath,
               "container": "gro_zip", "sha256": sha, "size_bytes": size}
        if plan.steam and "app_id" in plan.steam:
            src["steam_app_id"] = plan.steam["app_id"]
        if plan.steam and "build_id" in plan.steam:
            src["steam_build_id"] = plan.steam["build_id"]
        if plan.load_priority is not None:
            src["load_priority"] = plan.load_priority
        sources.append(src)
        summary_sources.append(_summarize_scan(sid, plan, scan))
        for e in scan.entries:
            groups.setdefault((plan.edition, group_key(e.name)), []).append((plan, sid, e))

    assets = []
    used_ids: dict[str, str] = {}
    id_collisions = 0
    fallback_order = 0
    unhashed = 0
    for (edition, gkey), members in sorted(groups.items()):
        if any(m[0].load_priority is None for m in members) and len(members) > 1:
            fallback_order += 1
        members.sort(key=lambda m: (m[0].load_priority or 0, m[0].relpath.casefold()))
        plan, sid, e = members[-1]
        if e.sha256 is None:
            unhashed += 1
            continue
        aid = f"{edition}:{normalized_id_path(e.name)}"
        if used_ids.get(aid, gkey) != gkey:
            id_collisions += 1
            aid = f"{aid}-{hashlib.sha1(gkey.encode()).hexdigest()[:8]}"
        used_ids[aid] = gkey
        rec = {"asset_id": aid, "edition": edition, "source_id": sid,
               "virtual_path": e.name, "source_sha256": e.sha256, "size_bytes": e.size,
               "asset_type": asset_type_for(e.name)}
        fmt = sniff_format(e.head)
        if fmt:
            rec["file_format"] = fmt
        if len(members) > 1:
            rec["shadowed_sources"] = [m[1] for m in members[:-1]]
            if any(m[0].load_priority is None for m in members):
                rec["notes"] = ("winner chosen by fallback order (lexicographically last "
                                "archive relpath); engine mount order unconfirmed")
        if rec["asset_type"] == "texture":
            rec["texture"] = {}
            if fmt and fmt.startswith("se1_tex_"):
                try:
                    rec["texture"] = texture_info(parse_classic_tex_header(e.head))
                except TexHeaderError as exc:
                    rec["file_format"] = "unknown"
                    rec["notes"] = f"tex header not parsed: {exc}"
        assets.append(rec)

    doc = {"schema_version": INVENTORY_SCHEMA_VERSION,
           "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "generator": {"name": GENERATOR_NAME, "version": __version__},
           "sources": sources, "assets": assets}
    summary = {
        "sources": summary_sources,
        "assets": len(assets),
        "assets_by_type": dict(sorted(Counter(a["asset_type"] for a in assets).items())),
        "assets_by_edition": dict(sorted(Counter(a["edition"] for a in assets).items())),
        "shadowed_assets": sum(1 for a in assets if "shadowed_sources" in a),
        "shadowed_with_fallback_order": fallback_order,
        "asset_id_collisions": id_collisions,
        "unhashed_entries_dropped": unhashed,
        "classic_tex": _tex_stats(assets),
        "warnings": warnings,
        "bytes_hashed_archives": cache.bytes_hashed,
        "elapsed_s": round(time.perf_counter() - t0, 3),
    }
    return doc, summary


def _summarize_scan(sid: str, plan: SourcePlan, scan: ArchiveScan) -> dict:
    exts = Counter(PurePosixPath(e.name.casefold()).suffix or "<none>" for e in scan.entries)
    return {
        "source_id": sid, "edition": plan.edition,
        "entries": len(scan.entries) + len(scan.unsafe), "directories": scan.directories,
        "uncompressed_bytes": sum(e.size for e in scan.entries),
        "methods": {str(k): v for k, v in sorted(scan.methods.items())},
        "encrypted": scan.encrypted, "unsafe_names": len(scan.unsafe),
        "read_errors": len(scan.errors),
        "by_extension": dict(sorted(exts.items(), key=lambda kv: (-kv[1], kv[0]))),
    }


def _tex_stats(assets: list[dict]) -> dict:
    out: dict = {}
    for a in assets:
        t = a.get("texture") or {}
        if "logical_width" not in t:
            continue
        s = out.setdefault(a["edition"], Counter())
        s["count"] += 1
        s[a["file_format"]] += 1
        s["procedural_effect"] += t["procedural_effect"]
        s["animated"] += t["animated"]
        s["flag_bit0_set"] += bool(t["flags_raw"] & 1)
        s["non_power_of_two"] += not t["is_power_of_two"]
        s["non_square"] += t["width_px"] != t["height_px"]
        mx = max(t["width_px"], t["height_px"])
        s[f"max_dim_{mx}"] += 1
    return {k: dict(sorted(v.items())) for k, v in sorted(out.items())}

