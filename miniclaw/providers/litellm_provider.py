"""LiteLLM provider implementation for multi-provider support."""

import json
from typing import Any

import litellm
from litellm import acompletion, aembedding

from miniclaw.providers.base import LLMProvider, LLMResponse, LLMStreamEvent, ToolCallRequest


class LiteLLMProvider(LLMProvider):
    """
    LLM provider using LiteLLM for multi-provider support.
    
    Supports OpenRouter, Anthropic, OpenAI, Gemini, and many other providers through
    a unified interface.
    """
    
    # Thinking token budget mapping
    THINKING_BUDGETS = {
        "off": 0,
        "low": 1024,
        "medium": 4096,
        "high": 16384,
    }

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "anthropic/claude-opus-4-5",
        extra_headers: dict[str, str] | None = None,
        thinking: str = "off",
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}
        self.thinking = thinking
        
        # Detect OpenRouter by api_key prefix or explicit api_base
        self.is_openrouter = (
            (api_key and api_key.startswith("sk-or-")) or
            (api_base and "openrouter" in api_base)
        )
        
        # Detect AiHubMix by api_base
        self.is_aihubmix = bool(api_base and "aihubmix" in api_base)
        
        # Track if using custom endpoint (vLLM, etc.)
        self.is_vllm = bool(api_base) and not self.is_openrouter and not self.is_aihubmix
        
        # Disable LiteLLM logging noise
        litellm.suppress_debug_info = True
    
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        thinking: str | None = None,
    ) -> LLMResponse:
        """
        Send a chat completion request via LiteLLM.
        
        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions in OpenAI format.
            model: Model identifier (e.g., 'anthropic/claude-sonnet-4-5').
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.
        
        Returns:
            LLMResponse with content and/or tool calls.
        """
        kwargs = self._build_completion_kwargs(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            thinking=thinking,
            stream=False,
        )

        try:
            response = await acompletion(**kwargs)
            return self._parse_response(response)
        except Exception as e:
            msg = str(e).lower()
            if "overload" in msg or "overloaded" in msg or "503" in msg or "service unavailable" in msg:
                return LLMResponse(content="", finish_reason="overloaded")
            # Return error as content for graceful handling
            return LLMResponse(
                content=f"Error calling LLM: {str(e)}",
                finish_reason="error",
            )

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        thinking: str | None = None,
    ):
        """Stream response deltas and then emit a final parsed response."""
        kwargs = self._build_completion_kwargs(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            thinking=thinking,
            stream=True,
        )
        try:
            stream = await acompletion(**kwargs)
        except Exception as e:
            msg = str(e).lower()
            if "overload" in msg or "overloaded" in msg or "503" in msg or "service unavailable" in msg:
                yield LLMStreamEvent(type="final", response=LLMResponse(content="", finish_reason="overloaded"))
                return
            yield LLMStreamEvent(
                type="final",
                response=LLMResponse(content=f"Error calling LLM: {str(e)}", finish_reason="error"),
            )
            return

        content_parts: list[str] = []
        tool_accumulator: dict[int, dict[str, str]] = {}
        finish_reason = "stop"
        usage: dict[str, int] = {}

        try:
            async for chunk in stream:
                choice = self._get_first_choice(chunk)
                if choice is None:
                    continue

                delta = self._obj_get(choice, "delta")
                if delta is not None:
                    text = self._extract_delta_text(delta)
                    if text:
                        content_parts.append(text)
                        yield LLMStreamEvent(type="delta", delta=text)
                    self._accumulate_tool_calls(delta, tool_accumulator)

                fr = self._obj_get(choice, "finish_reason")
                if fr:
                    finish_reason = fr

                chunk_usage = self._obj_get(chunk, "usage")
                if chunk_usage:
                    usage = {
                        "prompt_tokens": int(self._obj_get(chunk_usage, "prompt_tokens", 0) or 0),
                        "completion_tokens": int(self._obj_get(chunk_usage, "completion_tokens", 0) or 0),
                        "total_tokens": int(self._obj_get(chunk_usage, "total_tokens", 0) or 0),
                    }
        except Exception as e:
            yield LLMStreamEvent(
                type="final",
                response=LLMResponse(content=f"Error parsing LLM stream: {str(e)}", finish_reason="error"),
            )
            return

        final = LLMResponse(
            content="".join(content_parts),
            tool_calls=self._parse_stream_tool_calls(tool_accumulator),
            finish_reason=finish_reason,
            usage=usage,
        )
        yield LLMStreamEvent(type="final", response=final)

    async def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        """Generate embeddings via LiteLLM."""
        model = model or self.default_model
        kwargs: dict[str, Any] = {"model": model, "input": texts}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.extra_headers:
            kwargs["extra_headers"] = self.extra_headers
        response = await aembedding(**kwargs)
        data = response.data if hasattr(response, "data") else response.get("data", [])
        return [item.embedding for item in data]
    
    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse LiteLLM response into our standard format."""
        choice = response.choices[0]
        message = choice.message
        
        tool_calls = []
        if hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                # Parse arguments from JSON string if needed
                args = tc.function.arguments
                if isinstance(args, str):
                    args = self._parse_tool_arguments(args)
                
                tool_calls.append(ToolCallRequest(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                ))
        
        usage = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        
        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
        )
    
    def get_default_model(self) -> str:
        """Get the default model."""
        return self.default_model

    def _build_completion_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int,
        temperature: float,
        thinking: str | None,
        stream: bool = False,
    ) -> dict[str, Any]:
        model_name = self._normalize_model_name(model or self.default_model)
        adjusted_temperature = 1.0 if "kimi-k2.5" in model_name.lower() else temperature

        kwargs: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": adjusted_temperature,
        }
        if stream:
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}

        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.extra_headers:
            kwargs["extra_headers"] = self.extra_headers

        thinking_level = thinking or self.thinking
        budget = self.THINKING_BUDGETS.get(thinking_level, 0)
        if budget > 0 and ("anthropic" in model_name or "claude" in model_name.lower()):
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
        elif budget > 0:
            hints = {
                "low": "Provide brief reasoning.",
                "medium": "Reason step-by-step.",
                "high": "Provide detailed reasoning and verify your answer.",
            }
            hint = hints.get(thinking_level, "Reason step-by-step.")
            kwargs["messages"] = [{"role": "system", "content": hint}] + messages

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        return kwargs

    def _normalize_model_name(self, model: str) -> str:
        _prefix_rules = [
            (("glm", "zhipu"), "zai", ("zhipu/", "zai/", "openrouter/", "hosted_vllm/")),
            (("qwen", "dashscope"), "dashscope", ("dashscope/", "openrouter/")),
            (("moonshot", "kimi"), "moonshot", ("moonshot/", "openrouter/")),
            (("gemini",), "gemini", ("gemini/",)),
        ]
        model_name = model
        model_lower = model_name.lower()
        for keywords, prefix, skip in _prefix_rules:
            if any(kw in model_lower for kw in keywords) and not any(model_name.startswith(s) for s in skip):
                model_name = f"{prefix}/{model_name}"
                break

        if self.is_openrouter and not model_name.startswith("openrouter/"):
            model_name = f"openrouter/{model_name}"
        elif self.is_aihubmix:
            model_name = f"openai/{model_name.split('/')[-1]}"
        elif self.is_vllm and not model_name.startswith("hosted_vllm/"):
            bare = model_name.split("/", 1)[-1] if "/" in model_name else model_name
            model_name = f"hosted_vllm/{bare}"
        return model_name

    @staticmethod
    def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    def _get_first_choice(self, chunk: Any) -> Any:
        choices = self._obj_get(chunk, "choices", [])
        if isinstance(choices, list) and choices:
            return choices[0]
        return None

    def _extract_delta_text(self, delta: Any) -> str:
        content = self._obj_get(delta, "content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            out: list[str] = []
            for part in content:
                text = self._obj_get(part, "text")
                if isinstance(text, str):
                    out.append(text)
            return "".join(out)
        return ""

    def _accumulate_tool_calls(self, delta: Any, accumulator: dict[int, dict[str, str]]) -> None:
        tool_calls = self._obj_get(delta, "tool_calls")
        if not isinstance(tool_calls, list):
            return
        for tc in tool_calls:
            idx = int(self._obj_get(tc, "index", len(accumulator)) or 0)
            entry = accumulator.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            tc_id = self._obj_get(tc, "id")
            if isinstance(tc_id, str) and tc_id:
                entry["id"] = tc_id

            function = self._obj_get(tc, "function", {}) or {}
            name_part = self._obj_get(function, "name")
            if isinstance(name_part, str) and name_part:
                entry["name"] = name_part if not entry["name"] else entry["name"] + name_part

            args_part = self._obj_get(function, "arguments")
            if isinstance(args_part, str) and args_part:
                entry["arguments"] += args_part

    def _parse_stream_tool_calls(self, accumulator: dict[int, dict[str, str]]) -> list[ToolCallRequest]:
        calls: list[ToolCallRequest] = []
        for idx in sorted(accumulator):
            entry = accumulator[idx]
            if not entry["name"]:
                continue
            args = self._parse_tool_arguments(entry["arguments"])
            calls.append(
                ToolCallRequest(
                    id=entry["id"] or f"tool_{idx}",
                    name=entry["name"],
                    arguments=args,
                )
            )
        return calls

    @staticmethod
    def _parse_tool_arguments(raw: str) -> dict[str, Any]:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
            return {"value": parsed}
        except json.JSONDecodeError:
            return {"raw": raw}
