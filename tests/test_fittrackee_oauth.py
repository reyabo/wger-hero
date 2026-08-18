"""OAuth2 against FitTrackee: read-only scope, PKCE, state, and a safe store.

Nothing here touches a network. Every token, code and secret in this file is a
made-up string, and several tests exist purely to prove that such a string never
reaches a log, a message or a rendered page.
"""

import base64
import hashlib
import json
import logging
import os
import stat
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from app.fittrackee_oauth import (
    EXPIRY_MARGIN_SECONDS,
    REQUIRED_SCOPE,
    AuthorizationRequest,
    FitTrackeeAuthError,
    TokenSet,
    TokenStore,
    build_authorization_request,
    code_challenge_for,
    generate_code_verifier,
    generate_state,
    is_configured,
    load_client_secret,
    state_matches,
)

BASE = "https://fittrackee.example.com"
CLIENT_ID = "test-client-id"
REDIRECT = "https://hero.example.com/settings/fittrackee/callback"


def query_of(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

def test_only_the_read_scope_is_defined():
    assert REQUIRED_SCOPE == "workouts:read"


def test_the_authorization_url_asks_for_the_read_scope():
    request = build_authorization_request(BASE, CLIENT_ID, REDIRECT)
    assert query_of(request.url)["scope"] == "workouts:read"


@pytest.mark.parametrize(
    "forbidden",
    ["workouts:write", "profile:write", "users:write", "equipments:write"],
)
def test_no_write_scope_is_ever_requested(forbidden):
    request = build_authorization_request(BASE, CLIENT_ID, REDIRECT)
    assert forbidden not in request.url


def test_no_write_scope_appears_anywhere_in_the_module():
    source = Path("app/fittrackee_oauth.py").read_text()
    assert ":write" not in source


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------

def test_the_challenge_method_is_s256():
    assert query_of(build_authorization_request(BASE, CLIENT_ID, REDIRECT).url)[
        "code_challenge_method"
    ] == "S256"


def test_plain_pkce_is_never_offered():
    source = Path("app/fittrackee_oauth.py").read_text()
    assert '"plain"' not in source and "'plain'" not in source


def test_the_challenge_is_the_sha256_of_the_verifier():
    verifier = generate_code_verifier()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode().rstrip("=")
    assert code_challenge_for(verifier) == expected


def test_the_challenge_carries_no_padding():
    assert "=" not in code_challenge_for(generate_code_verifier())


def test_the_verifier_is_long_enough_for_rfc_7636():
    verifier = generate_code_verifier()
    assert 43 <= len(verifier) <= 128


def test_two_verifiers_are_never_the_same():
    assert len({generate_code_verifier() for _ in range(50)}) == 50


def test_the_verifier_never_appears_in_the_authorization_url():
    """Only the challenge travels; the verifier is the secret half."""
    request = build_authorization_request(BASE, CLIENT_ID, REDIRECT)
    assert request.verifier not in request.url


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def test_the_url_carries_a_state():
    request = build_authorization_request(BASE, CLIENT_ID, REDIRECT)
    assert query_of(request.url)["state"] == request.state


def test_two_states_are_never_the_same():
    assert len({generate_state() for _ in range(50)}) == 50


def test_a_matching_state_is_accepted():
    value = generate_state()
    assert state_matches(value, value)


def test_a_different_state_is_rejected():
    assert not state_matches(generate_state(), generate_state())


@pytest.mark.parametrize(
    "expected,received",
    [(None, "abc"), ("abc", None), ("", "abc"), ("abc", ""), (None, None)],
)
def test_a_missing_state_is_rejected(expected, received):
    """An absent state must never pass as a match — that is the whole attack."""
    assert not state_matches(expected, received)


# ---------------------------------------------------------------------------
# The authorization request as a whole
# ---------------------------------------------------------------------------

def test_the_flow_is_authorization_code():
    assert query_of(build_authorization_request(BASE, CLIENT_ID, REDIRECT).url)[
        "response_type"
    ] == "code"


def test_the_redirect_uri_is_passed_through():
    assert query_of(build_authorization_request(BASE, CLIENT_ID, REDIRECT).url)[
        "redirect_uri"
    ] == REDIRECT


def test_a_trailing_slash_on_the_base_url_is_harmless():
    request = build_authorization_request(BASE + "/", CLIENT_ID, REDIRECT)
    assert "//profile" not in request.url.replace("https://", "")


def test_the_client_secret_is_not_in_the_authorization_url():
    request = build_authorization_request(BASE, CLIENT_ID, REDIRECT)
    assert "secret" not in request.url.lower()


# ---------------------------------------------------------------------------
# TokenSet
# ---------------------------------------------------------------------------

def test_a_token_without_an_expiry_never_expires():
    assert not TokenSet(access_token="a").is_expired()


def test_an_expired_token_reports_expired():
    assert TokenSet(access_token="a", expires_at=100).is_expired(now=200)


def test_a_token_is_refreshed_before_it_actually_expires():
    """A sync that starts just under the wire must not fail halfway."""
    tokens = TokenSet(access_token="a", expires_at=1000)
    assert tokens.is_expired(now=1000 - EXPIRY_MARGIN_SECONDS + 1)
    assert not tokens.is_expired(now=1000 - EXPIRY_MARGIN_SECONDS - 1)


def test_expires_in_becomes_an_absolute_time():
    tokens = TokenSet.from_response({"access_token": "a", "expires_in": 3600}, now=1000)
    assert tokens.expires_at == 4600


def test_a_response_without_an_access_token_is_refused():
    with pytest.raises(FitTrackeeAuthError):
        TokenSet.from_response({"refresh_token": "r"})


def test_an_unreadable_expires_in_is_not_guessed():
    tokens = TokenSet.from_response({"access_token": "a", "expires_in": "bald"})
    assert tokens.expires_at is None


def test_a_token_set_round_trips():
    original = TokenSet("a", "r", 1234.0, "workouts:read")
    assert TokenSet.from_payload(original.to_payload()) == original


# ---------------------------------------------------------------------------
# The token store
# ---------------------------------------------------------------------------

def test_an_empty_store_has_nothing(tmp_path):
    assert TokenStore(tmp_path).load() is None
    assert not TokenStore(tmp_path).exists()


def test_tokens_round_trip_through_the_store(tmp_path):
    store = TokenStore(tmp_path / "oauth")
    store.save(TokenSet("access", "refresh", 999.0))

    loaded = store.load()
    assert loaded.access_token == "access"
    assert loaded.refresh_token == "refresh"


def test_the_token_file_is_owner_only(tmp_path):
    store = TokenStore(tmp_path / "oauth")
    store.save(TokenSet("access", "refresh"))
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600


def test_the_token_directory_is_owner_only(tmp_path):
    store = TokenStore(tmp_path / "oauth")
    store.save(TokenSet("access"))
    assert stat.S_IMODE(os.stat(tmp_path / "oauth").st_mode) == 0o700


def test_a_rewrite_leaves_no_temporary_file(tmp_path):
    """A half-written token file costs the refresh token and forces a
    reconnect, so the write is atomic and tidies up after itself."""
    store = TokenStore(tmp_path / "oauth")
    for n in range(5):
        store.save(TokenSet(f"access-{n}", "refresh"))

    files = sorted(p.name for p in (tmp_path / "oauth").iterdir())
    assert files == ["tokens.json"]


def test_a_rewrite_replaces_the_previous_tokens(tmp_path):
    store = TokenStore(tmp_path / "oauth")
    store.save(TokenSet("first", "r1"))
    store.save(TokenSet("second", "r2"))
    assert store.load().access_token == "second"


def test_a_corrupt_store_reads_as_no_authorization(tmp_path):
    """Reconnecting is the fix either way, so this must not raise mid-request."""
    directory = tmp_path / "oauth"
    directory.mkdir()
    (directory / "tokens.json").write_text("{not json")
    assert TokenStore(directory).load() is None


def test_a_store_without_an_access_token_reads_as_none(tmp_path):
    directory = tmp_path / "oauth"
    directory.mkdir()
    (directory / "tokens.json").write_text(json.dumps({"refresh_token": "r"}))
    assert TokenStore(directory).load() is None


def test_clearing_removes_the_file(tmp_path):
    store = TokenStore(tmp_path / "oauth")
    store.save(TokenSet("a", "r"))
    store.clear()
    assert not store.exists()


def test_clearing_an_empty_store_is_not_an_error(tmp_path):
    TokenStore(tmp_path / "oauth").clear()


def test_saving_does_not_log_the_token(tmp_path, caplog):
    with caplog.at_level(logging.DEBUG):
        TokenStore(tmp_path / "oauth").save(TokenSet("super-secret-token", "refresh-me"))
    assert "super-secret-token" not in caplog.text
    assert "refresh-me" not in caplog.text


def test_a_corrupt_store_does_not_log_its_contents(tmp_path, caplog):
    directory = tmp_path / "oauth"
    directory.mkdir()
    (directory / "tokens.json").write_text("super-secret-but-broken")
    with caplog.at_level(logging.DEBUG):
        TokenStore(directory).load()
    assert "super-secret-but-broken" not in caplog.text


# ---------------------------------------------------------------------------
# Client secret and configuration
# ---------------------------------------------------------------------------

class _Settings:
    def __init__(self, **kw):
        self.FITTRACKEE_BASE_URL = kw.get("base_url")
        self.FITTRACKEE_CLIENT_ID = kw.get("client_id")
        self.FITTRACKEE_CLIENT_SECRET_FILE = kw.get("secret_file")
        self.FITTRACKEE_TOKEN_DIR = kw.get("token_dir")


def test_the_client_secret_comes_from_its_file(tmp_path):
    path = tmp_path / "client_secret"
    path.write_text("  not-a-real-secret\n")
    assert load_client_secret(_Settings(secret_file=str(path))) == "not-a-real-secret"


def test_a_missing_client_secret_is_a_clear_error(tmp_path):
    with pytest.raises(FitTrackeeAuthError):
        load_client_secret(_Settings(secret_file=str(tmp_path / "nope")))


def test_an_empty_client_secret_is_refused(tmp_path):
    path = tmp_path / "client_secret"
    path.write_text("   ")
    with pytest.raises(FitTrackeeAuthError):
        load_client_secret(_Settings(secret_file=str(path)))


def test_the_error_does_not_contain_the_secret(tmp_path):
    path = tmp_path / "client_secret"
    path.write_text("do-not-leak-me")
    path.chmod(0o000)
    try:
        load_client_secret(_Settings(secret_file=str(path)))
    except FitTrackeeAuthError as exc:
        assert "do-not-leak-me" not in str(exc)
    finally:
        path.chmod(0o600)


def test_the_token_store_is_not_the_secret_directory(tmp_path):
    """A token refresh writes; the client secret must be somewhere it cannot
    reach, or a refresh could overwrite the secret."""
    from app.fittrackee_oauth import DEFAULT_CLIENT_SECRET_FILE, DEFAULT_TOKEN_DIR

    assert DEFAULT_CLIENT_SECRET_FILE.parent != DEFAULT_TOKEN_DIR


def test_configuration_needs_a_url_and_a_client_id():
    assert not is_configured(_Settings())
    assert not is_configured(_Settings(base_url=BASE))
    assert not is_configured(_Settings(client_id=CLIENT_ID))
    assert is_configured(_Settings(base_url=BASE, client_id=CLIENT_ID))


def test_checking_configuration_reads_no_secret(tmp_path):
    """It must be safe to call on every settings page render."""
    settings = _Settings(base_url=BASE, client_id=CLIENT_ID,
                         secret_file=str(tmp_path / "does-not-exist"))
    assert is_configured(settings) is True
