"""Polling mode: running with no inbound network access at all."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app
from app.models import ProcessedMessage
from app.poller import FakeInbox, Poller
from tests.conftest import SUBS, TEACHER, TZ


@pytest.fixture
def polled(staffed):
    """A fully staffed app driven by polling instead of webhooks."""
    app = staffed
    app.inbox = FakeInbox()
    app.poller = Poller(
        app.inbox,
        app.router,
        app.session_factory,
        app.clock,
        lookback=timedelta(minutes=10),
    )
    # First poll absorbs history; start from a clean slate.
    app.poller.poll()
    return app


def test_polled_message_is_handled(polled):
    polled.inbox.deliver(TEACHER, "SUB 3/15", polled.clock.now())
    assert polled.poller.poll() == 1
    assert "sub needed" in polled.last(TEACHER)
    assert len(polled.texted(*SUBS[:6])) == 3


def test_overlapping_windows_do_not_double_handle(polled):
    """The lookback deliberately re-fetches; MessageSid dedupe absorbs it."""
    polled.inbox.deliver(TEACHER, "SUB 3/15", polled.clock.now())
    assert polled.poller.poll() == 1

    polled.clear()
    polled.advance(seconds=20)
    assert polled.poller.poll() == 0  # same message re-fetched, ignored
    assert polled.gateway.sent == []


def test_a_full_conversation_over_polling(polled):
    now = polled.clock.now()
    polled.inbox.deliver(TEACHER, "SUB 3/15", now)
    polled.poller.poll()
    asked = sorted(polled.texted(*SUBS[:6]))
    polled.clear()

    polled.advance(minutes=1)
    polled.inbox.deliver(asked[0], "yes", polled.clock.now())
    polled.poller.poll()

    assert "You're subbing" in polled.last(asked[0])
    assert asked[0] in polled.last(TEACHER)


def test_messages_are_handled_in_order_sent(polled):
    base = polled.clock.now()
    polled.inbox.deliver("+15557770000", "Jane", base + timedelta(seconds=3))
    polled.inbox.deliver("+15557770000", "hi", base + timedelta(seconds=1))
    polled.inbox.deliver("+15557770000", "yes", base + timedelta(seconds=2))

    polled.advance(seconds=5)
    polled.poller.poll()

    replies = polled.outbox("+15557770000")
    assert "consent" in replies[0].lower()
    assert "What's your name" in replies[1]
    assert "TEACHER or a SUBSTITUTE" in replies[2]


def test_bootstrap_ignores_history(staffed):
    """Switching a live number to polling must not replay old messages."""
    app = staffed
    inbox = FakeInbox()
    for body in ("hi", "SUB 3/15", "yes"):
        inbox.deliver(TEACHER, body, app.clock.now())

    poller = Poller(inbox, app.router, app.session_factory, app.clock)
    assert poller.poll() == 0
    assert app.gateway.sent == []

    with app.session_factory() as session:
        assert session.query(ProcessedMessage).count() == 3

    # Anything arriving afterwards is handled normally.
    app.advance(minutes=1)
    inbox.deliver(TEACHER, "SUB 3/15", app.clock.now())
    assert poller.poll() == 1


def test_fetch_failure_does_not_crash_the_loop(polled):
    class Broken:
        def fetch(self, since):
            raise ConnectionError("network down")

    polled.poller.inbox = Broken()
    assert polled.poller.poll() == 0  # logged, not raised


def test_poll_mode_exposes_no_webhook():
    """Nothing to secure, because there is no endpoint."""
    api = create_app(
        Config(db_path=":memory:", timezone=TZ, inbound_mode="poll"),
        run_scheduler=False,
    )
    with TestClient(api) as client:
        assert client.get("/health").json()["mode"] == "poll"
        assert client.post("/sms", data={"From": "+1555", "Body": "hi"}).status_code == 404


def test_webhook_mode_still_serves_the_endpoint():
    api = create_app(
        Config(db_path=":memory:", timezone=TZ, validate_signature=False,
               inbound_mode="webhook"),
        run_scheduler=False,
    )
    with TestClient(api) as client:
        assert client.post(
            "/sms", data={"From": "+15551110001", "Body": "hi"}
        ).status_code == 204


def test_outage_longer_than_lookback_loses_nothing(polled):
    """A fixed window would skip texts sent early in a long outage."""
    class Broken:
        def fetch(self, since):
            raise ConnectionError("network down")

    real = polled.poller.inbox
    sent_during_outage = polled.clock.now() + timedelta(minutes=2)
    real.deliver(TEACHER, "SUB 3/15", sent_during_outage)

    polled.poller.inbox = Broken()
    for _ in range(3):
        polled.advance(minutes=10)
        assert polled.poller.poll() == 0  # 30 minutes down, lookback is 10

    polled.poller.inbox = real
    assert polled.poller.poll() == 1
    assert "sub needed" in polled.last(TEACHER)


def test_twilio_clients_carry_a_timeout():
    from app.gateway import HTTP_TIMEOUT, twilio_client

    client = twilio_client("ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "token")
    assert client.http_client.timeout == HTTP_TIMEOUT


def test_heartbeat_fires_only_after_a_successful_fetch(polled, monkeypatch):
    import app.poller as P

    hits = []

    class _Resp:
        def close(self):
            pass

    monkeypatch.setattr(P.urllib.request, "urlopen", lambda url, timeout: hits.append(url) or _Resp())
    polled.poller.heartbeat_url = "http://kuma/api/push/abc?status=up"

    polled.poller.poll()
    assert hits == ["http://kuma/api/push/abc?status=up"]

    class Broken:
        def fetch(self, since):
            raise ConnectionError("down")

    polled.poller.inbox = Broken()
    polled.poller.poll()
    assert len(hits) == 1  # no heartbeat when the fetch failed


def test_heartbeat_failure_does_not_break_polling(polled, monkeypatch):
    import app.poller as P

    def boom(url, timeout):
        raise OSError("kuma unreachable")

    monkeypatch.setattr(P.urllib.request, "urlopen", boom)
    polled.poller.heartbeat_url = "http://kuma/api/push/abc"
    polled.inbox.deliver(TEACHER, "SUB 3/15", polled.clock.now())
    assert polled.poller.poll() == 1


def test_malformed_twilio_from_refuses_to_start():
    """A bad number would make every poll return nothing, silently."""
    import pytest

    for bad in ("/resu", "3853547328", "+1 385 354 7328", "+1385354732x"):
        with pytest.raises(ValueError):
            Config(db_path=":memory:", timezone=TZ, twilio_from=bad).validate()
    Config(db_path=":memory:", timezone=TZ, twilio_from="+13853547328").validate()


def test_poll_mode_checks_the_number_is_on_the_account(monkeypatch):
    import pytest

    import app.main as main_mod

    class _Numbers:
        def list(self, phone_number):
            return []

    class _Client:
        def __init__(self, *a, **k):
            self.incoming_phone_numbers = _Numbers()

    monkeypatch.setattr("app.gateway.twilio_client", lambda sid, tok: _Client())
    cfg = Config(db_path=":memory:", timezone=TZ, inbound_mode="poll",
                 twilio_account_sid="ACx", twilio_auth_token="t", twilio_from="+15550000000")
    with pytest.raises(RuntimeError, match="not a number on this Twilio account"):
        main_mod.build(cfg)


def test_default_mode_is_poll_and_docs_are_off():
    """A container started without its env file must not expose an endpoint."""
    cfg = Config(db_path=":memory:", timezone=TZ)
    assert cfg.polling
    api = create_app(cfg, run_scheduler=False)
    with TestClient(api) as client:
        for path in ("/docs", "/redoc", "/openapi.json", "/sms"):
            assert client.get(path).status_code == 404, path


def test_push_urls_are_joined_whatever_their_shape():
    from app.monitor import with_param

    assert with_param("http://k/api/push/T", "ping", "5") == "http://k/api/push/T?ping=5"
    assert with_param("http://k/api/push/T?status=up&msg=OK", "ping", "5").endswith("&ping=5")
    assert with_param("http://k/api/push/T?status=up&msg=OK", "msg", "HALTED") == \
        "http://k/api/push/T?status=up&msg=HALTED"


def test_subctl_refuses_the_live_database_for_simulate_and_tick(tmp_path, monkeypatch, capsys):
    import pytest

    from cli.subctl import main

    live = str(tmp_path / "live.db")
    monkeypatch.setenv("DB_PATH", live)
    with pytest.raises(SystemExit) as exc:
        main(["simulate", "+15551110001", "hi"])
    assert "refusing" in str(exc.value)
    with pytest.raises(SystemExit):
        main(["tick"])

    # A scratch copy or an explicit --live is fine.
    main(["--db", str(tmp_path / "scratch.db"), "simulate", "+15551110001", "hi"])
    assert "consent" in capsys.readouterr().out.lower()
    main(["tick", "--live"])
