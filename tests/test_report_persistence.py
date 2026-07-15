import asyncio
import sys
from datetime import datetime
from types import ModuleType, SimpleNamespace

from app.services.report_metadata import resolve_report_analysis_date


def test_resolve_report_analysis_date_preserves_backdated_result_session():
    assert resolve_report_analysis_date(
        {"analysis_date": "2026-07-08"},
        generated_at=datetime(2026, 7, 12, 10, 30),
    ) == "2026-07-08"


def test_resolve_report_analysis_date_accepts_datetime_and_falls_back_to_generation_date():
    assert resolve_report_analysis_date(
        {"analysis_date": datetime(2026, 7, 9, 15, 0)},
        generated_at=datetime(2026, 7, 12, 10, 30),
    ) == "2026-07-09"
    assert resolve_report_analysis_date(
        {"analysis_date": "bad-date"},
        generated_at=datetime(2026, 7, 12, 10, 30),
    ) == "2026-07-12"


def test_web_style_report_persistence_writes_result_session_to_analysis_reports(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOCKER_CONTAINER", "false")
    monkeypatch.setenv("TRADINGAGENTS_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("USE_MONGODB_STORAGE", "false")
    graph_module = ModuleType("tradingagents.graph.trading_graph")
    graph_module.TradingAgentsGraph = object
    monkeypatch.setitem(sys.modules, "tradingagents.graph.trading_graph", graph_module)
    import app.services.simple_analysis_service as service_module

    class FakeCollection:
        def __init__(self):
            self.inserted_documents = []
            self.updates = []

        async def insert_one(self, document):
            self.inserted_documents.append(document)
            return SimpleNamespace(inserted_id="report-id")

        async def update_one(self, query, update):
            self.updates.append((query, update))
            return SimpleNamespace(modified_count=1)

    reports = FakeCollection()
    tasks = FakeCollection()
    fake_db = SimpleNamespace(analysis_reports=reports, analysis_tasks=tasks)
    monkeypatch.setattr(service_module, "get_mongo_db", lambda: fake_db)
    service = object.__new__(service_module.SimpleAnalysisService)

    asyncio.run(
        service._save_analysis_result_web_style(
            "task-backdated",
            {
                "stock_symbol": "AAPL",
                "analysis_date": "2026-07-08",
                "summary": "Backdated market-session analysis",
                "decision": {"action": "hold"},
            },
        )
    )

    assert len(reports.inserted_documents) == 1
    persisted = reports.inserted_documents[0]
    assert persisted["analysis_date"] == "2026-07-08"
    assert persisted["created_at"].date() > datetime(2026, 7, 8).date()
    assert tasks.updates[0][0] == {"task_id": "task-backdated"}
    assert tasks.updates[0][1]["$set"]["result"]["analysis_date"] == "2026-07-08"
