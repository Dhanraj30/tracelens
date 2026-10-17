"""Environment configuration. All values come from env vars (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    falkordb_host: str = field(default_factory=lambda: _env("FALKORDB_HOST", "localhost"))
    falkordb_port: int = field(default_factory=lambda: int(_env("FALKORDB_PORT", "6379")))
    graph_name: str = field(default_factory=lambda: _env("GRAPH_NAME", "tracelens"))
    auto_seed: bool = field(default_factory=lambda: _env("AUTO_SEED", "true").lower() == "true")
    query_timeout_ms: int = field(default_factory=lambda: int(_env("QUERY_TIMEOUT_MS", "5000")))
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL", ""))
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", ""))
    llm_timeout_s: float = field(default_factory=lambda: float(_env("LLM_TIMEOUT_S", "20")))

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)


settings = Settings()
