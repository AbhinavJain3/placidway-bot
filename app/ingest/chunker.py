"""Sections -> retrieval chunks.

Every chunk carries its page title and section path as a header. The three package pages are
near-identical apart from the cancer type, so without this header a "What's included" chunk
could not be told apart from its siblings.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass

from app.ingest.extractor import Page

MAX_CHARS = 1400   # ~350 tokens
MIN_CHARS = 250    # smaller sections are merged into the next one
OVERLAP_LINES = 1  # repeated line between split windows of a long section


@dataclass
class Chunk:
    id: str
    url: str
    page_title: str
    page_type: str
    section: str
    text: str  # what the LLM sees (header + body)

    def to_dict(self) -> dict:
        return asdict(self)


def _break_line(line: str) -> list[str]:
    """Break a single over-long line (e.g. a comma-joined list of video titles) at sentence/list boundaries."""
    if len(line) <= MAX_CHARS:
        return [line]
    parts = re.split(r"(?<=[.!?])\s+|\s,\s|,\s(?=[A-Z])", line)
    out, cur = [], ""
    for part in parts:
        if cur and len(cur) + len(part) > MAX_CHARS // 2:
            out.append(cur)
            cur = ""
        cur = f"{cur}, {part}" if cur else part
    if cur:
        out.append(cur)
    return out


def _split_long(lines: list[str]) -> list[list[str]]:
    lines = [piece for line in lines for piece in _break_line(line)]
    windows, cur, size = [], [], 0
    for line in lines:
        if cur and size + len(line) > MAX_CHARS:
            windows.append(cur)
            cur = cur[-OVERLAP_LINES:] if OVERLAP_LINES else []
            size = sum(len(x) for x in cur)
        cur.append(line)
        size += len(line) + 1
    if cur:
        windows.append(cur)
    return windows


def _make(page: Page, section: str, body: str, n: int) -> Chunk:
    cid = hashlib.sha1(f"{page.url}#{n}#{body[:80]}".encode()).hexdigest()[:12]
    header = f"[Page: {page.title}]\n[Section: {section}]\n"
    return Chunk(cid, page.url, page.title, page.page_type, section, header + body)


def chunk_page(page: Page) -> list[Chunk]:
    chunks: list[Chunk] = []

    # 1) Merge tiny sections forward so a lone heading + one line is not its own chunk.
    merged: list[tuple[str, list[str]]] = []
    pending_heads: list[str] = []
    pending_lines: list[str] = []
    for sec in page.sections:
        path = sec.heading_path[1:] if len(sec.heading_path) > 1 and sec.heading_path[0] == page.title else sec.heading_path
        name = path[-1]
        lines = sec.text.splitlines()
        if pending_lines:
            lines = pending_lines + [f"## {name}"] + lines
            name = " / ".join(pending_heads + [name])
            pending_heads, pending_lines = [], []
        if sum(len(x) for x in lines) < MIN_CHARS:
            pending_heads.append(name)
            pending_lines = lines
            continue
        merged.append((" > ".join(path[:-1] + [name]), lines))
    if pending_lines:
        merged.append((" / ".join(pending_heads), pending_lines))

    # 2) Split long sections into overlapping windows.
    for section, lines in merged:
        for window in _split_long(lines):
            chunks.append(_make(page, section, "\n".join(window), len(chunks)))

    # 3) Structured prices from the page's schema.org data as one explicit, citable chunk.
    if page.price_facts:
        facts = "\n".join(
            f"- {f.item}: starting from {f.currency} {f.price} (provider: {f.seller})" for f in page.price_facts
        )
        body = ("Starting-from prices published in this page's structured data. These are the lowest listed price; "
                "where the page text gives prices for specific program options (e.g. out-patient vs in-patient), "
                f"use those for that option.\n{facts}")
        chunks.append(_make(page, "Listed starting prices", body, len(chunks)))
    return chunks
