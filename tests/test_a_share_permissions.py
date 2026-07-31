import pytest

from app.services.a_share_permissions import (
    classify_a_share_board,
    normalize_market_permissions,
    permission_for_code,
)


@pytest.mark.parametrize(
    ("code", "board", "permission_key"),
    [
        ("688208", "STAR", "star_market"),
        ("689009", "STAR", "star_market"),
        ("430047", "BSE", "beijing_stock_exchange"),
        ("830799", "BSE", "beijing_stock_exchange"),
        ("873001", "BSE", "beijing_stock_exchange"),
        ("889999", "BSE", "beijing_stock_exchange"),
        ("920493", "BSE", "beijing_stock_exchange"),
        ("300000", "CHINEXT", "chi_next_market"),
        ("300450", "CHINEXT", "chi_next_market"),
        ("301999", "CHINEXT", "chi_next_market"),
        ("309799", "CHINEXT", "chi_next_market"),
        ("309800", "CHINEXT", "chi_next_market"),
        ("309999", "CHINEXT", "chi_next_market"),
        ("600562", "A_SHARE", None),
        ("002979", "A_SHARE", None),
        ("310000", "A_SHARE", None),
    ],
)
def test_classify_a_share_board(code, board, permission_key):
    result = classify_a_share_board(code)

    assert result["code"] == code
    assert result["board"] == board
    assert result["permission_key"] == permission_key


def test_missing_beijing_permission_is_fail_closed():
    permissions = normalize_market_permissions(
        {
            "star_market": {"verified": True, "tradable": False},
        }
    )

    assert permissions["star_market"] == {
        "verified": True,
        "tradable": False,
        "eligible": False,
        "reason_code": "permission_denied",
    }
    assert permissions["beijing_stock_exchange"] == {
        "verified": False,
        "tradable": False,
        "eligible": False,
        "reason_code": "permission_unverified",
    }
    assert permissions["chi_next_market"] == {
        "verified": False,
        "tradable": False,
        "eligible": False,
        "reason_code": "permission_unverified",
    }
    assert permission_for_code("920493", permissions) == {
        "code": "920493",
        "board": "BSE",
        "permission_key": "beijing_stock_exchange",
        "verified": False,
        "tradable": False,
        "eligible": False,
        "reason_code": "permission_unverified",
        "exclusion_reason_code": (
            "beijing_stock_exchange_permission_unverified"
        ),
    }


@pytest.mark.parametrize(
    ("entry", "eligible", "reason_code", "exclusion_reason_code"),
    [
        (
            {"verified": True, "tradable": True},
            True,
            None,
            None,
        ),
        (
            {"verified": True, "tradable": False},
            False,
            "permission_denied",
            "beijing_stock_exchange_permission_denied",
        ),
        (
            {"verified": False, "tradable": True},
            False,
            "permission_unverified",
            "beijing_stock_exchange_permission_unverified",
        ),
    ],
)
def test_beijing_permission_states(
    entry,
    eligible,
    reason_code,
    exclusion_reason_code,
):
    permissions = normalize_market_permissions(
        {"beijing_stock_exchange": entry}
    )
    result = permission_for_code("920493", permissions)

    assert result["eligible"] is eligible
    assert result["reason_code"] == reason_code
    assert result["exclusion_reason_code"] == exclusion_reason_code


@pytest.mark.parametrize(
    ("entry", "eligible", "reason_code", "exclusion_reason_code"),
    [
        (
            {"verified": True, "tradable": True},
            True,
            None,
            None,
        ),
        (
            {"verified": True, "tradable": False},
            False,
            "permission_denied",
            "chi_next_market_permission_denied",
        ),
        (
            {"verified": False, "tradable": False},
            False,
            "permission_unverified",
            "chi_next_market_permission_unverified",
        ),
    ],
)
def test_chinext_permission_states(
    entry,
    eligible,
    reason_code,
    exclusion_reason_code,
):
    permissions = normalize_market_permissions({"chi_next_market": entry})
    result = permission_for_code("300450", permissions)

    assert result["board"] == "CHINEXT"
    assert result["permission_key"] == "chi_next_market"
    assert result["eligible"] is eligible
    assert result["reason_code"] == reason_code
    assert result["exclusion_reason_code"] == exclusion_reason_code
