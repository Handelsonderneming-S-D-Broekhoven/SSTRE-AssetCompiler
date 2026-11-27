# Asset layers, build configuration and platform profiles

Module: `src/sstre_assetcompiler/layers.py` (layers, plan),
`platform_profiles.py` (platform profiles) and `jcs.py` (canonical JSON). CLI:
`sstre-ac plan`, `sstre-ac validate-config`, `sstre-ac profiles`.
Specification: SSTRE ADR-0009. Schemas (SSTRE umbrella `schemas/`):
`build-config`, `build-manifest`, `platform-profile`, `layer-approvals` and
`asset-pack`. Measurements: SSTRE `docs/experiments/EXP-015-asset-layers-profiles.md`.

## Layers

The build composes seven layers in a **fixed order**. A later layer overrides
an earlier one only for the assets it covers.

| # | Layer id | Default | Supplies an asset when |
|---|---|---|---|
| 1 | `classic-base` | on, cannot be disabled | always; source of truth and fallback |
| 2 | `hd-quality` | on | the source-selection record says `hd` (R08/R10) **and** the selected candidate is verified |
| 3 | `hd-material-maps` | on | maps belong to the final albedo: maps of the selected HD source, or the M01 maps verified for the Classic albedo (`classic_with_hd_maps`). Roles are filtered by the config and by the platform profile |
| 4 | `hd-artwork` | off | the record says `hd_art_candidate` (not UI), the layer is opted in **and** the approvals file approves that exact candidate (optionally pinned by hash) |
| 5 | `hd-interface` | off | as layer 4 for category `ui` (menus, HUD, Netricsa), with its own opt-in. `includeRelayout` also offers higher-resolution, re-laid-out UI images (EXP-012); they still need an approval and are marked `layoutChanged` (2D UI tolerates a mex change, so no firstMip cap) |
| 6 | `ai-enhancement` | quality profile `off` | processes the source chosen by layers 1–5 (see below). It never supplies a source |
| 7 | `overrides` | off | curated or community packs; `replace` supplies a file, `use-classic` forces Classic, `skip-ai` keeps the selected source unprocessed |

The winner for an asset is the contribution from the highest layer; inside a
layer, the higher explicit priority wins, then the lexicographically smaller
stable ID. Override packs are ranked by `priority` (higher first), then
curated before community, then `packId`. Every input list is sorted before use,
so file and list order never change the plan. Layers 4 and 5 never activate
implicitly, and layer 2 never takes a degraded or equivalent copy (those
records say `classic`).

Every plan entry lists the status of all seven layers: `applied`,
`superseded`, `disabled`, `awaiting-approval`, `rejected`, `blocked` (stale or
mismatched approval, unverified candidate), `not-applicable`, `dropped` (maps
incompatible with the final albedo or excluded by the profile), and for
layer 6 `off`, `not-eligible`, `suppressed`, `source-sufficient`,
`platform-capped`, `awaiting-output`.

## Inputs

```sh
sstre-ac validate-config --config build-config.json        # prints hashes to pin
sstre-ac plan --config build-config.json --selection <dir of sel-*.json> \
    [--assets set1/READY.json [--only-listed]] --out plan.json [--summary s.json] [--canonical]
sstre-ac profiles [--show pc-default]
```

- **Build config** (`build-config.schema.json`): layer switches, the quality
  profile, the platform profile (built-in id or inline object), the approvals
  file and the asset packs. Paths are relative to the config. Omitted layers
  take the defaults above. Examples: [`examples/build-config/`](../examples/build-config/).
- **Selection records**: the output of `sstre-ac select-source`. If one asset
  has several records, `selection.policyId`/`policyVersion` in the config
  selects one; otherwise the smallest `record_id` wins and the conflict is
  listed in the manifest.
- **Asset facts** (optional): a texture-set `READY.json` or a JSON list. They
  add firstMip (for the EXP-012 density cap), the decoded PNG hash (so AI
  outputs made from PNGs can be matched) and assets without a record.
- **Approvals** (`layer-approvals.schema.json`): `{assetId, layer, candidateId,
  decision, candidateSha256?, priority?}` entries for layers 4 and 5.

## Layer 6 interface for AI outputs

The AI track delivers outputs as an **asset pack** with `kind:
ai-enhancement` (`asset-pack.schema.json`), listed in the config under
`layers.ai-enhancement.packs`. An entry is used for an asset only if all of
these hold:

1. `assetId` is the plan's asset ID (`classic_tfe:…`), `role` is `albedo`
   (other roles become inferred material maps where HD supplies none);
2. `input.sourceSha256` equals the hash of the source the plan selected:
   `source.sha256` (source file) or `source.imageSha256` (decoded mip-0 PNG,
   from the asset facts or the record). An output made from Classic pixels is
   therefore not used when HD or HD Artwork supplies the source;
