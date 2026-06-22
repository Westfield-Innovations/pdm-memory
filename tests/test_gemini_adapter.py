from unittest.mock import MagicMock
import pytest
from pdm_memory import Memory
from pdm_memory.core.signature import MemoryHit
from pdm_memory.integrations.gemini_adapter import wrap_gemini


def test_wrap_gemini_new_sdk():
    memory = MagicMock(spec=Memory)
    hit = MemoryHit(
        id="abc",
        text="User likes blue",
        source="test",
        drawer="general",
        pressure=80.0,
        p_raw=80.0,
        p_effective=80.0,
        decay_factor=0.0,
        intent_weight=1.0,
        v_coefficient=1.0,
        quality=0.80,
        last_reinforced=None,
        retrieval_count=1,
        intent_tags=["color"],
        domain="insight",
    )
    memory.recall.return_value = [hit]

    client = MagicMock()
    # Mock client.models.generate_content (new SDK)
    response = MagicMock()
    response.text = "This is a Gemini reply."
    client.models.generate_content.return_value = response

    wrapped = wrap_gemini(client, memory, model="gemini-2.5-flash")
    reply = wrapped.chat("My query")

    assert reply == "This is a Gemini reply."
    memory.recall.assert_called_once_with(query="My query", k=5)
    client.models.generate_content.assert_called_once()
    
    # Check arguments
    call_kwargs = client.models.generate_content.call_args[1]
    assert call_kwargs["model"] == "gemini-2.5-flash"
    assert call_kwargs["contents"] == "My query"
    
    config = call_kwargs["config"]
    # If the GenerateContentConfig could not import google.genai, it defaults to dict
    if isinstance(config, dict):
        assert "User likes blue" in config["system_instruction"]
    else:
        assert "User likes blue" in config.system_instruction

    # Check saves
    assert memory.save.call_count == 2
    memory.save.assert_any_call(
        text="My query",
        source="gemini_chat",
        tags=["conversation", "user_input"],
        p_magnitude=40.0,
    )
    memory.save.assert_any_call(
        text="This is a Gemini reply.",
        source="gemini_chat",
        tags=["conversation", "ai_reply"],
        p_magnitude=35.0,
    )


def test_wrap_gemini_legacy_sdk(monkeypatch):
    memory = MagicMock(spec=Memory)
    memory.recall.return_value = []

    # Mock legacy client (has generate_content, lacks models)
    client = MagicMock()
    del client.models  # Ensure client.models does not exist
    
    response = MagicMock()
    response.text = "Legacy reply"
    
    # We mock google and google.generativeai module and its GenerativeModel class
    mock_genai = MagicMock()
    mock_model = MagicMock()
    mock_model.generate_content.return_value = response
    mock_genai.GenerativeModel.return_value = mock_model
    
    import sys
    mock_google = MagicMock()
    mock_google.generativeai = mock_genai
    monkeypatch.setitem(sys.modules, "google", mock_google)
    monkeypatch.setitem(sys.modules, "google.generativeai", mock_genai)

    wrapped = wrap_gemini(client, memory, model="gemini-1.5-flash")
    # Set mock model name
    client.model_name = "models/gemini-1.5-flash"
    reply = wrapped.chat("Legacy query")

    assert reply == "Legacy reply"
    mock_genai.GenerativeModel.assert_called_once_with(
        model_name="models/gemini-1.5-flash",
        system_instruction="You are a helpful AI assistant.",
    )
    mock_model.generate_content.assert_called_once_with(contents="Legacy query")
