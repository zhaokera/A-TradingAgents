from app.services import candidate_research_pipeline


def test_candidate_research_forwards_checkpoint_and_progress_callback(monkeypatch):
    captured = {}
    checkpoint = {"version": 1, "batches": {"0": {"status": "completed"}}}
    callback = lambda _value: None

    def fake_research(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(
        "app.services.holdings_cli.run_public_full_market_research",
        fake_research,
    )

    result = candidate_research_pipeline.run_candidate_research(
        external_risk_level="yellow",
        excluded_code_reasons={"600406": "user_excluded"},
        board_exclusion_reasons={"STAR": "permission_denied"},
        research_progress_callback=callback,
        resume_checkpoint=checkpoint,
    )

    assert result == {"ok": True}
    assert captured["research_progress_callback"] is callback
    assert captured["resume_checkpoint"] is checkpoint
