"""T08: provider adapter. Wire format, normalization, error classes and redaction. No external network."""

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sverka.model_adapter import (
    ImagePart,
    ModelMessage,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderError,
    ToolCall,
    ToolSpec,
)

KEY = "sk-test-0123456789"
CONFIG = ProviderConfig(base_url="https://llm.example/v1/", model="vision-1", api_key=KEY)
TOOL = ToolSpec("read_evidence", "Read blocks.", {"type": "object", "properties": {"block_ids": {"type": "array"}}})


def completion(message: dict, **extra) -> bytes:
    body = {"id": "resp_1", "model": "vision-1-0613", "choices": [{"message": message, "finish_reason": "tool_calls"}]}
    body.update(extra)
    return json.dumps(body).encode()


class Recorder:
    """A transport that records the request and returns a prepared answer or raises."""

    def __init__(self, status: int = 200, body: bytes = b"", error: Exception | None = None):
        self.status, self.body, self.error = status, body, error
        self.requests: list[tuple[str, dict, dict, float]] = []

    def __call__(self, url, headers, body, timeout):
        self.requests.append((url, dict(headers), json.loads(body), timeout))
        if self.error is not None:
            raise self.error
        return self.status, self.body


def test_request_keeps_conversation_order_tool_call_ids_and_images():
    png = b"\x89PNG fake bytes"
    messages = [
        ModelMessage("system", "rules"),
        ModelMessage("user", "extract"),
        ModelMessage(
            "assistant",
            tool_calls=(ToolCall("call_a", "inspect_page", '{"page": 1}'), ToolCall("call_b", "read_evidence", "{bad")),
        ),
        ModelMessage("tool", '{"ok": true}', tool_call_id="call_a"),
        ModelMessage("tool", '{"ok": false}', tool_call_id="call_b"),
        ModelMessage("user", "Image 1", images=(ImagePart("image/png", png, source="art_page"),)),
        ModelMessage("assistant", "done"),
    ]
    transport = Recorder(body=completion({"content": "ok"}))
    OpenAICompatibleProvider(CONFIG, transport=transport).complete(messages, [TOOL], timeout=30)

    url, headers, payload, timeout = transport.requests[0]
    assert url == "https://llm.example/v1/chat/completions"
    assert headers["Authorization"] == f"Bearer {KEY}"
    assert timeout == 30
    assert (payload["model"], payload["temperature"], payload["max_tokens"]) == ("vision-1", 0.0, 8192)
    assert payload["tool_choice"] == "auto"
    assert payload["tools"] == [
        {
            "type": "function",
            "function": {"name": "read_evidence", "description": "Read blocks.", "parameters": TOOL.parameters},
        }
    ]
    wire = payload["messages"]
    assert [m["role"] for m in wire] == ["system", "user", "assistant", "tool", "tool", "user", "assistant"]
    assert wire[2]["content"] is None
    assert wire[2]["tool_calls"] == [
        {"id": "call_a", "type": "function", "function": {"name": "inspect_page", "arguments": '{"page": 1}'}},
        {"id": "call_b", "type": "function", "function": {"name": "read_evidence", "arguments": "{bad"}},
    ]
    assert wire[3] == {"role": "tool", "tool_call_id": "call_a", "content": '{"ok": true}'}
    assert wire[4]["tool_call_id"] == "call_b"
    assert wire[5]["content"] == [
        {"type": "text", "text": "Image 1"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}},
    ]
    assert wire[6] == {"role": "assistant", "content": "done"}
    assert "art_page" not in json.dumps(payload)  # the trace label of an image is not sent


