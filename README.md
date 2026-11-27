# SSTRE-AssetCompiler

Serious Sam-specific asset discovery, extraction, mapping, conversion and
compilation for **SSTRE — Serious Sam: The Raytraced Encounter**, an
unofficial, open-source modernization of Serious Sam: The First Encounter and
The Second Encounter. Independent of Croteam and Devolver Digital.

## Status

**Sprint 2: proof of concept.** Working code: local edition detection and
archive inventory (`sstre-ac detect`, `sstre-ac inventory`), a lossless Classic
`.tex` codec with PNG export/import (`sstre-ac tex-*`, see
[docs/classic-tex-codec.md](./docs/classic-tex-codec.md)) and a per-asset
Classic/HD/HD-art source selector (`sstre-ac select-source`, see
[docs/source-selection.md](./docs/source-selection.md)), and the layered build
planner with platform profiles (`sstre-ac plan`, `validate-config`, `profiles`,
see [docs/asset-layers.md](./docs/asset-layers.md)). Everything reads
installs without modifying them and writes to a local workspace. There is no
packaging yet. The format research it builds on is in the SSTRE umbrella
(`docs/research/RD-002-hd-assets.md`, `docs/experiments/EXP-012-*`).

## Role

This is the only repository that knows Serious Sam's file formats and game data
layout. It will:

1. detect a user's installed editions (Classic / HD, TFE / TSE) and verify
   source hashes;
