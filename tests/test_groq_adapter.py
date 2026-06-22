from unittest.mock import MagicMock
from pdm_memory import Memory
from pdm_memory.core.signature import MemoryHit
from pdm_memory.integrations.groq_adapter import wrap_groq


def test_wrap_groq():
    memory = MagicMock(spec=Memory)
    hit = MemoryHit(
        id="abc",
        text="User preferred metric units",
        source="test",
        drawer="general",
        pressure=90.0,
        p_raw=90.0,
        p_effective=90.0,
        decay_factor=0.0,
        intent_weight=1.0,
        v_coefficient=1.0,
        quality=0.80,
        last_reinforced=None,
        retrieval_count=1,
        intent_tags=["units"],
        domain="insight",
    )
    memory.recall.return_value = [hit]

    client = MagicMock()
    # Mock client.chat.completions.create
    response = MagicMock()
    choice = MagicMock()
    choice.message.content = "Response from Groq."
    response.choices = [choice]
    client.chat.completions.create.return_value = response

    wrapped = wrap_groq(client, memory, model="llama-3.1-70b-versatile")
    reply = wrapped.chat("What units?")

    assert reply == "Response from Groq."
    memory.recall.assert_called_once_with(query="What units?", k=5)
    client.chat.completions.create.assert_called_once()
    
    call_kwargs = client.chat.completions.create.call_args[1]
    assert call_kwargs["model"] == "llama-3.1-70b-versatile"
    messages = call_kwargs["messages"]
    
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert "User preferred metric units" in messages[0]["content"]
    assert messages[1]["role"] == "user"
    assert messages[1]["content"] == "What units?"

    # Check saves
    assert memory.save.call_count == 2
    memory.save.assert_any_call(
        text="What units?",
        source="groq_chat",
        tags=["conversation", "user_input"],
        p_magnitude=40.0,
    )
    memory.save.assert_any_call(
        text="Response from Groq.",
        source="groq_chat",
        tags=["conversation", "ai_reply"],
        p_magnitude=35.0,
    )