def test_optional_request_settings():
    transport = Recorder(body=completion({"content": "ok"}))
    config = ProviderConfig(
        base_url="http://localhost:11434/v1",
        model="local",
        auth="none",
        temperature=None,
        max_output_tokens=0,
        request_timeout=20,
    )
    OpenAICompatibleProvider(config, transport=transport).complete([ModelMessage("user", "hi")], [], timeout=300)
    _, headers, payload, timeout = transport.requests[0]
    assert "Authorization" not in headers
    assert set(payload) == {"model", "messages"}  # no tools, temperature or token limit unless configured
    assert timeout == 20  # never longer than the configured request timeout

    transport = Recorder(body=completion({"content": "ok"}))
    newer = ProviderConfig(
        base_url="https://x.example", model="m", api_key=KEY, max_tokens_field="max_completion_tokens"
    )
    OpenAICompatibleProvider(newer, transport=transport).complete([ModelMessage("user", "hi")], [], timeout=5)
    assert transport.requests[0][2]["max_completion_tokens"] == 8192


@pytest.mark.parametrize("thinking", ["enabled", "disabled"])
def test_explicit_thinking_mode_is_configured_sent_and_recorded(thinking):
    config = ProviderConfig.from_env(
        {
            "SVERKA_PROVIDER_BASE_URL": "https://api.deepseek.com",
            "SVERKA_PROVIDER_MODEL": "deepseek-flash",
            "SVERKA_PROVIDER_API_KEY": KEY,
            "SVERKA_PROVIDER_THINKING": thinking,
        }
    )
    transport = Recorder(body=completion({"content": "ok"}))
    provider = OpenAICompatibleProvider(config, transport=transport)
    provider.complete([ModelMessage("user", "hi")], [TOOL], timeout=5)

    assert transport.requests[0][2]["thinking"] == {"type": thinking}
    assert config.redacted()["thinking"] == thinking
    assert provider.requires_reasoning_replay is (thinking == "enabled")


def test_unset_thinking_preserves_normal_provider_payload_and_detects_deepseek_default():
    assert not OpenAICompatibleProvider(CONFIG).requires_reasoning_replay
    deepseek = OpenAICompatibleProvider(ProviderConfig(base_url="https://api.deepseek.com", model="deepseek-flash"))
    assert deepseek.requires_reasoning_replay
    assert "thinking" not in deepseek.config.redacted()
    assert "thinking" not in deepseek.build_payload([ModelMessage("user", "hi")], [TOOL])


@pytest.mark.parametrize("reasoning", ["", "Inspect the cited page, then read the amount."])
def test_assistant_reasoning_is_preserved_on_the_next_tool_request(reasoning):
    transport = Recorder(
        body=completion(
            {
                "content": None,
                "reasoning_content": reasoning,
                "tool_calls": [{"id": "call_read", "function": {"name": "read_evidence", "arguments": "{}"}}],
            }
        )
    )
    provider = OpenAICompatibleProvider(CONFIG, transport=transport)
    messages = [ModelMessage("user", "Check this amount.")]
    response = provider.complete(messages, [TOOL], timeout=5)
    messages.extend([response.message, ModelMessage("tool", '{"ok":true}', tool_call_id="call_read")])
    provider.complete(messages, [TOOL], timeout=5)

    assistant = transport.requests[1][2]["messages"][1]
    assert assistant["reasoning_content"] == reasoning
    assert assistant["tool_calls"][0]["id"] == "call_read"
    assert transport.requests[1][2]["messages"][2]["tool_call_id"] == "call_read"


@pytest.mark.parametrize("reasoning", [123, [], {"text": "not a string"}])
def test_malformed_reasoning_content_is_a_provider_error(reasoning):
    provider = OpenAICompatibleProvider(
        CONFIG, transport=Recorder(body=completion({"content": "ok", "reasoning_content": reasoning}))
    )
    with pytest.raises(ProviderError, match="reasoning_content"):
        provider.complete([ModelMessage("user", "hi")], [TOOL], timeout=5)


