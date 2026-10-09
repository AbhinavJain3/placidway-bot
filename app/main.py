"""FastAPI app: chat UI, chat/lead APIs, and token-protected admin endpoints (refresh, content gaps, leads)."""
from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field

from app.chat.pipeline import ChatService
from app.chat.session import SessionStore
from app.config import Settings, get_settings
from app.ingest.pipeline import run_ingestion
from app.llm.client import LLMClient
from app.retrieval.index import KnowledgeIndex, get_embedder
from app.storage import Storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("app")

STATIC_DIR = Path(__file__).parent / "static"


class Services:
    """Holds the live index and lets /admin/refresh swap it without a restart."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.embedder = get_embedder(settings.embedding_model, settings.model_cache_dir)
        if not (settings.data_dir / "chunks.json").exists():
            log.info("no knowledge snapshot found; running first ingestion")
            run_ingestion(settings)
        self._index = KnowledgeIndex.load(settings.data_dir, self.embedder)
        self._refresh_lock = threading.Lock()
        self.storage = Storage(settings.data_dir / "app.db")
        self.chat = ChatService(settings, LLMClient(settings), lambda: self._index, self.storage,
                                SessionStore(settings.history_turns))

    def refresh(self) -> dict:
        if not self._refresh_lock.acquire(blocking=False):
            raise HTTPException(409, "a refresh is already running")
        try:
            manifest = run_ingestion(self.settings)
            self._index = KnowledgeIndex.load(self.settings.data_dir, self.embedder)  # atomic reference swap
            return manifest
        finally:
            self._refresh_lock.release()

    def manifest(self) -> dict:
        try:
            return json.loads((self.settings.data_dir / "manifest.json").read_text())
        except FileNotFoundError:
            return {}


services: Services | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global services
    services = Services(get_settings())
    log.info("ready: %d chunks indexed", len(services._index.chunks))
    yield


app = FastAPI(title="PlacidWay Knowledge Chatbot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------- models
class ChatRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    message: str = Field(min_length=1, max_length=1000)


class ChatResponse(BaseModel):
    answer: str
    sources: list[dict]
    status: str
    intent: str
    language: str
    show_lead_form: bool
    offer_quote: bool


class LeadRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    treatment: str = Field(default="", max_length=200)
    message: str = Field(default="", max_length=1000)


def rate_limit(limit: int, window_s: float):
    """Sliding-window limit per client IP: keeps a public demo from burning the LLM budget."""
    hits_by_ip: dict[str, deque] = defaultdict(deque)
    lock = threading.Lock()

    def dependency(request: Request) -> None:
        forwarded = request.headers.get("x-forwarded-for", "")
        ip = forwarded.split(",")[0].strip() or (request.client.host if request.client else "?")
        now = time.monotonic()
        with lock:
            hits = hits_by_ip[ip]
            while hits and now - hits[0] > window_s:
                hits.popleft()
            if len(hits) >= limit:
                raise HTTPException(429, "Too many requests. Please wait a minute and try again.")
            hits.append(now)

    return dependency


chat_limit = rate_limit(limit=20, window_s=60)
lead_limit = rate_limit(limit=5, window_s=600)


def require_admin(x_admin_token: str = Header(default="")) -> None:
    expected = get_settings().admin_token
    if not expected or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(401, "invalid admin token")


# ---------------------------------------------------------------- routes
@app.get("/", include_in_schema=False)
def index_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict:
    manifest = services.manifest() if services else {}
    return {"status": "ok", "chunks": manifest.get("chunk_count"), "kb_built_at": manifest.get("built_at")}


@app.post("/api/chat", response_model=ChatResponse, dependencies=[Depends(chat_limit)])
def chat(req: ChatRequest) -> ChatResponse:
    result = services.chat.chat(req.session_id, req.message.strip())
    return ChatResponse(**result.__dict__)


@app.post("/api/chat/reset")
def reset(req: dict) -> dict:
    sid = str(req.get("session_id", ""))
    if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", sid):
        services.chat.sessions.reset(sid)
    return {"ok": True}


@app.post("/api/lead", dependencies=[Depends(lead_limit)])
def lead(req: LeadRequest) -> dict:
    services.storage.save_lead(session_id=req.session_id, name=req.name.strip(), email=str(req.email),
                               treatment=req.treatment.strip(), message=req.message.strip())
    return {"ok": True}


@app.post("/api/admin/refresh", dependencies=[Depends(require_admin)])
def admin_refresh() -> dict:
    return services.refresh()


@app.get("/api/admin/unanswered", dependencies=[Depends(require_admin)])
def admin_unanswered(limit: int = 100) -> list[dict]:
    return services.storage.recent("unanswered", min(limit, 1000))


@app.get("/api/admin/leads", dependencies=[Depends(require_admin)])
def admin_leads(limit: int = 100) -> list[dict]:
    return services.storage.recent("leads", min(limit, 1000))


@app.get("/api/admin/sources")
def sources() -> dict:
    """Public: which pages the bot knows and when they were last ingested."""
    return services.manifest()
