"""
LLM factory – returns the appropriate LLM based on available credentials.
Falls back to Ollama if no OpenAI key is configured.
"""
from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from shared.config import settings


def get_llm(temperature: float = 0.1) -> BaseChatModel:
    """
    Return the appropriate LLM instance.

    Priority:
      1. OpenAI (GPT-4o) if OPENAI_API_KEY is set.
      2. Ollama local model (llama3 default).
    """
    if settings.use_openai:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=settings.openai_model,
            temperature=temperature,
            api_key=settings.openai_api_key,
        )
    else:
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=settings.ollama_model,
            base_url=settings.ollama_base_url,
            temperature=temperature,
        )
