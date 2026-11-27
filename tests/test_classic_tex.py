# SPDX-FileCopyrightText: 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe
# SPDX-License-Identifier: GPL-3.0-only
"""Synthetic tests for the Classic .tex codec. All inputs are generated here."""

from __future__ import annotations

import struct

import pytest

np = pytest.importorskip("numpy")

from sstre_assetcompiler import classic_tex as ct  # noqa: E402


def _pattern(h, w, alpha=True, seed=0):
    rng = np.random.default_rng(seed)
    a = rng.integers(0, 256, (h, w, 4), dtype=np.uint8)
    if not alpha:
        a[..., 3] = 255
    return a


def _anim_bytes(entries):
    b = b"ANIMADAT" + struct.pack("<i", len(entries))
    for name, spf, idx in entries:
        b += name.ljust(32, b"\0") + struct.pack("<fi", spf, len(idx))
        b += struct.pack(f"<{len(idx)}i", *idx)
    return b


def _v4_bytes(px, flags, mex_w, mex_h, first, fine, frames=1, anims=None):
    h, w = px[0].shape[:2]
    body = b"".join((f if flags & 1 else f[..., :3]).tobytes() for f in px)
    return (b"TVER" + struct.pack("<i", 4) + b"TDAT"
            + struct.pack("<6i", flags, mex_w, mex_h, fine, first, frames)
            + b"FRMS" + body
            + _anim_bytes(anims or [(b"OnlyAnim\0garbage", 0.02, [0])]))


def test_v4_rgb_decode_and_byte_identical_reencode():
    px = _pattern(8, 16, alpha=False)
    raw = _v4_bytes([px], 0, 64, 32, 2, 4)
    tex = ct.decode(raw)
    assert (tex.version, tex.width, tex.height, tex.mex_width, tex.first_mip) == (4, 16, 8, 64, 2)
    assert not tex.has_alpha_channel and tex.alpha_mode() == "opaque"
    assert np.array_equal(tex.mip0(), px)
    assert tex.animations[0].name == "OnlyAnim"
    assert tex.animations[0].name_raw.startswith(b"OnlyAnim\0garbage")  # junk preserved
    assert ct.encode(tex) == raw


def test_v4_rgba_multiframe_animation():
    frames = [_pattern(4, 4, seed=i) for i in range(3)]
    raw = _v4_bytes(frames, 1 | 2, 4, 4, 0, 3, frames=3,
                    anims=[(b"A", 0.04, [0, 1, 2]), (b"B", 0.5, [2, 2, 0])])
    tex = ct.decode(raw)
    assert tex.frame_count == 3 and tex.flags == 3
    assert [a.name for a in tex.animations] == ["A", "B"]
    assert tex.animations[1].frame_indices == [2, 2, 0]
    assert abs(tex.animations[0].seconds_per_frame - 0.04) < 1e-7
    for i in range(3):
        assert np.array_equal(tex.mip0(i), frames[i])
    assert tex.alpha_mode() == "blend"
    assert ct.encode(tex) == raw


def test_decode_encode_cycle_on_constructed_model():
    f = _pattern(32, 16)
    f[..., 3] = np.where(f[..., 3] > 127, 255, 0)
    tex = ct.from_rgba([f], mex_width=64, mex_height=128)
    assert tex.first_mip == 2 and tex.has_alpha_channel and tex.alpha_mode() == "cutout"
    again = ct.decode(ct.encode(tex))
    assert np.array_equal(again.mip0(), f)
    assert (again.mex_width, again.mex_height, again.fine_mips) == (64, 128, 5)
    assert ct.encode(again) == ct.encode(tex)


def _v3_bytes(levels_words, flags, mex, first, fine):
    mips = len(levels_words)
    fs = sum(w.size for w in levels_words) * 2
    return (b"TVER" + struct.pack("<i", 3) + b"TDAT"
            + struct.pack("<8i", flags, mex, mex, fine, mips, first, fs, 1)
            + b"FRMS" + b"".join(w.astype("<u2").tobytes() for w in levels_words)
            + _anim_bytes([(b"OnlyAnim", 0.02, [0])]))


@pytest.mark.parametrize("flags", [0, 1])
def test_v3_16bit_formats_round_trip(flags):
    rng = np.random.default_rng(flags)
    words = [rng.integers(0, 1 << 16, (s, s), dtype=np.uint32) for s in (8, 4, 2, 1)]
    if flags == 0:
        words = [w | 1 for w in words]  # opaque 5551
    raw = _v3_bytes(words, flags, 32, 2, 4)
    tex = ct.decode(raw)
    assert tex.version == 3 and len(tex.frames[0]) == 4 and tex.v3_mip_count == 4
    assert ct.encode(tex) == raw
    # v3 -> v4 keeps mip 0 pixels exactly (8-bit expansion)
    v4 = ct.decode(ct.encode(tex, version=4))
    assert v4.version == 4 and np.array_equal(v4.mip0(), tex.mip0())


def test_v3_channel_layout():
    # RGBA4444 word 0xF84C -> R=F G=8 B=4 A=C
    raw = _v3_bytes([np.array([[0xF84C]], np.uint32)], 1, 1, 0, 1)
    assert ct.decode(raw).mip0()[0, 0].tolist() == [0xFF, 0x88, 0x44, 0xCC]
    # RGBA5551: R=31 G=0 B=16 A=1 -> 0b11111_00000_10000_1
    w = (31 << 11) | (0 << 6) | (16 << 1) | 1
    raw = _v3_bytes([np.array([[w]], np.uint32)], 0, 1, 0, 1)
    assert ct.decode(raw).mip0()[0, 0].tolist() == [255, 0, 132, 255]


