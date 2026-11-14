"""Qwen-Agent's function loop with the application's scoped model/tool boundaries.

Qwen owns model/tool iteration. The callbacks own financial validation, request budgets, cancellation,
and persistence. No built-in file tools, RAG, model clients, or global tool registrations are enabled.
"""

from __future__ import annotations

import base64
import copy
from collections.abc import Callable, Sequence
from importlib.metadata import version

from qwen_agent.agents.fncall_agent import FnCallAgent
from qwen_agent.llm.base import BaseChatModel
from qwen_agent.llm.schema import ContentItem, FunctionCall, Message
from qwen_agent.tools.base import BaseTool

from .model_adapter import ModelMessage, ModelResponse, ToolCall, ToolSpec

QWEN_AGENT_VERSION = version("qwen-agent")
_SOURCE = "sverka_source"


class _Finished(Exception):
    """The application accepted the final outcome; stop before another model request."""


class _Tool(BaseTool):
    def __init__(self, spec: ToolSpec, dispatch: Callable[[ToolCall], str]):
        self.name, self.description = spec.name, spec.description
        self._spec, self._dispatch = spec, dispatch
        # Qwen's BaseTool rejects strict-schema keys such as additionalProperties. Publish the original
        # schema through function instead; argument acceptance stays with the application's validator.
        self.parameters = []
        super().__init__()

    @property
    def function(self) -> dict:
        return copy.deepcopy({"name": self.name, "description": self.description, "parameters": self._spec.parameters})

    def call(self, params: str, **kwargs) -> str:
        return self._dispatch(kwargs["call"])


class _Bridge(BaseChatModel):
    def __init__(self, specs, call_model, dispatch, on_response, after_turn):
        super().__init__({"model": "sverka-provider", "model_type": "sverka", "generate_cfg": {"max_retries": 0}})
        self.specs = tuple(copy.deepcopy(specs))
        if len({s.name for s in self.specs}) != len(self.specs):
            raise ValueError("duplicate Qwen-Agent tool names")
        self.call_model, self.dispatch = call_model, dispatch
        self.on_response, self.after_turn = on_response, after_turn
        self.sources: dict[str, ModelMessage] = {}
        self.history: list[Message] = []
        self.response: ModelResponse | None = None
        self.cursor = 0
        self.requests = 0

    @property
    def support_multimodal_input(self) -> bool:
        return True

    def encode(self, messages: Sequence[ModelMessage]) -> list[Message]:
        encoded = []
        for message in messages:
            source = str(len(self.sources))
            self.sources[source] = message
            extra = {_SOURCE: source}
            content = message.text
            if message.images:
                content = [ContentItem(text=message.text)] if message.text else []
                content.extend(
                    ContentItem(image=f"data:{i.media_type};base64,{base64.b64encode(i.data).decode('ascii')}")
                    for i in message.images
                )
            if message.tool_calls:
                # Qwen represents a parallel assistant batch as separate messages. Retain its source
                # identity so the next provider request reconstructs the original batch exactly once.
                for index, call in enumerate(message.tool_calls):
                    encoded.append(
                        Message(
                            role="assistant",
                            content=content if index == 0 else "",
                            reasoning_content=message.reasoning_content,
                            function_call=FunctionCall(call.name, call.arguments),
                            extra={**extra, "function_id": call.id},
                        )
                    )
            else:
                encoded.append(
                    Message(
                        role="function" if message.role == "tool" else message.role,
                        content=content,
                        reasoning_content=message.reasoning_content,
                        extra={**extra, **({"function_id": message.tool_call_id} if message.role == "tool" else {})},
                    )
                )
        return encoded

    def decode(self, messages: Sequence[Message]) -> list[ModelMessage]:
        decoded, seen = [], set()
        previous = None
        for message in messages:
            extra = message.extra or {}
            source = extra.get(_SOURCE)
            if source is not None:
                if source == previous:
                    continue
                if source not in self.sources or source in seen:
                    raise RuntimeError("Qwen-Agent changed a conversation source identity")
                decoded.append(self.sources[source])
                seen.add(source)
            elif message.role == "function":
                call_id = extra.get("function_id")
                if not isinstance(call_id, str) or not call_id or not isinstance(message.content, str):
                    raise RuntimeError("Qwen-Agent returned a tool result without its call ID or text")
                decoded.append(ModelMessage("tool", message.content, tool_call_id=call_id))
            else:
                raise RuntimeError("Qwen-Agent introduced an untracked conversation message")
            previous = source
        return decoded

    def advance(self, messages: list[Message]) -> None:
        self.history = messages
        if self.response is None:
            return
        response, self.response = self.response, None
        if self.cursor != len(response.message.tool_calls):
            raise RuntimeError("Qwen-Agent did not finish the model's tool batch")
        additions = self.after_turn(response)
        if additions is None:
            raise _Finished()
        messages.extend(self.encode(additions))

    def chat(self, messages, functions=None, stream=True, **kwargs):
        expected = [{"name": s.name, "description": s.description, "parameters": s.parameters} for s in self.specs]
        if (functions or []) != expected:
            raise RuntimeError("Qwen-Agent changed the allowed tool schemas")
        response = self.call_model(self.decode(messages), self.specs)
        self.response, self.cursor = response, 0
        self.requests += 1
        self.on_response(response)
        output = self.encode([response.message])
        return iter([output]) if stream else output

    # The public chat bridge deliberately bypasses stock prompt rewriting, automatic truncation,
    # text-only adapters, tokenization, caching and provider retries. The application supplies these.
    def _chat_with_functions(self, *args, **kwargs):
        raise NotImplementedError("use the normalized chat bridge")

    def _chat_stream(self, *args, **kwargs):
        raise NotImplementedError("use the normalized chat bridge")

    def _chat_no_stream(self, *args, **kwargs):
        raise NotImplementedError("use the normalized chat bridge")


