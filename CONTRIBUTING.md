# Contributing to SSTRE-AssetCompiler

This repository follows the shared SSTRE contribution guide:
**[SSTRE CONTRIBUTING.md](https://github.com/Handelsonderneming-S-D-Broekhoven/SSTRE/blob/main/CONTRIBUTING.md)**
(commit conventions, sign-off, branch naming, ADR process, the no-game-data rule
and the AI-generated-asset rule). Security reports: see the umbrella
[SECURITY.md](https://github.com/Handelsonderneming-S-D-Broekhoven/SSTRE/blob/main/SECURITY.md).

Repository-specific notes:

- This repository owns **Serious Sam-specific** knowledge: formats, archive layouts, edition detection, asset IDs and runtime conversion. Generic image processing belongs in [Remaster-Toolkit](https://github.com/Handelsonderneming-S-D-Broekhoven/Remaster-Toolkit); see [docs/adapters.md](./docs/adapters.md).
- Test fixtures must be synthetic or explicitly licensed for redistribution, never extracted from a game installation.
- Never commit game files, extracted assets, AI derivatives of game assets, model
  weights or secrets. The [`.gitignore`](./.gitignore) blocks the common cases; it is a
  safety net, not permission.

Licensing: `GPL-3.0-only`. Sign every commit off under the DCO (`git commit -s`).
