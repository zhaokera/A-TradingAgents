from collections.abc import Mapping
from copy import deepcopy

from app.services.research_only_safety import (
    FALSE_KEYS,
    ZERO_KEYS,
    enforce_research_only_safety,
)


EXPECTED_FALSE_KEYS = {
    "actionable",
    "reference_actionable",
    "new_position_allowed",
}
EXPECTED_ZERO_KEYS = {
    "suggested_lots",
    "suggested_quantity",
    "new_position_lots",
    "new_position_quantity",
    "max_new_exposure_amount",
    "max_new_exposure_pct",
    "external_new_exposure_amount",
    "market_adjusted_new_exposure_cap",
}


class CustomMapping(Mapping):
    def __init__(self, values):
        self._values = values

    def __getitem__(self, key):
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)


def _assert_all_research_only(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in EXPECTED_FALSE_KEYS:
                assert nested is False
            elif key in EXPECTED_ZERO_KEYS:
                assert nested == 0
            else:
                _assert_all_research_only(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _assert_all_research_only(nested)


def test_research_only_safety_rewrites_all_exact_keys_in_malicious_nesting():
    payload = {
        "actionable": True,
        "candidate": {
            "reference_actionable": True,
            "suggested_lots": 7,
            "nested": [
                {
                    "new_position_allowed": True,
                    "new_position_quantity": 900,
                },
                (
                    {
                        "suggested_quantity": 300,
                        "new_position_lots": 3,
                    },
                    {
                        "max_new_exposure_amount": 250_000.0,
                        "max_new_exposure_pct": 42.5,
                        "external_new_exposure_amount": 100_000.0,
                        "market_adjusted_new_exposure_cap": 80_000.0,
                    },
                ),
            ],
        },
    }

    safe_payload = enforce_research_only_safety(payload)

    _assert_all_research_only(safe_payload)


def test_research_only_safety_is_non_mutating_and_rebuilds_containers():
    payload = {
        "actionable": True,
        "items": [
            {
                "suggested_lots": 2,
                "tuple_value": ({"new_position_quantity": 200},),
            }
        ],
    }
    original = deepcopy(payload)

    safe_payload = enforce_research_only_safety(payload)

    assert payload == original
    assert safe_payload is not payload
    assert safe_payload["items"] is not payload["items"]
    assert safe_payload["items"][0] is not payload["items"][0]
    assert safe_payload["items"][0]["tuple_value"] is not payload["items"][0][
        "tuple_value"
    ]


def test_research_only_safety_matches_only_the_explicit_case_sensitive_keys():
    payload = {
        "Actionable": True,
        "is_actionable": True,
        "actionable_reason": True,
        "reference_actionable_status": True,
        "new_position_allowed_reason": True,
        "suggested_quantity_text": 500,
        "quote_quantity": 88_000,
        "historical_quantity": 77_000,
        "max_exposure_amount": 123_456.0,
        "data_complete": True,
        "gate_evaluated": True,
        "ordinary_boolean": True,
    }

    assert enforce_research_only_safety(payload) == payload


def test_research_only_safety_preserves_list_and_tuple_types():
    payload = [
        {"actionable": True},
        ({"suggested_lots": 4}, [{"reference_actionable": True}]),
    ]

    safe_payload = enforce_research_only_safety(payload)

    assert isinstance(safe_payload, list)
    assert isinstance(safe_payload[1], tuple)
    assert isinstance(safe_payload[1][1], list)
    _assert_all_research_only(safe_payload)


def test_research_only_safety_accepts_custom_mapping_and_returns_plain_dict():
    payload = CustomMapping(
        {
            "actionable": True,
            "nested": CustomMapping(
                {
                    "suggested_quantity": 300,
                    "ordinary_value": "preserved",
                }
            ),
        }
    )

    safe_payload = enforce_research_only_safety(payload)

    assert type(safe_payload) is dict
    assert type(safe_payload["nested"]) is dict
    assert safe_payload == {
        "actionable": False,
        "nested": {
            "suggested_quantity": 0,
            "ordinary_value": "preserved",
        },
    }
    assert payload["actionable"] is True
    assert payload["nested"]["suggested_quantity"] == 300


def test_research_only_safety_preserves_market_values_sources_and_amounts():
    payload = {
        "quote": {
            "price": 12.34,
            "open": 12.0,
            "high": 12.8,
            "low": 11.9,
            "amount": 987_654_321.0,
            "volume": 12_345_600,
            "quote_volume": 7_654_300,
            "source": "tencent",
        },
        "history": {
            "historical_volume": [1_000_000, 2_000_000],
            "historical_amount": 456_789_000.0,
            "data_source": "tencent_daily_bars",
        },
        "account": {
            "existing_position_amount": 50_000.0,
            "configured_total_assets": 200_000.0,
        },
    }

    assert enforce_research_only_safety(payload) == payload


def test_research_only_safety_exports_the_contract_key_sets():
    assert FALSE_KEYS == EXPECTED_FALSE_KEYS
    assert ZERO_KEYS == EXPECTED_ZERO_KEYS