2. extract and normalize assets into a local workspace outside the repository;
3. build an asset inventory with stable IDs;
4. propose HD → Classic asset mappings;
5. invoke [Remaster-Toolkit](https://github.com/Handelsonderneming-S-D-Broekhoven/Remaster-Toolkit)
   with resolved recipes from
   [SSTRE-Manifests](https://github.com/Handelsonderneming-S-D-Broekhoven/SSTRE-Manifests);
6. convert, mipmap, compress and package results for
   [SSTRE-Engine](https://github.com/Handelsonderneming-S-D-Broekhoven/SSTRE-Engine).

Adapter responsibilities are described in [docs/adapters.md](./docs/adapters.md).

### Non-goals

- Generic image processing, AI inference or QA metrics (those belong in
  Remaster-Toolkit).
- Storing recipes, mappings or overrides (those belong in SSTRE-Manifests).
- Distributing game data in any form. Users supply their own legally acquired
  installation; everything extracted or generated stays on their machine.

## Architecture and dependencies

- Depends on: Remaster-Toolkit (via its CLI), SSTRE-Manifests (data), cross-repo
  schemas in the [SSTRE umbrella](https://github.com/Handelsonderneming-S-D-Broekhoven/SSTRE)
  `schemas/`.
- Depended on by: the SSTRE umbrella (orchestration). The engine consumes only
  the packaged files this repository produces.
- Boundaries, interfaces and pinning: SSTRE
  [`docs/architecture/cross-repo-contracts.md`](https://github.com/Handelsonderneming-S-D-Broekhoven/SSTRE/blob/main/docs/architecture/cross-repo-contracts.md).

## Prerequisites

| Item | Status |
|---|---|
| A legally acquired Serious Sam installation (Classic and/or HD) | Required once code exists; never committed |
| Implementation language and runtime | Python ≥ 3.10 for the proof of concept; inventory is stdlib only, the texture codec and selector need the pinned `[image]` extra (numpy 2.2.6, BSD-3-Clause; Pillow 12.3.0, MIT-CMU/HPND); final choice still open |
| Runtime asset formats to produce | **TBD** — depends on engine selection (ADR-005) |
| GPU | Not required by this repository itself (TBD whether any step needs one) |

## Development

Phase 1 proof of concept (backlog POC-03): a small, stdlib-only Python
package `sstre_assetcompiler` (Python 3.10+, no runtime dependencies) that
detects installed editions and inventories `.gro` archives. It is read-only on
installs, extracts nothing to disk and runs no game binaries.

| Module | Purpose |
|---|---|
| `editions.py` | fingerprint files, `fingerprint` generation, detection by top-level file size + SHA-256 |
| `steam.py` | allowlisted Steam `appmanifest_*.acf` fields (no account data) |
| `archives.py` | zip central-directory scan, in-memory SHA-256 of entries, unsafe-name rejection |
| `texheader.py` | Classic `.tex` `TVER`/`TDAT` header reader (clean-room, from SSTRE RD-002 §5) |
| `inventory.py` | `asset-inventory.schema.json` documents, override/shadowing resolution |
| `classic_tex.py` | lossless Classic `.tex` decoder/encoder (v3 + v4, frames, animations, effect payloads), higher-density replacements with identical logical size (clean-room, EXP-012) |
| `texio.py`, `texextract.py` | PNG + JSON sidecar export/import; extraction of a user-selected texture set; round-trip counts |
| `source_select.py` | per-asset Classic/HD/HD-art selection with metrics, ordered rules and `source-selection.schema.json` records |
| `layers.py` | ADR-0009 asset layers: build-config → resolved per-asset plan and frozen effective build manifest (`build-manifest.schema.json`) |
| `platform_profiles.py` | declarative platform profiles (`data/platform-profiles/`): size limits, formats per target, mip policy, memory budget, preprocessing |
| `jcs.py` | RFC 8785 canonical JSON and `sha256:` content hashes |

Set up and test (pytest uses synthetic fixtures generated at test time; the
schema tests look for an SSTRE checkout next to this repository or in
`SSTRE_SCHEMAS_DIR`, and are skipped otherwise):

```sh
git clone https://github.com/Handelsonderneming-S-D-Broekhoven/SSTRE-AssetCompiler.git
git clone https://github.com/Handelsonderneming-S-D-Broekhoven/SSTRE.git
python -m venv .venv            # outside both repositories is fine too
.venv/bin/pip install "./SSTRE-AssetCompiler[test]"   # Windows: .venv\Scripts\pip
cd SSTRE-AssetCompiler && ../.venv/bin/python -m pytest
```

Use (write every output to a local workspace outside the repositories; real
fingerprint files, reports and inventories contain proprietary file names and
hashes and must not be committed, see SSTRE ADR-0002 and backlog GATE-05):

```sh
# 1. Fingerprint your own install once (repeat per edition):
sstre-ac fingerprint "<install dir>" --edition classic_tfe --profile-id my-classic-tfe \
    --include "*.gro" -o ~/sstre-work/fp-classic-tfe.json
# 2. Detect editions (writes a game-edition-report.schema.json document):
sstre-ac detect "<install dir>" ... --fingerprints ~/sstre-work/fp-classic-tfe.json \
    -o ~/sstre-work/detect.json
# 3. Inventory the archives of the matched profile (asset-inventory.schema.json):
sstre-ac inventory "<install dir>" --fingerprints ~/sstre-work/fp-classic-tfe.json \
    -o ~/sstre-work/inventory.json --summary ~/sstre-work/summary.json
```

`--fingerprints` is repeatable. Third-party `Mods/` content is excluded unless
`--include-mods` is given. `sstre-ac <command> --help` lists all options. The
fingerprint file format is
`src/sstre_assetcompiler/data/edition-fingerprints.schema.json`.

Known limitations: loose files outside archives are not inventoried; the
override order between archives is a fallback (lexicographically last archive
wins) until the engine's mount order is documented; HD/Fusion (`CTSEMETA`)
headers are only sniffed, not decoded.

Repository checks (see SSTRE CONTRIBUTING.md):

```sh
python SSTRE/scripts/validate_repo.py --root SSTRE-AssetCompiler --skip-schemas
```

## Licensing

Copyright © 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe, and contributors.

License: GPL-3.0-only — licensed under the GNU General Public License, version 3 only ([`LICENSE`](LICENSE); machine-readable in [`REUSE.toml`](REUSE.toml)). See SSTRE [ADR-0006](https://github.com/Handelsonderneming-S-D-Broekhoven/SSTRE/blob/main/docs/adr/0006-licensing-boundaries.md) for the licensing boundaries between repositories. This repository must not contain code copied or ported from the GPL-2.0-only engine; anything that needs engine code runs as a separate GPL-2.0-only helper process or is implemented clean-room from a written specification. Third-party notices are added with the first third-party dependency.


## Related repositories

- [SSTRE](https://github.com/Handelsonderneming-S-D-Broekhoven/SSTRE) — umbrella, documentation, release pins
- [SSTRE-Engine](https://github.com/Handelsonderneming-S-D-Broekhoven/SSTRE-Engine) — engine fork and runtime
- [SSTRE-Manifests](https://github.com/Handelsonderneming-S-D-Broekhoven/SSTRE-Manifests) — recipes, mappings, overrides, locks
- [Remaster-Toolkit](https://github.com/Handelsonderneming-S-D-Broekhoven/Remaster-Toolkit) — game-agnostic processing toolkit

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md). Never commit game files, extracted
assets or derivatives of them.
