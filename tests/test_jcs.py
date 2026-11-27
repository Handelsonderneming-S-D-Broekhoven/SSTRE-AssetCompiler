# SPDX-FileCopyrightText: 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe
# SPDX-License-Identifier: GPL-3.0-only
"""RFC 8785 canonical JSON: test vectors from the RFC text plus edge cases."""

from __future__ import annotations

import json

import pytest

from sstre_assetcompiler.jcs import (CanonicalizationError, canonicalize, content_hash, dumps_pretty,
                                     file_hash)


def test_rfc8785_section_3_2_example():
    doc = {"numbers": [333333333.33333329, 1E30, 4.50, 2e-3, 0.000000000000000000000000001],
           "string": chr(0x20AC) + "$" + chr(0x0F) + chr(0x0A) + "A'B" + chr(0x22) + chr(0x5C) * 2 + chr(0x22) + "/",
           "literals": [None, True, False]}
    want = ('{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
            '"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}')
    assert canonicalize(doc) == want.encode("utf-8")


def test_rfc8785_sorting_by_utf16_code_units():
    doc = {"€": "Euro Sign", "\r": "Carriage Return", "דּ": "Hebrew Letter Dalet With Dagesh",
           "1": "One", "\U0001f600": "Emoji: Grinning Face", "\u0080": "Control", "ö": "Latin Small Letter O With Diaeresis"}
    keys = list(json.loads(canonicalize(doc).decode("utf-8")))
    assert keys == ["\r", "1", "\u0080", "ö", "€", "\U0001f600", "דּ"]


@pytest.mark.parametrize("x,want", [
    (0.0, "0"), (-0.0, "0"), (1.0, "1"), (-1.5, "-1.5"), (1e21, "1e+21"), (1e20, "100000000000000000000"),
    (1e-6, "0.000001"), (1e-7, "1e-7"), (123456789012345680000.0, "123456789012345680000"),
    (5e-324, "5e-324"), (1.7976931348623157e308, "1.7976931348623157e+308"), (0.1, "0.1"),
    (100.25, "100.25"), (9007199254740991, "9007199254740991"),
])
def test_numbers(x, want):
    assert canonicalize(x) == want.encode()


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 2 ** 53 + 1, "\ud800", {1: "x"}, {"a": {1, 2}}])
def test_errors(bad):
    with pytest.raises(CanonicalizationError):
        canonicalize(bad)


def test_hash_and_pretty(tmp_path):
    a = {"b": [1, {"y": 2, "x": 1}], "a": "é"}
    b = {"a": "é", "b": [1, {"x": 1, "y": 2}]}
    assert content_hash(a) == content_hash(b)
    assert content_hash(a).startswith("sha256:") and len(content_hash(a)) == 71
    text = dumps_pretty(a)
    assert text.endswith("\n") and "\r" not in text and text.index('"a"') < text.index('"b"')
    assert json.loads(text) == a
    p = tmp_path / "f.bin"
    p.write_bytes(b"abc")
    assert file_hash(p) == "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
