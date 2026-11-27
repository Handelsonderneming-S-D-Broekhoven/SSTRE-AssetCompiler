# SPDX-FileCopyrightText: 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe
# SPDX-License-Identifier: GPL-3.0-only
import hashlib
import zipfile

import pytest

from conftest import make_zip
from sstre_assetcompiler.archives import scan_archive, unsafe_reason


@pytest.mark.parametrize("name,bad", [
    ("Textures/a.tex", False), ("a..b/c", False), ("", True), ("/abs/x", True),
    ("\\abs\\x", True), ("C:/x", True), ("a/../../x", True), ("..\\x", True), ("a\x00b", True),
])
def test_unsafe_reason(name, bad):
    assert (unsafe_reason(name) is not None) == bad


@pytest.mark.parametrize("method", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_scan_hashes_uncompressed_bytes(tmp_path, method):
    big = bytes(range(256)) * 9000  # > 1 MiB chunk to exercise streaming
    entries = {"a/b.txt": b"abc", "big.bin": big, "empty": b""}
    scan = scan_archive(make_zip(tmp_path / "t.gro", entries, method), head_bytes=8)
    got = {e.name: e for e in scan.entries}
    for name, data in entries.items():
        assert got[name].sha256 == hashlib.sha256(data).hexdigest()
        assert got[name].size == len(data) and got[name].head == data[:8]
    assert scan.methods == {method: 3} and not scan.errors


def test_directories_and_unsafe_names_are_not_entries(tmp_path):
    p = make_zip(tmp_path / "t.gro", {"dir/": b"", "../evil.txt": b"x", "/abs.txt": b"y",
                                      "ok.txt": b"z"})
    scan = scan_archive(p)
    assert [e.name for e in scan.entries] == ["ok.txt"]
    assert scan.directories == 1 and len(scan.unsafe) == 2


def test_crc_error_is_reported_not_raised(tmp_path):
    p = make_zip(tmp_path / "t.gro", {"x.txt": b"hello world"}, zipfile.ZIP_STORED)
    raw = bytearray(p.read_bytes())
    i = raw.index(b"hello world")
    raw[i] ^= 0xFF
    p.write_bytes(bytes(raw))
    scan = scan_archive(p)
    assert scan.entries[0].sha256 is None and scan.entries[0].problem
    assert len(scan.errors) == 1


def test_encrypted_flag_is_reported(tmp_path):
    p = make_zip(tmp_path / "t.gro", {"x.txt": b"secret"}, zipfile.ZIP_STORED)
    raw = bytearray(p.read_bytes())
    for sig, off in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):  # general purpose bit flag
        j = raw.index(sig) + off
        raw[j] |= 0x01
    p.write_bytes(bytes(raw))
    scan = scan_archive(p)
    assert scan.encrypted == 1 and scan.entries[0].problem == "encrypted"


def test_not_a_zip(tmp_path):
    p = tmp_path / "x.gro"
    p.write_bytes(b"not a zip")
    with pytest.raises(zipfile.BadZipFile):
        scan_archive(p)
