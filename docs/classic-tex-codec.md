# Classic `.tex` codec

Module: `src/sstre_assetcompiler/classic_tex.py` (decode/encode), `texio.py`
(PNG + JSON sidecar), `texextract.py` (set extraction, round-trip counts).
Evidence and measurements: SSTRE `docs/experiments/EXP-012-tex-codec-source-selection.md`.

## Clean-room procedure (ADR-0006)

The codec is GPL-3.0-only. It was written without reading engine source code
(`upstream/`, SSTRE-Engine branches) and without reading the Phase 0/1 research
readers that were derived from engine source. Inputs were only:

1. the written format description in SSTRE `docs/research/RD-002-hd-assets.md` §5;
2. the v3 `TDAT` field-order correction in SSTRE `docs/experiments/EXP-007`;
3. black-box observation of the bytes of the user's own local files
   (chunk tags, sizes implied by header fields, visual inspection of decodes).

## Observed layout

All integers are little-endian int32. There are no chunk sizes: every length
follows from earlier fields.

| Part | Content |
|---|---|
| `TVER` | version: 3 or 4 |
| `TDAT` v4 | flags, mexW, mexH, fineMips, firstMip, frames |
| `TDAT` v3 | flags, mexW, mexH, fineMips, ctMips, firstMip, frameSize, frames |
| `FRMS` v4 | frames × W×H×bpp bytes, mip 0 only, rows top-down; bpp = 4 (R,G,B,A) when flags bit 0 is set, else 3 (R,G,B) |
| `FRMS` v3 | frames × frameSize bytes = ctMips levels of 16-bit words: flags bit 0 clear → RGBA5551 (R 15–11, G 10–6, B 5–1, A bit 0); set → RGBA4444 (R 15–12 … A 3–0) |
| `FXDT` | procedural effect payload (instead of `FRMS`), kept as opaque bytes |
| `ANIM` `ADAT` | count, then per animation: 32-byte name field (NUL-terminated, trailing bytes are not zeroed in real files and are preserved), float32 seconds per frame, int32 n, n × int32 frame index |
| `BAST` `DFNM` | optional (effects): int32 length + base texture name |

W = mexW >> firstMip, H = mexH >> firstMip. The logical size (mex) is what
model and brush UVs are authored against.

## Guarantees

- `encode(decode(b)) == b` for every observed Classic file (v3, v4, effects),
  and `decode(encode(t))` reproduces every pixel and field of a model `t`.
- v3 channels are expanded to 8 bits by bit replication, which `>> (8-bits)`
  inverts exactly; a v3 texture can be written as v4 (mip 0, same pixels).
- `make_replacement(original, frames)` writes a higher pixel density with the
  **same mex**: firstMip drops by log2(scale). The scale is therefore limited
  to 2^firstMip; EXP-012 measured what the runtime accepts.
- Input is untrusted: sizes, counts and indices are bounded and checked.

## CLI

```sh
sstre-ac tex-info  FILE.tex
sstre-ac tex-decode FILE.tex -o OUTDIR           # PNG per frame + JSON sidecar
sstre-ac tex-encode OUTDIR/FILE.json OUTDIR/FILE.png [...] -o NEW.tex
sstre-ac tex-replace ORIGINAL.tex HIGHER_RES.png [...] -o NEW.tex
sstre-ac tex-extract INSTALL --selection SEL.json -o OUTDIR   # local only
sstre-ac tex-roundtrip INSTALL [...]             # counts only
```