def test_endpoint_appends_path_before_query_and_excludes_url_user_info_and_fragment():
    transport = Recorder(body=completion({"content": "ok"}))
    config = ProviderConfig(
        base_url="https://user:password@llm.example:8443/api/v1/?api-version=7&sig=signature#fragment",
        model="m",
        api_key=KEY,
    )
    OpenAICompatibleProvider(config, transport=transport).complete([ModelMessage("user", "hi")], [], timeout=5)
    assert transport.requests[0][0] == "https://llm.example:8443/api/v1/chat/completions?api-version=7&sig=signature"
    assert transport.requests[0][1]["Authorization"] == f"Bearer {KEY}"


def test_response_is_normalized_with_tool_call_ids_and_raw_arguments():
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "inspect_page", "arguments": '{"page": 1}'}},
            {
                "id": "call_2",
                "type": "function",
                "function": {"name": "validate_extraction", "arguments": '{"proposal": '},
            },
            {
                "id": "call_3",
                "type": "function",
                "function": {"name": "read_cells", "arguments": {"sheet": "S", "ref": "A1"}},
            },
            {"type": "function", "function": {"name": "read_evidence", "arguments": None}},
        ],
    }
    body = completion(message, usage={"prompt_tokens": 120, "completion_tokens": 30})
    response = OpenAICompatibleProvider(CONFIG, transport=Recorder(body=body)).complete(
        [ModelMessage("user", "x")], [TOOL], timeout=5
    )

    calls = response.message.tool_calls
    assert [c.id for c in calls[:3]] == ["call_1", "call_2", "call_3"]
    assert calls[1].arguments == '{"proposal": '  # malformed JSON is passed on untouched for the harness to refuse
    assert json.loads(calls[2].arguments) == {"sheet": "S", "ref": "A1"}
    assert (
        calls[3].id.startswith("call_")
        and calls[3].id not in {"call_1", "call_2", "call_3"}
        and calls[3].arguments == ""
    )
    assert (response.message.role, response.message.text) == ("assistant", "")
    assert (response.finish_reason, response.model, response.response_id) == ("tool_calls", "vision-1-0613", "resp_1")
    assert (response.usage.input_tokens, response.usage.output_tokens) == (120, 30)


def test_text_content_parts_are_joined():
    parts = [
        {"type": "text", "text": "Hello "},
        {"type": "refusal", "refusal": "no"},
        {"type": "text", "text": "world"},
    ]
    provider = OpenAICompatibleProvider(CONFIG, transport=Recorder(body=completion({"content": parts})))
    assert provider.complete([ModelMessage("user", "x")], [], timeout=5).message.text == "Hello world"


@pytest.mark.parametrize(
    "body",
    [
        b"<html>Bad gateway</html>",
        b"{}",
        b'{"choices": []}',
        b'{"choices": [{"message": "text"}]}',
        b'{"choices": [{"message": {"content": 5}}]}',
        b'{"choices": [{"message": {"tool_calls": [{"id": "c", "function": {"arguments": "{}"}}]}}]}',
    ],
)
def test_malformed_provider_response_is_a_retryable_provider_error(body):
    provider = OpenAICompatibleProvider(CONFIG, transport=Recorder(body=body))
    with pytest.raises(ProviderError) as caught:
        provider.complete([ModelMessage("user", "x")], [], timeout=5)
    assert (caught.value.code, caught.value.retryable, caught.value.fatal) == ("provider_error", True, False)
    assert "malformed provider response" in caught.value.message


@pytest.mark.parametrize(
    ("status", "code", "retryable", "fatal"),
    [
        (401, "missing_credentials", False, True),
        (402, "provider_error", False, True),
        (403, "provider_error", False, True),
        (404, "provider_error", False, True),
        (400, "provider_error", False, False),
        (413, "provider_error", False, False),
        (429, "provider_error", True, False),
        (500, "provider_error", True, False),
        (503, "provider_error", True, False),
        (302, "provider_error", False, False),
    ],
)
def test_http_errors_are_classified(status, code, retryable, fatal):
    body = json.dumps({"error": {"message": f"key {KEY} is not allowed", "type": "x"}}).encode()
    provider = OpenAICompatibleProvider(CONFIG, transport=Recorder(status=status, body=body))
    with pytest.raises(ProviderError) as caught:
        provider.complete([ModelMessage("user", "x")], [], timeout=5)
    error = caught.value
    assert (error.code, error.retryable, error.fatal, error.status) == (code, retryable, fatal, status)
    assert KEY not in error.message and KEY not in str(error)
    assert "is not allowed" in error.message  # the provider's explanation is kept for the operator


