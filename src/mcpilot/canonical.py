"""RFC 8785 JSON Canonicalization Scheme, byte-identical with the TypeScript SDK.

Used for manifest fingerprints and credential keys, so a Python and a
TypeScript host sharing one policy or secret store derive the same values.
"""

from __future__ import annotations

import hashlib
import math
from decimal import Decimal
from typing import Any

_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}
_MAX_SAFE_INTEGER = 2**53 - 1


def _string(value: str) -> str:
    out = []
    for char in value:
        if char in _ESCAPES:
            out.append(_ESCAPES[char])
        elif ord(char) < 0x20:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def _number(value: int | float) -> str:
    """ECMAScript Number::toString of the shortest round-trip digits."""
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INTEGER:
            raise ValueError("Integer outside the interoperable JSON range")
        return str(value)
    if not math.isfinite(value):
        raise ValueError("JSON cannot represent NaN or infinity")
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    _, digits, exponent = Decimal(repr(abs(value))).as_tuple()
    digits = list(digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    s = "".join(map(str, digits))
    k, n = len(s), exponent + len(s)
    if k <= n <= 21:
        text = s + "0" * (n - k)
    elif 0 < n <= 21:
        text = s[:n] + "." + s[n:]
    elif -6 < n <= 0:
        text = "0." + "0" * -n + s
    else:
        e = n - 1
        text = (s if k == 1 else s[0] + "." + s[1:]) + "e" + ("+" if e > 0 else "-") + str(abs(e))
    return sign + text


def canonical_json(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return _number(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        keys = sorted(value, key=lambda key: key.encode("utf-16-be"))
        return "{" + ",".join(_string(key) + ":" + canonical_json(value[key]) for key in keys) + "}"
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
