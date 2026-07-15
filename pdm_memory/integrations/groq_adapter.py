# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Groq Adapter

Wraps Groq client for automatic PDM memory injection and saving.

Usage:
    from pdm_memory.integrations import wrap_groq
    from pdm_memory import Memory
    from groq import Groq

    mem = Memory(store="./my_app.db")
    client = Groq(api_key="gsk_...")
    wrapped_client = wrap_groq(client, memory=mem)
    reply = wrapped_client.chat("What are my preferences?")
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class PDMGroqClient:
    """
    Memory-augmented Groq client.

    All chat() calls automatically:
      - Recall relevant memories before the API call.
      - Inject them into the system prompt.
      - Save both user message and AI reply as new memories.
    """

    def __init__(
        self,
        groq_client: Any,
        memory: Any,                  # pdm_memory.Memory instance
        model: str = "llama-3.1-70b-versatile",
        max_memory_tokens: int = 1500,
        recall_k: int = 5,
        auto_save: bool = True,
        system_prompt: str = "You are a helpful AI assistant.",
    ) -> None:
        self._client = groq_client
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
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        recall_k: Optional[int] = None,
        save_reply: Optional[bool] = None,
        **groq_kwargs: Any,
    ) -> str:
        """
        Send a message to Groq with automatic PDM memory injection.

        Args:
            message:      The user's message.
            model:        Override the default model.
            system_prompt: Override the base system prompt.
            recall_k:     Override the number of memories to recall.
            save_reply:   Override whether to save this exchange to memory.
            **groq_kwargs: Passed through to groq.chat.completions.create().

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

        # Step 3: Call Groq
        response = self._client.chat.completions.create(
            model=model or self._model,
            messages=[
                {"role": "system", "content": full_system},
                {"role": "user", "content": message},
            ],
            **groq_kwargs,
        )
        reply_text = response.choices[0].message.content or ""

        # Step 4: Save this turn to memory
        if should_save:
            self._save_turn(message, reply_text)

        logger.debug(
            "[PDM-Groq] chat() | recalled=%d injected=%d model=%s",
            len(hits), len(trimmed), model or self._model,
        )
        return reply_text

    def _save_turn(self, user_msg: str, assistant_reply: str) -> None:
        """Save user message and assistant reply as memories."""
        try:
            if user_msg.strip():
                self._memory.save(
                    text=user_msg[:500],
                    source="groq_chat",
                    tags=["conversation", "user_input"],
                    p_magnitude=40.0,
                )
        except Exception as e:
            logger.warning("[PDM-Groq] Failed to save user message: %s", e)

        try:
            if assistant_reply.strip():
                self._memory.save(
                    text=assistant_reply[:500],
                    source="groq_chat",
                    tags=["conversation", "ai_reply"],
                    p_magnitude=35.0,
                )
        except Exception as e:
            logger.warning("[PDM-Groq] Failed to save assistant reply: %s", e)


def wrap_groq(
    client: Any,
    memory: Any,
    model: str = "llama-3.1-70b-versatile",
    max_memory_tokens: int = 1500,
    recall_k: int = 5,
    system_prompt: str = "You are a helpful AI assistant.",
) -> PDMGroqClient:
    """
    Create a memory-augmented Groq client.

    Args:
        client:            groq.Groq client instance.
        memory:            pdm_memory.Memory instance.
        model:             Default model (default: llama-3.1-70b-versatile).
        max_memory_tokens: Token budget for injected memories.
        recall_k:          Number of memories to recall per turn.
        system_prompt:     Base system prompt.

    Returns:
        PDMGroqClient wrapping the provided groq client.
    """
    return PDMGroqClient(
        groq_client=client,
        memory=memory,
        model=model,
        max_memory_tokens=max_memory_tokens,
        recall_k=recall_k,
        system_prompt=system_prompt,
    )
