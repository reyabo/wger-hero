"""The FitTrackee client against a mocked API.

No live instance is contacted. Every response here is hand-built to the shape
the official FitTrackee documentation describes, and the workout fixture is the
synthetic one from the specification — no real activity, no real route.

The two things worth breaking are pagination (a first page silently taken for
the whole collection loses history) and the 401 path (a refresh loop would
hammer the user's own server).
"""

import logging
from datetime import date, datetime

import httpx
import pytest

from app.fittrackee_client import (
    MAX_PER_PAGE,
    WORKOUT_FIELDS,
    FitTrackeeClient,
    FitTrackeeClientError,
    exchange_code_for_tokens,
    parse_duration,
    parse_workout_date,
    project_workout,
)
from app.fittrackee_oauth import FitTrackeeAuthError, TokenSet, TokenStore

BASE = "https://fittrackee.example.com"


def a_workout(external_id="fake-workout-abc", **extra):
    """The synthetic record from the specification."""
    payload = {
        "id": external_id,
        "sport_id": 5,
        "workout_date": "Mon, 17 Aug 2026 06:30:00 GMT",
        "duration": "0:42:10",
        "moving": "0:39:55",
        "distance": 7.2,
        "ave_speed": 10.8,
        "max_speed": 14.7,
        "ave_hr": 142,
        "max_hr": 167,
        "modification_date": None,
    }
    payload.update(extra)
    return payload


