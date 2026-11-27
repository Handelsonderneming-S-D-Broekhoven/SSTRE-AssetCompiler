# SPDX-FileCopyrightText: 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe
# SPDX-License-Identifier: GPL-3.0-only
import copy
import json

import pytest

from conftest import PKG_SCHEMAS, make_zip, validator
from sstre_assetcompiler.editions import (FingerprintError, detect_install, generate_profile,
                                          load_fingerprints, parse_fingerprints)


def _profile(root, edition="classic_tfe", pid="tfe-local", includes=("*.gro", "Bin/*.exe"), **kw):
    return generate_profile(root, edition, pid, includes=includes, **kw)


def test_generate_profile_contents(classic_like):
    doc = _profile(classic_like)
    validator(PKG_SCHEMAS / "edition-fingerprints.schema.json").validate(doc)
    p = doc["profiles"][0]
    assert p["steam_app_id"] == 111 and p["steam_build_id"] == 222
    assert [f["relpath"] for f in p["files"]] == ["base.gro", "Bin/game.exe", "patch.gro"]
    assert {f["role"] for f in p["files"]} == {"archive", "executable"}
    assert not any("Mods" in f["relpath"] for f in p["files"])


def test_detect_exact(classic_like, tmp_path):
    fp = tmp_path / "fp.json"
    fp.write_text(json.dumps(_profile(classic_like)))
    profiles, meta = load_fingerprints([fp])
    entry, best = detect_install(classic_like, profiles)
    assert entry["status"] == "exact" and entry["detected_edition"] == "classic_tfe"
    assert best.profile_id == "tfe-local" and meta[0]["profile_count"] == 1
    assert entry["files"]["matched"] == 3 and not entry["warnings"]
    assert entry["excluded"] == [{"relpath": "Mods/Other/mod.gro", "reason": "third_party_mods"}]
    assert all("sha256" in a for a in entry["archives"])


def test_detect_partial_on_changed_file(classic_like):
    profiles = parse_fingerprints(_profile(classic_like))
    make_zip(classic_like / "patch.gro", {"changed": b"1"})
    entry, _ = detect_install(classic_like, profiles)
    assert entry["status"] == "partial"
    assert entry["files"]["mismatched"] == [{"relpath": "patch.gro", "reason": "size"}]


def test_detect_same_size_different_hash(classic_like):
    doc = _profile(classic_like)
    doc["profiles"][0]["files"][0]["sha256"] = "0" * 64
    entry, _ = detect_install(classic_like, parse_fingerprints(doc))
    assert entry["files"]["mismatched"] == [{"relpath": "base.gro", "reason": "sha256"}]


def test_detect_picks_best_of_overlapping_profiles(classic_like, hd_like):
    a = _profile(classic_like)
    b = _profile(hd_like, "hd_tfe", "hd-local", includes=("*/*/*.gro",))
    # profile c shares one file with a (like a patch shipped with two games)
    c = copy.deepcopy(a)
    c["profiles"][0].update(profile_id="other", edition="classic_tse")
    c["profiles"][0]["files"] = c["profiles"][0]["files"][:1] + [
        {"relpath": "missing.gro", "size_bytes": 1, "sha256": "1" * 64}]
    profiles = parse_fingerprints(a) + parse_fingerprints(b) + parse_fingerprints(c)
    entry, best = detect_install(classic_like, profiles)
    assert best.profile_id == "tfe-local"
    assert [c["profile_id"] for c in entry["candidates"]] == ["tfe-local", "other"]
    entry, best = detect_install(hd_like, profiles)
    assert best.profile_id == "hd-local" and entry["status"] == "exact"


def test_detect_unknown(tmp_path):
    (tmp_path / "x").mkdir()
    entry, best = detect_install(tmp_path / "x", [])
    assert best is None and entry["status"] == "unknown" and "steam" not in entry


def test_steam_build_mismatch_warns(classic_like):
    doc = _profile(classic_like)
    doc["profiles"][0]["steam_build_id"] = 999
    entry, _ = detect_install(classic_like, parse_fingerprints(doc))
    assert entry["status"] == "exact" and any("build ID" in w for w in entry["warnings"])


def test_include_mods_opt_in(classic_like):
    doc = _profile(classic_like, includes=("*/*/*.gro",), include_mods=True)
    assert any(f["relpath"].startswith("Mods/") for f in doc["profiles"][0]["files"])


@pytest.mark.parametrize("mutate,msg", [
    (lambda d: d.update(format="x"), "format"),
    (lambda d: d.update(format_version=2), "format_version"),
    (lambda d: d["profiles"][0].update(edition="nope"), "edition"),
    (lambda d: d["profiles"][0].update(profile_id="Bad ID"), "profile_id"),
    (lambda d: d["profiles"][0]["files"][0].update(sha256="ABC"), "sha256"),
    (lambda d: d["profiles"][0]["files"][0].update(relpath="../x"), "relpath"),
    (lambda d: d["profiles"][0]["files"][0].update(size_bytes="1"), "size_bytes"),
    (lambda d: d["profiles"][0]["files"][0].update(role="weird"), "role"),
    (lambda d: d["profiles"][0].update(files=[]), "empty"),
    (lambda d: d["profiles"].append(copy.deepcopy(d["profiles"][0])), "duplicate"),
])
def test_fingerprint_validation(classic_like, mutate, msg):
    doc = _profile(classic_like)
    mutate(doc)
    with pytest.raises(FingerprintError, match=msg):
        parse_fingerprints(doc)


def test_schema_example_is_accepted_by_loader():
    schema = json.loads((PKG_SCHEMAS / "edition-fingerprints.schema.json").read_text())
    for ex in schema["examples"]:
        validator(PKG_SCHEMAS / "edition-fingerprints.schema.json").validate(ex)
        assert parse_fingerprints(ex)
