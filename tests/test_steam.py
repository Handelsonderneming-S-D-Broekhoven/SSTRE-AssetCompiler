# SPDX-FileCopyrightText: 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe
# SPDX-License-Identifier: GPL-3.0-only
import pytest

from conftest import make_steam_install
from sstre_assetcompiler.steam import VdfError, find_appmanifest, parse_vdf, read_appmanifest


def test_parse_vdf_nested_and_escapes():
    doc = parse_vdf('// comment\n"A"\n{\n "k" "v \\"q\\"" \n "B" { "x" "1" }\n}\n')
    assert doc == {"A": {"k": 'v "q"', "B": {"x": "1"}}}


@pytest.mark.parametrize("text", ['"A" {', '"A" { "k" }', '}', '"unterminated', "bare"])
def test_parse_vdf_errors(text):
    with pytest.raises(VdfError):
        parse_vdf(text)


def test_manifest_allowlist_drops_account_data(tmp_path):
    root = make_steam_install(tmp_path, "Some Game", 42, 7)
    info = find_appmanifest(root)
    assert info == {"manifest_file": "appmanifest_42.acf", "app_id": 42,
                    "name": 'Invented Game "Deluxe"', "installdir": "Some Game",
                    "build_id": 7, "last_updated": 1700000000}
    assert "12345678901234567" not in repr(info)


def test_no_manifest_outside_steam_library(tmp_path):
    (tmp_path / "loose").mkdir()
    assert find_appmanifest(tmp_path / "loose") is None


def test_broken_manifest_is_ignored(tmp_path):
    p = tmp_path / "appmanifest_1.acf"
    p.write_text('"AppState" { "appid" ', encoding="utf-8")
    assert read_appmanifest(p) is None