class _Agent(FnCallAgent):
    def __init__(self, bridge: _Bridge):
        self.bridge = bridge
        # FnCallAgent otherwise constructs unused file/RAG Memory. All documents here are already
        # prepared and access is provided only by the application-bound tools below.
        self.mem = None
        super().__init__(
            llm=bridge,
            function_list=[_Tool(spec, bridge.dispatch) for spec in bridge.specs],
            system_message="",
        )

    def _call_llm(self, messages, **kwargs):
        self.bridge.advance(messages)
        return super()._call_llm(messages=messages, **kwargs)

    def _call_tool(self, tool_name, tool_args="{}", **kwargs):
        # The base implementation catches Exception and logs a traceback. Domain cancellation and
        # budget failures must instead reach the caller unchanged, without becoming model prose.
        bridge = self.bridge
        if bridge.response is None or bridge.cursor >= len(bridge.response.message.tool_calls):
            raise RuntimeError("Qwen-Agent dispatched an unrequested tool")
        call = bridge.response.message.tool_calls[bridge.cursor]
        bridge.cursor += 1
        if (tool_name, tool_args) != (call.name, call.arguments):
            raise RuntimeError("Qwen-Agent changed a tool request")
        tool = self.function_map.get(tool_name)
        return tool.call(tool_args, call=call) if tool is not None else bridge.dispatch(call)


def run_agent(
    *,
    messages: Sequence[ModelMessage],
    specs: Sequence[ToolSpec],
    call_model: Callable[[Sequence[ModelMessage], Sequence[ToolSpec]], ModelResponse],
    dispatch: Callable[[ToolCall], str],
    on_response: Callable[[ModelResponse], None],
    after_turn: Callable[[ModelResponse], Sequence[ModelMessage] | None],
) -> None:
    """Run Qwen's function-calling loop until the application's after_turn callback returns None.

    after_turn runs once after a complete tool batch (or a prose response). Returned messages are added
    before the next request; an empty sequence continues. Callback exceptions propagate unchanged.
    Application budgets bound the logical run across Qwen's finite internal loop segments.
    """
    bridge = _Bridge(specs, call_model, dispatch, on_response, after_turn)
    agent = _Agent(bridge)
    history = bridge.encode(messages)
    while True:
        requests_before = bridge.requests
        output = agent.run(messages=history)
        try:
            for _ in output:
                pass
            if bridge.requests == requests_before:
                raise RuntimeError("Qwen-Agent stopped without making a model request")
            bridge.advance(bridge.history)
            # Qwen can stop after a prose answer or its own loop cap. The application decides whether
            # a nudge/continuation is needed, with the complete history and its remaining budgets.
            history = bridge.history
        except _Finished:
            return
        finally:
            output.close()
