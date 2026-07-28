# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Ollama Adapter

Wraps official ollama client library for automatic PDM memory injection and saving.

Usage:
    from pdm_memory.integrations import wrap_ollama
    from pdm_memory import Memory
    import ollama

    mem = Memory(store="./my_app.db")
    client = ollama.Client(host="http://localhost:11434")
    wrapped_client = wrap_ollama(client, memory=mem)
    reply = wrapped_client.chat("What are my preferences?")
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class PDMOllamaClient:
    """
    Memory-augmented Ollama client.

    All chat() calls automatically:
      - Recall relevant memories before the API call.
      - Inject them into the system prompt.
      - Save both user message and AI reply as new memories.
    """

    def __init__(
        self,
        ollama_client: Any,
        memory: Any,                  # pdm_memory.Memory instance
        model: str = "llama3",
        max_memory_tokens: int = 1500,
        recall_k: int = 5,
        auto_save: bool = True,
        system_prompt: str = "You are a helpful AI assistant.",
    ) -> None:
        self._client = ollama_client
        self._memory = memory
        self._model = model
        self._max_memory_tokens = max_memory_tokens
        self._recall_k = recall_k
        self._auto_save = auto_save
        self._system_prompt = system_prompt

        from pdm_memory.integrations.context_manager import ContextWindowManager
        self._ctx_manager = ContextWindowManager(
            max_tokens=max_memory_tokens,
            model="gpt-4o-mini",      # Use cl100k_base approximation
        )

    def chat(
        self,
        message: str,
        model: str | None = None,
        system_prompt: str | None = None,
        recall_k: int | None = None,
        save_reply: bool | None = None,
        **ollama_kwargs: Any,
    ) -> str:
        """
        Send a message to Ollama with automatic PDM memory injection.

        Args:
            message:        The user's message.
            model:          Override the default model name.
            system_prompt:   Override the base system prompt.
            recall_k:       Override the number of memories to recall.
            save_reply:     Override whether to save this exchange to memory.
            **ollama_kwargs: Passed through to ollama.chat API call.

        Returns:
            The assistant's reply text.
        """
        k = recall_k if recall_k is not None else self._recall_k
        should_save = save_reply if save_reply is not None else self._auto_save
        base_system = system_prompt or self._system_prompt

        # Step 1: Recall relevant memories
        hits = self._memory.recall(query=message, k=k)
        trimmed = self._ctx_manager.fit(hits)

        # Step 2: Build system message with memory block
        memory_block = self._ctx_manager.format_for_prompt(trimmed)
        full_system = base_system
        if memory_block:
            full_system = f"{base_system}\n\n{memory_block}"

        model_name = model or self._model

        # Step 3: Call Ollama API
        response = self._client.chat(
            model=model_name,
            messages=[
                {"role": "system", "content": full_system},
                {"role": "user", "content": message},
            ],
            **ollama_kwargs,
        )

        # Handle both dict response and object response shapes
        if isinstance(response, dict):
            reply_text = response.get("message", {}).get("content", "")
        else:
            message_obj = getattr(response, "message", None)
            if message_obj and hasattr(message_obj, "content"):
                reply_text = message_obj.content
            elif hasattr(response, "get"):
                reply_text = response.get("message", {}).get("content", "")
            else:
                reply_text = str(response)

        # Step 4: Save this turn to memory
        if should_save:
            self._save_turn(message, reply_text)

        logger.debug(
            "[PDM-Ollama] chat() | recalled=%d injected=%d model=%s",
            len(hits), len(trimmed), model_name,
        )
        return reply_text

    def _save_turn(self, user_msg: str, assistant_reply: str) -> None:
        """Save user message and assistant reply as memories."""
        try:
            if user_msg.strip():
                self._memory.save(
                    text=user_msg[:500],
                    source="ollama_chat",
                    tags=["conversation", "user_input"],
                    p_magnitude=40.0,
                )
        except Exception as e:
            logger.warning("[PDM-Ollama] Failed to save user message: %s", e)

        try:
            if assistant_reply.strip():
                self._memory.save(
                    text=assistant_reply[:500],
                    source="ollama_chat",
                    tags=["conversation", "ai_reply"],
                    p_magnitude=35.0,
                )
        except Exception as e:
            logger.warning("[PDM-Ollama] Failed to save assistant reply: %s", e)


def wrap_ollama(
    client: Any,
    memory: Any,
    model: str = "llama3",
    max_memory_tokens: int = 1500,
    recall_k: int = 5,
    system_prompt: str = "You are a helpful AI assistant.",
) -> PDMOllamaClient:
    """
    Create a memory-augmented Ollama client.

    Args:
        client:            ollama Client instance or ollama module itself.
        memory:            pdm_memory.Memory instance.
        model:             Default model (default: llama3).
        max_memory_tokens: Token budget for injected memories.
        recall_k:          Number of memories to recall per turn.
        system_prompt:     Base system prompt.

    Returns:
        PDMOllamaClient wrapping the provided client/module.
    """
    return PDMOllamaClient(
        ollama_client=client,
        memory=memory,
        model=model,
        max_memory_tokens=max_memory_tokens,
        recall_k=recall_k,
        system_prompt=system_prompt,
    )
