"""FastAPI entrypoint.

Exposed as a factory rather than a module-level app so that importing this
module has no side effects -- tests can build a throwaway instance against an
in-memory database, which is how the missing python-multipart dependency
should have been caught before it reached a container.

Run with exactly one worker. Two would mean two schedulers racing to send the
same batch twice, plus needless SQLite write contention.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, Request, Response

from .backup import backup
from .clock import SystemClock
from .config import Config
from .conversation import Router
from .db import make_engine, make_session_factory
from .fill import Filler
from .gateway import FakeGateway, TwilioGateway
from .poller import FakeInbox, Poller, TwilioInbox

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger(__name__)


def build(config: Config):
    """Wire the object graph. Returns everything the routes and CLI need."""
    engine = make_engine(config.db_path)
    session_factory = make_session_factory(engine)
    clock = SystemClock()

    if config.twilio_account_sid and config.twilio_auth_token and config.twilio_from:
        gateway = TwilioGateway(
            config.twilio_account_sid, config.twilio_auth_token, config.twilio_from
        )
    else:
        log.warning("Twilio not configured -- using FakeGateway, nothing will send")
        gateway = FakeGateway(clock=clock)

    filler = Filler(session_factory, gateway, clock, config)
    router = Router(session_factory, gateway, clock, config, filler)

    poller = None
    if config.polling:
        if config.twilio_account_sid and config.twilio_auth_token and config.twilio_from:
            inbox = TwilioInbox(
                config.twilio_account_sid,
                config.twilio_auth_token,
                config.twilio_from,
            )
        else:
            log.warning("polling requested but Twilio not configured -- using FakeInbox")
            inbox = FakeInbox()
        poller = Poller(
            inbox,
            router,
            session_factory,
            clock,
            lookback=timedelta(seconds=max(600, config.poll_seconds * 10)),
        )

    return session_factory, gateway, clock, filler, router, poller


def create_app(config: Config | None = None, *, run_scheduler: bool = True) -> FastAPI:
    config = config or Config.from_env()
    session_factory, gateway, clock, filler, router, poller = build(config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        scheduler = None
        if run_scheduler:
            scheduler = BackgroundScheduler(timezone=str(config.timezone))
            scheduler.add_job(
                filler.tick, "interval", minutes=1, id="tick", max_instances=1
            )
            scheduler.add_job(lambda: backup(config), "cron", hour=3, minute=15,
                              id="backup")
            if poller:
                scheduler.add_job(
                    poller.poll,
                    "interval",
                    seconds=config.poll_seconds,
                    id="poll",
                    max_instances=1,
                )
                log.info("polling Twilio every %ss (no inbound access needed)",
                         config.poll_seconds)
            scheduler.start()
            log.info("scheduler started")
        try:
            yield
        finally:
            if scheduler:
                scheduler.shutdown(wait=False)

    api = FastAPI(lifespan=lifespan)

    # Handy for tests and the CLI to reach the same wiring.
    api.state.config = config
    api.state.gateway = gateway
    api.state.filler = filler
    api.state.router = router
    api.state.session_factory = session_factory
    api.state.poller = poller

    def valid_signature(request: Request, form: dict) -> bool:
        if not config.validate_signature:
            return True
        if not config.twilio_auth_token:
            return False
        from twilio.request_validator import RequestValidator

        validator = RequestValidator(config.twilio_auth_token)
        signature = request.headers.get("X-Twilio-Signature", "")
        url = config.public_url or str(request.url)
        return validator.validate(url, form, signature)

    @api.get("/health")
    def health() -> dict:
        return {"ok": True, "mode": config.inbound_mode}

    if config.polling:
        # No webhook route at all in poll mode. An endpoint that does not
        # exist cannot be abused, and nothing needs to reach this process
        # from outside the machine.
        return api

    @api.post("/sms")
    async def sms(
        request: Request,
        From: str = Form(...),
        Body: str = Form(""),
        MessageSid: str = Form(None),
    ) -> Response:
        """Twilio inbound webhook.

        Signature validation is the single most important line here: without
        it, anyone who finds this endpoint can impersonate any member's phone.
        """
        form = dict(await request.form())
        if not valid_signature(request, form):
            log.warning("rejected unsigned webhook from %s", request.client)
            return Response(status_code=403)

        router.handle(From, Body, MessageSid)
        return Response(status_code=204)

    return api
