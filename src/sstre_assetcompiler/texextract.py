# SPDX-FileCopyrightText: 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe
# SPDX-License-Identifier: GPL-3.0-only
"""Extract selected Classic textures from a local install to PNG + JSON.

The selection (which virtual paths, which category) is supplied by the user
in a local file; this module contains no game data. Output must stay in a
local workspace (ADR-0002, GATE-05).

Selection file::

    {"set_id": "set1", "edition": "classic_tfe",
     "items": [{"virtual_path": "Textures/.../X.tex", "category": "architecture",
                "notes": "optional"}]}
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import zipfile
from pathlib import Path, PurePosixPath

from . import GENERATOR_NAME, __version__
from . import classic_tex as ct
from .archives import unsafe_reason
from .inventory import normalized_id_path

EXTRACT_RECORD_VERSION = "0.1.0"
_SLUG = re.compile(r"[^a-z0-9]+")


def archive_index(root: Path, *, include_mods: bool = False) -> dict[str, tuple[Path, str]]:
    """Map casefolded virtual path -> (archive, entry name) for all top-level
    ``.gro`` archives; the lexicographically last archive wins (same
    [ASM] override rule as the inventory, EXP-007)."""
    archives = sorted(root.glob("*.gro"), key=lambda p: p.name.casefold())
    if include_mods:
        archives += sorted(root.glob("Mods/*/*.gro"))
    idx: dict[str, tuple[Path, str]] = {}
    for a in archives:
        with zipfile.ZipFile(a) as zf:
            for info in zf.infolist():
                if info.is_dir() or unsafe_reason(info.filename):
                    continue
                idx[info.filename.replace("\\", "/").casefold()] = (a, info.filename)
    return idx


def slug_for(asset_id: str) -> str:
    path = asset_id.split(":", 1)[-1]
    stem = PurePosixPath(path).with_suffix("").as_posix()
    s = _SLUG.sub("_", stem.casefold()).strip("_")
    h = hashlib.sha1(asset_id.encode()).hexdigest()[:6]
    return f"{s[-60:].strip('_')}_{h}"


def extract_one(data: bytes, *, edition: str, archive: Path, root: Path, virtual_path: str,
                category: str, out_dir: Path, notes: str | None = None,
                extra: dict | None = None) -> dict:
    from . import texio  # Pillow needed only here
    tex = ct.decode(data)
    asset_id = f"{edition}:{normalized_id_path(virtual_path)}"
    slug = slug_for(asset_id)
    rec = {
        "record_version": EXTRACT_RECORD_VERSION,
        "asset_id": asset_id,
        "category": category,
        "edition": edition,
        "source_archive": archive.relative_to(root).as_posix(),
        "virtual_path": virtual_path,
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "source_size_bytes": len(data),
        "texture": texio.metadata(tex),
        "mip_info": {
            "stored_levels": len(tex.frames[0]) if tex.frames else 0,
            "runtime_generates_mips": tex.version == 4,
            "full_chain_levels": ct.full_mip_count(tex.width, tex.height),
            "fine_mips": tex.fine_mips,
        },
        "decoder": {"name": GENERATOR_NAME, "version": __version__,
                    "module": "classic_tex", "lossless": True,
                    "reencode_byte_identical": ct.encode(tex) == data},
        "local_only": True,
    }
    if notes:
        rec["notes"] = notes
    if extra:
        rec["extra"] = extra
    if tex.procedural_effect:
        rec["images"] = []
    else:
        rec.update(texio.export(tex, out_dir, slug))
        if tex.has_alpha_channel:
            from PIL import Image
            ap = out_dir / f"{slug}.alpha.png"
            Image.fromarray(tex.mip0()[..., 3], "L").save(ap)
            rec["alpha_image"] = {"file": ap.name,
                                  "sha256": hashlib.sha256(ap.read_bytes()).hexdigest()}
    rec["record_file"] = f"{slug}.json"
    (out_dir / rec["record_file"]).write_text(json.dumps(rec, indent=1) + "\n", encoding="utf-8")
    return rec


def extract_set(root: Path, selection: dict, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    idx = archive_index(root)
    edition = selection["edition"]
    done, missing = [], []
    for item in selection["items"]:
        hit = idx.get(item["virtual_path"].replace("\\", "/").casefold())
        if hit is None:
            missing.append(item["virtual_path"])
            continue
        archive, name = hit
        with zipfile.ZipFile(archive) as zf:
            data = zf.read(name)
        rec = extract_one(data, edition=edition, archive=archive, root=root, virtual_path=name,
                          category=item["category"], out_dir=out_dir, notes=item.get("notes"),
                          extra=item.get("extra"))
        done.append(rec)
    ready = {
        "set_id": selection.get("set_id", out_dir.name),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "generator": {"name": GENERATOR_NAME, "version": __version__},
        "local_only": True,
        "count": len(done),
        "missing": missing,
        "textures": [{"asset_id": r["asset_id"], "category": r["category"],
                      "record": r["record_file"],
                      "images": [i["file"] for i in r.get("images", [])],
                      "width_px": r["texture"]["width_px"],
                      "height_px": r["texture"]["height_px"],
                      "frames": r["texture"]["frames"],
                      "alpha_mode": r["texture"]["alpha_mode"]} for r in done],
    }
    (out_dir / "READY.json").write_text(json.dumps(ready, indent=1) + "\n", encoding="utf-8")
    return ready


def roundtrip_counts(roots: list[Path]) -> dict:
    """Decode and re-encode every ``.tex`` in the top-level archives of each
    root. Returns counts only (no paths), suitable for reports."""
    import numpy as np
    out: dict[str, dict[str, int]] = {}
    for root in roots:
        c: dict[str, int] = {}

        def inc(k: str) -> None:
            c[k] = c.get(k, 0) + 1
        for a in sorted(root.glob("*.gro"), key=lambda p: p.name.casefold()):
            with zipfile.ZipFile(a) as zf:
                for info in zf.infolist():
                    if not info.filename.casefold().endswith(".tex"):
                        continue
                    data = zf.read(info)
                    inc("files")
                    try:
                        tex = ct.decode(data)
                    except ct.TexCodecError:
                        inc("decode_failed")
                        continue
                    kind = "effect" if tex.procedural_effect else f"v{tex.version}"
                    same = ct.encode(tex) == data
                    inc(f"{kind}_{'byte_identical' if same else 'different'}")
                    again = ct.decode(ct.encode(tex))
                    ok = all(np.array_equal(x, y) for fx, fy in zip(tex.frames, again.frames)
                             for x, y in zip(fx, fy))
                    inc("model_cycle_equal" if ok else "model_cycle_different")
        out[root.name] = dict(sorted(c.items()))
    return out
