"""Canonical A-share board classification and restricted-market permissions."""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional


STAR_BOARD = "STAR"
BSE_BOARD = "BSE"
CHINEXT_BOARD = "CHINEXT"
A_SHARE_BOARD = "A_SHARE"

STAR_PERMISSION_KEY = "star_market"
BSE_PERMISSION_KEY = "beijing_stock_exchange"
CHINEXT_PERMISSION_KEY = "chi_next_market"

_BSE_PREFIXES = ("43", "83", "87", "88", "92")
_RESTRICTED_PERMISSION_BY_BOARD = {
    STAR_BOARD: STAR_PERMISSION_KEY,
    BSE_BOARD: BSE_PERMISSION_KEY,
    CHINEXT_BOARD: CHINEXT_PERMISSION_KEY,
}
_CODE_PATTERNS = (
    re.compile(r"(?:SH|SZ|BJ)\.?([0-9]{1,6})", re.IGNORECASE),
    re.compile(r"([0-9]{1,6})\.(?:SH|SZ|BJ)", re.IGNORECASE),
    re.compile(r"([0-9]{1,6})"),
)


def normalize_a_share_code(value: Any) -> str:
    text = str(value or "").strip()
    for pattern in _CODE_PATTERNS:
        match = pattern.fullmatch(text)
        if match:
            return match.group(1).zfill(6)
    return text.upper()


def classify_a_share_board(value: Any) -> Dict[str, Optional[str]]:
    code = normalize_a_share_code(value)
    if code.startswith(("688", "689")):
        board = STAR_BOARD
    elif code.startswith(_BSE_PREFIXES):
        board = BSE_BOARD
    elif code.isdigit() and 300000 <= int(code) <= 309999:
        board = CHINEXT_BOARD
    else:
        board = A_SHARE_BOARD
    return {
        "code": code,
        "board": board,
        "permission_key": _RESTRICTED_PERMISSION_BY_BOARD.get(board),
    }


def _normalize_permission_entry(value: Any) -> Dict[str, Any]:
    entry = value if isinstance(value, Mapping) else {}
    verified = entry.get("verified") is True
    tradable = entry.get("tradable") is True
    eligible = verified and tradable
    result = {
        "verified": verified,
        "tradable": tradable,
        "eligible": eligible,
        "reason_code": (
            None
            if eligible
            else "permission_denied"
            if verified
            else "permission_unverified"
        ),
    }
    for field in ("source", "updated_by", "updated_at"):
        if entry.get(field) not in (None, ""):
            result[field] = entry[field]
    return result


def normalize_market_permissions(value: Any) -> Dict[str, Dict[str, Any]]:
    permissions = value if isinstance(value, Mapping) else {}
    return {
        STAR_PERMISSION_KEY: _normalize_permission_entry(
            permissions.get(STAR_PERMISSION_KEY)
        ),
        BSE_PERMISSION_KEY: _normalize_permission_entry(
            permissions.get(BSE_PERMISSION_KEY)
        ),
        CHINEXT_PERMISSION_KEY: _normalize_permission_entry(
            permissions.get(CHINEXT_PERMISSION_KEY)
        ),
    }


def permission_for_code(
    code: Any,
    market_permissions: Any,
) -> Dict[str, Any]:
    classification = classify_a_share_board(code)
    permission_key = classification["permission_key"]
    if permission_key is None:
        return {
            **classification,
            "verified": True,
            "tradable": True,
            "eligible": True,
            "reason_code": None,
            "exclusion_reason_code": None,
        }
    permissions = normalize_market_permissions(market_permissions)
    permission = permissions[permission_key]
    reason_code = permission["reason_code"]
    return {
        **classification,
        **permission,
        "exclusion_reason_code": (
            f"{permission_key}_{reason_code}" if reason_code else None
        ),
    }


def board_exclusion_reasons(
    market_permissions: Any,
) -> Dict[str, str]:
    permissions = normalize_market_permissions(market_permissions)
    reasons: Dict[str, str] = {}
    for board, permission_key in _RESTRICTED_PERMISSION_BY_BOARD.items():
        permission = permissions[permission_key]
        if permission["eligible"]:
            continue
        reasons[board] = (
            f"{permission_key}_{permission['reason_code']}"
        )
    return reasons
