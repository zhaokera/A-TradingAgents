from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import app.services.a_share_calendar_service as calendar_module
from app.core.config import Settings
from app.services.a_share_calendar_service import AShareCalendarService
from app.services.market_session_policy_service import MarketSessionPolicyService


SHANGHAI = ZoneInfo("Asia/Shanghai")


class StubCalendar:
    def __init__(self, result):
        self.result = result

    async def is_trading_day(self, value=None):
        return {**self.result, "date": (value or date(2026, 7, 22)).isoformat()}


class RaisingCalendar:
    async def is_trading_day(self, value=None):
        raise RuntimeError("calendar unavailable")


class FakeCollection:
    def __init__(self, cached=None):
        self.cached = cached
        self.bulk_operations = None

    async def find_one(self, query):
        return self.cached if self.cached and self.cached.get("_id") == query["_id"] else None

    async def bulk_write(self, operations, ordered=False):
        self.bulk_operations = list(operations)


class FakeDatabase:
    def __init__(self, collection):
        self.collection = collection

    def __getitem__(self, name):
        assert name == "a_share_trade_calendar"
        return self.collection


def authoritative_calendar(*, is_trading_day=True):
    return {
        "is_trading_day": is_trading_day,
        "status": "verified",
        "source": "akshare_trade_calendar",
        "verified_at": "2026-07-22T00:00:00+00:00",
        "authoritative": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("clock", "phase", "buy_now_allowed", "freshness_seconds"),
    [
        ("09:29:59", "pre_open", False, 0),
        ("09:30:00", "live_am", True, 90),
        ("11:30:00", "midday_break", False, 0),
        ("13:00:00", "live_pm", True, 90),
        ("15:00:00", "post_close", False, 0),
    ],
)
async def test_session_boundaries(clock, phase, buy_now_allowed, freshness_seconds):
    policy = MarketSessionPolicyService(calendar=StubCalendar(authoritative_calendar()))
    now = datetime.fromisoformat(f"2026-07-22T{clock}+08:00")

    result = await policy.classify(now=now)

    assert result["phase"] == phase
    assert result["is_trading_day"] is True
    assert result["buy_now_allowed"] is buy_now_allowed
    assert result["quote_freshness_required_seconds"] == freshness_seconds


@pytest.mark.asyncio
async def test_non_trading_date_is_closed_day():
    policy = MarketSessionPolicyService(
        calendar=StubCalendar(authoritative_calendar(is_trading_day=False))
    )

    result = await policy.classify(
        now=datetime(2026, 7, 25, 10, 0, tzinfo=SHANGHAI)
    )

    assert result["phase"] == "closed_day"
    assert result["is_trading_day"] is False
    assert result["buy_now_allowed"] is False


@pytest.mark.asyncio
async def test_classify_converts_utc_to_shanghai_before_applying_boundaries():
    policy = MarketSessionPolicyService(calendar=StubCalendar(authoritative_calendar()))

    result = await policy.classify(
        now=datetime(2026, 7, 22, 1, 30, tzinfo=timezone.utc)
    )

    assert result["phase"] == "live_am"
    assert result["classified_at"] == "2026-07-22T09:30:00+08:00"


@pytest.mark.asyncio
async def test_live_quote_requires_tencent_same_trade_date_and_at_most_90_seconds_old():
    policy = MarketSessionPolicyService(calendar=StubCalendar(authoritative_calendar()))
    now = datetime(2026, 7, 22, 10, 0, tzinfo=SHANGHAI)

    exactly_90_seconds = await policy.quote_status(
        {"source": "tencent", "trade_at": "2026-07-22T09:58:30+08:00"},
        now=now,
    )
    stale_exchange_time = await policy.quote_status(
        {
            "source": "tencent",
            "trade_at": "2026-07-22T09:58:29+08:00",
            "quote_checked_at": "2026-07-22T10:00:00+08:00",
        },
        now=now,
    )
    wrong_source = await policy.quote_status(
        {"source": "akshare", "trade_at": "2026-07-22T10:00:00+08:00"},
        now=now,
    )
    previous_trade_date = await policy.quote_status(
        {"source": "tencent", "trade_at": "2026-07-21T15:00:00+08:00"},
        now=now,
    )

    assert exactly_90_seconds["actionable"] is True
    assert exactly_90_seconds["status"] == "fresh"
    assert exactly_90_seconds["age_seconds"] == 90
    assert stale_exchange_time["actionable"] is False
    assert stale_exchange_time["status"] == "stale_trade_at"
    assert wrong_source["status"] == "unsupported_source"
    assert previous_trade_date["status"] == "wrong_trade_date"


