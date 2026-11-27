# SPDX-FileCopyrightText: 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe
# SPDX-License-Identifier: GPL-3.0-only
"""Minimal reader for the header of Classic (Serious Engine 1) ``.tex`` files.

Clean-room implementation (ADR-0006). Sources: the written format description
in SSTRE ``docs/research/RD-002-hd-assets.md`` section 5, plus black-box
observation of header bytes (EXP-007). No engine source code was consulted.

Layout handled here (all integers little-endian int32, no chunk sizes)::

    "TVER" version                     version is 3 or 4
    "TDAT" flags mexW mexH fineMips    common part
      v4:  firstMip frames
      v3:  ctMips firstMip frameSize frames
    <next chunk tag>                   e.g. "FRMS" or "FXDT"

RD-002 section 5 lists the v3 fields as "fineMips, firstMip, [ctMips,
frameSize], frames". Observation (EXP-007) shows that in v3 files the
first-mip value is the *sixth* integer after ``TDAT``; the order above is the
observed one. The naming of the fourth and fifth v3 integers (fine mips vs.
stored mip count) is an assumption.

Only header fields are decoded. Pixel data, animations and effect data are
not read. All input is treated as untrusted.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

HEADER_PROBE_BYTES = 64

MAGIC_TVER = b"TVER"
MAGIC_TDAT = b"TDAT"
MAGIC_CTSEMETA = b"CTSEMETA"
MAGIC_WRKSTRM = b"WRKSTRM1"
CHUNK_FXDT = b"FXDT"

# Sanity bounds for untrusted values. Logical (mex) sizes far above the pixel
# size occur in real files (EXP-007 observed 2**21), so only the int32 range
# is bounded here; pixel size is derived as mex >> first_mip.
_MAX_MEX = 1 << 30
_MAX_SHIFT = 30
_MAX_FRAMES = 1 << 16


class TexHeaderError(ValueError):
    """The bytes are not a well-formed Classic .tex header."""


@dataclass(frozen=True)
class ClassicTexHeader:
    version: int
    flags: int
    mex_width: int
    mex_height: int
    fine_mips: int
    first_mip: int
    frames: int
    next_chunk: bytes
    v3_mip_count: int | None = None
    v3_frame_size: int | None = None

    @property
    def width_px(self) -> int:
        return self.mex_width >> self.first_mip

    @property
    def height_px(self) -> int:
        return self.mex_height >> self.first_mip

    @property
    def procedural_effect(self) -> bool:
        return self.next_chunk == CHUNK_FXDT

    @property
    def file_format(self) -> str:
        return f"se1_tex_v{self.version}"


def sniff_format(head: bytes) -> str | None:
    """Return a coarse container-format id from the first bytes, or None."""
    if head.startswith(MAGIC_TVER) and len(head) >= 8:
        (version,) = struct.unpack_from("<i", head, 4)
        if version in (3, 4):
            return f"se1_tex_v{version}"
        return None
    if head.startswith(MAGIC_CTSEMETA):
        return "se3_meta_bin"
    if head.startswith(MAGIC_WRKSTRM):
        return "se3_wrkstrm"
    return None


def _is_pow2(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


def is_power_of_two(header: ClassicTexHeader) -> bool:
    return _is_pow2(header.width_px) and _is_pow2(header.height_px)


def parse_classic_tex_header(data: bytes) -> ClassicTexHeader:
    """Parse TVER/TDAT from the first bytes of a Classic .tex file."""
    if len(data) < 8 or data[:4] != MAGIC_TVER:
        raise TexHeaderError("missing TVER magic")
    (version,) = struct.unpack_from("<i", data, 4)
    if version not in (3, 4):
        raise TexHeaderError(f"unsupported TVER version {version}")
    if data[8:12] != MAGIC_TDAT:
        raise TexHeaderError("missing TDAT chunk after TVER")
    n_ints = 6 if version == 4 else 8
    end = 12 + 4 * n_ints
    if len(data) < end + 4:
        raise TexHeaderError("truncated TDAT chunk")
    ints = struct.unpack_from(f"<{n_ints}i", data, 12)
    if version == 4:
        flags, mex_w, mex_h, fine, first, frames = ints
        mips = frame_size = None
    else:
        flags, mex_w, mex_h, fine, mips, first, frame_size, frames = ints
        if mips < 0 or frame_size < 0:
            raise TexHeaderError("negative v3 mip count or frame size")
    if not (0 < mex_w <= _MAX_MEX and 0 < mex_h <= _MAX_MEX):
        raise TexHeaderError(f"implausible logical size {mex_w}x{mex_h}")
    if not 0 <= first <= _MAX_SHIFT:
        raise TexHeaderError(f"implausible first mip level {first}")
    if (mex_w >> first) < 1 or (mex_h >> first) < 1:
        raise TexHeaderError("first mip level exceeds logical size")
    if not 0 <= frames <= _MAX_FRAMES:
        raise TexHeaderError(f"implausible frame count {frames}")
    if fine < 0:
        raise TexHeaderError("negative fine mip count")
    return ClassicTexHeader(
        version=version,
        flags=flags & 0xFFFFFFFF,
        mex_width=mex_w,
        mex_height=mex_h,
        fine_mips=fine,
        first_mip=first,
        frames=frames,
        next_chunk=bytes(data[end:end + 4]),
        v3_mip_count=mips,
        v3_frame_size=frame_size,
    )


def texture_info(header: ClassicTexHeader) -> dict:
    """Map a header to an ``asset-inventory`` ``textureInfo`` object."""
    return {
        "width_px": header.width_px,
        "height_px": header.height_px,
        "logical_width": header.mex_width,
        "logical_height": header.mex_height,
        "first_mip_level": header.first_mip,
        "mip_count": header.fine_mips,
        "frames": header.frames,
        "animated": header.frames > 1,
        "procedural_effect": header.procedural_effect,
        "flags_raw": header.flags,
        "is_power_of_two": is_power_of_two(header),
    }