@pytest.mark.parametrize("as_json", [True, False])
def test_provider_error_redacts_secrets_before_truncating_the_message(as_json):
    detail = "x" * 290 + KEY + " is not allowed"
    body = json.dumps({"error": {"message": detail}}).encode() if as_json else detail.encode()
    provider = OpenAICompatibleProvider(CONFIG, transport=Recorder(status=400, body=body))
    with pytest.raises(ProviderError) as caught:
        provider.complete([ModelMessage("user", "x")], [], timeout=5)
    assert KEY not in caught.value.message
    assert KEY[:10] not in caught.value.message
    assert "[redacted]" in caught.value.message


def test_timeouts_and_network_failures_are_explicit():
    provider = OpenAICompatibleProvider(CONFIG, transport=Recorder(error=TimeoutError("timed out")))
    with pytest.raises(ProviderError) as caught:
        provider.complete([ModelMessage("user", "x")], [], timeout=7)
    assert (caught.value.code, caught.value.retryable) == ("timeout", True)
    assert "7 s" in caught.value.message

    provider = OpenAICompatibleProvider(CONFIG, transport=Recorder(error=OSError(f"proxy refused {KEY}")))
    with pytest.raises(ProviderError) as caught:
        provider.complete([ModelMessage("user", "x")], [], timeout=7)
    assert (caught.value.code, caught.value.retryable) == ("provider_error", True)
    assert KEY not in caught.value.message

    with pytest.raises(ProviderError) as caught:
        provider.complete([ModelMessage("user", "x")], [], timeout=0)
    assert caught.value.code == "timeout"


def test_missing_configuration_is_reported_without_sending_anything():
    transport = Recorder()
    provider = OpenAICompatibleProvider(ProviderConfig(), transport=transport)
    with pytest.raises(ProviderError) as caught:
        provider.complete([ModelMessage("user", "x")], [], timeout=5)
    error = caught.value
    assert (error.code, error.fatal, error.retryable) == ("missing_credentials", True, False)
    for name in ("SVERKA_PROVIDER_BASE_URL", "SVERKA_PROVIDER_MODEL", "SVERKA_PROVIDER_API_KEY"):
        assert name in error.message
    assert transport.requests == []
    assert ProviderConfig(base_url="http://localhost:1/v1", model="m", auth="none").missing() == []
    assert ProviderConfig(base_url="http://localhost:1/v1", model="m").missing() == ["SVERKA_PROVIDER_API_KEY"]