class MockTransport:
    """Records requests and replays queued responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._responses:
            return httpx.Response(200, json={"data": {"workouts": []}})
        item = self._responses.pop(0)
        return item(request) if callable(item) else item


@pytest.fixture
def mocked(monkeypatch):
    """Install a mock transport for every httpx.AsyncClient the code makes."""

    def install(responses):
        transport = MockTransport(responses)
        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(transport.handler)
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        return transport

    return install


def client_for(tokens=None, store=None):
    return FitTrackeeClient(
        BASE,
        tokens or TokenSet("access-token", "refresh-token"),
        client_id="cid",
        client_secret="csecret",
        store=store,
    )


def workouts_page(entries):
    return httpx.Response(200, json={"data": {"workouts": entries}})


# ---------------------------------------------------------------------------
# Field parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0:42:10", 2530),
        ("1:00:00", 3600),
        ("0:10:00", 600),
        ("10:00", 600),
        ("0:00:01", 1),
        (900, 900),
        ("900", 900),
    ],
)
def test_durations_parse(raw, expected):
    assert parse_duration(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "  ", "später", "abc", "1:2:3:4"])
def test_an_unreadable_duration_is_none(raw):
    assert parse_duration(raw) is None


def test_a_negative_duration_is_none():
    assert parse_duration(-5) is None


def test_the_rfc_1123_date_parses():
    assert parse_workout_date("Mon, 17 Aug 2026 06:30:00 GMT") == datetime(
        2026, 8, 17, 6, 30
    )


def test_an_iso_date_parses_too():
    assert parse_workout_date("2026-08-17T06:30:00Z") == datetime(2026, 8, 17, 6, 30)


def test_an_offset_is_converted_to_utc():
    assert parse_workout_date("2026-08-17T08:30:00+02:00") == datetime(2026, 8, 17, 6, 30)


@pytest.mark.parametrize("raw", [None, "", "irgendwann"])
def test_an_unreadable_date_is_none(raw):
    assert parse_workout_date(raw) is None


# ---------------------------------------------------------------------------
# The privacy boundary
# ---------------------------------------------------------------------------

def test_the_projection_keeps_only_the_documented_fields():
    assert set(project_workout(a_workout())) == set(WORKOUT_FIELDS)


@pytest.mark.parametrize(
    "private",
    ["notes", "title", "description", "map", "bounds", "segments", "records",
     "weather_start", "gpx", "map_visibility"],
)
def test_private_fields_are_dropped_at_the_boundary(private):
    """Dropped here, before anything can store, hash or log them."""
    projected = project_workout(a_workout(**{private: "etwas Privates"}))
    assert private not in projected
    assert "etwas Privates" not in str(projected)


@pytest.mark.asyncio
async def test_fetched_workouts_carry_no_private_fields(mocked):
    mocked([workouts_page([a_workout(notes="privat", map="track-1")])])
    result = await client_for().get_workouts()
    assert "notes" not in result[0]
    assert "map" not in result[0]


# ---------------------------------------------------------------------------
# Sports
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sports_are_read(mocked):
    mocked([
        httpx.Response(200, json={"data": {"sports": [
            {"id": 1, "label": "Cycling (Sport)", "is_active": True},
            {"id": 5, "label": "Running", "is_active": True},
        ]}})
    ])
    sports = await client_for().get_sports()
    assert [s["sport_id"] for s in sports] == [1, 5]
    assert sports[1]["label"] == "Running"


@pytest.mark.asyncio
async def test_a_sport_without_an_id_is_skipped(mocked):
    mocked([httpx.Response(200, json={"data": {"sports": [{"label": "kaputt"}]}})])
    assert await client_for().get_sports() == []


@pytest.mark.asyncio
async def test_the_sports_endpoint_is_the_documented_one(mocked):
    transport = mocked([httpx.Response(200, json={"data": {"sports": []}})])
    await client_for().get_sports()
    assert transport.requests[0].url.path == "/api/sports"


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_single_short_page_ends_the_walk(mocked):
    transport = mocked([workouts_page([a_workout("a"), a_workout("b")])])
    result = await client_for().get_workouts()
    assert len(result) == 2
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_an_empty_collection_is_fine(mocked):
    mocked([workouts_page([])])
    assert await client_for().get_workouts() == []


@pytest.mark.asyncio
async def test_two_full_pages_are_both_fetched(mocked):
    first = [a_workout(f"p1-{n}") for n in range(MAX_PER_PAGE)]
    mocked([workouts_page(first), workouts_page([a_workout("p2-0")])])

    result = await client_for().get_workouts()
    assert len(result) == MAX_PER_PAGE + 1


@pytest.mark.asyncio
async def test_more_than_two_pages_are_all_fetched(mocked):
    pages = [
        workouts_page([a_workout(f"p{page}-{n}") for n in range(MAX_PER_PAGE)])
        for page in range(3)
    ]
    pages.append(workouts_page([a_workout("last")]))
    mocked(pages)

    result = await client_for().get_workouts()
    assert len(result) == 3 * MAX_PER_PAGE + 1


@pytest.mark.asyncio
async def test_the_page_number_advances(mocked):
    transport = mocked([
        workouts_page([a_workout(f"p1-{n}") for n in range(MAX_PER_PAGE)]),
        workouts_page([a_workout("p2")]),
    ])
    await client_for().get_workouts()

    pages = [dict(r.url.params).get("page") for r in transport.requests]
    assert pages == ["1", "2"]


@pytest.mark.asyncio
async def test_a_server_ignoring_the_page_parameter_does_not_loop(mocked):
    """The same full page forever would otherwise never terminate."""
    same = [a_workout(f"x-{n}") for n in range(MAX_PER_PAGE)]
    transport = mocked([workouts_page(same) for _ in range(10)])

    result = await client_for().get_workouts()

    assert len(result) == MAX_PER_PAGE
    assert len(transport.requests) == 2      # one real page, one that repeated


@pytest.mark.asyncio
async def test_duplicate_ids_across_pages_are_collapsed(mocked):
    first = [a_workout(f"d-{n}") for n in range(MAX_PER_PAGE)]
    second = [first[0], a_workout("fresh")]
    mocked([workouts_page(first), workouts_page(second)])

    result = await client_for().get_workouts()
    assert len(result) == MAX_PER_PAGE + 1


@pytest.mark.asyncio
async def test_a_workout_without_an_id_is_skipped(mocked):
    mocked([workouts_page([a_workout(None), a_workout("ok")])])
    result = await client_for().get_workouts()
    assert [w["id"] for w in result] == ["ok"]


@pytest.mark.asyncio
async def test_the_from_date_is_passed_through(mocked):
    transport = mocked([workouts_page([])])
    await client_for().get_workouts(from_date=date(2026, 8, 1))
    assert dict(transport.requests[0].url.params)["from"] == "2026-08-01"


@pytest.mark.asyncio
async def test_the_page_size_stays_within_the_documented_cap(mocked):
    transport = mocked([workouts_page([])])
    await client_for().get_workouts()
    assert int(dict(transport.requests[0].url.params)["per_page"]) <= 100


# ---------------------------------------------------------------------------
# Authorization header and errors
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_bearer_token_is_sent(mocked):
    transport = mocked([workouts_page([])])
    await client_for().get_workouts()
    assert transport.requests[0].headers["Authorization"] == "Bearer access-token"


@pytest.mark.asyncio
async def test_a_server_error_is_sanitized(mocked):
    mocked([httpx.Response(500, json={"detail": "boom"})])
    with pytest.raises(FitTrackeeClientError) as excinfo:
        await client_for().get_workouts()
    assert "500" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_rate_limit_is_an_error_not_an_empty_result(mocked):
    """Returning [] would look like "no workouts" and could revoke nothing —
    but it would also mark a sync successful that never happened."""
    mocked([httpx.Response(429, json={})])
    with pytest.raises(FitTrackeeClientError):
        await client_for().get_workouts()


@pytest.mark.asyncio
async def test_invalid_json_is_an_error(mocked):
    mocked([httpx.Response(200, content=b"<html>nope</html>")])
    with pytest.raises(FitTrackeeClientError):
        await client_for().get_workouts()


@pytest.mark.asyncio
async def test_a_timeout_is_an_error(mocked):
    def raise_timeout(request):
        raise httpx.TimeoutException("timeout", request=request)

    mocked([raise_timeout])
    with pytest.raises(FitTrackeeClientError):
        await client_for().get_workouts()


@pytest.mark.asyncio
async def test_no_error_message_contains_the_token(mocked, caplog):
    mocked([httpx.Response(500, json={})])
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(FitTrackeeClientError) as excinfo:
            await client_for().get_workouts()
    assert "access-token" not in str(excinfo.value)
    assert "access-token" not in caplog.text


# ---------------------------------------------------------------------------
# 401, refresh and retry
# ---------------------------------------------------------------------------

def token_response(access="new-access", refresh="new-refresh"):
    return httpx.Response(
        200,
        json={
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": 3600,
            "scope": "workouts:read",
        },
    )


@pytest.mark.asyncio
async def test_a_401_triggers_one_refresh_and_one_retry(mocked):
    transport = mocked([
        httpx.Response(401, json={}),
        token_response(),
        workouts_page([a_workout("after-refresh")]),
    ])

    result = await client_for().get_workouts()

    assert [w["id"] for w in result] == ["after-refresh"]
    assert len(transport.requests) == 3
    assert transport.requests[1].url.path == "/api/oauth/token"


@pytest.mark.asyncio
async def test_the_retry_uses_the_new_token(mocked):
    transport = mocked([
        httpx.Response(401, json={}),
        token_response(access="fresh-one"),
        workouts_page([]),
    ])
    await client_for().get_workouts()
    assert transport.requests[2].headers["Authorization"] == "Bearer fresh-one"


@pytest.mark.asyncio
async def test_a_second_401_does_not_loop(mocked):
    """Exactly one retry. Looping would be a request storm against the user's
    own instance."""
    transport = mocked([
        httpx.Response(401, json={}),
        token_response(),
        httpx.Response(401, json={}),
    ])

    with pytest.raises(FitTrackeeAuthError):
        await client_for().get_workouts()

    assert len(transport.requests) == 3


@pytest.mark.asyncio
async def test_a_rejected_refresh_token_asks_for_reauthorization(mocked):
    mocked([httpx.Response(401, json={}), httpx.Response(400, json={})])
    with pytest.raises(FitTrackeeAuthError) as excinfo:
        await client_for().get_workouts()
    assert "neu verbinden" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_a_rejected_refresh_token_is_cleared(tmp_path, mocked):
    """Keeping a refused refresh token only produces a retry loop later."""
    store = TokenStore(tmp_path / "oauth")
    store.save(TokenSet("access-token", "refresh-token"))
    mocked([httpx.Response(401, json={}), httpx.Response(400, json={})])

    with pytest.raises(FitTrackeeAuthError):
        await client_for(store=store).get_workouts()

    assert not store.exists()


@pytest.mark.asyncio
async def test_no_refresh_token_means_reconnect(mocked):
    mocked([httpx.Response(401, json={})])
    tokens = TokenSet("access-token", refresh_token=None)
    with pytest.raises(FitTrackeeAuthError):
        await client_for(tokens=tokens).get_workouts()


@pytest.mark.asyncio
async def test_a_refreshed_token_is_persisted(tmp_path, mocked):
    store = TokenStore(tmp_path / "oauth")
    store.save(TokenSet("old-access", "old-refresh"))
    mocked([token_response(access="rotated", refresh="rotated-refresh")])

    await client_for(store=store).refresh_access_token()

    assert store.load().access_token == "rotated"
    assert store.load().refresh_token == "rotated-refresh"


@pytest.mark.asyncio
async def test_the_refresh_request_never_contains_the_word_write(mocked):
    transport = mocked([token_response()])
    await client_for().refresh_access_token()
    assert b"write" not in transport.requests[0].content


@pytest.mark.asyncio
async def test_an_expiring_token_is_refreshed_proactively(mocked):
    transport = mocked([token_response()])
    tokens = TokenSet("access", "refresh", expires_at=0)   # long expired

    await client_for(tokens=tokens).ensure_fresh_token()

    assert transport.requests[0].url.path == "/api/oauth/token"


@pytest.mark.asyncio
async def test_a_valid_token_is_not_refreshed(mocked):
    import time

    transport = mocked([])
    tokens = TokenSet("access", "refresh", expires_at=time.time() + 86400)
    await client_for(tokens=tokens).ensure_fresh_token()
    assert transport.requests == []


@pytest.mark.asyncio
async def test_no_token_reaches_the_log_on_refresh(mocked, caplog):
    mocked([token_response(access="brand-new-secret")])
    with caplog.at_level(logging.DEBUG):
        await client_for().refresh_access_token()
    assert "brand-new-secret" not in caplog.text
    assert "refresh-token" not in caplog.text


# ---------------------------------------------------------------------------
# The code exchange
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_code_exchange_sends_the_verifier(mocked):
    transport = mocked([token_response()])
    await exchange_code_for_tokens(
        BASE, code="the-code", code_verifier="the-verifier",
        client_id="cid", client_secret="csecret", redirect_uri="https://h/cb",
    )
    body = transport.requests[0].content.decode()
    assert "code_verifier=the-verifier" in body
    assert "grant_type=authorization_code" in body


@pytest.mark.asyncio
async def test_a_rejected_code_is_a_clear_error(mocked):
    mocked([httpx.Response(400, json={"error": "invalid_grant"})])
    with pytest.raises(FitTrackeeAuthError):
        await exchange_code_for_tokens(
            BASE, code="bad", code_verifier="v", client_id="c",
            client_secret="s", redirect_uri="https://h/cb",
        )


@pytest.mark.asyncio
async def test_the_exchange_error_does_not_leak_the_code(mocked, caplog):
    mocked([httpx.Response(400, json={})])
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(FitTrackeeAuthError) as excinfo:
            await exchange_code_for_tokens(
                BASE, code="secret-code-value", code_verifier="secret-verifier",
                client_id="c", client_secret="super-secret", redirect_uri="https://h/cb",
            )
    for secret in ("secret-code-value", "secret-verifier", "super-secret"):
        assert secret not in str(excinfo.value)
        assert secret not in caplog.text
