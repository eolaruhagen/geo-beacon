"""FastAPI application entry point."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from scripts.apply_migrations import apply, DEFAULT_DB_PATH, DEFAULT_MIGRATIONS_DIR

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    apply(os.environ.get("MISSION_DB_PATH", DEFAULT_DB_PATH), DEFAULT_MIGRATIONS_DIR)
    yield


app = FastAPI(title="geo-beacon SAR Mission Control", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from api.routes import missions, field, mission, admin, debug  # noqa: E402

app.include_router(missions.router)
app.include_router(field.router)
app.include_router(mission.router)
app.include_router(admin.router)
# Debug-only; strip before any non-demo deploy.
app.include_router(debug.router)


if __name__ == "__main__":
    for r in app.routes:
        print(r.path, getattr(r, "methods", ""))
