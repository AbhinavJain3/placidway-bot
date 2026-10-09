"""Chat orchestration: route -> retrieve -> answer -> verify -> respond.

    user message
        |  1. router LLM: language, intent, standalone English search query (resolves follow-ups)
        v
    retrieve top-k passages (hybrid BM25 + embeddings)        [skipped for out_of_scope / smalltalk]
        |  2. answer LLM: JSON {answer, sources, status}, grounded in numbered passages only
        v
    guardrails: figures must appear in cited passages -> retry once -> safe fallback
        |  citations -> URLs attached server-side; unanswered questions logged
        v
    ChatResult
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from app.chat import guardrails
from app.chat.session import SessionStore, format_history
from app.config import Settings
from app.llm import prompts
from app.llm.client import LLMClient, LLMError
from app.retrieval.index import Hit, KnowledgeIndex
from app.storage import Storage

log = logging.getLogger(__name__)

VALID_INTENTS = {"kb_question", "booking", "medical_advice", "out_of_scope", "smalltalk"}
NO_RETRIEVAL = {"out_of_scope", "smalltalk"}

FALLBACK_ANSWER = (
    "I'm sorry, I couldn't find reliable information about that on our pages, so I'd rather not guess. "
    "PlacidWay's patient team can help: request a free, no-obligation quote and they will reply with "
    "accurate details."
)
ERROR_ANSWER = (
    "Sorry, I'm having trouble answering right now. Please try again in a moment, or request a free quote "
    "and PlacidWay's team will get back to you."
)


@dataclass
class ChatResult:
    answer: str
    sources: list[dict] = field(default_factory=list)  # [{title, url}]
    status: str = "answered"
    intent: str = "kb_question"
    language: str = "English"
    show_lead_form: bool = False
    offer_quote: bool = False


class ChatService:
    def __init__(self, settings: Settings, llm: LLMClient, index_provider, storage: Storage, sessions: SessionStore):
        self.settings = settings
        self.llm = llm
        self._index_provider = index_provider  # callable -> current KnowledgeIndex (hot-swappable)
        self.storage = storage
        self.sessions = sessions

    @property
    def index(self) -> KnowledgeIndex:
        return self._index_provider()

    # ------------------------------------------------------------------ steps
    def _route(self, message: str, history: str) -> dict:
        try:
            route = self.llm.json_chat(
                self.settings.rewrite_model,
                prompts.ROUTER_SYSTEM,
                prompts.ROUTER_USER.format(history=history, message=message),
                max_tokens=200,
                reasoning_effort=self.settings.rewrite_reasoning_effort,
            )
        except LLMError:
            route = {}
        intent = route.get("intent") if route.get("intent") in VALID_INTENTS else "kb_question"
        return {
            "intent": intent,
            "language": str(route.get("language") or "the same language as the user's message"),
            "search_query": str(route.get("search_query") or message),
            "medical_topic": bool(route.get("medical_topic")),
        }

    def _answer(self, message: str, history: str, route: dict, hits: list[Hit], disclaimer: bool,
                retry_note: str = "") -> dict:
        context = "\n\n".join(
            f"[{i}] (Source: {h.chunk['page_title']})\n{h.chunk['text']}" for i, h in enumerate(hits, start=1)
        ) or "(no passages: the question is outside the knowledge base)"
        system = prompts.ANSWER_SYSTEM.format(language=route["language"]) + retry_note
        user = prompts.ANSWER_USER.format(
            context=context,
            history=history,
            intent=route["intent"],
            intent_note=prompts.INTENT_NOTES[route["intent"]],
            disclaimer_note=prompts.DISCLAIMER_NOTE if disclaimer else "",
            message=message,
        )
        return self.llm.json_chat(self.settings.chat_model, system, user,
                                  reasoning_effort=self.settings.chat_reasoning_effort)

    # ------------------------------------------------------------------ main entry
    def chat(self, session_id: str, message: str) -> ChatResult:
        started = time.monotonic()
        session = self.sessions.get(session_id)
        history = format_history(session)
        route = self._route(message, history)
        intent = route["intent"]

        hits: list[Hit] = []
        if intent not in NO_RETRIEVAL:
            queries = [route["search_query"]]
            if route["language"].lower().startswith("english"):
                queries.append(message)  # the user's own wording often matches page text best
            hits = self.index.search(queries, k=self.settings.retrieval_top_k)
        passages = [h.chunk["text"] for h in hits]
        add_disclaimer = route["medical_topic"] and not session.disclaimer_shown and intent not in NO_RETRIEVAL

        try:
            result = self._answer(message, history, route, hits, add_disclaimer)
            answer, cited, bad = self._verify(result, passages, message)
            if bad:
                log.warning("ungrounded figures %s; retrying", bad)
                retry = prompts.RETRY_NOTE.format(bad=", ".join(f"{v:,.0f}" for v in sorted(bad)))
                result = self._answer(message, history, route, hits, add_disclaimer, retry_note=retry)
                answer, cited, bad = self._verify(result, passages, message)
            status = str(result.get("status") or "answered")
            if bad:
                log.warning("still ungrounded after retry %s; using fallback", bad)
                answer, cited, status = FALLBACK_ANSWER, [], "not_found"
            elif status in {"answered", "partial"} and intent == "kb_question" and not cited:
                # A factual answer that cites nothing is not grounded.
                answer, status = FALLBACK_ANSWER, "not_found"
        except LLMError:
            answer, cited, status = ERROR_ANSWER, [], "error"
            result = {}

        if add_disclaimer and status != "error":
            session.disclaimer_shown = True

        sources, answer = self._sources(hits, cited, answer)
        offer_quote = bool(result.get("offer_quote")) or status in {"not_found", "partial"} or intent == "booking"
        res = ChatResult(
            answer=answer,
            sources=sources,
            status=status,
            intent=intent,
            language=route["language"],
            show_lead_form=intent == "booking",
            offer_quote=offer_quote,
        )

        self.sessions.add_turn(session, message, answer)
        top_sim = max((h.dense_sim for h in hits), default=0.0)
        if status in {"not_found", "partial"} and intent in {"kb_question", "medical_advice", "booking"}:
            self.storage.log_unanswered(session_id=session_id, question=message, search_query=route["search_query"],
                                        language=route["language"], status=status, top_sim=round(top_sim, 3))
        self.storage.log_chat(session_id=session_id, question=message, intent=intent, status=status, answer=answer,
                              sources=",".join(s["url"] for s in sources),
                              latency_ms=int((time.monotonic() - started) * 1000))
        return res

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _verify(result: dict, passages: list[str], message: str) -> tuple[str, list[int], set[float]]:
        answer = guardrails.strip_invalid_citations(str(result.get("answer") or ""), len(passages))
        raw_sources = [int(s) for s in result.get("sources") or [] if str(s).isdigit()]
        cited = guardrails.cited_numbers(answer, raw_sources, len(passages))
        cited, bad = guardrails.check_numbers(answer, cited, passages, message)
        return answer.strip() or FALLBACK_ANSWER, cited, bad

    @staticmethod
    def _sources(hits: list[Hit], cited: list[int], answer: str) -> tuple[list[dict], str]:
        """De-duplicate cited passages by page and renumber the [n] markers to match the source list shown."""
        out: list[dict] = []
        url_to_num: dict[str, int] = {}
        passage_to_num: dict[int, int] = {}
        for n in cited:
            chunk = hits[n - 1].chunk
            if chunk["url"] not in url_to_num:
                out.append({"title": chunk["page_title"], "url": chunk["url"]})
                url_to_num[chunk["url"]] = len(out)
            passage_to_num[n] = url_to_num[chunk["url"]]
        answer = guardrails.renumber_citations(answer, passage_to_num)
        return out, answer
