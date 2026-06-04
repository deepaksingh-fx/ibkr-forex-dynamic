"""
Customizable config for the coil-index session strategy: session windows, the
per-session pair lists, and the ADX/coil thresholds/periods. Loadable from JSON
(see coil_config.json) so everything is a param, not hard-code.

Falls back to the built-in defaults (sessions.SESSIONS + CoilParams()) when no
file is given or a key is omitted.
"""
from __future__ import annotations

import json
from typing import Optional, Tuple

from coil_selection import CoilParams
from sessions import SESSIONS as DEFAULT_SESSIONS
from sessions import Session, build_sessions


# Price feed the ADX/CPR are computed on. "midpoint" = IDEALPRO MIDPOINT spot
# (default; CFD contracts serve no historical bars on this account).
DEFAULT_FEED = "midpoint"


def load_coil_config(path: Optional[str]) -> Tuple[Tuple[Session, ...], CoilParams, str]:
    """
    Return (sessions, params, feed). With path=None, returns the built-in defaults.
    JSON shape:
        {"feed": "cfd" | "midpoint",
         "sessions": [{"name","start","end","pairs":[...]}, ...],
         "params": {"adx_filter_max":..., "adx_coil_max":..., ...}}
    All top-level keys are optional.
    """
    if not path:
        return DEFAULT_SESSIONS, CoilParams(), DEFAULT_FEED
    with open(path, "r") as f:
        data = json.load(f)
    sessions = build_sessions(data["sessions"]) if data.get("sessions") else DEFAULT_SESSIONS
    params = CoilParams(**data.get("params", {}))
    feed = data.get("feed", DEFAULT_FEED)
    if feed not in ("cfd", "midpoint"):
        raise ValueError(f"feed must be 'cfd' or 'midpoint', got {feed!r}")
    return sessions, params, feed