def test_configuration_from_environment_is_explicit_and_redacted():
    env = {
        "SVERKA_PROVIDER_BASE_URL": " https://user:pa55word@llm.example:8443/api/v1/?api-version=7&sig=s3cr3tsig ",
        "SVERKA_PROVIDER_MODEL": "vision-1",
        "SVERKA_PROVIDER_API_KEY": KEY,
        "SVERKA_PROVIDER_NAME": "team-gateway",
        "SVERKA_PROVIDER_TIMEOUT": "45",
        "SVERKA_PROVIDER_MAX_OUTPUT_TOKENS": "2048",
        "SVERKA_PROVIDER_TEMPERATURE": "",
        "SVERKA_LLM_URL": "http://legacy-classifier:11434/v1",  # the legacy fallback setting is not the provider
    }
    config = ProviderConfig.from_env(env)
    assert (config.name, config.model, config.request_timeout, config.max_output_tokens) == (
        "team-gateway",
        "vision-1",
        45.0,
        2048,
    )
    assert config.temperature is None and config.missing() == []
    redacted = config.redacted()
    assert redacted == {
        "provider": "team-gateway",
        "model": "vision-1",
        "endpoint": "https://llm.example:8443/api/v1",
        "auth": "bearer",
        "api_key_set": True,
        "request_timeout": 45.0,
        "max_output_tokens": 2048,
        "temperature": None,
    }
    shown = json.dumps(redacted) + repr(config) + str(OpenAICompatibleProvider(config).describe())
    for secret in (KEY, "pa55word", "s3cr3tsig"):
        assert secret not in shown
    assert set(config.secret_values()) == {KEY, "pa55word", "api-version=7&sig=s3cr3tsig"}

    defaults = ProviderConfig.from_env({})
    assert defaults.missing() == ["SVERKA_PROVIDER_BASE_URL", "SVERKA_PROVIDER_MODEL", "SVERKA_PROVIDER_API_KEY"]
    assert (defaults.temperature, defaults.request_timeout, defaults.auth) == (0.0, 120.0, "bearer")
    assert defaults.redacted()["api_key_set"] is False and defaults.endpoint == ""


@pytest.mark.parametrize(
    ("env", "match"),
    [
        ({"SVERKA_PROVIDER_TIMEOUT": "soon"}, "SVERKA_PROVIDER_TIMEOUT"),
        ({"SVERKA_PROVIDER_TIMEOUT": "0"}, "SVERKA_PROVIDER_TIMEOUT"),
        ({"SVERKA_PROVIDER_BASE_URL": "file:///etc/passwd"}, "SVERKA_PROVIDER_BASE_URL"),
        ({"SVERKA_PROVIDER_AUTH": "basic"}, "SVERKA_PROVIDER_AUTH"),
        ({"SVERKA_PROVIDER_MAX_TOKENS_FIELD": "n"}, "SVERKA_PROVIDER_MAX_TOKENS_FIELD"),
        ({"SVERKA_PROVIDER_THINKING": "sometimes"}, "SVERKA_PROVIDER_THINKING"),
    ],
)
def test_invalid_configuration_names_the_setting(env, match):
    with pytest.raises(ValueError, match=match):
        ProviderConfig.from_env(env)


@pytest.mark.parametrize("name", ["TIMEOUT", "TEMPERATURE", "MAX_OUTPUT_TOKENS"])
@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_nonfinite_environment_settings_are_rejected_with_the_setting_name(name, value):
    setting = "SVERKA_PROVIDER_" + name
    with pytest.raises(ValueError, match=setting):
        ProviderConfig.from_env({setting: value})


