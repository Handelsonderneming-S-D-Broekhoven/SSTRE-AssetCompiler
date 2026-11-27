# SPDX-FileCopyrightText: 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe
# SPDX-License-Identifier: GPL-3.0-only
import struct

import pytest

from conftest import make_tex_v3, make_tex_v4
from sstre_assetcompiler.texheader import (TexHeaderError, parse_classic_tex_header,
                                           sniff_format, texture_info)


def test_v4_header():
    h = parse_classic_tex_header(make_tex_v4(256, 128, first=1, frames=1, flags=1)[:64])
    assert (h.version, h.mex_width, h.mex_height, h.first_mip) == (4, 256, 128, 1)
    assert (h.width_px, h.height_px) == (128, 64)
    assert h.next_chunk == b"FRMS" and not h.procedural_effect
    info = texture_info(h)
    assert info["is_power_of_two"] and not info["animated"] and info["flags_raw"] == 1
    assert info["logical_width"] == 256 and info["mip_count"] == 7


def test_v3_header_uses_observed_field_order():
    h = parse_classic_tex_header(make_tex_v3(1024, 512, first=3, mips=7, frame_size=999, fine=2))
    assert h.version == 3 and h.first_mip == 3 and h.fine_mips == 2
    assert h.v3_mip_count == 7 and h.v3_frame_size == 999
    assert (h.width_px, h.height_px) == (128, 64)


def test_effect_and_animated():
    fx = parse_classic_tex_header(make_tex_v4(frames=0, next_chunk=b"FXDT"))
    assert fx.procedural_effect and texture_info(fx)["frames"] == 0
    anim = parse_classic_tex_header(make_tex_v4(frames=4))
    assert texture_info(anim)["animated"]


def test_large_logical_size_is_accepted():
    h = parse_classic_tex_header(make_tex_v4(1 << 21, 1 << 16, first=12))
    assert (h.width_px, h.height_px) == (512, 16)


def test_non_power_of_two_reported():
    assert not texture_info(parse_classic_tex_header(make_tex_v4(96, 64)))["is_power_of_two"]


@pytest.mark.parametrize("data,msg", [
    (b"", "TVER"),
    (b"XXXX" + bytes(60), "TVER"),
    (b"TVER" + struct.pack("<i", 5) + bytes(56), "version"),
    (b"TVER" + struct.pack("<i", 4) + b"FRMS" + bytes(40), "TDAT"),
    (make_tex_v4()[:30], "truncated"),
    (make_tex_v4(mex_w=0), "logical size"),
    (make_tex_v4(mex_w=-4), "logical size"),
    (make_tex_v4(first=40), "first mip"),
    (make_tex_v4(16, 16, first=5), "exceeds"),
    (make_tex_v4(frames=-1), "frame count"),
    (make_tex_v3(frame_size=-1), "negative"),
])
def test_rejects_malformed(data, msg):
    with pytest.raises(TexHeaderError, match=msg):
        parse_classic_tex_header(data)


def test_sniff():
    assert sniff_format(make_tex_v4()) == "se1_tex_v4"
    assert sniff_format(make_tex_v3()) == "se1_tex_v3"
    assert sniff_format(b"TVER" + struct.pack("<i", 9)) is None
    assert sniff_format(b"CTSEMETA....") == "se3_meta_bin"
    assert sniff_format(b"WRKSTRM1") == "se3_wrkstrm"
    assert sniff_format(b"RIFF") is None
