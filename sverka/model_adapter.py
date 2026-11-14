"""Model provider adapter (T08): one configured OpenAI-compatible chat-completions endpoint.

The harness talks to a model only through the normalized types below (``ModelMessage``, ``ToolCall``,
``ModelResponse``) and the ``interfaces.ModelProvider`` protocol, so tests run against deterministic fakes and
the wire format stays in this module.

Configuration is explicit and read from the environment by ``ProviderConfig.from_env``; see docs/harness.md.
The API key stays in the backend: it is sent only in the ``Authorization`` header, never appears in
``ProviderConfig.redacted()``, ``repr`` or error messages, and redirects are not followed.

Retries are not hidden here. ``complete`` makes one attempt and raises ``ProviderError`` with ``retryable`` and
``fatal`` flags; the harness decides whether to retry, and every attempt consumes its request budget.
"""

from __future__ import annotations

import base64
import http.client
import json
import math
import os
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

ADAPTER_VERSION = "1"

Role = Literal["system", "user", "assistant", "tool"]
ProviderErrorCode = Literal["missing_credentials", "provider_error", "timeout"]
SettingValue = str | int | float | bool | None

IMAGE_MEDIA_TYPES = ("image/png", "image/jpeg")

# ======================= normalized conversation =======================


@dataclass(frozen=True)
class ImagePart:
    """Image bytes for the model. ``source`` is a label for traces (an artifact ID); it is never sent."""

    media_type: str
    data: bytes = field(repr=False)
    source: str = ""

    def __post_init__(self) -> None:
        if self.media_type not in IMAGE_MEDIA_TYPES:
            raise ValueError(f"unsupported image media type: {self.media_type}")
        if not self.data:
            raise ValueError("an image part needs bytes")


@dataclass(frozen=True)
class ToolCall:
    """A tool request from the model. ``arguments`` is the raw JSON text exactly as the model produced it."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ModelMessage:
    """One conversation turn.

    Ordering rule kept by the harness: an assistant message with ``tool_calls`` is followed by exactly one
    ``tool`` message per call ID, in order, before any other message. Images travel in ``user`` messages.
    """

    role: Role
    text: str = ""
    images: tuple[ImagePart, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    reasoning_content: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only assistant messages carry tool calls")
        if self.images and self.role != "user":
            raise ValueError("only user messages carry images")
        if (self.role == "tool") != (self.tool_call_id is not None):
            raise ValueError("a tool message, and only a tool message, names its tool_call_id")
        if self.reasoning_content is not None:
            if self.role != "assistant" or not isinstance(self.reasoning_content, str):
                raise ValueError("reasoning_content must be a string on an assistant message")


@dataclass(frozen=True)
class ToolSpec:
    """A tool offered to the model. ``parameters`` is a JSON Schema object without ``$ref``."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class ModelResponse:
    message: ModelMessage
    finish_reason: str = "stop"
    model: str = ""
    usage: Usage = field(default_factory=Usage)
    response_id: str | None = None

    def __post_init__(self) -> None:
        if self.message.role != "assistant":
            raise ValueError("a model response is an assistant message")