@pytest.mark.parametrize(
    ("field", "name"),
    [
        ("request_timeout", "SVERKA_PROVIDER_TIMEOUT"),
        ("temperature", "SVERKA_PROVIDER_TEMPERATURE"),
        ("max_output_tokens", "SVERKA_PROVIDER_MAX_OUTPUT_TOKENS"),
        ("max_response_bytes", "max_response_bytes"),
    ],
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_direct_configuration_is_rejected(field, name, value):
    with pytest.raises(ValueError, match=name):
        ProviderConfig(**{field: value})


def test_message_shapes_are_checked():
    with pytest.raises(ValueError, match="reasoning_content"):
        ModelMessage("user", "x", reasoning_content="assistant state")
    with pytest.raises(ValueError, match="reasoning_content"):
        ModelMessage("assistant", "x", reasoning_content=123)
    with pytest.raises(ValueError):
        ModelMessage("tool", "result")  # no tool_call_id
    with pytest.raises(ValueError):
        ModelMessage("user", "x", tool_call_id="call_1")
    with pytest.raises(ValueError):
        ModelMessage("user", "x", tool_calls=(ToolCall("c", "t", "{}"),))
    with pytest.raises(ValueError):
        ModelMessage("assistant", "x", images=(ImagePart("image/png", b"x"),))
    with pytest.raises(ValueError):
        ImagePart("application/pdf", b"%PDF")
    assert "data" not in repr(ImagePart("image/png", b"secret-bytes"))


# ======================= real HTTP transport against a local server =======================


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.server.seen.append((self.path, self.headers.get("Authorization"), payload))
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/v1/chat/completions")
            self.end_headers()
            return
        if self.path.startswith("/slow"):
            time.sleep(1.0)
        if self.path.startswith("/busy"):
            body, status = json.dumps({"error": {"message": "rate limited"}}).encode(), 429
        else:
            call = {
                "id": "call_live",
                "type": "function",
                "function": {"name": "read_evidence", "arguments": '{"block_ids": ["blk_a"]}'},
            }
            body, status = completion({"role": "assistant", "content": None, "tool_calls": [call]}), 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            if self.path.startswith("/drip"):
                for offset in range(0, len(body), 4):
                    self.wfile.write(body[offset : offset + 4])
                    self.wfile.flush()
                    time.sleep(0.03)
            else:
                self.wfile.write(body)
        except OSError:
            pass  # the client gave up (timeout test)

    def log_message(self, *args):
        pass


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.seen = []
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def _local(server, path: str = "/v1") -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        ProviderConfig(base_url=f"http://127.0.0.1:{server.server_port}{path}", model="local-test", api_key=KEY)
    )


def test_http_roundtrip_with_tool_call(server):
    response = _local(server).complete([ModelMessage("user", "Привет")], [TOOL], timeout=10)
    assert [(c.id, c.name) for c in response.message.tool_calls] == [("call_live", "read_evidence")]
    path, authorization, payload = server.seen[0]
    assert (path, authorization) == ("/v1/chat/completions", f"Bearer {KEY}")
    assert payload["messages"] == [{"role": "user", "content": "Привет"}]


def test_http_error_status_and_redirect_are_not_followed_or_hidden(server):
    with pytest.raises(ProviderError) as caught:
        _local(server, "/busy").complete([ModelMessage("user", "x")], [], timeout=10)
    assert (caught.value.status, caught.value.retryable) == (429, True)
    assert "rate limited" in caught.value.message

    with pytest.raises(ProviderError) as caught:
        _local(server, "/redirect").complete([ModelMessage("user", "x")], [], timeout=10)
    assert caught.value.status == 302
    assert [path for path, _, _ in server.seen] == ["/busy/chat/completions", "/redirect/chat/completions"]


def test_http_timeout_is_a_timeout_error(server):
    started = time.monotonic()
    with pytest.raises(ProviderError) as caught:
        _local(server, "/slow").complete([ModelMessage("user", "x")], [], timeout=0.2)
    assert caught.value.code == "timeout"
    assert time.monotonic() - started < 0.9


def test_http_body_deadline_stops_a_response_that_drips_within_the_idle_timeout(server):
    # The server sends four bytes every30ms, so its full response needs well over one second while no
    # individual socket read would hit this150ms idle timeout. The total body deadline must stop it.
    started = time.monotonic()
    with pytest.raises(ProviderError) as caught:
        _local(server, "/drip").complete([ModelMessage("user", "x")], [], timeout=0.15)
    elapsed = time.monotonic() - started
    assert caught.value.code == "timeout"
    assert elapsed < 0.8


def test_connection_refused_is_a_retryable_provider_error(server):
    port = server.server_port
    server.shutdown()
    server.server_close()
    provider = OpenAICompatibleProvider(
        ProviderConfig(base_url=f"http://127.0.0.1:{port}/v1", model="local-test", api_key=KEY)
    )
    with pytest.raises(ProviderError) as caught:
        provider.complete([ModelMessage("user", "x")], [], timeout=5)
    assert (caught.value.code, caught.value.retryable) == ("provider_error", True)
