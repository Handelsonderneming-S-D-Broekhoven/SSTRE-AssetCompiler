# SPDX-FileCopyrightText: 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe
# SPDX-License-Identifier: GPL-3.0-only
"""Command line: ``sstre-ac fingerprint | detect | inventory``.

All commands are read-only on the installs. Output goes to stdout or to the
file given with ``-o``; keep outputs in a local workspace, never in a repo.
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

from . import __version__
from .editions import (DEFAULT_INCLUDES, PROFILE_EDITIONS, SOURCE_EDITIONS, FingerprintError,
                       HashCache, build_report, detect_install, generate_profile,
                       load_fingerprints)
from .inventory import build_inventory


def _write_json(doc, out: Path | None) -> None:
    text = json.dumps(doc, indent=1, ensure_ascii=False) + "\n"
    if out is None:
        sys.stdout.write(text)
    else:
        out.write_text(text, encoding="utf-8")


def _pair(s: str) -> tuple[str, str]:
    if "=" not in s:
        raise argparse.ArgumentTypeError("expected GLOB=EDITION")
    glob, ed = s.rsplit("=", 1)
    if ed not in SOURCE_EDITIONS:
        raise argparse.ArgumentTypeError(f"unknown edition '{ed}'")
    return glob, ed


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="sstre-ac", description=__doc__.splitlines()[0])
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    fp = sub.add_parser("fingerprint", help="write a local fingerprint profile for an install")
    fp.add_argument("root", type=Path)
    fp.add_argument("--edition", required=True, choices=PROFILE_EDITIONS)
    fp.add_argument("--profile-id", required=True)
    fp.add_argument("--include", action="append", metavar="GLOB",
                    help=f"files to fingerprint, relative to root (default: {' '.join(DEFAULT_INCLUDES)})")
    fp.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    fp.add_argument("--file-edition", action="append", default=[], type=_pair,
                    metavar="GLOB=EDITION", help="per-file source edition (e.g. for Fusion packs)")
    fp.add_argument("--include-mods", action="store_true")
    fp.add_argument("-o", "--output", type=Path)

    for name, hlp in (("detect", "detect editions of installs (game-edition-report)"),
                      ("inventory", "inventory archives of installs (asset-inventory)")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("roots", type=Path, nargs="+")
        p.add_argument("--fingerprints", type=Path, action="append", default=[],
                       help="local fingerprint file (repeatable)")
        p.add_argument("--include-mods", action="store_true",
                       help="do not exclude third-party Mods/ content")
        p.add_argument("-o", "--output", type=Path)
        if name == "detect":
            p.add_argument("--hash-unlisted", action="store_true",
                           help="also hash archives not listed in the matched profile")
        else:
            p.add_argument("--all-archives", action="store_true",
                           help="also inventory archives not listed in the matched profile")
            p.add_argument("--edition", choices=SOURCE_EDITIONS,
                           help="override the source edition of every archive")
            p.add_argument("--jobs", type=int, default=4)
            p.add_argument("--summary", type=Path, help="write aggregate counts here")
            p.add_argument("--report", type=Path, help="also write the detection report here")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "fingerprint":
            doc = generate_profile(args.root, args.edition, args.profile_id,
                                   includes=args.include or DEFAULT_INCLUDES,
                                   excludes=args.exclude, file_editions=args.file_edition,
                                   include_mods=args.include_mods)
            _write_json(doc, args.output)
            return 0
        profiles, meta = load_fingerprints(args.fingerprints)
        cache = HashCache()
        entries, matched = [], []
        for root in args.roots:
            entry, profile = detect_install(root, profiles, cache=cache,
                                            hash_unlisted=getattr(args, "hash_unlisted", False),
                                            include_mods=args.include_mods)
            entries.append(entry)
            matched.append((root.resolve(), profile, entry.get("steam")))
            print(f"{root}: {entry['status']} {entry['detected_edition']}"
                  f" ({entry.get('profile_id', '-')})", file=sys.stderr)
        report = build_report(entries, meta)
        if args.cmd == "detect":
            _write_json(report, args.output)
            return 0
        if args.report:
            _write_json(report, args.report)
        doc, summary = build_inventory(matched, jobs=args.jobs, all_archives=args.all_archives,
                                       edition_override=args.edition,
                                       include_mods=args.include_mods, cache=cache)
        _write_json(doc, args.output)
        if args.summary:
            _write_json(summary, args.summary)
        print(f"inventory: {len(doc['sources'])} sources, {len(doc['assets'])} assets,"
              f" {summary['elapsed_s']} s", file=sys.stderr)
        return 0
    except (FingerprintError, OSError, zipfile.BadZipFile) as exc:
        print(f"sstre-ac: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
