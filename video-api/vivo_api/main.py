"""The FastAPI app. `create_app()` wires settings to the parts; tests pass their own fakes.

Run on the gateway (in the container):  uvicorn --factory vivo_api.main:create_app ...
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import API_VERSION, errors
from .audit import Audit
from .config import Settings
from .context import Context
from .grants import GrantChecker, KeySet
from .inventory import Inventory


def create_app(settings: Settings | None = None, recorder=None, go2rtc=None, health=None,
               clock=time.time, start_background: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    from . import recorders
    from .upstream import Go2rtc, HealthSource, StreamWatch

    recorder = recorder if recorder is not None else recorders.make(settings)
    go2rtc = go2rtc if go2rtc is not None else Go2rtc(settings.go2rtc_url, settings.go2rtc_user,
                                                      settings.go2rtc_password)
    health = health if health is not None else HealthSource(settings.health_url)
    c = Context(
        settings=settings,
        checker=GrantChecker(KeySet(settings.keys_file), settings.audience, settings.grant_issuer,
                             settings.grant_max_lifetime, settings.grant_leeway, clock=clock),
        inventory=Inventory(settings.inventory_file),
        audit=Audit(settings.data_dir / "audit", settings.audit_days, clock=clock),
        recorder=recorder, go2rtc=go2rtc, health=health, watch=StreamWatch(go2rtc), clock=clock)
    from .clips import ClipManager
    from .live.sessions import LiveManager
    from .playback import PlaybackManager
    c.live = LiveManager(c)
    c.playbacks = PlaybackManager(c)
    c.clips = ClipManager(c)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_background:
            c.watch.start()
            c.live.start()
            c.clips.start()
        try:
            yield
        finally:
            await c.clips.stop()
            await c.live.stop()
            await c.watch.stop()
            for part in (recorder, go2rtc, health):
                close = getattr(part, "aclose", None)
                if close:
                    await close()

    app = FastAPI(title="Vivo Video API", version=API_VERSION, lifespan=lifespan,
                  description="The gateway's interface to cameras and recordings. Contract: docs/api-contract.md.",
                  docs_url=None, redoc_url=None)
    app.state.ctx = c
    errors.install(app)
    if settings.cors_origins:        # browser clients on another origin (Vivo Watch web, the test player)
        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=False,
                           allow_methods=["GET", "POST", "DELETE"], allow_headers=["Authorization", "Content-Type"],
                           max_age=600)
    from .routes import cameras, clips, live, playback
    app.include_router(cameras.router)
    app.include_router(live.router)
    app.include_router(playback.router)
    app.include_router(clips.router)
    return app
