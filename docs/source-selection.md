# Source selection (Classic / HD / HD-art)

Module: `src/sstre_assetcompiler/source_select.py`; CLI `sstre-ac select-source`.
Record schema: SSTRE `schemas/source-selection.schema.json`. Measurements and
calibration: SSTRE `docs/experiments/EXP-012-tex-codec-source-selection.md`.

Owner policy (2026-11-27): same-resolution HD is not rejected categorically.
Each asset is decided on effective image quality, resolution, compression
artefacts, extra material maps, artwork difference, UV compatibility and
preservation of the original art direction. Classic wins where HD is merely a
degraded or lossy copy. Every decision is a record with inputs, metrics,
thresholds, the rule trace and the result.

## Decoder interface (HD pixels stay outside this repository)

Reading HD (CTSEMETA) textures is an open legal question (SSTRE RD-007). The
selector therefore never decodes HD formats. It reads PNG files plus metadata
produced by any local decoder the user runs. A request file is a JSON list:

```json
[{
  "asset_id": "classic_tfe:textures/example/wall.tex",
  "category": "architecture",
  "classic": {"image": "classic/wall.png", "alpha_mode": "opaque",
              "width_px": 256, "height_px": 256, "mex_width": 4096, "mex_height": 4096},
  "candidates": [{
    "candidate_id": "hd_tfe:textures/example/wall", "source_kind": "hd", "edition": "hd_tfe",
    "image": "hd/wall.png", "source_format": "bc1",
    "companion_maps": {"normal": "hd/wall_nm.png"},
    "match": {"verified": true, "related": false, "method": "exact_path",
              "embedding_score": 0.97, "phash_distance": 2}
  }]
}]
```

- `image` paths are relative to the request file; images are RGBA8 PNG, mip 0.
- `match.verified` comes from a pixel-level same-content verifier (EXP-008:
  DINOv2 ≥ 0.90 and pHash ≤ 16). `match.related` marks retrieved but
  unverified candidates (embedding ≥ 0.75 or exact path).
- Requests and records contain local paths and hashes: keep them local (GATE-05).

## Metrics (computed here, deterministic)

All comparisons are made at the Classic resolution (HD box-downsampled),
alpha composited over mid-grey.

| Metric | Meaning |
|---|---|
| `resolution_ratio` | candidate pixels / Classic pixels |
| `aspect_match`, `layout_ssim` | same aspect; SSIM at ≤ 32 px (layout / UV compatibility) |
| `psnr_db`, `ssim` | fidelity to the Classic art |
| `delta_e`, `hist_intersection` | colour/tone drift on a low-pass image; luminance histogram overlap |
| `blockiness_*`, `artefact_index` | 4×4 block-boundary gradient ratio (BCn artefacts); excess over Classic, divided by the linear scale for upscales |
| `sharpness_classic`, `sharpness_ratio` | Laplacian variance of Classic; candidate / Classic |
| `detail_energy` | share of HD signal above the Classic Nyquist band (0 for an interpolated copy) |
| `alpha_mae`, `candidate_alpha_present` | alpha preservation (alpha-carrying Classic only) |

## Rules (first match decides)

| Rule | Result |
|---|---|
| R01 category never replaced (font, technical, procedural effect) | classic |
| R02 no candidate | classic_fallback |
| R03 not verified (related + same layout + different art → R07 instead) | classic_fallback |
| R04 aspect or layout differs | classic |
| R05 Classic alpha not preserved | classic |
| R06 bit-identical or PSNR ≥ 48 dB at same size (no gain) | classic |
| R07 same layout, redrawn/recoloured (SSIM < 0.80, ΔE > 6 or histogram < 0.80) | hd_art_candidate (opt-in) |
| R08 ≥ 2× pixels, detail_energy ≥ 0.10, artefact_index ≤ 0.20 | **hd** |
| R09 more pixels but no real detail or dominated by block artefacts | classic |
| R10 same size, not blockier, sharper (≥ 1.15×) than a non-flat Classic | **hd** |
| R11 lossy copy (PSNR < 40 dB or blockiness excess > 0.06) | classic |
| R12 nothing better | classic |
| M01 modifier: verified, same layout, HD normal/gloss/AO/height maps | classic → classic_with_hd_maps |

Thresholds are part of the policy and recorded in every record; override them
with `--policy policy.json` (`{"thresholds": {...}, "hd_art_opt_in": false}`).

## HD-art layer

`hd_art_candidate` never changes the base build: the base keeps Classic and the
candidate is listed with `hd_art.status = pending_owner_opt_in`. An owner
opt-in (`hd_art_opt_in: true` in the policy, plus per-asset approval in
SSTRE-Manifests) would build a separate, optional layer from those candidates.
That layer is implemented by `sstre-ac plan` as ADR-0009 layers 4 (HD Artwork)
and 5 (HD Interface, category `ui`); see [asset-layers.md](./asset-layers.md).
