from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import STORAGE_DIR
from db import Base, ensure_schema, engine
from routes import assets, channels, generate, projects, settings, templates

import models  # noqa: F401  (registers models on Base metadata)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_schema()
    yield


app = FastAPI(title="Renderly API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://flow.google.com",
        "https://labs.google",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/storage", StaticFiles(directory=STORAGE_DIR), name="storage")

app.include_router(channels.router)
app.include_router(projects.router)
app.include_router(assets.router)
app.include_router(generate.router)
app.include_router(settings.router)
app.include_router(templates.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}
