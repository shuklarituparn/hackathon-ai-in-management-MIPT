"""Exercise the real Qwen-Agent loop against deterministic application boundaries."""

import copy

import pytest
from qwen_agent.agents.fncall_agent import FnCallAgent

from sverka.model_adapter import ImagePart, ModelMessage, ModelResponse, ToolCall, ToolSpec
from sverka.qwen_runtime import QWEN_AGENT_VERSION, run_agent

SPEC = ToolSpec(
    "read_evidence",
    "Read only this document's blocks.",
    {
        "type": "object",
        "properties": {"block_ids": {"type": "array", "items": {"type": "string"}}},
        "required": ["block_ids"],
        "additionalProperties": False,
    },
)


def response(*calls, text="", reasoning=None):
    return ModelResponse(ModelMessage("assistant", text, tool_calls=tuple(calls), reasoning_content=reasoning))


def execute(script, *, specs=(SPEC,), after_turn=None, dispatch=None, on_response=None):
    seen, calls, turns = [], [], []
    script = iter(script)

    def model(messages, tools):
        seen.append((list(messages), list(tools)))
        return next(script)

    def tool(call):
        calls.append(call)
        return dispatch(call) if dispatch else '{"ok":true}'

    def finish(result):
        turns.append(result)
        return after_turn(result) if after_turn else ([] if result.message.tool_calls else None)

    run_agent(
        messages=[ModelMessage("system", "Fixed instructions."), ModelMessage("user", "Read this report.")],
        specs=specs,
        call_model=model,
        dispatch=tool,
        on_response=on_response or (lambda result: None),
        after_turn=finish,
    )
    return seen, calls, turns


def test_actual_qwen_function_loop_owns_iteration_and_only_registered_tools(monkeypatch):
    entered = []
    original = FnCallAgent._run

    def spy(self, *args, **kwargs):
        entered.append(set(self.function_map))
        yield from original(self, *args, **kwargs)

    monkeypatch.setattr(FnCallAgent, "_run", spy)
    call = ToolCall("call_1", SPEC.name, '{"block_ids":["blk_a"]}')
    seen, calls, turns = execute([response(call), response(text="Done.")])

    assert QWEN_AGENT_VERSION == "0.0.34"
    assert entered == [{"read_evidence"}]
    assert calls == [call] and len(turns) == len(seen) == 2
    assert seen[0][1] == [SPEC]
    assert seen[0][1][0].parameters["additionalProperties"] is False


def test_parallel_calls_images_reasoning_and_tool_ids_survive_the_framework():
    first = ToolCall("call_a", SPEC.name, '{"block_ids":["blk_a"]}')
    second = ToolCall("call_b", SPEC.name, '{"block_ids":["blk_b"]}')
    image = ImagePart("image/png", b"\x89PNG scoped image", source="art_scoped")
    thought = "Inspect both source blocks."

    def after(result):
        return [ModelMessage("user", "Inspect this image.", images=(image,))] if result.message.tool_calls else None

    seen, calls, turns = execute(
        [response(first, second, text="Reading.", reasoning=thought), response(text="Done.")], after_turn=after
    )

    messages = seen[1][0]
    assert calls == [first, second]
    assert messages[2] == turns[0].message
    assert messages[2].reasoning_content == thought and messages[2].tool_calls == (first, second)
    assert [(m.role, m.tool_call_id) for m in messages[3:5]] == [("tool", "call_a"), ("tool", "call_b")]
    assert messages[5] == ModelMessage("user", "Inspect this image.", images=(image,))
    assert sum(m.role == "assistant" for m in messages) == 1


def test_prose_nudge_restarts_framework_with_existing_history():
    nudge = ModelMessage("user", "Submit a proposal instead of prose.")
    call = ToolCall("call_1", SPEC.name, "{}")

    def after(result):
        return None if result.message.tool_calls else [nudge]

    seen, calls, turns = execute([response(text="I will do it."), response(call)], after_turn=after)

    assert len(seen) == len(turns) == 2 and calls == [call]
    assert seen[1][0][-2:] == [ModelMessage("assistant", "I will do it."), nudge]


def test_continuation_beyond_framework_call_cap_keeps_exact_history():
    script = [response(ToolCall(f"call_{i}", SPEC.name, "{}")) for i in range(23)] + [response(text="Done.")]
    seen, calls, turns = execute(script)

    assert len(seen) == len(turns) == 24 and len(calls) == 23
    history = seen[-1][0]
    assert [m.tool_call_id for m in history if m.role == "tool"] == [f"call_{i}" for i in range(23)]
    assert len(history) == 2 + 2 * 23


def test_finished_validation_stops_before_an_extra_model_request():
    call = ToolCall("call_1", SPEC.name, "{}")
    seen, calls, turns = execute([response(call)], after_turn=lambda result: None)
    assert len(seen) == len(turns) == 1 and calls == [call]


def test_unknown_tools_and_malformed_arguments_reach_only_application_dispatch():
    malformed = ToolCall("call_bad", SPEC.name, "{bad-json")
    unknown = ToolCall("call_shell", "code_interpreter", '{"code":"unsafe"}')
    seen, calls, turns = execute(
        [response(malformed, unknown), response(text="Stopped.")],
        dispatch=lambda call: '{"ok":false,"error":"refused"}',
    )
    assert calls == [malformed, unknown]
    assert seen[0][1] == [SPEC]
    assert [m.text for m in seen[1][0] if m.role == "tool"] == ['{"ok":false,"error":"refused"}'] * 2


@pytest.mark.parametrize("boundary", ["model", "dispatch", "on_response", "after_turn"])
def test_domain_exceptions_propagate_without_becoming_tool_output(boundary, capsys):
    sentinel = RuntimeError("private domain cancellation")
    call = ToolCall("call_1", SPEC.name, "{}")

    def fail(*args):
        raise sentinel

    options = {
        "messages": [ModelMessage("user", "Run.")],
        "specs": [SPEC],
        "call_model": lambda messages, tools: response(call),
        "dispatch": lambda tool: "ok",
        "on_response": lambda result: None,
        "after_turn": lambda result: None,
    }
    options[{"model": "call_model"}.get(boundary, boundary)] = fail
    with pytest.raises(RuntimeError) as caught:
        run_agent(**options)
    assert caught.value is sentinel
    output = capsys.readouterr()
    assert "private domain cancellation" not in output.out + output.err


def test_invocations_have_separate_tool_scopes_and_schema_copies():
    original = copy.deepcopy(SPEC.parameters)
    second = ToolSpec("read_other", "A different document tool.", {"type": "object", "properties": {}})
    first_seen, _, _ = execute([response(text="One.")])
    second_seen, _, _ = execute([response(text="Two.")], specs=[second])

    assert [s.name for s in first_seen[0][1]] == [SPEC.name]
    assert [s.name for s in second_seen[0][1]] == [second.name]
    assert SPEC.parameters == original


def test_file_memory_is_never_constructed(monkeypatch):
    import qwen_agent.agents.fncall_agent as module

    def forbidden(*args, **kwargs):
        raise AssertionError("file memory should not be enabled")

    monkeypatch.setattr(module, "Memory", forbidden)
    execute([response(text="No implicit files or RAG.")])


def test_duplicate_tool_names_are_rejected_before_any_model_call():
    with pytest.raises(ValueError, match="duplicate"):
        execute([response(text="Never called.")], specs=[SPEC, SPEC])
