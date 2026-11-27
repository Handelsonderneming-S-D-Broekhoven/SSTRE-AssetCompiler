# SPDX-FileCopyrightText: 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe
# SPDX-License-Identifier: GPL-3.0-only
"""Read Steam ``appmanifest_*.acf`` metadata for an install directory.

Only an allowlist of non-personal keys is returned. Account-related keys in
the manifest (for example the last owner's account ID) are never read into the
result.
"""

from __future__ import annotations

from pathlib import Path

_MAX_MANIFEST_BYTES = 1 << 20
_ALLOWED_KEYS = {"appid", "name", "installdir", "buildid", "lastupdated"}


class VdfError(ValueError):
    pass


def _tokens(text: str):
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c == "/" and text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif c in "{}":
            yield c
            i += 1
        elif c == '"':
            buf = []
            i += 1
            while True:
                if i >= n:
                    raise VdfError("unterminated string")
                c = text[i]
                if c == "\\" and i + 1 < n:
                    buf.append(text[i + 1])
                    i += 2
                elif c == '"':
                    i += 1
                    break
                else:
                    buf.append(c)
                    i += 1
            yield ("s", "".join(buf))
        else:
            raise VdfError(f"unexpected character {c!r}")


def parse_vdf(text: str) -> dict:
    """Parse Valve KeyValues text into nested dicts (keys kept as written)."""
    root: dict = {}
    stack = [root]
    pending_key: str | None = None
    for tok in _tokens(text):
        if tok == "{":
            if pending_key is None:
                raise VdfError("block without key")
            child: dict = {}
            stack[-1][pending_key] = child
            stack.append(child)
            pending_key = None
        elif tok == "}":
            if len(stack) == 1 or pending_key is not None:
                raise VdfError("unbalanced block")
            stack.pop()
        else:
            if pending_key is None:
                pending_key = tok[1]
            else:
                stack[-1][pending_key] = tok[1]
                pending_key = None
    if len(stack) != 1 or pending_key is not None:
        raise VdfError("unexpected end of input")
    return root


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def read_appmanifest(path: Path) -> dict | None:
    """Return allowlisted AppState fields of one manifest, or None."""
    try:
        if path.stat().st_size > _MAX_MANIFEST_BYTES:
            return None
        data = parse_vdf(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, VdfError):
        return None
    state = next((v for k, v in data.items() if k.lower() == "appstate"), None)
    if not isinstance(state, dict):
        return None
    vals = {k.lower(): v for k, v in state.items()
            if k.lower() in _ALLOWED_KEYS and isinstance(v, str)}
    out: dict = {"manifest_file": path.name}
    if (app := _int_or_none(vals.get("appid"))) is not None:
        out["app_id"] = app
    if "name" in vals:
        out["name"] = vals["name"]
    if "installdir" in vals:
        out["installdir"] = vals["installdir"]
    if (b := _int_or_none(vals.get("buildid"))) is not None:
        out["build_id"] = b
    if (t := _int_or_none(vals.get("lastupdated"))) is not None:
        out["last_updated"] = t
    return out


def find_appmanifest(install_root: Path) -> dict | None:
    """Find the manifest of a Steam library install (``steamapps/common/<dir>``)."""
    root = install_root.resolve()
    steamapps = root.parent.parent
    if root.parent.name.lower() != "common" or not steamapps.is_dir():
        return None
    for acf in sorted(steamapps.glob("appmanifest_*.acf")):
        info = read_appmanifest(acf)
        if info and info.get("installdir", "").casefold() == root.name.casefold():
            return info
    return None
