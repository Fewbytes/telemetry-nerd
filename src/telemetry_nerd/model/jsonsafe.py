"""Strict JSON: NaN and +-Inf are not JSON, so they become null on the way out."""

from __future__ import annotations

import json
import math
from typing import Any


def finite(obj: Any) -> Any:
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [finite(v) for v in obj]
    return obj


def dumps(obj: Any, **kwargs: Any) -> str:
    return json.dumps(finite(obj), allow_nan=False, **kwargs)
