# SPDX-FileCopyrightText: 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe
# SPDX-License-Identifier: GPL-3.0-only
"""Decoder and encoder for Classic (Serious Engine 1) ``.tex`` files.

Clean-room implementation (ADR-0006). Sources, and only these: the written
format description in SSTRE ``docs/research/RD-002-hd-assets.md`` section 5,
the v3 field-order correction in ``docs/experiments/EXP-007``, and black-box
observation of the bytes of the owner's local files (EXP-012). No engine
source code and no code derived from it was consulted.

Observed layout (little-endian; there are **no chunk sizes**, every length is
implied by earlier fields)::

    "TVER" int32 version                               3 or 4
    "TDAT" int32 flags, mexW, mexH, fineMips           common part
        v4: int32 firstMip, frames
        v3: int32 ctMips, firstMip, frameSize, frames
    either
      "FRMS" pixels                                    bitmap texture
        v4: frames x (W*H*bpp) bytes, mip 0 only, rows top-down,
            bpp = 4 (R,G,B,A) if flags bit 0 else 3 (R,G,B)
        v3: frames x frameSize bytes, ctMips levels each, 16-bit words:
            flags bit 0 clear: RGBA5551 (R 15-11, G 10-6, B 5-1, A bit 0)
            flags bit 0 set:   RGBA4444 (R 15-12, G 11-8, B 7-4, A 3-0)
    or
      "FXDT" ... (procedural effect; kept as opaque bytes)
    "ANIM" "ADAT" int32 count, count x {
        char name[32]              NUL-terminated; trailing bytes are not
                                   zeroed in real files and are preserved
        float32 seconds_per_frame
        int32 n, int32 frame_index[n] }
    optional "BAST" "DFNM" int32 len, char[len]        base texture (effects)

W = mexW >> firstMip, H = mexH >> firstMip. The logical size ("mex") is what
model and brush UVs are authored against; replacing a texture with a higher
pixel density means keeping mex and lowering firstMip.

The decoded model keeps every stored field, so ``encode(decode(b)) == b`` for
every observed file (v4 and v3). v3 16-bit channels are expanded to 8 bits by
bit replication, which the encoder inverts exactly. Input is untrusted.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field, replace

import numpy as np

TAG_TVER = b"TVER"
TAG_TDAT = b"TDAT"
TAG_FRMS = b"FRMS"
TAG_FXDT = b"FXDT"
TAG_ANIM = b"ANIM"
TAG_ADAT = b"ADAT"
TAG_BAST = b"BAST"
TAG_DFNM = b"DFNM"

FLAG_ALPHA = 0x1
ANIM_NAME_BYTES = 32
DEFAULT_SECONDS_PER_FRAME = 0.02  # observed default of single-frame textures

# Bounds for untrusted input. The pixel limit is generous on purpose: whether
# the runtime accepts > 1024 px is an experimental question (EXP-012).
MAX_PIXEL_SIDE = 1 << 14
MAX_FRAMES = 1 << 12
MAX_ANIMS = 1 << 12
MAX_NAME = 1 << 12


class TexCodecError(ValueError):
    """The bytes are not a well-formed Classic .tex file, or a texture model
    cannot be encoded."""


@dataclass
class Animation:
    """One ``ANIM`` record. ``name_raw`` keeps all 32 stored bytes."""

    name_raw: bytes
    seconds_bits: int  # raw IEEE-754 bits of seconds per frame (exact round trip)
    frame_indices: list[int]

    @property
    def name(self) -> str:
        return self.name_raw.split(b"\0", 1)[0].decode("latin-1")

    @property
    def seconds_per_frame(self) -> float:
        return struct.unpack("<f", struct.pack("<I", self.seconds_bits))[0]

    @classmethod
    def make(cls, name: str, seconds_per_frame: float, frame_indices: list[int]) -> "Animation":
        raw = name.encode("latin-1")
        if len(raw) >= ANIM_NAME_BYTES:
            raise TexCodecError("animation name too long")
        bits = struct.unpack("<I", struct.pack("<f", seconds_per_frame))[0]
        return cls(raw.ljust(ANIM_NAME_BYTES, b"\0"), bits, list(frame_indices))


@dataclass
class ClassicTexture:
    """Lossless in-memory model of a Classic ``.tex`` file.

    ``frames`` holds one list of mip levels per frame; each level is an
    ``(H, W, 4)`` uint8 RGBA array. v4 stores mip 0 only (one level per
    frame); v3 stores ``v3_mip_count`` levels. For textures without the
    alpha flag the decoded alpha is 255.
    """

    version: int
    flags: int
    mex_width: int
    mex_height: int
    fine_mips: int
    first_mip: int
    frame_count: int
    frames: list[list[np.ndarray]] = field(default_factory=list)
    animations: list[Animation] = field(default_factory=list)
    effect_data: bytes | None = None  # FXDT payload (procedural effect), opaque
    base_texture: bytes | None = None  # BAST/DFNM string, raw
    v3_mip_count: int | None = None
    v3_frame_size: int | None = None
    trailing: bytes = b""  # unparsed bytes after the last known chunk
    effect_split: bool = True  # False: FXDT payload runs to EOF (no ANIM found)

    # --- derived ------------------------------------------------------------
    @property
    def width(self) -> int:
        return self.mex_width >> self.first_mip

    @property
    def height(self) -> int:
        return self.mex_height >> self.first_mip

    @property
    def has_alpha_channel(self) -> bool:
        return bool(self.flags & FLAG_ALPHA)

    @property
    def procedural_effect(self) -> bool:
        return self.effect_data is not None

    def mip0(self, frame: int = 0) -> np.ndarray:
        return self.frames[frame][0]

    def alpha_mode(self) -> str:
        """``opaque`` / ``cutout`` (alpha only 0 or 255) / ``blend`` / ``none``."""
        if self.procedural_effect or not self.frames:
            return "none"
        if not self.has_alpha_channel:
            return "opaque"
        a = np.concatenate([f[0][..., 3].ravel() for f in self.frames])
        if a.min() == 255:
            return "opaque"
        if np.all((a == 0) | (a == 255)):
            return "cutout"
        return "blend"


# --- helpers ---------------------------------------------------------------

class _Reader:
    def __init__(self, data: bytes):
        self.b = memoryview(data)
        self.o = 0

    def need(self, n: int) -> None:
        if n < 0 or self.o + n > len(self.b):
            raise TexCodecError(f"truncated at offset {self.o} (need {n} bytes)")

    def tag(self) -> bytes:
        self.need(4)
        t = bytes(self.b[self.o:self.o + 4])
        self.o += 4
        return t

    def peek(self, n: int = 4) -> bytes:
        return bytes(self.b[self.o:self.o + n])

    def ints(self, n: int, fmt: str = "i") -> tuple:
        self.need(4 * n)
        v = struct.unpack_from(f"<{n}{fmt}", self.b, self.o)
        self.o += 4 * n
        return v

    def raw(self, n: int) -> bytes:
        self.need(n)
        v = bytes(self.b[self.o:self.o + n])
        self.o += n
        return v

    @property
    def left(self) -> int:
        return len(self.b) - self.o


def mip_sizes(w: int, h: int, count: int) -> list[tuple[int, int]]:
    out = []
    for i in range(count):
        out.append((max(1, w >> i), max(1, h >> i)))
    return out


def full_mip_count(w: int, h: int) -> int:
    """Number of levels until the smaller side reaches one pixel (observed
    value of ``fineMips`` in 84% of v4 files)."""
    return min(w, h).bit_length()


def _expand(v: np.ndarray, bits: int) -> np.ndarray:
    # bit replication: exact inverse is ``x >> (8 - bits)``
    v = v.astype(np.uint16)
    out = v << (8 - bits)
    sh = 8 - bits
    while sh > 0:
        sh -= bits
        out |= (v << sh) if sh >= 0 else (v >> -sh)
    return out.astype(np.uint8)


def _unpack16(words: np.ndarray, alpha4444: bool) -> np.ndarray:
    w = words.astype(np.uint32)
    if alpha4444:
        ch = [(w >> 12) & 15, (w >> 8) & 15, (w >> 4) & 15, w & 15]
        return np.stack([_expand(c, 4) for c in ch], -1)
    ch = [(w >> 11) & 31, (w >> 6) & 31, (w >> 1) & 31]
    rgb = [_expand(c, 5) for c in ch]
    a = np.where((w & 1) != 0, 255, 0).astype(np.uint8)
    return np.stack(rgb + [a], -1)


def _pack16(rgba: np.ndarray, alpha4444: bool) -> np.ndarray:
    p = rgba.astype(np.uint16)
    if alpha4444:
        return ((p[..., 0] >> 4) << 12 | (p[..., 1] >> 4) << 8 | (p[..., 2] >> 4) << 4
                | (p[..., 3] >> 4)).astype("<u2")
    return ((p[..., 0] >> 3) << 11 | (p[..., 1] >> 3) << 6 | (p[..., 2] >> 3) << 1
            | (p[..., 3] >> 7)).astype("<u2")


# --- decode ----------------------------------------------------------------

def decode(data: bytes) -> ClassicTexture:
    """Decode a complete Classic ``.tex`` file."""
    r = _Reader(data)
    if r.tag() != TAG_TVER:
        raise TexCodecError("missing TVER magic")
    (version,) = r.ints(1)
    if version not in (3, 4):
        raise TexCodecError(f"unsupported TVER version {version}")
    if r.tag() != TAG_TDAT:
        raise TexCodecError("missing TDAT chunk")
    if version == 4:
        flags, mw, mh, fine, first, nfr = r.ints(6)
        v3_mips = v3_fs = None
    else:
        flags, mw, mh, fine, v3_mips, first, v3_fs, nfr = r.ints(8)
    if not (0 < mw < 1 << 31 and 0 < mh < 1 << 31) or not 0 <= first <= 30:
        raise TexCodecError(f"implausible logical size {mw}x{mh} / first mip {first}")
    w, h = mw >> first, mh >> first
    if not (1 <= w <= MAX_PIXEL_SIDE and 1 <= h <= MAX_PIXEL_SIDE):
        raise TexCodecError(f"implausible pixel size {w}x{h}")
    if not 0 <= nfr <= MAX_FRAMES:
        raise TexCodecError(f"implausible frame count {nfr}")
    tex = ClassicTexture(version=version, flags=flags & 0xFFFFFFFF, mex_width=mw,
                         mex_height=mh, fine_mips=fine, first_mip=first, frame_count=nfr,
                         v3_mip_count=v3_mips, v3_frame_size=v3_fs)
    tag = r.tag()
    if tag == TAG_FRMS:
        _decode_frames(r, tex, w, h)
        _decode_anim(r, tex)
        if r.left >= 8 and r.peek(4) == TAG_BAST:
            _decode_bast(r, tex)
        tex.trailing = r.raw(r.left)
    elif tag == TAG_FXDT:
        _decode_effect(r, tex)
    else:
        raise TexCodecError(f"unknown chunk {tag!r} after TDAT")
    return tex


def _decode_frames(r: _Reader, tex: ClassicTexture, w: int, h: int) -> None:
    if tex.version == 4:
        bpp = 4 if tex.has_alpha_channel else 3
        n = w * h * bpp
        for _ in range(tex.frame_count):
            a = np.frombuffer(r.raw(n), np.uint8).reshape(h, w, bpp)
            if bpp == 3:
                a = np.concatenate([a, np.full((h, w, 1), 255, np.uint8)], -1)
            tex.frames.append([np.ascontiguousarray(a)])
        return
    mips = tex.v3_mip_count or 0
    sizes = mip_sizes(w, h, mips)
    need = 2 * sum(a * b for a, b in sizes)
    if mips < 1 or tex.v3_frame_size != need:
        raise TexCodecError(f"v3 frame size {tex.v3_frame_size} != {need} for {mips} mips")
    a4 = tex.has_alpha_channel
    for _ in range(tex.frame_count):
        levels = []
        for lw, lh in sizes:
            words = np.frombuffer(r.raw(2 * lw * lh), "<u2").reshape(lh, lw)
            levels.append(_unpack16(words, a4))
        tex.frames.append(levels)


def _read_anims(r: _Reader) -> list[Animation]:
    if r.tag() != TAG_ANIM or r.tag() != TAG_ADAT:
        raise TexCodecError("missing ANIM/ADAT chunk")
    (count,) = r.ints(1)
    if not 0 <= count <= MAX_ANIMS:
        raise TexCodecError(f"implausible animation count {count}")
    anims = []
    for _ in range(count):
        name = r.raw(ANIM_NAME_BYTES)
        (bits,) = r.ints(1, "I")
        (n,) = r.ints(1)
        if not 0 <= n <= MAX_FRAMES * 16:
            raise TexCodecError(f"implausible animation length {n}")
        idx = list(r.ints(n)) if n else []
        anims.append(Animation(name, bits, idx))
    return anims


def _decode_anim(r: _Reader, tex: ClassicTexture) -> None:
    tex.animations = _read_anims(r)


def _read_bast(r: _Reader) -> bytes:
    r.tag()
    if r.tag() != TAG_DFNM:
        raise TexCodecError("BAST without DFNM")
    (n,) = r.ints(1)
    if not 0 <= n <= MAX_NAME:
        raise TexCodecError("implausible base texture name length")
    return r.raw(n)


def _decode_bast(r: _Reader, tex: ClassicTexture) -> None:
    tex.base_texture = _read_bast(r)


def _decode_effect(r: _Reader, tex: ClassicTexture) -> None:
    """Procedural effect textures: the FXDT payload is not interpreted. The
    payload ends where an ``ANIM`` chunk starts that parses to the end of the
    file (optionally followed by ``BAST``); if no such split exists the whole
    rest is kept opaque (``effect_split = False``)."""
    start = r.o
    data = bytes(r.b)
    pos = len(data)
    while True:
        pos = data.rfind(TAG_ANIM + TAG_ADAT, start, pos)
        if pos < 0:
            break
        sub = _Reader(data)
        sub.o = pos
        try:
            anims = _read_anims(sub)
            bast = _read_bast(sub) if sub.left >= 8 and sub.peek(4) == TAG_BAST else None
        except TexCodecError:
            continue
        if sub.left == 0:
            tex.effect_data, tex.animations, tex.base_texture = data[start:pos], anims, bast
            r.o = len(data)
            return
    tex.effect_data = data[start:]
    tex.effect_split = False
    r.o = len(data)


# --- encode ----------------------------------------------------------------

def encode(tex: ClassicTexture, version: int | None = None) -> bytes:
    """Serialize ``tex``. ``version`` defaults to ``tex.version``.

    Encoding a v3 model as v4 keeps mip 0 only and stores 8-bit channels; all
    other fields are carried over.
    """
    version = tex.version if version is None else version
    if version not in (3, 4):
        raise TexCodecError("only versions 3 and 4 can be written")
    out = bytearray(TAG_TVER + struct.pack("<i", version) + TAG_TDAT)
    w, h = tex.width, tex.height
    if w < 1 or h < 1:
        raise TexCodecError("first mip exceeds logical size")
    if tex.effect_data is None and len(tex.frames) != tex.frame_count:
        raise TexCodecError(f"frame count {tex.frame_count} != {len(tex.frames)} frame arrays")
    if version == 4:
        out += struct.pack("<6i", _i32(tex.flags), tex.mex_width, tex.mex_height,
                           tex.fine_mips, tex.first_mip, tex.frame_count)
    else:
        mips = tex.v3_mip_count or (len(tex.frames[0]) if tex.frames else 1)
        fs = tex.v3_frame_size
        if tex.effect_data is None:
            fs = 2 * sum(a * b for a, b in mip_sizes(w, h, mips))
        out += struct.pack("<8i", _i32(tex.flags), tex.mex_width, tex.mex_height,
                           tex.fine_mips, mips, tex.first_mip, fs or 0, tex.frame_count)
    if tex.effect_data is not None:
        out += TAG_FXDT + tex.effect_data
        if tex.effect_split:
            out += _anim_bytes(tex)
            out += _bast_bytes(tex)
        return bytes(out)
    out += TAG_FRMS
    for levels in tex.frames:
        if version == 4:
            a = _check_level(levels[0], w, h)
            out += (a if tex.has_alpha_channel else a[..., :3]).tobytes()
        else:
            mips = tex.v3_mip_count or len(levels)
            if len(levels) < mips:
                levels = list(levels) + generate_mips(levels[-1], mips - len(levels))[1:]
            for (lw, lh), lvl in zip(mip_sizes(w, h, mips), levels):
                out += _pack16(_check_level(lvl, lw, lh), tex.has_alpha_channel).tobytes()
    out += _anim_bytes(tex)
    out += _bast_bytes(tex)
    out += tex.trailing
    return bytes(out)


def _i32(v: int) -> int:
    return v - (1 << 32) if v >= 1 << 31 else v


def _check_level(a: np.ndarray, w: int, h: int) -> np.ndarray:
    if a.dtype != np.uint8 or a.shape != (h, w, 4):
        raise TexCodecError(f"level must be uint8 ({h}, {w}, 4), got {a.dtype} {a.shape}")
    return np.ascontiguousarray(a)


def _anim_bytes(tex: ClassicTexture) -> bytes:
    anims = tex.animations or [Animation.make("OnlyAnim", DEFAULT_SECONDS_PER_FRAME, [0])]
    b = bytearray(TAG_ANIM + TAG_ADAT + struct.pack("<i", len(anims)))
    for an in anims:
        if len(an.name_raw) != ANIM_NAME_BYTES:
            raise TexCodecError("animation name field must be 32 bytes")
        for i in an.frame_indices:
            if not 0 <= i < max(1, tex.frame_count):
                raise TexCodecError(f"animation frame index {i} out of range")
        b += an.name_raw + struct.pack("<Ii", an.seconds_bits, len(an.frame_indices))
        b += struct.pack(f"<{len(an.frame_indices)}i", *an.frame_indices)
    return bytes(b)


def _bast_bytes(tex: ClassicTexture) -> bytes:
    if tex.base_texture is None:
        return b""
    return TAG_BAST + TAG_DFNM + struct.pack("<i", len(tex.base_texture)) + tex.base_texture


# --- construction helpers ----------------------------------------------------

def generate_mips(level0: np.ndarray, count: int) -> list[np.ndarray]:
    """Box-filtered mip chain (used only when writing v3 from fewer levels)."""
    out = [level0]
    for _ in range(count - 1):
        a = out[-1].astype(np.uint32)
        h, w = a.shape[:2]
        if h > 1:
            a = (a[0:h - h % 2:2] + a[1:h:2]) // 2 if h >= 2 else a
        if w > 1:
            a = (a[:, 0:w - w % 2:2] + a[:, 1:w:2]) // 2 if w >= 2 else a
        out.append(a.astype(np.uint8))
    return out


def _log2_exact(n: int) -> int:
    if n < 1 or n & (n - 1):
        raise TexCodecError(f"{n} is not a power of two")
    return n.bit_length() - 1


def from_rgba(frames: list[np.ndarray], *, mex_width: int | None = None,
              mex_height: int | None = None, animations: list[Animation] | None = None,
              alpha: bool | None = None, fine_mips: int | None = None,
              extra_flags: int = 0) -> ClassicTexture:
    """Build a v4 texture from RGBA frames (uint8 ``(H, W, 4)``).

    ``mex_*`` default to the pixel size. ``alpha`` defaults to "any alpha
    below 255". Dimensions must be powers of two (Classic observation: 0 NPOT
    files), and mex must be pixel size times a power of two.
    """
    if not frames:
        raise TexCodecError("at least one frame is required")
    h, w = frames[0].shape[:2]
    for f in frames:
        _check_level(f, w, h)
    mex_width = w if mex_width is None else mex_width
    mex_height = h if mex_height is None else mex_height
    _log2_exact(w), _log2_exact(h)
    if mex_width % w or mex_height % h or mex_width // w != mex_height // h:
        raise TexCodecError("mex must be the pixel size times one common power of two")
    first = _log2_exact(mex_width // w)
    if alpha is None:
        alpha = any(int(f[..., 3].min()) < 255 for f in frames)
    flags = (extra_flags & ~FLAG_ALPHA) | (FLAG_ALPHA if alpha else 0)
    return ClassicTexture(
        version=4, flags=flags, mex_width=mex_width, mex_height=mex_height,
        fine_mips=full_mip_count(w, h) if fine_mips is None else fine_mips,
        first_mip=first, frame_count=len(frames), frames=[[f] for f in frames],
        animations=list(animations) if animations else [])


def make_replacement(original: ClassicTexture, frames: list[np.ndarray]) -> ClassicTexture:
    """A v4 texture with the *same logical size (mex)*, flags and animations
    as ``original`` but new pixels, e.g. at 2x or 4x pixel density.

    ``firstMip`` drops by log2(scale). ``fineMips`` keeps its distance to the
    full mip count ([ASM] semantics of the field are not observed; in 84% of
    files it equals the full count, which this preserves).
    """
    if original.procedural_effect:
        raise TexCodecError("procedural effect textures have no pixels to replace")
    if len(frames) != original.frame_count:
        raise TexCodecError("replacement must have the same number of frames")
    h, w = frames[0].shape[:2]
    if w * original.height != h * original.width:
        raise TexCodecError("replacement must keep the aspect ratio")
    if w < original.width:
        raise TexCodecError("replacement must not have fewer pixels")
    k = _log2_exact(w // original.width) if w % original.width == 0 else None
    if k is None or (original.width << k) != w:
        raise TexCodecError("scale must be a power of two")
    if original.first_mip - k < 0:
        raise TexCodecError(
            f"scale 2^{k} exceeds the logical size (firstMip {original.first_mip}); "
            "mex cannot stay identical")
    deficit = full_mip_count(original.width, original.height) - original.fine_mips
    new = from_rgba(frames, mex_width=original.mex_width, mex_height=original.mex_height,
                    animations=original.animations, alpha=original.has_alpha_channel,
                    fine_mips=max(0, full_mip_count(w, h) - deficit),
                    extra_flags=original.flags)
    return replace(new, base_texture=original.base_texture)


def scale_factor(original: ClassicTexture, new: ClassicTexture) -> float:
    return math.sqrt((new.width * new.height) / (original.width * original.height))