def test_effect_texture_kept_opaque_with_anim_and_base():
    payload = b"\x02\x00\x00\x00" + bytes(range(40)) + b"ANIMADAT-not-really"
    raw = (b"TVER" + struct.pack("<i", 4) + b"TDAT" + struct.pack("<6i", 1, 512, 512, 0, 4, 1)
           + b"FXDT" + payload + _anim_bytes([(b"OnlyAnim", 0.02, [0])])
           + b"BASTDFNM" + struct.pack("<i", 9) + b"Base\\x.tx")
    tex = ct.decode(raw)
    assert tex.procedural_effect and tex.effect_split
    assert tex.effect_data == payload
    assert tex.base_texture == b"Base\\x.tx"
    assert tex.alpha_mode() == "none"
    assert ct.encode(tex) == raw


def test_replacement_keeps_mex_and_lowers_first_mip():
    orig = ct.decode(_v4_bytes([_pattern(16, 8, alpha=False)], 0, 64, 128, 3, 3))
    assert orig.fine_mips == 3 and ct.full_mip_count(8, 16) == 4
    for k in (2, 4):
        hi = _pattern(16 * k, 8 * k, alpha=False, seed=k)
        rep = ct.make_replacement(orig, [hi])
        assert (rep.mex_width, rep.mex_height) == (64, 128)
        assert rep.first_mip == 3 - k.bit_length() + 1
        assert (rep.width, rep.height) == (8 * k, 16 * k)
        assert rep.fine_mips == ct.full_mip_count(8 * k, 16 * k) - 1  # deficit kept
        assert np.array_equal(ct.decode(ct.encode(rep)).mip0(), hi)


def test_replacement_rejections():
    orig = ct.decode(_v4_bytes([_pattern(8, 8, alpha=False)], 0, 16, 16, 1, 4))
    with pytest.raises(ct.TexCodecError):
        ct.make_replacement(orig, [_pattern(32, 32)])  # 4x needs firstMip >= 2
    with pytest.raises(ct.TexCodecError):
        ct.make_replacement(orig, [_pattern(24, 24)])  # not a power of two
    with pytest.raises(ct.TexCodecError):
        ct.make_replacement(orig, [_pattern(16, 8)])  # aspect change


@pytest.mark.parametrize("raw", [
    b"", b"TVER", b"TVER\x05\x00\x00\x00TDAT",
    b"TVER\x04\x00\x00\x00TDAT" + struct.pack("<6i", 0, 4, 4, 3, 0, 1) + b"FRMS" + bytes(10),
    b"TVER\x04\x00\x00\x00TDAT" + struct.pack("<6i", 0, 4, 4, 3, 0, 1) + b"XXXX",
    b"TVER\x04\x00\x00\x00TDAT" + struct.pack("<6i", 0, 4, 4, 3, 5, 1) + b"FRMS",
    b"TVER\x04\x00\x00\x00TDAT" + struct.pack("<6i", 0, 4, 4, 3, 0, -1) + b"FRMS",
])
def test_malformed_inputs_raise(raw):
    with pytest.raises(ct.TexCodecError):
        ct.decode(raw)


def test_v3_inconsistent_frame_size_rejected():
    raw = bytearray(_v3_bytes([np.zeros((2, 2), np.uint32)], 0, 2, 0, 2))
    struct.pack_into("<i", raw, 12 + 4 * 6, 999)
    with pytest.raises(ct.TexCodecError):
        ct.decode(bytes(raw))


def test_png_export_and_rebuild(tmp_path):
    pytest.importorskip("PIL")
    from sstre_assetcompiler import texio
    frames = [_pattern(4, 8, seed=i) for i in range(2)]
    tex = ct.from_rgba(frames, mex_width=32, mex_height=16,
                       animations=[ct.Animation.make("Loop", 0.1, [0, 1])])
    files = texio.export(tex, tmp_path, "t")["images"]
    assert [f["file"] for f in files] == ["t.png", "t.f001.png"]
    meta = texio.metadata(tex)
    back = texio.texture_from_export(meta, [texio.load_png(tmp_path / f["file"]) for f in files])
    assert ct.encode(back) == ct.encode(tex)


def test_cli_tex_commands(tmp_path, capsys):
    pytest.importorskip("PIL")
    from sstre_assetcompiler import texio
    from sstre_assetcompiler.cli import main
    src = tmp_path / "t.tex"
    raw = _v4_bytes([_pattern(8, 8, alpha=False)], 0, 32, 32, 2, 4)
    src.write_bytes(raw)
    assert main(["tex-info", str(src)]) == 0
    assert '"mex_width": 32' in capsys.readouterr().out
    out = tmp_path / "dec"
    assert main(["tex-decode", str(src), "-o", str(out)]) == 0
    assert main(["tex-encode", str(out / "t.json"), str(out / "t.png"), "-o",
                 str(tmp_path / "re.tex")]) == 0
    assert (tmp_path / "re.tex").read_bytes() == raw
    texio.save_png(_pattern(32, 32, alpha=False, seed=9), tmp_path / "hi.png")
    assert main(["tex-replace", str(src), str(tmp_path / "hi.png"), "-o",
                 str(tmp_path / "hi.tex")]) == 0
    rep = ct.decode((tmp_path / "hi.tex").read_bytes())
    assert (rep.width, rep.mex_width, rep.first_mip) == (32, 32, 0)
    assert main(["tex-replace", str(src), str(tmp_path / "hi.png"), str(tmp_path / "hi.png"),
                 "-o", str(tmp_path / "bad.tex")]) == 1  # frame count mismatch -> error