@pytest.mark.asyncio
async def test_live_candidate_quote_requires_recent_observed_market_event():
    policy = MarketSessionPolicyService(calendar=StubCalendar(authoritative_calendar()))
    now = datetime(2026, 7, 22, 10, 0, tzinfo=SHANGHAI)

    missing_confirmation = await policy.quote_status(
        {
            "source": "tencent",
            "trade_at": "2026-07-22T10:00:00+08:00",
            "event_confirmation_required": True,
            "event_observed_at": None,
        },
        now=now,
    )
    recent_confirmation = await policy.quote_status(
        {
            "source": "tencent",
            "trade_at": "2026-07-22T10:00:00+08:00",
            "event_confirmation_required": True,
            "event_observed_at": "2026-07-22T09:59:30+08:00",
        },
        now=now,
    )
    stale_confirmation = await policy.quote_status(
        {
            "source": "tencent",
            "trade_at": "2026-07-22T10:00:00+08:00",
            "event_confirmation_required": True,
            "event_observed_at": "2026-07-22T09:58:29+08:00",
        },
        now=now,
    )

    assert missing_confirmation["actionable"] is False
    assert missing_confirmation["status"] == "missing_event_confirmation"
    assert recent_confirmation["actionable"] is True
    assert recent_confirmation["status"] == "fresh"
    assert recent_confirmation["event_age_seconds"] == 30
    assert stale_confirmation["actionable"] is False
    assert stale_confirmation["status"] == "stale_event_confirmation"


@pytest.mark.asyncio
async def test_event_confirmation_allows_only_small_same_request_clock_skew():
    policy = MarketSessionPolicyService(calendar=StubCalendar(authoritative_calendar()))
    now = datetime(2026, 7, 22, 10, 0, tzinfo=SHANGHAI)

    same_request = await policy.quote_status(
        {
            "source": "tencent",
            "trade_at": "2026-07-22T10:00:00+08:00",
            "event_confirmation_required": True,
            "event_observed_at": "2026-07-22T10:00:05+08:00",
        },
        now=now,
    )
    future_event = await policy.quote_status(
        {
            "source": "tencent",
            "trade_at": "2026-07-22T10:00:00+08:00",
            "event_confirmation_required": True,
            "event_observed_at": "2026-07-22T10:00:06+08:00",
        },
        now=now,
    )

    assert same_request["actionable"] is True
    assert same_request["status"] == "fresh"
    assert same_request["event_age_seconds"] == 0
    assert future_event["actionable"] is False
    assert future_event["status"] == "future_event_confirmation"


@pytest.mark.asyncio
async def test_quote_status_preserves_session_subsecond_precision_for_freshness():
    policy = MarketSessionPolicyService(calendar=StubCalendar(authoritative_calendar()))
    session = await policy.classify(
        now=datetime(2026, 7, 22, 10, 0, 0, 900000, tzinfo=SHANGHAI)
    )

    result = await policy.quote_status(
        {"source": "tencent", "trade_at": "2026-07-22T09:58:30+08:00"},
        session=session,
    )

    assert session["classified_at"] == "2026-07-22T10:00:00.900000+08:00"
    assert result["age_seconds"] == pytest.approx(90.9)
    assert result["actionable"] is False
    assert result["status"] == "stale_trade_at"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("clock", "status"),
    [("11:30:00", "not_live_session"), ("15:00:00", "final_close_observation")],
)
async def test_non_live_sessions_never_make_a_quote_actionable(clock, status):
    policy = MarketSessionPolicyService(calendar=StubCalendar(authoritative_calendar()))
    now = datetime.fromisoformat(f"2026-07-22T{clock}+08:00")

    result = await policy.quote_status(
        {"source": "tencent", "trade_at": now.isoformat()},
        now=now,
    )

    assert result["actionable"] is False
    assert result["status"] == status


@pytest.mark.asyncio
async def test_post_close_identifies_only_same_day_tencent_final_close_observation():
    policy = MarketSessionPolicyService(calendar=StubCalendar(authoritative_calendar()))
    now = datetime(2026, 7, 22, 15, 0, tzinfo=SHANGHAI)
    session = await policy.classify(now=now)

    final_close = await policy.quote_status(
        {"source": "tencent", "trade_at": "2026-07-22T15:00:00+08:00"},
        session=session,
    )
    older_quote = await policy.quote_status(
        {"source": "tencent", "trade_at": "2026-07-22T14:59:59+08:00"},
        session=session,
    )

    assert final_close["actionable"] is False
    assert final_close["status"] == "final_close_observation"
    assert older_quote["actionable"] is False
    assert older_quote["status"] == "not_live_session"


