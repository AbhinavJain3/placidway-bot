"""Ingestion: fetch -> extract -> chunk -> embed -> write snapshot.

Output (all under DATA_DIR):
  pages.json       extracted pages (for inspection / debugging)
  chunks.json      retrieval chunks
  embeddings.npy   one normalised vector per chunk
  manifest.json    when each page was ingested, its content hash, and what changed
Files are written to temp names and swapped in at the end, so a failed run never leaves a half-built index.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

import numpy as np

from app.config import Settings
from app.ingest.chunker import chunk_page
from app.ingest.extractor import extract
from app.ingest.fetcher import PoliteFetcher
from app.retrieval.index import KnowledgeIndex, get_embedder
from app.sources import SOURCES

log = logging.getLogger(__name__)


def run_ingestion(settings: Settings) -> dict:
    data_dir = settings.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    old_manifest = _read_json(data_dir / "manifest.json", {}).get("pages", {})

    fetcher = PoliteFetcher(settings.user_agent, data_dir / "cache", delay_s=settings.crawl_delay_s)
    pages, chunks, report = [], [], {}
    try:
        for src in SOURCES:
            res = fetcher.fetch(src.url)
            if res.html is None:
                log.error("skipping %s: %s", src.url, res.error)
                report[src.url] = {"status": "failed", "error": res.error}
                continue
            page = extract(res.html, src.url, src.page_type)
            page_chunks = chunk_page(page)
            digest = hashlib.sha256("\n".join(c.text for c in page_chunks).encode()).hexdigest()[:16]
            prev = old_manifest.get(src.url, {}).get("hash")
            report[src.url] = {
                "status": "fetched" if res.status == 200 else "not_modified",
                "changed": prev is not None and prev != digest,
                "new": prev is None,
                "hash": digest,
                "title": page.title,
                "chunks": len(page_chunks),
                "ingested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            if res.error:
                report[src.url]["warning"] = res.error
            pages.append(page.to_dict())
            chunks.extend(c.to_dict() for c in page_chunks)
    finally:
        fetcher.close()

    if not chunks:
        raise RuntimeError("ingestion produced no content; keeping the previous index")

    embedder = get_embedder(settings.embedding_model, settings.model_cache_dir)
    vectors = KnowledgeIndex.embed_passages(embedder, [c["text"] for c in chunks])

    _atomic_write(data_dir / "pages.json", json.dumps(pages, indent=1, ensure_ascii=False))
    _atomic_write(data_dir / "chunks.json", json.dumps(chunks, indent=1, ensure_ascii=False))
    tmp = data_dir / "embeddings.tmp.npy"
    np.save(tmp, vectors)
    tmp.replace(data_dir / "embeddings.npy")
    manifest = {"built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "chunk_count": len(chunks), "pages": report}
    _atomic_write(data_dir / "manifest.json", json.dumps(manifest, indent=1))
    log.info("ingestion done: %d pages, %d chunks", len(pages), len(chunks))
    return manifest


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
