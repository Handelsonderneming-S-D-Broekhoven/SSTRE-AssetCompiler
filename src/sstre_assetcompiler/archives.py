# SPDX-FileCopyrightText: 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe
# SPDX-License-Identifier: GPL-3.0-only
"""Read-only inventory of ``.gro`` archives (standard PKZIP, per RD-002 section 4).

Uses the Python standard library ``zipfile`` module, which implements the
PKWARE APPNOTE format. Entries are streamed and hashed in memory; nothing is
written to disk. Entry names are untrusted and are checked for absolute paths
and traversal before they are reported as assets.
"""

from __future__ import annotations

import hashlib
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

CHUNK = 1 << 20


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(CHUNK):
            h.update(block)
    return h.hexdigest()


def unsafe_reason(name: str) -> str | None:
    """Return why an archive member name is unsafe, or None if it is safe."""
    if not name:
        return "empty name"
    if "\x00" in name:
        return "NUL in name"
    norm = name.replace("\\", "/")
    if norm.startswith("/"):
        return "absolute path"
    if len(norm) >= 2 and norm[1] == ":":
        return "drive-qualified path"
    if any(part == ".." for part in norm.split("/")):
        return "path traversal"
    return None


@dataclass
class EntryRecord:
    name: str
    size: int
    compressed_size: int
    method: int
    sha256: str | None = None
    head: bytes = b""
    problem: str | None = None


@dataclass
class ArchiveScan:
    entries: list[EntryRecord] = field(default_factory=list)
    directories: int = 0
    unsafe: list[tuple[str, str]] = field(default_factory=list)
    encrypted: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    methods: dict[int, int] = field(default_factory=dict)


def scan_archive(
    path: Path,
    *,
    hash_entries: bool = True,
    head_bytes: int = 64,
    want_head: Callable[[str], bool] = lambda name: True,
) -> ArchiveScan:
    """Inventory one zip archive from its central directory.

    With ``hash_entries`` every entry is decompressed in memory, in chunks,
    to compute the SHA-256 of its uncompressed bytes (zipfile also verifies
    the CRC-32). ``head`` holds the first ``head_bytes`` bytes of entries for
    which ``want_head(name)`` is true.
    """
    scan = ArchiveScan()
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                scan.directories += 1
                continue
            scan.methods[info.compress_type] = scan.methods.get(info.compress_type, 0) + 1
            reason = unsafe_reason(info.filename)
            if reason:
                scan.unsafe.append((info.filename, reason))
                continue
            rec = EntryRecord(info.filename, info.file_size, info.compress_size,
                              info.compress_type)
            if info.flag_bits & 0x1:
                scan.encrypted += 1
                rec.problem = "encrypted"
                scan.entries.append(rec)
                continue
            need_head = head_bytes > 0 and want_head(info.filename)
            if hash_entries or need_head:
                try:
                    _read_entry(zf, info, rec, hash_entries, head_bytes if need_head else 0)
                except (zipfile.BadZipFile, zlib.error, NotImplementedError, OSError, EOFError) as exc:
                    rec.problem = f"read error: {exc}"
                    rec.sha256 = None
                    scan.errors.append((info.filename, str(exc)))
            scan.entries.append(rec)
    return scan


def _read_entry(zf: zipfile.ZipFile, info: zipfile.ZipInfo, rec: EntryRecord,
                hash_entries: bool, head_bytes: int) -> None:
    h = hashlib.sha256() if hash_entries else None
    total = 0
    with zf.open(info) as f:
        if not hash_entries:
            rec.head = f.read(head_bytes)
            return
        while block := f.read(CHUNK):
            if total < head_bytes:
                rec.head += block[: head_bytes - total]
            total += len(block)
            h.update(block)
    if total != info.file_size:
        raise zipfile.BadZipFile(f"size mismatch: {total} != {info.file_size}")
    rec.sha256 = h.hexdigest()

