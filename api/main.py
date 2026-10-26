"""KnowledgeFlow API: POST /api/ask answers a question with a cited path across documentation sets."""
from __future__ import annotations

import os
import threading
import time
from collections import deque

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from kf import __version__
from kf.answer import Engine
from kf.gaps import GapStore
from kf.model import has_key

app = FastAPI(title="KnowledgeFlow", version=__version__)
origins = os.environ.get("KF_CORS_ORIGINS", "https://siddharthraop.github.io,http://localhost:8000").split(",")
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"], allow_headers=["Content-Type"])

engine = Engine(gaps=GapStore())

# A small per-instance limit keeps a public page inside the Gemini free tier.
_LIMIT = int(os.environ.get("KF_ASKS_PER_MINUTE", "12"))
_recent: deque = deque()
_lock = threading.Lock()


class Ask(BaseModel):
    question: str = Field(min_length=3, max_length=500)


@app.get("/api/health")
def health():
    return {"ok": True, "version": __version__, "model": os.environ.get("KF_MODEL", "gemini-3.8-flash") if has_key()
            else "none (extractive)", "gaps_store": engine.gaps.backend, "graph": engine.g.stats()}


@app.post("/api/ask")
def ask(body: Ask):
    now = time.time()
    with _lock:
        while _recent and now - _recent[0] > 60:
            _recent.popleft()
        if len(_recent) >= _LIMIT:
            raise HTTPException(429, "Too many questions this minute. Try again shortly.")
        _recent.append(now)
    return engine.ask(body.question)


@app.get("/api/gaps")
def gaps():
    return {"store": engine.gaps.backend, "gaps": engine.gaps.list()}


@app.get("/api/graph")
def graph():
    g = engine.g
    return {"stats": g.stats(),
            "topics": [{"id": t.id, "title": t.title, "set": t.set, "file": t.file, "url": t.url}
                       for t in g.topics.values()],
            "subjects": g.subjects,
            "edges": [e.as_dict() for e in g.edges if e.type != "states"]}
