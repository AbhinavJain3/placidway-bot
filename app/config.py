"""Central configuration. Every value comes from the environment / .env; nothing secret lives in code."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_provider: str = "azure"  # "azure" | "openai"
    azure_openai_api_key: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_version: str = "2025-01-01-preview"
    openai_api_key: str = ""
    chat_model: str = "gpt-4o"
    chat_reasoning_effort: str = ""  # set (e.g. "low") when CHAT_MODEL is a reasoning model
    rewrite_model: str = "gpt-4o"
    rewrite_reasoning_effort: str = ""
    llm_timeout_s: float = 30.0

    embedding_model: str = "BAAI/bge-small-en-v1.5"
    model_cache_dir: Path = Path("models")
    retrieval_top_k: int = 10

    admin_token: str = ""
    crawler_contact: str = "unknown"
    crawl_delay_s: float = 1.0
    data_dir: Path = Path("data")

    history_turns: int = 6
    quote_url: str = "https://www.placidway.com/free-quote/"

    @property
    def user_agent(self) -> str:
        return f"PlacidWayKBBot/1.0 (assessment crawler; +mailto:{self.crawler_contact})"


@lru_cache
def get_settings() -> Settings:
    return Settings()
