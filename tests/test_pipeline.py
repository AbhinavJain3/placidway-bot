"""Chat pipeline behaviour with a scripted fake LLM and a fake index (no network, no model download)."""
from pathlib import Path

import pytest

from app.chat.pipeline import FALLBACK_ANSWER, ChatService
from app.chat.session import SessionStore
from app.config import Settings
from app.retrieval.index import Hit
from app.storage import Storage

URL = "https://www.placidway.com/package/7902/x"
CHUNK = {"url": URL, "page_title": "Adrenal package", "text": "Out-Patient Program | 3 Weeks | Total Cost (USD): $18,995"}


class FakeIndex:
    def __init__(self):
        self.queries = []

    def search(self, query, k=8):
        self.queries.append(query)
        return [Hit(CHUNK, 1.0, 0.9)]


class FakeLLM:
    """Returns queued responses: first the router output, then answer outputs."""

    def __init__(self, route, *answers):
        self.queue = [route, *answers]
        self.calls = 0

    def json_chat(self, model, system, user, max_tokens=900, reasoning_effort=""):
        self.calls += 1
        return self.queue.pop(0)


@pytest.fixture
def make_service(tmp_path: Path):
    def _make(llm):
        index = FakeIndex()
        svc = ChatService(Settings(_env_file=None), llm, lambda: index, Storage(tmp_path / "t.db"), SessionStore(6))
        return svc, index
    return _make


def route(intent="kb_question", medical=False):
    return {"intent": intent, "language": "English", "search_query": "adrenal cost", "medical_topic": medical}


def test_grounded_answer_gets_source_url(make_service):
    svc, _ = make_service(FakeLLM(route(), {"answer": "It starts from $18,995 USD [1].", "sources": [1], "status": "answered"}))
    res = svc.chat("session-1234", "How much?")
    assert res.status == "answered"
    assert res.sources == [{"title": "Adrenal package", "url": URL}]


def test_invented_price_retried_then_falls_back(make_service):
    bad = {"answer": "It costs $9,999 [1].", "sources": [1], "status": "answered"}
    llm = FakeLLM(route(), bad, bad)
    svc, _ = make_service(llm)
    res = svc.chat("session-1234", "How much?")
    assert llm.calls == 3  # router + answer + one retry
    assert res.answer == FALLBACK_ANSWER and res.status == "not_found" and res.sources == []
    assert svc.storage.recent("unanswered")[0]["question"] == "How much?"


def test_uncited_factual_answer_is_rejected(make_service):
    svc, _ = make_service(FakeLLM(route(), {"answer": "It is great.", "sources": [], "status": "answered"}))
    assert svc.chat("session-1234", "Is it good?").answer == FALLBACK_ANSWER


def test_out_of_scope_skips_retrieval(make_service):
    svc, index = make_service(FakeLLM(route("out_of_scope"), {"answer": "I can only help with...", "sources": [], "status": "declined"}))
    res = svc.chat("session-1234", "Weather in Cancun?")
    assert index.queries == [] and res.status == "declined" and res.sources == []


def test_booking_opens_lead_form(make_service):
    svc, _ = make_service(FakeLLM(route("booking"), {"answer": "I can't book appointments.", "sources": [], "status": "declined"}))
    res = svc.chat("session-1234", "Book me for tomorrow")
    assert res.show_lead_form and res.offer_quote


def test_disclaimer_requested_only_once(make_service):
    ans = {"answer": "General info [1].", "sources": [1], "status": "answered"}
    llm = FakeLLM(route(medical=True), ans, route(medical=True), ans)
    seen = []
    original = llm.json_chat

    def spy(model, system, user, max_tokens=900, reasoning_effort=""):
        seen.append("not medical advice" in user)
        return original(model, system, user, max_tokens)

    llm.json_chat = spy
    svc, _ = make_service(llm)
    svc.chat("session-1234", "Is it safe?")
    svc.chat("session-1234", "And the side effects?")
    assert seen == [False, True, False, False]  # router, answer(with disclaimer), router, answer(without)


def test_api_routes_accept_valid_requests():
    """Guards FastAPI dependency wiring (e.g. the rate limiter must not turn `request` into a query param)."""
    from fastapi.testclient import TestClient

    from app import main

    class Stub:
        def chat(self, sid, msg):
            from app.chat.pipeline import ChatResult
            return ChatResult(answer="ok")

    class StubServices:
        chat = Stub()

        class storage:
            @staticmethod
            def save_lead(**_):
                pass

        @staticmethod
        def manifest():
            return {}

    client = TestClient(main.app)  # no context manager: lifespan (model load) is skipped
    main.services = StubServices()
    r = client.post("/api/chat", json={"session_id": "8a4f2b1c-1234-4c5d", "message": "hi"})
    assert r.status_code == 200 and r.json()["answer"] == "ok"
    r = client.post("/api/lead", json={"session_id": "8a4f2b1c-1234-4c5d", "name": "A", "email": "a@example.com"})
    assert r.status_code == 200
    for _ in range(25):
        last = client.post("/api/chat", json={"session_id": "8a4f2b1c-1234-4c5d", "message": "hi"})
    assert last.status_code == 429