@pytest.mark.asyncio
async def test_calendar_fallback_is_calendar_unknown_and_fail_closed():
    collection = FakeCollection()
    calendar = AShareCalendarService(cache_max_age_hours=168)
    calendar.db = FakeDatabase(collection)

    def fail_fetch():
        raise RuntimeError("calendar provider unavailable")

    calendar._fetch_dates = fail_fetch
    result = await calendar.is_trading_day(date(2026, 7, 22))
    classification = await MarketSessionPolicyService(calendar=calendar).classify(
        now=datetime(2026, 7, 22, 10, 0, tzinfo=SHANGHAI)
    )

    assert result["is_trading_day"] is True
    assert result["source"] == "weekday_fallback"
    assert result["verified_at"] is None
    assert result["authoritative"] is False
    assert classification["phase"] == "calendar_unknown"
    assert classification["buy_now_allowed"] is False


@pytest.mark.asyncio
async def test_calendar_error_is_calendar_unknown_and_fail_closed():
    result = await MarketSessionPolicyService(calendar=RaisingCalendar()).classify(
        now=datetime(2026, 7, 22, 10, 0, tzinfo=SHANGHAI)
    )

    assert result["phase"] == "calendar_unknown"
    assert result["calendar_authoritative"] is False
    assert result["calendar"]["source"] == "calendar_unavailable"
    assert result["calendar"]["reason"] == "calendar unavailable"


@pytest.mark.asyncio
async def test_calendar_cache_older_than_configured_168_hours_is_unknown():
    stale_verified_at = datetime.now(timezone.utc) - timedelta(hours=168, seconds=1)
    collection = FakeCollection(
        {
            "_id": "2026-07-22",
            "is_trading_day": True,
            "source": "akshare_trade_calendar",
            "updated_at": stale_verified_at,
        }
    )
    calendar = AShareCalendarService(cache_max_age_hours=168)
    calendar.db = FakeDatabase(collection)

    def fail_fetch():
        raise RuntimeError("calendar provider unavailable")

    calendar._fetch_dates = fail_fetch
    result = await calendar.is_trading_day(date(2026, 7, 22))
    classification = await MarketSessionPolicyService(calendar=calendar).classify(
        now=datetime(2026, 7, 22, 10, 0, tzinfo=SHANGHAI)
    )

    assert result["is_trading_day"] is True
    assert result["source"] == "akshare_trade_calendar"
    assert result["verified_at"] == stale_verified_at.isoformat()
    assert result["authoritative"] is False
    assert classification["phase"] == "calendar_unknown"


@pytest.mark.asyncio
async def test_incomplete_future_provider_coverage_is_not_authoritative_or_persisted():
    collection = FakeCollection()
    calendar = AShareCalendarService(cache_max_age_hours=168)
    calendar.db = FakeDatabase(collection)
    calendar._fetch_dates = lambda: ["2026-01-05", "2026-07-21"]

    result = await calendar.is_trading_day(date(2026, 7, 22))

    assert result["is_trading_day"] is True
    assert result["status"] == "degraded"
    assert result["source"] == "weekday_fallback"
    assert result["verified_at"] is None
    assert result["authoritative"] is False
    assert result["reason"] == "target_outside_provider_range"
    assert result["coverage_start"] == "2026-01-05"
    assert result["coverage_end"] == "2026-07-21"
    assert collection.bulk_operations is None


@pytest.mark.asyncio
async def test_successful_provider_ingestion_is_authoritative_within_coverage():
    collection = FakeCollection()
    calendar = AShareCalendarService(cache_max_age_hours=168)
    calendar.db = FakeDatabase(collection)
    calendar._fetch_dates = lambda: ["2026-07-20", "2026-07-22"]

    result = await calendar.is_trading_day(date(2026, 7, 21))

    assert result["is_trading_day"] is False
    assert result["status"] == "verified"
    assert result["source"] == "akshare_trade_calendar"
    assert result["verified_at"] is not None
    assert result["authoritative"] is True
    assert result["coverage_start"] == "2026-07-20"
    assert result["coverage_end"] == "2026-07-22"
    assert len(collection.bulk_operations) == 3


