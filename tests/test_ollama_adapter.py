from unittest.mock import MagicMock
from pdm_memory import Memory
from pdm_memory.core.signature import MemoryHit
from pdm_memory.integrations.ollama_adapter import wrap_ollama


def test_wrap_ollama():
    memory = MagicMock(spec=Memory)
    hit = MemoryHit(
        id="abc",
        text="User prefers short responses",
        source="test",
        drawer="general",
        pressure=85.0,
        p_raw=85.0,
        p_effective=85.0,
        decay_factor=0.0,
        intent_weight=1.0,
        v_coefficient=1.0,
        quality=0.80,
        last_reinforced=None,
        retrieval_count=1,
        intent_tags=["style"],
        domain="insight",
    )
    memory.recall.return_value = [hit]

    client = MagicMock()
    response = {
        "model": "llama3",
        "message": {
            "role": "assistant",
            "content": "This is an Ollama response.",
        },
    }
    client.chat.return_value = response

    wrapped = wrap_ollama(client, memory, model="llama3")
    reply = wrapped.chat("Preferences?")

    assert reply == "This is an Ollama response."
    memory.recall.assert_called_once_with(query="Preferences?", k=5)
    client.chat.assert_called_once()
    
    call_kwargs = client.chat.call_args[1]
    assert call_kwargs["model"] == "llama3"
    messages = call_kwargs["messages"]
    
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert "User prefers short responses" in messages[0]["content"]
    assert messages[1]["role"] == "user"
    assert messages[1]["content"] == "Preferences?"

    assert memory.save.call_count == 2
    memory.save.assert_any_call(
        text="Preferences?",
        source="ollama_chat",
        tags=["conversation", "user_input"],
        p_magnitude=40.0,
    )
    memory.save.assert_any_call(
        text="This is an Ollama response.",
        source="ollama_chat",
        tags=["conversation", "ai_reply"],
        p_magnitude=35.0,
    )
