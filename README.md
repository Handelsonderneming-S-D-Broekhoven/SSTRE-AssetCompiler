# SSTRE-AssetCompiler

Serious Sam-specific asset discovery, extraction, mapping, conversion and
compilation for **SSTRE — Serious Sam: The Raytraced Encounter**, an
unofficial, open-source modernization of Serious Sam: The First Encounter and
The Second Encounter. Independent of Croteam and Devolver Digital.

## Status

**Phase 0 — bootstrap and research. No code exists yet.** Nothing in this
repository can be run. The format research it will build on is in the SSTRE
umbrella (`docs/research/RD-002-hd-assets.md`).

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
| Implementation language and runtime | **TBD** (Python is a candidate, not decided) |
| Runtime asset formats to produce | **TBD** — depends on engine selection (ADR-005) |
| GPU | Not required by this repository itself (TBD whether any step needs one) |

## Development

There is nothing to build or test yet. The planned `src/` and `tests/fixtures/`
directories are intentionally absent until implementation starts, so that the
repository does not contain placeholder code that pretends to work.

What works today:

```sh
git clone https://github.com/Handelsonderneming-S-D-Broekhoven/SSTRE-AssetCompiler.git
git clone https://github.com/Handelsonderneming-S-D-Broekhoven/SSTRE.git
# Link and forbidden-file checks for this repository
# (requires Python 3.10+; see SSTRE CONTRIBUTING.md):
python SSTRE/scripts/validate_repo.py --root SSTRE-AssetCompiler --skip-schemas
```

## Licensing

License: proposed GPL-3.0. `LICENSE` contains the unmodified GNU GPL v3 text,
added on the unpublished `phase0/bootstrap` branch as a proposal. The SPDX
identifier (`GPL-3.0-only` as the plan proposes, or `GPL-3.0-or-later`) awaits
owner confirmation; see [ADR-0006](https://github.com/Handelsonderneming-S-D-Broekhoven/SSTRE/blob/phase0/bootstrap/docs/adr/0006-licensing-boundaries.md)
and SSTRE `docs/research/RD-007-licensing.md`. This repository must not contain code copied or ported
from the GPL-2.0-only engine. Third-party notices will be added with the first
third-party dependency.

## Related repositories

- [SSTRE](https://github.com/Handelsonderneming-S-D-Broekhoven/SSTRE) — umbrella, documentation, release pins
- [SSTRE-Engine](https://github.com/Handelsonderneming-S-D-Broekhoven/SSTRE-Engine) — engine fork and runtime
- [SSTRE-Manifests](https://github.com/Handelsonderneming-S-D-Broekhoven/SSTRE-Manifests) — recipes, mappings, overrides, locks
- [Remaster-Toolkit](https://github.com/Handelsonderneming-S-D-Broekhoven/Remaster-Toolkit) — game-agnostic processing toolkit

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md). Never commit game files, extracted
assets or derivatives of them.
