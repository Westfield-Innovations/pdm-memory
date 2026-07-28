# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Gemini Adapter

Wraps Google Gemini client for automatic PDM memory injection and saving.
Supports both the new google-genai Client and legacy google-generativeai GenerativeModel.

Usage:
    from pdm_memory.integrations import wrap_gemini
    from pdm_memory import Memory
    from google import genai

    mem = Memory(store="./my_app.db")
    client = genai.Client(api_key="...")
    wrapped_client = wrap_gemini(client, memory=mem)
    reply = wrapped_client.chat("What are my preferences?")
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class PDMGeminiClient:
    """
    Memory-augmented Gemini client.

    All chat() calls automatically:
      - Recall relevant memories before the API call.
      - Inject them into the system instruction/prompt.
      - Save both user message and AI reply as new memories.
    """

    def __init__(
        self,
        gemini_client: Any,
        memory: Any,                  # pdm_memory.Memory instance
        model: str = "gemini-2.5-flash",
        max_memory_tokens: int = 1500,
        recall_k: int = 5,
        auto_save: bool = True,
        system_prompt: str = "You are a helpful AI assistant.",
    ) -> None:
        self._client = gemini_client
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
        **gemini_kwargs: Any,
    ) -> str:
        """
        Send a message to Gemini with automatic PDM memory injection.

        Args:
            message:        The user's message.
            model:          Override the default model name.
            system_prompt:   Override the base system prompt.
            recall_k:       Override the number of memories to recall.
            save_reply:     Override whether to save this exchange to memory.
            **gemini_kwargs: Passed through to generate_content API calls.

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

        # Step 3: Call Gemini API
        if hasattr(self._client, "models") and hasattr(self._client.models, "generate_content"):
            # New google-genai Client SDK
            config = gemini_kwargs.pop("config", None)
            if config is None:
                try:
                    from google.genai import types
                    config = types.GenerateContentConfig(system_instruction=full_system)
                except ImportError:
                    config = {"system_instruction": full_system}
            else:
                if hasattr(config, "system_instruction"):
                    config.system_instruction = full_system
                elif isinstance(config, dict):
                    config["system_instruction"] = full_system

            response = self._client.models.generate_content(
                model=model_name,
                contents=message,
                config=config,
                **gemini_kwargs,
            )
            reply_text = response.text or ""
        elif hasattr(self._client, "generate_content"):
            # Legacy google-generativeai SDK (GenerativeModel object)
            # Recreate model with dynamic system_instruction or prepend to content
            try:
                import google.generativeai as genai
                temp_model = genai.GenerativeModel(
                    model_name=getattr(self._client, "model_name", model_name),
                    system_instruction=full_system,
                )
                response = temp_model.generate_content(
                    contents=message,
                    **gemini_kwargs,
                )
                reply_text = response.text or ""
            except ImportError:
                # Fallback: prepend system prompt as context if package is not importable
                # (unlikely if legacy client was passed)
                contents_with_ctx = f"System Instruction:\n{full_system}\n\nUser Input:\n{message}"
                response = self._client.generate_content(
                    contents=contents_with_ctx,
                    **gemini_kwargs,
                )
                reply_text = response.text or ""
        else:
            raise TypeError(
                "Unsupported Gemini client type. Expected a Client from google-genai "
                "or GenerativeModel from google-generativeai."
            )

        # Step 4: Save this turn to memory
        if should_save:
            self._save_turn(message, reply_text)

        logger.debug(
            "[PDM-Gemini] chat() | recalled=%d injected=%d model=%s",
            len(hits), len(trimmed), model_name,
        )
        return reply_text

    def _save_turn(self, user_msg: str, assistant_reply: str) -> None:
        """Save user message and assistant reply as memories."""
        try:
            if user_msg.strip():
                self._memory.save(
                    text=user_msg[:500],
                    source="gemini_chat",
                    tags=["conversation", "user_input"],
                    p_magnitude=40.0,
                )
        except Exception as e:
            logger.warning("[PDM-Gemini] Failed to save user message: %s", e)

        try:
            if assistant_reply.strip():
                self._memory.save(
                    text=assistant_reply[:500],
                    source="gemini_chat",
                    tags=["conversation", "ai_reply"],
                    p_magnitude=35.0,
                )
        except Exception as e:
            logger.warning("[PDM-Gemini] Failed to save assistant reply: %s", e)


def wrap_gemini(
    client: Any,
    memory: Any,
    model: str = "gemini-2.5-flash",
    max_memory_tokens: int = 1500,
    recall_k: int = 5,
    system_prompt: str = "You are a helpful AI assistant.",
) -> PDMGeminiClient:
    """
    Create a memory-augmented Gemini client.

    Args:
        client:            google-genai Client or google-generativeai GenerativeModel instance.
        memory:            pdm_memory.Memory instance.
        model:             Default model (default: gemini-2.5-flash).
        max_memory_tokens: Token budget for injected memories.
        recall_k:          Number of memories to recall per turn.
        system_prompt:     Base system prompt.

    Returns:
        PDMGeminiClient wrapping the provided client.
    """
    return PDMGeminiClient(
        gemini_client=client,
        memory=memory,
        model=model,
        max_memory_tokens=max_memory_tokens,
        recall_k=recall_k,
        system_prompt=system_prompt,
    )
