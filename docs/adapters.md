# Adapter boundaries

Status: **Proposal (Phase 0).** No adapter is implemented. This document fixes
*where* responsibilities sit so implementation can start without blurring the
boundaries to the other repositories. Interfaces are described in
language-neutral terms; the implementation language is not chosen yet.

Cross-repo rules and schema ownership: SSTRE
[`docs/architecture/cross-repo-contracts.md`](https://github.com/Handelsonderneming-S-D-Broekhoven/SSTRE/blob/main/docs/architecture/cross-repo-contracts.md).

## What lives here and what does not

| Concern | SSTRE-AssetCompiler | Elsewhere |
|---|---|---|
| Detecting installed editions (Classic TFE/TSE, HD TFE/TSE, Fusion) and verifying source hashes | Yes | – |
| Reading Serious Engine archives and formats (`.gro`, `.tex`, `.mdl`, `.wld`, ...) | Yes | Formats researched in SSTRE `docs/research/RD-002-hd-assets.md` |
| Reading HD-remaster formats | Yes | – |
| Stable asset IDs, inventory generation | Yes (produces `asset-inventory.schema.json` documents) | Schema in SSTRE `schemas/` |
| HD → Classic mapping proposals | Yes (proposes) | Curated results stored in SSTRE-Manifests `mappings/` |
| Serious Sam recipe-layer order (which Manifests directories apply, in which order) | Yes | Generic layered resolution in Remaster-Toolkit (proposed) |
| Upscaling, diffusion, PBR inference, generic QA metrics, caching | **No** | Remaster-Toolkit |
| Recipes, profiles, overrides, locks (data) | **No** | SSTRE-Manifests |
| Converting processed images back to runtime formats, mipmaps, compression, packaging | Yes | Runtime formats depend on ADR-005 / RD-005 |
| Loading assets at runtime | **No** | SSTRE-Engine |

## Adapter kinds

Each adapter is a self-contained unit with a narrow, documented input and
output. Adapters never call each other directly; the pipeline driver connects
them through files.

1. **Edition detector** — input: an installation directory chosen by the user.
   Output: edition identifier, version strings, SHA-256 of top-level archives,
   and whether the installation matches a known profile. Read-only on the
   installation; never modifies or copies it outside the local workspace.
2. **Archive/source reader** — input: an archive or loose-file root. Output:
   extracted files in the local workspace plus their source path and hash.
   Must reject path traversal and absolute paths inside archives.
3. **Format decoder** (one per format) — input: one native file. Output:
   a normalized intermediate (for textures: PNG/TIFF/EXR with explicit color
   space, alpha mode and animation frames) and a metadata record that keeps
   everything the intermediate cannot express (engine flags, frame timing,
   mip settings). Decoding must be lossless with respect to that metadata.
4. **Inventory builder** — combines decoder metadata into an inventory document
   with stable asset IDs, dimensions, color space, alpha, tileability hints and
   dependencies.
5. **Mapping proposer** — compares Classic and HD inventories and emits
   candidate matches with a confidence and the evidence used; never silently
   accepts ambiguous matches.
6. **Format encoder / packager** (one per runtime target) — input: processed
   intermediates plus original metadata. Output: engine-loadable files and a
   package manifest with output hashes. Unsupported features fall back to the
   original asset and are reported.

The generic work between steps 4 and 6 is delegated to the Remaster-Toolkit CLI
with resolved recipes; AssetCompiler does not reimplement it.

## Rules

- Game files are processed only from a local installation the user owns. No
  adapter downloads game data.
- Everything extracted or generated goes to a user-chosen workspace outside the
  repository (`workspace/` and `extracted/` are git-ignored as a safety net).
- Decoders and encoders must be testable with **synthetic fixtures** created
  for the purpose (for example a hand-built minimal texture file whose format
  is documented in RD-002). Fixtures copied or derived from game data are not
  allowed in `tests/`.
- Parsers treat all inputs as untrusted: bounded allocations, explicit size
  checks, no execution of embedded content.
- Every adapter reports what it could not handle instead of dropping it.

## Open questions

1. Which runtime formats the engine will load (native SE1 `.tex` only, or added
   DDS/KTX2 support) — ADR-005, RD-005.
2. Whether format code from upstream engine sources may be reused or ported,
   given license boundaries — RD-007, ADR-006.
3. Implementation language — depends on RD-003 tooling choices.
