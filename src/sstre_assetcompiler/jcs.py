# SPDX-FileCopyrightText: 2026 Handelsonderneming S.D. Broekhoven, trading as ITEagle Europe
# SPDX-License-Identifier: GPL-3.0-only
"""Canonical JSON (RFC 8785 JSON Canonicalization Scheme) and content hashes.

Implemented from the RFC text (https://www.rfc-editor.org/rfc/rfc8785):

- object members sorted by the UTF-16 code units of their names;
- no insignificant whitespace; UTF-8 output; strings escape only ``"``, ``\\``
  and control characters (``\\b \\f \\n \\r \\t`` or ``\\u00xx``);
- numbers in the ECMAScript ``Number.prototype.toString`` form (shortest
  round-trip digits); NaN, Infinity and lone surrogates are errors;
- integers must lie within +/-2**53 (I-JSON), so they survive a double.

Hashes are written ``sha256:<64 lowercase hex>`` (SSTRE RD-004 section 5).
"""

from __future__ import annotations

import hashlib
import math
import re

_MAX_SAFE = 2 ** 53
_ESC = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
# characters that need escaping or are errors (lone surrogates)
_SPECIAL = re.compile("[\"\\\\\x00-\x1f\ud800-\udfff]")


class CanonicalizationError(ValueError):
    pass


def _number(x: float) -> str:
    if math.isnan(x) or math.isinf(x):
        raise CanonicalizationError("NaN and Infinity are not allowed in canonical JSON")
    if x == 0:
        return "0"
    sign = "-" if x < 0 else ""
    r = repr(abs(x))  # shortest round-trip digits (Python >= 3.1)
    if "e" in r:
        mant, exp = r.split("e")
        exp_i = int(exp)
    else:
        mant, exp_i = r, 0
    if "." in mant:
        ip, fp = mant.split(".")
    else:
        ip, fp = mant, ""
    all_digits = ip + fp
    lead = len(all_digits) - len(all_digits.lstrip("0"))
    # value = 0.<digits> * 10^n
    n = len(ip) + exp_i - lead
    digits = all_digits.lstrip("0").rstrip("0") or "0"
    k = len(digits)
    if k <= n <= 21:
        out = digits + "0" * (n - k)
    elif 0 < n <= 21:
        out = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        out = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        es = ("+" if e >= 0 else "-") + str(abs(e))
        out = digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + es
    return sign + out


def _string(s: str) -> str:
    if not _SPECIAL.search(s):
        return '"' + s + '"'
    out = ['"']
    for ch in s:
        o = ord(ch)
        if 0xD800 <= o <= 0xDFFF:
            raise CanonicalizationError("lone surrogate in string")
        if ch in _ESC:
            out.append(_ESC[ch])
        elif o < 0x20:
            out.append(f"\\u{o:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _utf16_key(s: str) -> bytes:
    return s.encode("utf-16-be", "surrogatepass")


def _ser(v, out: list[str]) -> None:
    if v is None:
        out.append("null")
    elif v is True:
        out.append("true")
    elif v is False:
        out.append("false")
    elif isinstance(v, int):
        if abs(v) > _MAX_SAFE:
            raise CanonicalizationError(f"integer {v} outside +/-2^53")
        out.append(str(v))
    elif isinstance(v, float):
        out.append(_number(v))
    elif isinstance(v, str):
        out.append(_string(v))
    elif isinstance(v, (list, tuple)):
        out.append("[")
        for i, x in enumerate(v):
            if i:
                out.append(",")
            _ser(x, out)
        out.append("]")
    elif isinstance(v, dict):
        for k in v:
            if not isinstance(k, str):
                raise CanonicalizationError(f"object key {k!r} is not a string")
        out.append("{")
        for i, k in enumerate(sorted(v, key=_utf16_key)):
            if i:
                out.append(",")
            out.append(_string(k))
            out.append(":")
            _ser(v[k], out)
        out.append("}")
    else:
        raise CanonicalizationError(f"type {type(v).__name__} is not JSON")


def canonicalize(value) -> bytes:
    """RFC 8785 canonical UTF-8 bytes of ``value``."""
    out: list[str] = []
    _ser(value, out)
    return "".join(out).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash(value) -> str:
    """``sha256:<hex>`` of the canonical form."""
    return "sha256:" + sha256_hex(canonicalize(value))


def file_hash(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def dumps_pretty(value) -> str:
    """Human-readable form with the same member order as the canonical form,
    UTF-8, LF line ends and a final newline. Hashes are always computed over
    :func:`canonicalize`, never over this text."""
    import json

    def order(v):
        if isinstance(v, dict):
            return {k: order(v[k]) for k in sorted(v, key=_utf16_key)}
        if isinstance(v, list):
            return [order(x) for x in v]
        return v
    canonicalize(value)  # raise on non-canonicalizable input
    return json.dumps(order(value), indent=1, ensure_ascii=False, allow_nan=False) + "\n"
