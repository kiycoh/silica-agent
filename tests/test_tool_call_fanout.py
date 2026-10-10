"""expand_tool_calls: fan out concatenated tool-call arg blobs (backends that
can't emit parallel tool_calls cram N objects into one arguments string)."""

import json

from silica.agent.llm import expand_tool_calls


def test_concatenated_objects_fan_out_to_separate_calls():
    blob = (
        '{"name": "Principio FIFO.md"}'
        '{"name": "Coda (Struttura dati)"}'
        '{"name": "Tipi di strutture dati"}'
    )
    parsed, wire = expand_tool_calls([("call_1", "silica_read_note", blob)])

    assert [c.name for c in parsed] == ["silica_read_note"] * 3
    assert [c.args["name"] for c in parsed] == [
        "Principio FIFO.md",
        "Coda (Struttura dati)",
        "Tipi di strutture dati",
    ]
    # Distinct ids and API-valid JSON args keep the assistant/tool pairing sound.
    assert len({c.id for c in parsed}) == 3
    assert [w["id"] for w in wire] == [c.id for c in parsed]
    for w in wire:
        json.loads(w["function"]["arguments"])  # must not raise


def test_single_object_passes_through_unchanged():
    parsed, wire = expand_tool_calls([("call_1", "silica_read_note", '{"name": "X"}')])
    assert len(parsed) == 1
    assert parsed[0].id == "call_1"
    assert parsed[0].args == {"name": "X"}


def test_unsalvageable_args_degrade_to_empty_dict():
    parsed, wire = expand_tool_calls([("call_1", "silica_read_note", "not json at all")])
    assert parsed[0].args == {}
    assert wire[0]["function"]["arguments"] == "{}"


if __name__ == "__main__":
    test_concatenated_objects_fan_out_to_separate_calls()
    test_single_object_passes_through_unchanged()
    test_unsalvageable_args_degrade_to_empty_dict()
    print("ok")
