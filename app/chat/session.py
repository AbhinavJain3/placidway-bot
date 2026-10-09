"""In-memory conversation state per browser session (history for follow-ups, disclaimer shown once)."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

SESSION_TTL_S = 2 * 60 * 60
MAX_SESSIONS = 2000


@dataclass
class Session:
    turns: list[tuple[str, str]] = field(default_factory=list)  # (role, text)
    disclaimer_shown: bool = False
    last_seen: float = field(default_factory=time.time)


class SessionStore:
    def __init__(self, max_turns: int):
        self.max_turns = max_turns
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> Session:
        with self._lock:
            now = time.time()
            if len(self._sessions) > MAX_SESSIONS:
                for sid in [k for k, s in self._sessions.items() if now - s.last_seen > SESSION_TTL_S]:
                    del self._sessions[sid]
            session = self._sessions.setdefault(session_id, Session())
            session.last_seen = now
            return session

    def add_turn(self, session: Session, user: str, assistant: str) -> None:
        session.turns.extend([("user", user), ("assistant", assistant)])
        del session.turns[: -2 * self.max_turns]

    def reset(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)


def format_history(session: Session, max_chars: int = 600) -> str:
    if not session.turns:
        return "(no previous messages)"
    return "\n".join(f"{role.upper()}: {text[:max_chars]}" for role, text in session.turns)
