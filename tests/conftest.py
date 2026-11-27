# SPDX-FileCopyrightText: 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe
# SPDX-License-Identifier: GPL-3.0-only
"""Synthetic fixtures, generated at test time. No game data is used or derived."""

from __future__ import annotations

import os
import struct
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PKG_SCHEMAS = REPO / "src" / "sstre_assetcompiler" / "data"


def make_tex_v4(mex_w=64, mex_h=32, first=0, frames=1, flags=0, fine=7, next_chunk=b"FRMS",
                payload=b"") -> bytes:
    return (b"TVER" + struct.pack("<i", 4) + b"TDAT"
            + struct.pack("<6i", flags, mex_w, mex_h, fine, first, frames)
            + next_chunk + payload + bytes(16))


def make_tex_v3(mex_w=128, mex_h=128, first=2, frames=1, flags=0, fine=1, mips=6,
                frame_size=1234) -> bytes:
    return (b"TVER" + struct.pack("<i", 3) + b"TDAT"
            + struct.pack("<8i", flags, mex_w, mex_h, fine, mips, first, frame_size, frames)
            + b"FRMS" + bytes(16))


def make_zip(path: Path, entries: dict[str, bytes], method=zipfile.ZIP_DEFLATED) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=method) as zf:
        for name, data in entries.items():
            zf.writestr(zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0)), data,
                        compress_type=method)
    return path


def make_steam_install(lib: Path, dirname: str, app_id: int, build_id: int) -> Path:
    root = lib / "steamapps" / "common" / dirname
    root.mkdir(parents=True)
    (lib / "steamapps" / f"appmanifest_{app_id}.acf").write_text(
        '"AppState"\n{\n'
        f'\t"appid"\t\t"{app_id}"\n'
        '\t"name"\t\t"Invented Game \\"Deluxe\\""\n'
        f'\t"installdir"\t\t"{dirname}"\n'
        f'\t"buildid"\t\t"{build_id}"\n'
        '\t"LastUpdated"\t\t"1700000000"\n'
        '\t"LastOwner"\t\t"12345678901234567"\n'
        '\t"InstalledDepots"\n\t{\n\t\t"1"\n\t\t{\n\t\t\t"manifest"\t\t"9"\n\t\t}\n\t}\n'
        '}\n', encoding="utf-8")
    return root


@pytest.fixture
def classic_like(tmp_path):
    """A fake Steam install with a base archive, a patch archive and a Mods/ dir."""
    root = make_steam_install(tmp_path / "lib", "Invented Classic", 111, 222)
    make_zip(root / "base.gro", {
        "Textures/Wall.tex": make_tex_v4(64, 64, flags=1),
        "Textures/Old.tex": make_tex_v3(),
        "Textures/Fire.tex": make_tex_v4(32, 32, frames=0, next_chunk=b"FXDT"),
        "Models/Thing.mdl": b"MDAT" + bytes(40),
        "Sounds/Beep.wav": b"RIFF" + bytes(40),
        "Text/Hello World.txt": b"hello",
    })
    make_zip(root / "patch.gro", {"Textures/Wall.tex": make_tex_v4(128, 128)},
             method=zipfile.ZIP_STORED)
    make_zip(root / "Mods" / "Other" / "mod.gro", {"x.txt": b"third party"})
    (root / "Bin").mkdir()
    (root / "Bin" / "game.exe").write_bytes(b"MZ" + bytes(100))
    return root


@pytest.fixture
def hd_like(tmp_path):
    root = make_steam_install(tmp_path / "lib2", "Invented HD", 333, 444)
    make_zip(root / "Content" / "GameHD" / "All.gro", {
        "Content/GameHD/Textures/Wall.tex": b"CTSEMETA" + bytes(56),
        "Content/GameHD/Levels/A.wld": b"CTSEMETA" + bytes(8),
        "Content/Shared/Textures/Wall.tex": b"CTSEMETA" + b"\x01" * 56,
    })
    return root


def sstre_schemas_dir() -> Path | None:
    env = os.environ.get("SSTRE_SCHEMAS_DIR")
    for cand in ([Path(env)] if env else []) + [REPO.parent / "SSTRE" / "schemas"]:
        if (cand / "asset-inventory.schema.json").is_file():
            return cand
    return None


def validator(schema_path: Path):
    jsonschema = pytest.importorskip("jsonschema")
    import json
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema, format_checker=cls.FORMAT_CHECKER)