3. the pack's `qualityProfile` equals the config's quality profile;
4. `qa.passed` is true;
5. the output keeps the source aspect ratio and is not smaller than the source.

The entry must also carry `sha256` and `px` of the output file and the
`recipeHash` (`resolvedRecipeHash` of the `asset-recipe` document). It should
carry `provenanceSha256` (JCS hash of the `build-provenance` record). Rejected
outputs are listed per asset in `processing.rejectedOutputs`. Without a usable
output, the asset is `awaiting-output` and the plan gives the size to produce
in `processing.plannedPx` (Classic size × profile scale, clamped by the
platform profile, the firstMip cap and the memory budget). A build falls back to
the unprocessed source until the output exists (ADR-0004).

Quality profiles (built in, `data/quality-profiles.json`; a config can add its
own under `qualityProfiles`):

| Profile | Processing |
|---|---|
| `off` | none |
| `conservative` | deterministic SR ×2 for world textures (architecture, terrain, vegetation, props, items, characters, weapons, sky, detail); sources: Classic and HD Quality |
| `standard` | SR ×4 with generative refinement for tileable surfaces and sky; SR ×4 for the rest; SR ×2 for effects; sources: Classic, HD Quality, HD Artwork |

UI, fonts and technical maps are never processed by the built-in profiles.

## Platform profiles

A profile (`platform-profile.schema.json`) is independent of the layers. It
holds size limits, the targets with their container, allowed formats and
default format per role slot (`color-opaque`, `color-alpha`, `normal`,
`mask`), the mip policy, the memory budget and preprocessing settings
(downscale policy and filter, colour space, alpha, normals).

| Profile | Status | Limits | Targets | Budget |
|---|---|---|---|---|
| `pc-default` | provisional | ≤ 4096 per axis, short edge ≤ 2048, ≤ 8 388 608 px (EXP-012: 2048×4096 loads, 4096×4096 crashes), firstMip density cap | `raster`: `.tex` v4 RGB8/RGBA8, validated; `rt`: KTX2 BC7/BC5/BC4 (RGBA8 allowed), reader validated (EXP-014), render test pending | 2 GiB raster, report only |
| `pc-low-memory` | provisional | ≤ 2048 per axis, ≤ 4 Mpx | as above, BC1 for opaque colour on RT; only normal maps | 512 MiB raster, reduce largest first |
| `legacy-constrained` | **hypothetical** | ≤ 512, power of two | 16-bit, palettised, ASTC, ETC2: all **not validated** placeholders | 32 MiB, may go below Classic |

The hypothetical profile has invented round numbers and contains no platform-SDK
information. Applying a profile:

1. firstMip density cap (when firstMip is known and mex is kept): a replacement
   may not exceed 2^firstMip × the Classic size (EXP-012);
2. power-of-two rounding if required (per axis, downward);
3. downscale until all limits hold (`halve-until-fit`, or `fit-exact`);
4. memory budget over all assets: `reduce-largest` halves the largest asset
   (ties: asset ID) until the total fits, never below the Classic size when
   `neverBelowClassic` is set. Planned AI sizes count and are reduced first; an
   AI gain that the budget removes entirely becomes `platform-capped`;
5. formats per target and role, and the ordered preprocessing steps.

Every change is an action in `platform.actions`. Material maps share the
candidate's size (`pxAssumed`) until the HD plugin records map sizes.

## Effective build manifest

`plan` writes `build-manifest.schema.json`: the frozen `effectiveConfig` (layer
switches, quality profile definition, the full platform profile, approvals,
packs by version and hash, the selection record set by hash, the asset facts
hash), the per-asset plan and a summary. Hashes are SHA-256 over RFC 8785
canonical JSON, written `sha256:<hex>`:

- `effectiveConfigHash` = H(effectiveConfig); `planHash` = H({effectiveConfig, assets});
- pins for approvals and packs are JCS hashes of the parsed document with its
  list put in canonical order (approvals by asset, layer, candidate; pack
  entries by asset and role), so formatting and list order do not matter;
  `validate-config` prints them;
- a selection record is hashed without `generated_at`.

The manifest contains no timestamps, so two runs on the same inputs write
byte-identical files. The default output is indented, key-sorted UTF-8 with LF
line ends; `--canonical` writes the exact RFC 8785 bytes. Manifests reference
proprietary assets: keep them local (GATE-05).

## Limitations

- Records do not carry firstMip; the density cap applies only to assets with
  facts (EXP-012 headroom: about 83 % of textures allow ≥ 4×).
- The planner plans; it does not convert, compress or write `.tex`/`.ktx2`.
- Map sizes are assumed equal to the candidate size.
- The firstMip cap also limits the RT target, because one target size is
  planned per texture.
