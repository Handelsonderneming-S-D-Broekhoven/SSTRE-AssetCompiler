# SPDX-FileCopyrightText: 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe
# SPDX-License-Identifier: GPL-3.0-only
"""PNG import/export and metadata sidecars for decoded Classic textures.

Requires Pillow (``pip install sstre-assetcompiler[image]``). Everything a PNG
cannot express (logical size, flags, animations, effect data) goes into the
JSON sidecar so a texture can be rebuilt with :func:`texture_from_export`.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import numpy as np

from . import classic_tex as ct

SIDECAR_VERSION = "0.1.0"


def _pil():
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - exercised only without Pillow
        raise RuntimeError("Pillow is required: pip install 'sstre-assetcompiler[image]'") from exc
    return Image


def save_png(rgba: np.ndarray, path: Path, *, keep_alpha: bool = True) -> None:
    Image = _pil()
    path.parent.mkdir(parents=True, exist_ok=True)
    if keep_alpha:
        Image.fromarray(rgba, "RGBA").save(path, optimize=False, compress_level=6)
    else:
        Image.fromarray(np.ascontiguousarray(rgba[..., :3]), "RGB").save(path, compress_level=6)


def load_png(path: Path) -> np.ndarray:
    Image = _pil()
    with Image.open(path) as im:
        return np.asarray(im.convert("RGBA")).copy()


def metadata(tex: ct.ClassicTexture) -> dict:
    """Format-level metadata (no game-specific fields)."""
    md = {
        "format": f"se1_tex_v{tex.version}",
        "width_px": tex.width,
        "height_px": tex.height,
        "mex_width": tex.mex_width,
        "mex_height": tex.mex_height,
        "first_mip": tex.first_mip,
        "fine_mips": tex.fine_mips,
        "stored_mip_levels": len(tex.frames[0]) if tex.frames else 0,
        "flags_raw": tex.flags,
        "has_alpha_channel": tex.has_alpha_channel,
        "alpha_mode": tex.alpha_mode(),
        "frames": tex.frame_count,
        "procedural_effect": tex.procedural_effect,
        "animations": [
            {"name": a.name, "name_raw_b64": base64.b64encode(a.name_raw).decode(),
             "seconds_per_frame": round(a.seconds_per_frame, 7),
             "seconds_bits": a.seconds_bits, "frame_indices": a.frame_indices}
            for a in tex.animations],
    }
    if tex.version == 3:
        md["v3_mip_count"] = tex.v3_mip_count
        md["v3_frame_size"] = tex.v3_frame_size
        md["v3_pixel_format"] = "rgba4444" if tex.has_alpha_channel else "rgba5551"
    if tex.base_texture is not None:
        md["base_texture"] = tex.base_texture.decode("latin-1")
    return md


def export(tex: ct.ClassicTexture, out_dir: Path, stem: str) -> dict:
    """Write ``<stem>.png`` (frame 0, mip 0) and ``<stem>.fNNN.png`` for
    further frames; returns the per-file list with SHA-256 of each PNG."""
    files = []
    for i in range(tex.frame_count):
        name = f"{stem}.png" if i == 0 else f"{stem}.f{i:03d}.png"
        p = out_dir / name
        save_png(tex.mip0(i), p, keep_alpha=True)
        files.append({"frame": i, "file": name,
                      "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
    return {"images": files}


def texture_from_export(meta: dict, frames: list[np.ndarray]) -> ct.ClassicTexture:
    """Rebuild a v4 texture from exported frames and sidecar metadata."""
    anims = [ct.Animation(base64.b64decode(a["name_raw_b64"]), a["seconds_bits"],
                          a["frame_indices"]) for a in meta.get("animations", [])]
    return ct.ClassicTexture(
        version=4, flags=meta["flags_raw"], mex_width=meta["mex_width"],
        mex_height=meta["mex_height"], fine_mips=meta["fine_mips"],
        first_mip=meta["first_mip"], frame_count=len(frames), frames=[[f] for f in frames],
        animations=anims,
        base_texture=meta["base_texture"].encode("latin-1") if "base_texture" in meta else None)
