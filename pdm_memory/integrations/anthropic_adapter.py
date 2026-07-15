# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Anthropic Adapter — Task 4.1

Wraps an Anthropic client for automatic PDM memory injection and saving.

Usage:
    from pdm_memory.integrations import wrap_anthropic
    from pdm_memory import Memory

    mem = Memory(store="./my_app.db")
    client = wrap_anthropic(api_key="sk-ant-...", memory=mem)
    reply = client.chat("What units should I use?")
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class PDMAnthropicClient:
    """
    Memory-augmented Anthropic client.

    All chat() calls automatically:
      - Recall relevant memories before the API call.
      - Inject them via the system prompt.
      - Save both turns as new memories.
    """

    def __init__(
        self,
        anthropic_client: Any,
        memory: Any,
        model: str = "claude-3-haiku-20240307",
        max_memory_tokens: int = 1500,
        recall_k: int = 5,
        auto_save: bool = True,
        system_prompt: str = "You are a helpful AI assistant.",
    ) -> None:
        self._client = anthropic_client
        self._memory = memory
        self._model = model
        self._max_memory_tokens = max_memory_tokens
        self._recall_k = recall_k
        self._auto_save = auto_save
        self._system_prompt = system_prompt

        from pdm_memory.integrations.context_manager import ContextWindowManager
        self._ctx_manager = ContextWindowManager(
            max_tokens=max_memory_tokens,
            model="gpt-4o-mini",  # Use cl100k_base approximation for Anthropic
        )

    def chat(
        self,
        message: str,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        recall_k: Optional[int] = None,
        save_reply: Optional[bool] = None,
        max_tokens: int = 1024,
        **anthropic_kwargs: Any,
    ) -> str:
        """
        Send a message to Anthropic Claude with automatic PDM memory injection.

        Args:
            message:      The user's message.
            model:        Override the default model.
            system_prompt: Override the base system prompt.
            recall_k:     Override the number of memories to recall.
            save_reply:   Override whether to save this exchange to memory.
            max_tokens:   Max tokens for Claude's response.
            **anthropic_kwargs: Passed to anthropic.messages.create().

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

        # Step 3: Call Anthropic
        response = self._client.messages.create(
            model=model or self._model,
            max_tokens=max_tokens,
            system=full_system,
            messages=[{"role": "user", "content": message}],
            **anthropic_kwargs,
        )
        reply_text = response.content[0].text if response.content else ""

        # Step 4: Save this turn to memory
        if should_save:
            self._save_turn(message, reply_text)

        logger.debug(
            "[PDM-Anthropic] chat() | recalled=%d injected=%d model=%s",
            len(hits), len(trimmed), model or self._model,
        )
        return reply_text

    def _save_turn(self, user_msg: str, assistant_reply: str) -> None:
        try:
            if user_msg.strip():
                self._memory.save(
                    text=user_msg[:500],
                    source="anthropic_chat",
                    tags=["conversation", "user_input"],
                    p_magnitude=40.0,
                )
        except Exception as e:
            logger.warning("[PDM-Anthropic] Failed to save user message: %s", e)

        try:
            if assistant_reply.strip():
                self._memory.save(
                    text=assistant_reply[:500],
                    source="anthropic_chat",
                    tags=["conversation", "ai_reply"],
                    p_magnitude=35.0,
                )
        except Exception as e:
            logger.warning("[PDM-Anthropic] Failed to save assistant reply: %s", e)


def wrap_anthropic(
    api_key: str,
    memory: Any,
    model: str = "claude-3-haiku-20240307",
    max_memory_tokens: int = 1500,
    recall_k: int = 5,
    system_prompt: str = "You are a helpful AI assistant.",
    **anthropic_init_kwargs: Any,
) -> PDMAnthropicClient:
    """
    Create a memory-augmented Anthropic client.

    Args:
        api_key:           Your Anthropic API key.
        memory:            pdm_memory.Memory instance.
        model:             Default Claude model.
        max_memory_tokens: Token budget for injected memories.
        recall_k:          Memories to recall per turn.
        system_prompt:     Base system prompt.
        **anthropic_init_kwargs: Passed to anthropic.Anthropic().

    Returns:
        PDMAnthropicClient wrapping an anthropic.Anthropic client.
    """
    try:
        import anthropic
    except ImportError:
        raise ImportError(
            "anthropic package is required. Install it: pip install pdm-memory[anthropic]"
        )

    client = anthropic.Anthropic(api_key=api_key, **anthropic_init_kwargs)
    return PDMAnthropicClient(
        anthropic_client=client,
        memory=memory,
        model=model,
        max_memory_tokens=max_memory_tokens,
        recall_k=recall_k,
        system_prompt=system_prompt,
    )
