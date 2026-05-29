"""
Shared configuration and settings for all AI agents.
"""
from __future__ import annotations

import os
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Global settings loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM configuration
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    ollama_base_url: str = "http://ollama:11434"
    ollama_model: str = "llama3"

    # LangSmith / observability
    langsmith_api_key: str = ""
    langsmith_project: str = "netauto-agents"
    langsmith_tracing: bool = False

    # Nautobot
    nautobot_url: str = "http://nautobot:8080"
    nautobot_token: str = ""

    # Prometheus
    prometheus_url: str = "http://prometheus:9090"

    # Alertmanager (separate container from Prometheus)
    alertmanager_url: str = "http://alertmanager:9093"

    # Loki
    loki_url: str = "http://loki:3100"

    # Alert event receiver
    alert_event_receiver_url: str = "http://alert-event-receiver:8770"

    # Agent API ports
    ops_agent_port: int = 8000
    eng_agent_port: int = 8001
    chaos_agent_port: int = 8002

    # Cost control — token budgets and pricing
    # The hourly limit is set high so the daily dollar budget is the real hard stop.
    # A low hourly limit causes problems during testing when multiple runs happen
    # within the same clock-hour window.
    max_tokens_per_agent_per_hour: int = 2_000_000
    max_tokens_per_agent_per_day: int = 1_000_000
    daily_budget_usd: float = 5.00
    # GPT-4o pricing per 1k tokens (update when model changes)
    openai_input_cost_per_1k: float = 0.005
    openai_output_cost_per_1k: float = 0.015

    @property
    def use_openai(self) -> bool:
        """Return True if OpenAI API key is configured."""
        return bool(self.openai_api_key)


settings = Settings()
