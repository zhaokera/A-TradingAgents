from app.routers.holdings import _build_settings_payload


def test_holding_settings_defaults_total_assets_to_holding_cost():
    result = _build_settings_payload(None, total_holding_cost=6400.0)

    assert result["total_assets"] == 6400.0
    assert result["configured_total_assets"] is None
    assert result["is_auto_total_assets"] is True


def test_holding_settings_uses_configured_total_assets():
    result = _build_settings_payload(
        {"total_assets": 10000.0, "updated_at": "2026-06-04T00:00:00"},
        total_holding_cost=6400.0,
    )

    assert result["total_assets"] == 10000.0
    assert result["configured_total_assets"] == 10000.0
    assert result["is_auto_total_assets"] is False
