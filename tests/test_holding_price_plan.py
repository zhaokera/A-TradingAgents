from app.routers.holdings import HoldingCreateRequest, HoldingUpdateRequest


def test_holding_create_accepts_manual_price_plan_fields():
    payload = HoldingCreateRequest(
        code="000977",
        quantity=100,
        cost_price=64,
        manual_stop_loss_price=58.8,
        manual_target_price=70.4,
        manual_sell_price=66.0,
        manual_buy_price=65.2,
        price_plan_notes="突破后追入，跌破止损",
    )

    dumped = payload.model_dump()

    assert dumped["manual_stop_loss_price"] == 58.8
    assert dumped["manual_target_price"] == 70.4
    assert dumped["manual_sell_price"] == 66.0
    assert dumped["manual_buy_price"] == 65.2
    assert dumped["price_plan_notes"] == "突破后追入，跌破止损"


def test_holding_update_accepts_partial_manual_price_plan_fields():
    payload = HoldingUpdateRequest(
        manual_sell_price=66.0,
        manual_buy_price=None,
        price_plan_notes="只更新计划，不改持仓",
    )

    dumped = payload.model_dump(exclude_unset=True)

    assert dumped == {
        "manual_sell_price": 66.0,
        "manual_buy_price": None,
        "price_plan_notes": "只更新计划，不改持仓",
    }