@pytest.mark.asyncio
async def test_recent_verified_calendar_cache_is_authoritative():
    verified_at = datetime.now(timezone.utc) - timedelta(hours=1)
    calendar = AShareCalendarService(cache_max_age_hours=168)
    calendar.db = FakeDatabase(
        FakeCollection(
            {
                "_id": "2026-07-22",
                "is_trading_day": True,
                "source": "akshare_trade_calendar",
                "updated_at": verified_at,
                "coverage_start": "2026-01-05",
                "coverage_end": "2026-12-31",
            }
        )
    )

    def unexpected_fetch():
        raise AssertionError("fresh verified cache should be used")

    calendar._fetch_dates = unexpected_fetch
    result = await calendar.is_trading_day(date(2026, 7, 22))

    assert result["status"] == "verified"
    assert result["source"] == "akshare_trade_calendar"
    assert result["verified_at"] == verified_at.isoformat()
    assert result["authoritative"] is True


@pytest.mark.asyncio
async def test_cached_closure_outside_provider_coverage_is_not_authoritative():
    calendar = AShareCalendarService(cache_max_age_hours=168)
    calendar.db = FakeDatabase(
        FakeCollection(
            {
                "_id": "2026-07-22",
                "is_trading_day": False,
                "source": "akshare_trade_calendar",
                "updated_at": datetime.now(timezone.utc),
                "coverage_start": "2026-01-05",
                "coverage_end": "2026-07-21",
            }
        )
    )

    def fail_fetch():
        raise RuntimeError("calendar provider unavailable")

    calendar._fetch_dates = fail_fetch
    result = await calendar.is_trading_day(date(2026, 7, 22))

    assert result["status"] == "stale"
    assert result["is_trading_day"] is False
    assert result["authoritative"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cached_fields",
    [{}, {"is_trading_day": "false"}],
    ids=["missing", "string"],
)
async def test_cached_trading_day_requires_an_actual_boolean(cached_fields):
    cached = {
        "_id": "2026-07-22",
        "source": "akshare_trade_calendar",
        "updated_at": datetime.now(timezone.utc),
        "coverage_start": "2026-01-05",
        "coverage_end": "2026-12-31",
        **cached_fields,
    }
    calendar = AShareCalendarService(cache_max_age_hours=168)
    calendar.db = FakeDatabase(FakeCollection(cached))

    def fail_fetch():
        raise RuntimeError("calendar provider unavailable")

    calendar._fetch_dates = fail_fetch
    result = await calendar.is_trading_day(date(2026, 7, 22))

    assert result["status"] == "stale"
    assert result["is_trading_day"] is False
    assert result["authoritative"] is False


@pytest.mark.asyncio
async def test_weekday_fallback_cache_is_never_authoritative():
    calendar = AShareCalendarService(cache_max_age_hours=168)
    calendar.db = FakeDatabase(
        FakeCollection(
            {
                "_id": "2026-07-22",
                "is_trading_day": True,
                "source": "weekday_fallback",
                "updated_at": datetime.now(timezone.utc),
            }
        )
    )

    def fail_fetch():
        raise RuntimeError("calendar provider unavailable")

    calendar._fetch_dates = fail_fetch
    result = await calendar.is_trading_day(date(2026, 7, 22))

    assert result["status"] == "stale"
    assert result["source"] == "weekday_fallback"
    assert result["authoritative"] is False


@pytest.mark.asyncio
async def test_default_calendar_target_uses_shanghai_date(monkeypatch):
    real_datetime = datetime
    fixed_utc = real_datetime(2026, 7, 21, 16, 30, tzinfo=timezone.utc)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_utc.replace(tzinfo=None)
            return fixed_utc.astimezone(tz)

    monkeypatch.setattr(calendar_module, "datetime", FixedDateTime)
    collection = FakeCollection()
    calendar = AShareCalendarService(cache_max_age_hours=168)
    calendar.db = FakeDatabase(collection)
    calendar._fetch_dates = lambda: ["2026-07-21", "2026-07-22"]

    result = await calendar.is_trading_day()

    assert result["date"] == "2026-07-22"
    assert result["is_trading_day"] is True
    assert result["authoritative"] is True


def test_calendar_cache_max_age_defaults_to_168_hours():
    assert Settings.model_fields["A_SHARE_CALENDAR_CACHE_MAX_AGE_HOURS"].default == 168
