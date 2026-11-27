# SPDX-FileCopyrightText: 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe
# SPDX-License-Identifier: GPL-3.0-only
"""Edition detection by top-level file hashes plus Steam metadata.

The code knows no game data. Expected editions are described by *profiles*
in a user-supplied fingerprint file (format:
``data/edition-fingerprints.schema.json``), which the user generates locally
from their own installs with ``sstre-ac fingerprint``.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import GENERATOR_NAME, __version__
from .archives import sha256_file
from .steam import find_appmanifest

FINGERPRINT_FORMAT = "sstre-edition-fingerprints"
FINGERPRINT_FORMAT_VERSION = 1
REPORT_SCHEMA_VERSION = "0.1.0"

PROFILE_EDITIONS = ("classic_tfe", "classic_tse", "hd_tfe", "hd_tse", "fusion")
SOURCE_EDITIONS = ("classic_tfe", "classic_tse", "hd_tfe", "hd_tse",
                   "fusion_hd_tfe", "fusion_hd_tse", "fusion_other", "unknown")
FILE_ROLES = ("archive", "executable", "other")
DEFAULT_INCLUDES = ("*.gro", "*/*.gro", "*/*/*.gro")
THIRD_PARTY_DIRS = ("mods",)

_ID_RE = re.compile(r"^[a-z0-9_.-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class FingerprintError(ValueError):
    pass


@dataclass(frozen=True)
class FileFingerprint:
    relpath: str
    size_bytes: int
    sha256: str
    role: str = "archive"
    edition: str | None = None
    load_priority: int | None = None


@dataclass
class Profile:
    profile_id: str
    edition: str
    files: list[FileFingerprint]
    steam_app_id: int | None = None
    steam_build_id: int | None = None
    description: str | None = None

    def source_edition(self, f: FileFingerprint) -> str:
        if f.edition:
            return f.edition
        return self.edition if self.edition in SOURCE_EDITIONS else "fusion_other"


def default_source_edition(profile_edition: str | None) -> str:
    if profile_edition is None:
        return "unknown"
    return profile_edition if profile_edition in SOURCE_EDITIONS else "fusion_other"


# --------------------------------------------------------------------------
# Fingerprint files


def _req(obj: dict, key: str, typ, where: str):
    if key not in obj or not isinstance(obj[key], typ) or isinstance(obj[key], bool) and typ is int:
        raise FingerprintError(f"{where}: '{key}' missing or not {typ.__name__}")
    return obj[key]


def _opt_int(obj: dict, key: str, where: str) -> int | None:
    v = obj.get(key)
    if v is None:
        return None
    if not isinstance(v, int) or isinstance(v, bool):
        raise FingerprintError(f"{where}: '{key}' must be an integer")
    return v


def parse_fingerprints(doc: dict, origin: str = "<fingerprints>") -> list[Profile]:
    """Validate a fingerprint document (dependency-free) and return profiles."""
    if not isinstance(doc, dict):
        raise FingerprintError(f"{origin}: top level must be an object")
    if doc.get("format") != FINGERPRINT_FORMAT:
        raise FingerprintError(f"{origin}: 'format' must be '{FINGERPRINT_FORMAT}'")
    if doc.get("format_version") != FINGERPRINT_FORMAT_VERSION:
        raise FingerprintError(f"{origin}: unsupported 'format_version'")
    profiles = []
    seen = set()
    for i, p in enumerate(_req(doc, "profiles", list, origin)):
        where = f"{origin}: profiles[{i}]"
        if not isinstance(p, dict):
            raise FingerprintError(f"{where}: must be an object")
        pid = _req(p, "profile_id", str, where)
        if not _ID_RE.match(pid) or pid in seen:
            raise FingerprintError(f"{where}: invalid or duplicate profile_id")
        seen.add(pid)
        edition = _req(p, "edition", str, where)
        if edition not in PROFILE_EDITIONS:
            raise FingerprintError(f"{where}: unknown edition '{edition}'")
        files = []
        for j, f in enumerate(_req(p, "files", list, where)):
            fw = f"{where}.files[{j}]"
            if not isinstance(f, dict):
                raise FingerprintError(f"{fw}: must be an object")
            rel = _req(f, "relpath", str, fw)
            if rel.startswith("/") or ".." in rel.split("/") or "\\" in rel or ":" in rel:
                raise FingerprintError(f"{fw}: relpath must be relative, '/'-separated")
            sha = _req(f, "sha256", str, fw)
            if not _SHA_RE.match(sha):
                raise FingerprintError(f"{fw}: sha256 must be 64 lowercase hex digits")
            size = _req(f, "size_bytes", int, fw)
            role = f.get("role", "archive")
            if role not in FILE_ROLES:
                raise FingerprintError(f"{fw}: unknown role '{role}'")
            fed = f.get("edition")
            if fed is not None and fed not in SOURCE_EDITIONS:
                raise FingerprintError(f"{fw}: unknown edition '{fed}'")
            files.append(FileFingerprint(rel, size, sha, role, fed,
                                         _opt_int(f, "load_priority", fw)))
        if not files:
            raise FingerprintError(f"{where}: 'files' must not be empty")
        profiles.append(Profile(pid, edition, files, _opt_int(p, "steam_app_id", where),
                                _opt_int(p, "steam_build_id", where), p.get("description")))
    return profiles


def load_fingerprints(paths: list[Path]) -> tuple[list[Profile], list[dict]]:
    """Load several fingerprint files. Returns profiles and per-file metadata."""
    profiles: list[Profile] = []
    meta = []
    for path in paths:
        raw = path.read_bytes()
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FingerprintError(f"{path.name}: not valid JSON ({exc})") from exc
        got = parse_fingerprints(doc, path.name)
        ids = {p.profile_id for p in profiles}
        if dup := ids & {p.profile_id for p in got}:
            raise FingerprintError(f"{path.name}: duplicate profile_id {sorted(dup)}")
        profiles.extend(got)
        meta.append({"sha256": hashlib.sha256(raw).hexdigest(), "profile_count": len(got)})
    return profiles, meta


# --------------------------------------------------------------------------
# Scanning an install


def _is_third_party(rel: str) -> bool:
    return rel.split("/", 1)[0].casefold() in THIRD_PARTY_DIRS


def _match_any(rel: str, patterns) -> bool:
    low = rel.casefold()
    return any(fnmatch.fnmatchcase(low, p.casefold()) for p in patterns)


def scan_files(root: Path, includes=DEFAULT_INCLUDES, excludes=(), include_mods=False):
    """Return (sorted relpaths, sorted excluded third-party relpaths)."""
    found, mods = set(), set()
    for pattern in includes:
        for p in root.glob(pattern):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if _match_any(rel, excludes):
                continue
            if _is_third_party(rel) and not include_mods:
                mods.add(rel)
                continue
            found.add(rel)
    return sorted(found, key=str.casefold), sorted(mods, key=str.casefold)


class HashCache:
    def __init__(self):
        self._d: dict[str, str] = {}
        self.bytes_hashed = 0

    def get(self, path: Path) -> str:
        key = str(path.resolve())
        if key not in self._d:
            self._d[key] = sha256_file(path)
            self.bytes_hashed += path.stat().st_size
        return self._d[key]


def _role_for(rel: str) -> str:
    low = rel.casefold()
    if low.endswith(".gro"):
        return "archive"
    if low.endswith(".exe"):
        return "executable"
    return "other"


def generate_profile(root: Path, edition: str, profile_id: str, includes=DEFAULT_INCLUDES,
                     excludes=(), file_editions=(), include_mods=False,
                     cache: HashCache | None = None) -> dict:
    """Build a fingerprint document (one profile) from an install on disk."""
    if edition not in PROFILE_EDITIONS:
        raise FingerprintError(f"unknown edition '{edition}'")
    if not _ID_RE.match(profile_id):
        raise FingerprintError("profile_id must match ^[a-z0-9_.-]+$")
    cache = cache or HashCache()
    rels, _ = scan_files(root, includes, excludes, include_mods)
    files = []
    for rel in rels:
        p = root / rel
        entry = {"relpath": rel, "size_bytes": p.stat().st_size,
                 "sha256": cache.get(p), "role": _role_for(rel)}
        for pattern, ed in file_editions:
            if fnmatch.fnmatchcase(rel.casefold(), pattern.casefold()):
                entry["edition"] = ed
                break
        files.append(entry)
    profile: dict = {"profile_id": profile_id, "edition": edition}
    steam = find_appmanifest(root)
    if steam:
        if "app_id" in steam:
            profile["steam_app_id"] = steam["app_id"]
        if "build_id" in steam:
            profile["steam_build_id"] = steam["build_id"]
    profile["files"] = files
    doc = {"format": FINGERPRINT_FORMAT, "format_version": FINGERPRINT_FORMAT_VERSION,
           "profiles": [profile]}
    parse_fingerprints(doc)  # self-check
    return doc


# --------------------------------------------------------------------------
# Detection


@dataclass
class ProfileMatch:
    profile: Profile
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    mismatched: list[dict] = field(default_factory=list)

    @property
    def score(self) -> float:
        return len(self.matched) / len(self.profile.files)


def _match_profile(root: Path, profile: Profile, cache: HashCache) -> ProfileMatch:
    m = ProfileMatch(profile)
    for f in profile.files:
        p = root / f.relpath
        if not p.is_file():
            m.missing.append(f.relpath)
        elif p.stat().st_size != f.size_bytes:
            m.mismatched.append({"relpath": f.relpath, "reason": "size"})
        elif cache.get(p) != f.sha256:
            m.mismatched.append({"relpath": f.relpath, "reason": "sha256"})
        else:
            m.matched.append(f.relpath)
    return m


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def detect_install(root: Path, profiles: list[Profile], *, cache: HashCache | None = None,
                   hash_unlisted: bool = False, include_mods: bool = False) -> tuple[dict, Profile | None]:
    """Detect the edition of one install. Returns (report entry, best profile)."""
    t0 = time.perf_counter()
    cache = cache or HashCache()
    root = root.resolve()
    entry: dict = {"root": str(root)}
    warnings: list[str] = []
    steam = find_appmanifest(root)
    if steam:
        entry["steam"] = steam
    if not root.is_dir():
        raise FileNotFoundError(f"not a directory: {root}")

    # Cheap pre-filter: only profiles with at least one file present are hashed.
    candidates = [p for p in profiles if any((root / f.relpath).is_file() for f in p.files)]
    matches = [_match_profile(root, p, cache) for p in candidates]
    matches.sort(key=lambda m: (m.score, len(m.matched)), reverse=True)
    best = matches[0] if matches and matches[0].matched else None

    if best is None:
        entry["status"] = "unknown"
        entry["detected_edition"] = "unknown"
    else:
        exact = not best.missing and not best.mismatched
        entry["status"] = "exact" if exact else "partial"
        entry["detected_edition"] = best.profile.edition
        entry["profile_id"] = best.profile.profile_id
        entry["files"] = {"expected": len(best.profile.files), "matched": len(best.matched),
                          "missing": best.missing, "mismatched": best.mismatched}
        if len(matches) > 1 and matches[1].score == best.score:
            warnings.append(f"ambiguous: profile '{matches[1].profile.profile_id}' scores equally")
        if steam and best.profile.steam_app_id is not None and \
                steam.get("app_id") != best.profile.steam_app_id:
            warnings.append("Steam app ID differs from the matched profile")
        if steam and best.profile.steam_build_id is not None and \
                steam.get("build_id") != best.profile.steam_build_id:
            warnings.append("Steam build ID differs from the matched profile")
    entry["candidates"] = [{"profile_id": m.profile.profile_id, "matched": len(m.matched),
                            "expected": len(m.profile.files)} for m in matches]

    listed = {}
    if best:
        listed = {f.relpath.casefold(): f for f in best.profile.files}
    rels, mods = scan_files(root, include_mods=include_mods)
    archives = []
    for rel in rels:
        p = root / rel
        f = listed.get(rel.casefold())
        a = {"relpath": rel, "size_bytes": p.stat().st_size, "listed_in_profile": f is not None}
        if f is not None:
            a["edition"] = best.profile.source_edition(f)
        if (f is not None and f.relpath not in best.missing) or hash_unlisted:
            a["sha256"] = cache.get(p)
        archives.append(a)
    entry["archives"] = archives
    if mods:
        entry["excluded"] = [{"relpath": r, "reason": "third_party_mods"} for r in mods]
    entry["warnings"] = warnings
    entry["elapsed_s"] = round(time.perf_counter() - t0, 3)
    return entry, (best.profile if best else None)


def build_report(entries: list[dict], fingerprint_meta: list[dict]) -> dict:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": _now(),
        "generator": {"name": GENERATOR_NAME, "version": __version__},
        "fingerprint_sets": fingerprint_meta,
        "installs": entries,
    }