class ProviderError(Exception):
    """A failed provider attempt.

    ``retryable``: another attempt may succeed (rate limit, 5xx, network, timeout).
    ``fatal``: no document can be processed until configuration changes (missing or rejected credentials,
    unknown model or endpoint); the harness stops the run instead of failing every document in turn.
    """

    def __init__(
        self,
        code: ProviderErrorCode,
        message: str,
        *,
        retryable: bool = False,
        fatal: bool = False,
        status: int | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code: ProviderErrorCode = code
        self.message = message
        self.retryable = retryable
        self.fatal = fatal
        self.status = status


# ======================= configuration =======================

ENV_PREFIX = "SVERKA_PROVIDER_"


def _env_number(env: Mapping[str, str], name: str, default: float, *, minimum: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} must be at least {minimum:g}")
    return value


@dataclass(frozen=True)
class ProviderConfig:
    """Explicit provider settings. Nothing is guessed: endpoint, model and key have no defaults."""

    base_url: str = field(default="", repr=False)  # may embed credentials; ``endpoint`` is the safe form
    model: str = ""
    api_key: str = field(default="", repr=False)
    name: str = "openai-compatible"
    auth: Literal["bearer", "none"] = "bearer"
    request_timeout: float = 120.0
    max_output_tokens: int = 8192
    max_tokens_field: str = "max_tokens"
    temperature: float | None = 0.0
    max_response_bytes: int = 8_000_000
    thinking: Literal["enabled", "disabled"] | None = None

    def __post_init__(self) -> None:
        if self.base_url and urlsplit(self.base_url).scheme not in ("http", "https"):
            raise ValueError(f"{ENV_PREFIX}BASE_URL must start with http:// or https://")
        if self.auth not in ("bearer", "none"):
            raise ValueError(f"{ENV_PREFIX}AUTH must be 'bearer' or 'none'")
        if self.max_tokens_field not in ("max_tokens", "max_completion_tokens"):
            raise ValueError(f"{ENV_PREFIX}MAX_TOKENS_FIELD must be 'max_tokens' or 'max_completion_tokens'")
        if self.thinking not in (None, "enabled", "disabled"):
            raise ValueError(f"{ENV_PREFIX}THINKING must be 'enabled', 'disabled', or unset")
        if (
            not isinstance(self.request_timeout, (int, float))
            or isinstance(self.request_timeout, bool)
            or not math.isfinite(self.request_timeout)
            or self.request_timeout <= 0
        ):
            raise ValueError(f"{ENV_PREFIX}TIMEOUT must be finite and positive")
        if (
            not isinstance(self.max_output_tokens, int)
            or isinstance(self.max_output_tokens, bool)
            or self.max_output_tokens < 0
        ):
            raise ValueError(f"{ENV_PREFIX}MAX_OUTPUT_TOKENS must be a nonnegative integer")
        if self.temperature is not None and (
            not isinstance(self.temperature, (int, float))
            or isinstance(self.temperature, bool)
            or not math.isfinite(self.temperature)
            or self.temperature < 0
        ):
            raise ValueError(f"{ENV_PREFIX}TEMPERATURE must be finite and nonnegative, or None")
        if (
            not isinstance(self.max_response_bytes, int)
            or isinstance(self.max_response_bytes, bool)
            or self.max_response_bytes < 1
        ):
            raise ValueError("max_response_bytes must be a positive integer")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ProviderConfig:
        """Read ``SVERKA_PROVIDER_*``. Unset endpoint/model/key are reported by ``missing()``, not here, so the
        application can start and show a clear configuration state."""
        env = os.environ if env is None else env
        temperature = env.get(f"{ENV_PREFIX}TEMPERATURE", "0").strip()
        return cls(
            base_url=env.get(f"{ENV_PREFIX}BASE_URL", "").strip(),
            model=env.get(f"{ENV_PREFIX}MODEL", "").strip(),
            api_key=env.get(f"{ENV_PREFIX}API_KEY", "").strip(),
            name=env.get(f"{ENV_PREFIX}NAME", "").strip() or "openai-compatible",
            auth=env.get(f"{ENV_PREFIX}AUTH", "").strip().lower() or "bearer",  # type: ignore[arg-type]
            request_timeout=_env_number(env, f"{ENV_PREFIX}TIMEOUT", 120.0, minimum=1),
            max_output_tokens=int(_env_number(env, f"{ENV_PREFIX}MAX_OUTPUT_TOKENS", 8192, minimum=0)),
            max_tokens_field=env.get(f"{ENV_PREFIX}MAX_TOKENS_FIELD", "").strip() or "max_tokens",
            temperature=_env_number(env, f"{ENV_PREFIX}TEMPERATURE", 0.0, minimum=0) if temperature else None,
            thinking=env.get(f"{ENV_PREFIX}THINKING", "").strip().lower() or None,  # type: ignore[arg-type]
        )

    def missing(self) -> list[str]:
        """Names of required settings that are not set. Empty means the provider can be called."""
        names = []
        if not self.base_url:
            names.append(f"{ENV_PREFIX}BASE_URL")
        if not self.model:
            names.append(f"{ENV_PREFIX}MODEL")
        if self.auth == "bearer" and not self.api_key:
            names.append(f"{ENV_PREFIX}API_KEY")
        return names

    @property
    def endpoint(self) -> str:
        """Endpoint without user info, query or fragment, which may carry credentials."""
        parts = urlsplit(self.base_url)
        if not parts.scheme or not parts.hostname:
            return ""
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{parts.hostname}{port}{parts.path.rstrip('/')}"

    def secret_values(self) -> tuple[str, ...]:
        """Strings that must never reach traces, replay manifests, logs or the model."""
        parts = urlsplit(self.base_url)
        candidates = (self.api_key, parts.password or "", parts.query)
        return tuple(s for s in candidates if len(s) >= 4)

    def redacted(self) -> dict[str, SettingValue]:
        """Settings safe to store in a replay manifest: no key, only whether one is set."""
        settings: dict[str, SettingValue] = {
            "provider": self.name,
            "model": self.model,
            "endpoint": self.endpoint,
            "auth": self.auth,
            "api_key_set": bool(self.api_key),
            "request_timeout": self.request_timeout,
            "max_output_tokens": self.max_output_tokens,
            "temperature": self.temperature,
        }
        if self.thinking is not None:
            settings["thinking"] = self.thinking
        return settings


# ======================= HTTP transport =======================

# (url, headers, body, timeout seconds) -> (HTTP status, response body). Raises TimeoutError when no complete
# response arrived in time and OSError for network failures. HTTP error statuses are returned, not raised.
Transport = Callable[[str, Mapping[str, str], bytes, float], tuple[int, bytes]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: the Authorization header must not travel to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_bounded(stream: Any, deadline: float, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    # HTTPResponse.read(n) tries to fill n bytes. A server that keeps dripping bytes can therefore hold
    # that call beyond the total deadline without triggering its socket inactivity timeout. read1 returns
    # after one buffered/raw read, letting us check the deadline and shorten the socket timeout each time.
    read = getattr(stream, "read1", stream.read)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("provider response did not complete in time")
        _set_read_timeout(stream, remaining)
        chunk = read(65536)
        if time.monotonic() > deadline:
            raise TimeoutError("provider response did not complete in time")
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > max_bytes:
            raise OSError(f"provider response is larger than {max_bytes} bytes")
        chunks.append(chunk)


def _set_read_timeout(stream: Any, timeout: float) -> None:
    """Set the remaining deadline on urllib's HTTPResponse socket (also wrapped by HTTPError).

    urllib exposes no public response timeout setter. Its response uses a BufferedReader over SocketIO;
    walk the small fp wrapper chain to reach that socket. Non-socket test/file streams need no adjustment.
    """
    for _ in range(4):
        raw = getattr(stream, "raw", None)
        sock = getattr(raw, "_sock", None)
        if sock is not None:
            sock.settimeout(timeout)
            return
        stream = getattr(stream, "fp", None)
        if stream is None:
            return


def urllib_transport(max_response_bytes: int = 8_000_000) -> Transport:
    """Standard-library HTTPS transport. Honors the usual proxy environment variables."""
    opener = urllib.request.build_opener(_NoRedirect)

    def send(url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
        deadline = time.monotonic() + timeout
        request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.status, _read_bounded(response, deadline, max_response_bytes)
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, _read_bounded(exc, deadline, max_response_bytes)
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TimeoutError(str(exc.reason)) from None
            raise OSError(str(exc.reason)) from None
        except http.client.HTTPException as exc:
            raise OSError(f"{type(exc).__name__}: {exc}") from None

    return send


# ======================= adapter =======================

_FATAL_STATUS = {401, 402, 403, 404}
_RETRYABLE_STATUS = {408, 409, 425, 429}


class OpenAICompatibleProvider:
    """``interfaces.ModelProvider`` for a chat-completions endpoint (``POST {base_url}/chat/completions``)."""

    def __init__(self, config: ProviderConfig, *, transport: Transport | None = None) -> None:
        self.config = config
        self.name = config.name
        self.model = config.model
        self._transport = transport or urllib_transport(config.max_response_bytes)

    def describe(self) -> dict[str, SettingValue]:
        return self.config.redacted()

    def secret_values(self) -> tuple[str, ...]:
        return self.config.secret_values()

    @property
    def requires_reasoning_replay(self) -> bool:
        """Whether assistant history needs reasoning state when tools are present.

        Explicit thinking mode opts in. DeepSeek also enables it by default; old saved chat prose has no
        such state, so the follow-up responder passes that history as quoted user-side data instead.
        """
        return self.config.thinking == "enabled" or (
            self.config.thinking is None and urlsplit(self.config.base_url).hostname == "api.deepseek.com"
        )

    def complete(self, messages: Sequence[ModelMessage], tools: Sequence[ToolSpec], *, timeout: float) -> ModelResponse:
        missing = self.config.missing()
        if missing:
            raise ProviderError(
                "missing_credentials", "the model provider is not configured: set " + ", ".join(missing), fatal=True
            )
        timeout = min(timeout, self.config.request_timeout)
        if timeout <= 0:
            raise ProviderError("timeout", "no time left for a provider request", retryable=False)
        body = json.dumps(self.build_payload(messages, tools), ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "sverka-harness"}
        if self.config.auth == "bearer":
            headers["Authorization"] = "Bearer " + self.config.api_key
        # Append to the URL path, not after a query string. User info and fragments are excluded: this
        # adapter authenticates through its explicit auth setting, while query parameters may configure
        # a gateway API version or signature.
        endpoint = urlsplit(self.config.endpoint)
        url = urlunsplit(
            (
                endpoint.scheme,
                endpoint.netloc,
                endpoint.path + "/chat/completions",
                urlsplit(self.config.base_url).query,
                "",
            )
        )
        try:
            status, raw = self._transport(url, headers, body, timeout)
        except TimeoutError:
            raise ProviderError("timeout", f"no provider response within {timeout:.0f} s", retryable=True) from None
        except OSError as exc:
            detail = self._scrub(f"{type(exc).__name__}: {exc}")
            raise ProviderError("provider_error", f"network error: {detail}", retryable=True) from None
        if status != 200:
            raise self._http_error(status, raw)
        return self.parse_response(raw)

    # ---------- request ----------

    def build_payload(self, messages: Sequence[ModelMessage], tools: Sequence[ToolSpec]) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": self.config.model, "messages": [_wire_message(m) for m in messages]}
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                }
                for t in tools
            ]
            payload["tool_choice"] = "auto"
        if self.config.temperature is not None:
            payload["temperature"] = self.config.temperature
        if self.config.max_output_tokens:
            payload[self.config.max_tokens_field] = self.config.max_output_tokens
        if self.config.thinking is not None:
            payload["thinking"] = {"type": self.config.thinking}
        return payload

    # ---------- response ----------

    def parse_response(self, raw: bytes) -> ModelResponse:
        try:
            data = json.loads(raw.decode("utf-8"))
            choice = data["choices"][0]
            message = choice["message"]
            if not isinstance(message, dict):
                raise TypeError("message is not an object")
            calls = tuple(_parse_tool_call(c) for c in message.get("tool_calls") or ())
            text = _content_text(message.get("content"))
            reasoning = message.get("reasoning_content")
            if reasoning is not None and not isinstance(reasoning, str):
                raise TypeError("reasoning_content is not a string")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            reason = f"{type(exc).__name__}: {exc}"
            raise ProviderError(
                "provider_error", self._scrub(f"malformed provider response ({reason})"), retryable=True
            ) from None
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return ModelResponse(
            message=ModelMessage(role="assistant", text=text, tool_calls=calls, reasoning_content=reasoning),
            finish_reason=str(choice.get("finish_reason") or "stop"),
            model=str(data.get("model") or self.config.model),
            usage=Usage(
                input_tokens=_int_or_none(usage.get("prompt_tokens")),
                output_tokens=_int_or_none(usage.get("completion_tokens")),
            ),
            response_id=str(data["id"]) if data.get("id") else None,
        )

    # ---------- errors ----------

    def _http_error(self, status: int, raw: bytes) -> ProviderError:
        detail = _error_detail(raw, self.config.secret_values())
        suffix = f": {detail}" if detail else ""
        if status == 401:
            return ProviderError(
                "missing_credentials",
                f"the provider rejected the configured credentials (HTTP 401){suffix}",
                fatal=True,
                status=status,
            )
        retryable = status in _RETRYABLE_STATUS or status >= 500
        return ProviderError(
            "provider_error",
            f"provider returned HTTP {status}{suffix}",
            retryable=retryable,
            fatal=status in _FATAL_STATUS,
            status=status,
        )

    def _scrub(self, text: str) -> str:
        return scrub(text, self.config.secret_values())


def scrub(text: str, secrets: Sequence[str]) -> str:
    """Replace every occurrence of a secret value in ``text``."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _wire_message(message: ModelMessage) -> dict[str, Any]:
    if message.role == "tool":
        return {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.text}
    if message.role == "assistant":
        wire: dict[str, Any] = {"role": "assistant", "content": message.text or None}
        if message.reasoning_content is not None:
            wire["reasoning_content"] = message.reasoning_content
        if message.tool_calls:
            wire["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in message.tool_calls
            ]
        else:
            wire["content"] = message.text
        return wire
    if message.images:
        parts: list[dict[str, Any]] = [{"type": "text", "text": message.text}] if message.text else []
        for image in message.images:
            encoded = base64.b64encode(image.data).decode("ascii")
            parts.append({"type": "image_url", "image_url": {"url": f"data:{image.media_type};base64,{encoded}"}})
        return {"role": message.role, "content": parts}
    return {"role": message.role, "content": message.text}


def _parse_tool_call(call: Any) -> ToolCall:
    function = call["function"]
    name = function["name"]
    if not isinstance(name, str) or not name:
        raise TypeError("tool call without a name")
    arguments = function.get("arguments")
    if arguments is None:
        arguments = ""
    elif not isinstance(arguments, str):  # some servers return an object instead of JSON text
        arguments = json.dumps(arguments, ensure_ascii=False)
    call_id = call.get("id")
    if not isinstance(call_id, str) or not call_id:
        call_id = f"call_{uuid.uuid4().hex[:16]}"  # the server sent none; pairing still needs a unique ID
    return ToolCall(id=call_id, name=name, arguments=arguments)


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # content parts: keep the text ones
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    raise TypeError("unsupported message content")


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _error_detail(raw: bytes, secrets: Sequence[str] = ()) -> str:
    """Short provider error text for operators. Never the whole body."""
    text = raw.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        return " ".join(scrub(text, secrets).split())[:300]
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        error = error.get("message") or error.get("code")
    if error is None and isinstance(data, dict):
        error = data.get("message") or data.get("detail")
    return " ".join(scrub(str(error or ""), secrets).split())[:300]
