"""
OpenAI Adapter — Task 4.1

Wraps an OpenAI client so that PDM memories are automatically:
  1. Injected into the system prompt before each AI call.
  2. Saved back to memory after each AI response.

Usage:
    from pdm_memory.integrations import wrap_openai
    from pdm_memory import Memory

    mem = Memory(store="./my_app.db")
    client = wrap_openai(api_key="sk-...", memory=mem)

    # Memory is handled invisibly
    reply = client.chat("What units should I use?")

    # Or with more control:
    reply = client.chat(
        "What units should I use?",
        model="gpt-4o",
        system_prompt="You are a helpful assistant.",
        recall_k=5,
        save_reply=True,
    )
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


class PDMOpenAIClient:
    """
    Memory-augmented OpenAI client.

    All chat() calls automatically:
      - Recall relevant memories before the API call.
      - Inject them into the system prompt.
      - Save both the user message and AI reply as new memories.
    """

    def __init__(
        self,
        openai_client: Any,
        memory: Any,                  # pdm_memory.Memory instance
        model: str = "gpt-4o-mini",
        max_memory_tokens: int = 1500,
        recall_k: int = 5,
        auto_save: bool = True,
        system_prompt: str = "You are a helpful AI assistant.",
    ) -> None:
        self._client = openai_client
        self._memory = memory
        self._model = model
        self._max_memory_tokens = max_memory_tokens
        self._recall_k = recall_k
        self._auto_save = auto_save
        self._system_prompt = system_prompt

        from pdm_memory.integrations.context_manager import ContextWindowManager
        self._ctx_manager = ContextWindowManager(
            max_tokens=max_memory_tokens, model=model
        )

    def chat(
        self,
        message: str,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        recall_k: Optional[int] = None,
        save_reply: Optional[bool] = None,
        **openai_kwargs: Any,
    ) -> str:
        """
        Send a message to OpenAI with automatic PDM memory injection.

        Args:
            message:      The user's message.
            model:        Override the default model.
            system_prompt: Override the base system prompt.
            recall_k:     Override the number of memories to recall.
            save_reply:   Override whether to save this exchange to memory.
            **openai_kwargs: Passed through to openai.chat.completions.create().

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

        # Step 3: Call OpenAI
        response = self._client.chat.completions.create(
            model=model or self._model,
            messages=[
                {"role": "system", "content": full_system},
                {"role": "user", "content": message},
            ],
            **openai_kwargs,
        )
        reply_text = response.choices[0].message.content or ""

        # Step 4: Save this turn to memory
        if should_save:
            self._save_turn(message, reply_text)

        logger.debug(
            "[PDM-OpenAI] chat() | recalled=%d injected=%d model=%s",
            len(hits), len(trimmed), model or self._model,
        )
        return reply_text

    def _save_turn(self, user_msg: str, assistant_reply: str) -> None:
        """Save user message and assistant reply as memories."""
        try:
            if user_msg.strip():
                self._memory.save(
                    text=user_msg[:500],
                    source="openai_chat",
                    tags=["conversation", "user_input"],
                    p_magnitude=40.0,
                )
        except Exception as e:
            logger.warning("[PDM-OpenAI] Failed to save user message: %s", e)

        try:
            if assistant_reply.strip():
                self._memory.save(
                    text=assistant_reply[:500],
                    source="openai_chat",
                    tags=["conversation", "ai_reply"],
                    p_magnitude=35.0,
                )
        except Exception as e:
            logger.warning("[PDM-OpenAI] Failed to save assistant reply: %s", e)


def wrap_openai(
    api_key: str,
    memory: Any,
    model: str = "gpt-4o-mini",
    max_memory_tokens: int = 1500,
    recall_k: int = 5,
    system_prompt: str = "You are a helpful AI assistant.",
    **openai_init_kwargs: Any,
) -> PDMOpenAIClient:
    """
    Create a memory-augmented OpenAI client.

    Args:
        api_key:           Your OpenAI API key.
        memory:            pdm_memory.Memory instance.
        model:             Default model (default: gpt-4o-mini).
        max_memory_tokens: Token budget for injected memories.
        recall_k:          Number of memories to recall per turn.
        system_prompt:     Base system prompt.
        **openai_init_kwargs: Passed to openai.OpenAI().

    Returns:
        PDMOpenAIClient wrapping an openai.OpenAI client.
    """
    try:
        import openai
    except ImportError:
        raise ImportError(
            "openai package is required. Install it: pip install pdm-memory[openai]"
        )

    client = openai.OpenAI(api_key=api_key, **openai_init_kwargs)
    return PDMOpenAIClient(
        openai_client=client,
        memory=memory,
        model=model,
        max_memory_tokens=max_memory_tokens,
        recall_k=recall_k,
        system_prompt=system_prompt,
    )
