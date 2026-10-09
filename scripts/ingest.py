"""Refresh the knowledge base from the source URLs.

Usage:  python -m scripts.ingest
"""
import json
import logging

from app.config import get_settings
from app.ingest.pipeline import run_ingestion

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    manifest = run_ingestion(get_settings())
    for url, info in manifest["pages"].items():
        flag = "NEW" if info.get("new") else ("CHANGED" if info.get("changed") else info["status"])
        print(f"{flag:13} {info.get('chunks', 0):3} chunks  {url}")
    print(f"total chunks: {manifest['chunk_count']}")
