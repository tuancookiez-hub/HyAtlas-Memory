"""
Agent Memory - LLM Provider

统一的 LLM 调用接口，支持多种模型后端。

功能：
- 统一的调用接口
- 多模型支持（OpenAI、Hunyuan 等）
- 请求重试和错误处理
- Token 统计
- 模型路由

示例：
    provider = LLMProvider(config)
    
    # 调用 LLM
    response = await provider.complete(
        prompt="请总结以下内容：...",
        max_tokens=500,
        temperature=0.7
    )
    
    print(response.content)
    print(response.tokens_used)
"""

from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from enum import Enum
import asyncio
import logging

from ..config import MemoryConfig

logger = logging.getLogger(__name__)


def _parse_tool_calls_list(raw_calls: list, _json) -> List[Dict[str, Any]]:
    """
    解析 tool_calls 列表。

    保留原始完整结构（蒸馏平台可能有 signature 等私有字段），
    只规范化 arguments（dict → JSON str）。
    """
    result = []
    for tc in raw_calls:
        tc_id = tc.get("id") or ""
        if not tc_id:
            logger.error(
                f"[llm] tool_call missing 'id'! raw: "
                f"{_json.dumps(tc, ensure_ascii=False, default=str)[:500]}"
            )

        # 保留原始 tc 完整结构，只规范化 arguments
        tc_copy = dict(tc)  # 浅拷贝保留所有字段（signature 等）
        fn = tc_copy.get("function", {})
        if isinstance(fn, dict):
            args = fn.get("arguments", "")
            if isinstance(args, dict):
                fn["arguments"] = _json.dumps(args, ensure_ascii=False)
            tc_copy["function"] = fn

        # 确保基础字段存在
        tc_copy.setdefault("id", tc_id)
        tc_copy.setdefault("type", "function")

        result.append(tc_copy)
    return result


class LLMBackend(Enum):
    """LLM 后端"""
    OPENAI = "openai"
    EVAL_PLATFORM = "eval_platform"


@dataclass
class LLMResponse:
    """
    LLM 响应
    """
    content: str
    tokens_used: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    finish_reason: str = ""
    # function/tool calling：当 LLM 决定调用工具时填充，结构为 OpenAI 风格：
    #   [{"id": "...", "type": "function", "function": {"name": "...", "arguments": "<json str>"}}]
    # 调用方应使用 tools.base.parse_tool_calls_from_json 解析
    tool_calls: Optional[List[Dict[str, Any]]] = None


@dataclass
class LLMConfig:
    """
    LLM 配置
    """
    backend: LLMBackend = LLMBackend.OPENAI
    model: str = "gpt-3.5-turbo"
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.7
    timeout: int = 30
    max_retries: int = 5
    retry_delay: float = 1.0  # 初始 delay，每次递增
    extra_headers: Optional[Dict[str, str]] = None
    extra_body: Optional[Dict[str, Any]] = None
    # 蒸馏平台专用
    eval_user: Optional[str] = None
    eval_apikey: Optional[str] = None


class LLMProvider:
    """
    LLM 提供器

    统一的 LLM 调用接口。

    _extract_response: 安全提取 choices[0].message，当 API 返回空 choices 时给出
    明确错误信息而非 'NoneType' object is not subscriptable。
    """

    @staticmethod
    def _extract_response(response) -> tuple:
        """
        从 OpenAI 兼容 response 中提取 message、usage、finish_reason。
        当 choices 为空时抛出明确的 ValueError。

        Returns:
            (msg, usage, finish_reason)
        """
        choices = getattr(response, "choices", None)
        if not choices:
            # 尝试拿到更多诊断信息
            raw = ""
            try:
                raw = str(response)[:500]
            except Exception:
                pass
            raise ValueError(
                f"LLM returned empty choices (no completion). "
                f"Response: {raw}"
            )
        msg = choices[0].message
        usage = response.usage
        finish_reason = choices[0].finish_reason
        return msg, usage, finish_reason

    def __init__(self, config: Optional[MemoryConfig] = None):
        """
        初始化 LLM Provider
        
        Args:
            config: 配置对象
        """
        self.config = config or MemoryConfig.from_env()
        self._llm_config = self._build_llm_config()
        self._client = None
        
        # 统计
        self._total_calls = 0
        self._total_tokens = 0
        self._errors = 0
        
        logger.debug(f"LLMProvider initialized, backend={self._llm_config.backend.value}")
    
    def _build_llm_config(self) -> LLMConfig:
        """从配置构建 LLM 配置"""
        llm = self.config.llm
        provider = (llm.provider or "openai").lower().strip()

        if provider == "eval_platform":
            backend = LLMBackend.EVAL_PLATFORM
        else:
            backend = LLMBackend.OPENAI

        return LLMConfig(
            backend=backend,
            model=llm.model,
            api_key=llm.api_key,
            base_url=llm.base_url,
            temperature=llm.temperature if llm.temperature is not None else 0.7,
            timeout=llm.timeout or 120,
            max_retries=llm.max_retries if llm.max_retries is not None else 5,
            retry_delay=llm.retry_delay if llm.retry_delay is not None else 1.0,
            extra_headers=llm.extra_headers,
            extra_body=llm.extra_body,
            eval_user=llm.eval_user,
            eval_apikey=llm.eval_apikey,
        )
    
    async def complete(
        self,
        prompt: str,
        max_tokens: int = 500,
        temperature: float = 0.7,
        stop: List[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        **kwargs
    ) -> LLMResponse:
        """
        调用 LLM 完成文本生成

        Args:
            prompt: 提示词
            max_tokens: 最大 token 数
            temperature: 温度参数
            stop: 停止词列表
            tools: 可选，OpenAI function-calling 风格的 tools schema 列表
                   （[{"type":"function","function":{"name":..., "parameters":...}}, ...]）。
                   传入时 LLM 可以在响应中返回 tool_calls，由调用方决定如何 dispatch。
            tool_choice: 可选，OpenAI 风格 tool_choice（"auto" / "none" / {"type":"function","function":{"name":...}}）
            **kwargs: 其他参数

        Returns:
            LLM 响应（成功时 response.tool_calls 可能非空）
        """
        for attempt in range(self._llm_config.max_retries):
            try:
                if attempt == 0:
                    logger.debug(
                        f"[llm] model={self._llm_config.model} "
                        f"backend={self._llm_config.backend.value} "
                        f"base_url={self._llm_config.base_url or 'default'} "
                        f"prompt_len={len(prompt)} tools={len(tools) if tools else 0}"
                    )
                logger.debug(f"[llm] prompt:\n{prompt}")

                response = await self._call_llm(
                    prompt=prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    stop=stop,
                    tools=tools,
                    tool_choice=tool_choice,
                    **kwargs
                )

                self._total_calls += 1
                self._total_tokens += response.tokens_used

                logger.debug(
                    f"[llm] response: {len(response.content)} chars, "
                    f"tokens={response.tokens_used}, "
                    f"tool_calls={len(response.tool_calls) if response.tool_calls else 0}"
                )
                logger.debug(f"[llm] response content: {response.content!r}")

                return response

            except Exception as e:
                self._errors += 1
                logger.warning(f"LLM call failed (attempt {attempt + 1}): {e}")

                if attempt < self._llm_config.max_retries - 1:
                    delay = self._llm_config.retry_delay * (attempt + 1)
                    await asyncio.sleep(delay)

        # 所有重试都失败
        raise RuntimeError("LLM call failed after all retries")

    def _eval_platform_api_key(self) -> str:
        """
        构造蒸馏平台标准协议的 API Key。

        标准协议认证格式：Authorization: Bearer $APP_ID:$APP_KEY
        兼容旧配置：eval_user 作为 APP_ID，eval_apikey 作为 APP_KEY。
        也支持直接在 api_key 中配置完整的 "$APP_ID:$APP_KEY" 格式。
        """
        # 优先使用 api_key（如果已经是 APP_ID:APP_KEY 格式）
        if self._llm_config.api_key and ":" in self._llm_config.api_key:
            return self._llm_config.api_key

        # 兼容旧配置：eval_user + eval_apikey → "user:key"
        eval_user = self._llm_config.eval_user
        eval_apikey = self._llm_config.eval_apikey
        if eval_user and eval_apikey:
            return f"{eval_user}:{eval_apikey}"

        # fallback: 直接用 api_key
        if self._llm_config.api_key:
            return self._llm_config.api_key

        raise ValueError(
            "eval_platform requires API credentials. "
            "Set MEMORY_LLM_API_KEY='APP_ID:APP_KEY' or "
            "set both MEMORY_LLM_EVAL_USER and MEMORY_LLM_EVAL_APIKEY."
        )

    async def _call_llm(
        self,
        prompt: str,
        max_tokens: int,
        temperature: float,
        stop: List[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        **kwargs
    ) -> LLMResponse:
        """根据 backend 分发到对应的调用实现"""
        if self._llm_config.backend == LLMBackend.EVAL_PLATFORM:
            return await self._call_eval_platform(
                prompt, max_tokens, temperature,
                tools=tools, tool_choice=tool_choice,
            )
        return await self._call_openai(
            prompt, max_tokens, temperature, stop,
            tools=tools, tool_choice=tool_choice,
        )

    async def _call_openai(
        self,
        prompt: str,
        max_tokens: int,
        temperature: float,
        stop: List[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
    ) -> LLMResponse:
        """
        调用 OpenAI 兼容接口

        支持所有 OpenAI 兼容的 LLM 服务：
        - OpenAI 官方 / DeepSeek / Qwen 等
        - 可选 function calling（tools 参数非空时启用）
        """
        try:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(
                api_key=self._llm_config.api_key,
                base_url=self._llm_config.base_url or None,
            )

            # 构建请求参数，确保有 system 消息（部分内部平台要求）
            messages = [{"role": "user", "content": prompt}]
            if not any(m.get("role") == "system" for m in messages):
                messages.insert(0, {"role": "system", "content": ""})

            kwargs: Dict[str, Any] = {
                "model": self._llm_config.model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            if stop:
                kwargs["stop"] = stop
            if tools:
                kwargs["tools"] = tools
                if tool_choice is not None:
                    kwargs["tool_choice"] = tool_choice
            if self._llm_config.extra_headers:
                kwargs["extra_headers"] = self._llm_config.extra_headers
            if self._llm_config.extra_body:
                kwargs["extra_body"] = self._llm_config.extra_body

            response = await client.chat.completions.create(**kwargs)

            msg, usage, _finish = self._extract_response(response)
            content = msg.content or ""
            tokens = usage.total_tokens if usage else 0
            p_tokens = usage.prompt_tokens if usage else 0
            c_tokens = usage.completion_tokens if usage else 0

            # tool_calls 反序列化为纯 dict 列表（方便后续传递 / 记录）
            tool_calls_raw = getattr(msg, "tool_calls", None) or []
            tool_calls: Optional[List[Dict[str, Any]]] = None
            if tool_calls_raw:
                tool_calls = []
                for tc in tool_calls_raw:
                    # OpenAI SDK 对象 → dict
                    try:
                        fn = getattr(tc, "function", None)
                        tool_calls.append({
                            "id": getattr(tc, "id", "") or "",
                            "type": getattr(tc, "type", "function") or "function",
                            "function": {
                                "name": getattr(fn, "name", "") if fn else "",
                                "arguments": getattr(fn, "arguments", "") if fn else "",
                            },
                        })
                    except Exception as e:
                        logger.warning(f"[llm] failed to serialize tool_call: {e}")

            return LLMResponse(
                content=content,
                tokens_used=tokens,
                prompt_tokens=p_tokens,
                completion_tokens=c_tokens,
                model=self._llm_config.model,
                finish_reason=_finish,
                tool_calls=tool_calls,
            )

        except ImportError:
            raise ImportError("openai is required. Install with: pip install openai")

    async def _call_eval_platform(
        self,
        prompt: str,
        max_tokens: int,
        temperature: float,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
    ) -> LLMResponse:
        """
        调用腾讯蒸馏平台（Eval Platform）— 标准协议

        使用 OpenAI Chat Completions 兼容格式：
        - Base URL: http://llm-api.model-eval.woa.com
        - Auth: Bearer $APP_ID:$APP_KEY
        - model 填平台 model_marker
        """
        from openai import AsyncOpenAI

        api_key = self._eval_platform_api_key()
        base_url = self._llm_config.base_url or "http://llm-api.model-eval.woa.com/v1"

        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url if base_url.endswith("/v1") else f"{base_url}/v1",
        )

        messages = [
            {"role": "system", "content": ""},
            {"role": "user", "content": prompt},
        ]

        kwargs: Dict[str, Any] = {
            "model": self._llm_config.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        if self._llm_config.extra_headers:
            kwargs["extra_headers"] = self._llm_config.extra_headers
        if self._llm_config.extra_body:
            kwargs["extra_body"] = self._llm_config.extra_body

        response = await client.chat.completions.create(**kwargs)

        msg, usage, _finish = self._extract_response(response)
        content = msg.content or ""
        tokens = usage.total_tokens if usage else 0
        p_tokens = usage.prompt_tokens if usage else 0
        c_tokens = usage.completion_tokens if usage else 0

        tool_calls_raw = getattr(msg, "tool_calls", None) or []
        tool_calls: Optional[List[Dict[str, Any]]] = None
        if tool_calls_raw:
            tool_calls = []
            for tc in tool_calls_raw:
                try:
                    fn = getattr(tc, "function", None)
                    tool_calls.append({
                        "id": getattr(tc, "id", "") or "",
                        "type": getattr(tc, "type", "function") or "function",
                        "function": {
                            "name": getattr(fn, "name", "") if fn else "",
                            "arguments": getattr(fn, "arguments", "") if fn else "",
                        },
                    })
                except Exception as e:
                    logger.warning(f"[llm] failed to serialize tool_call: {e}")

        return LLMResponse(
            content=content,
            tokens_used=tokens,
            prompt_tokens=p_tokens,
            completion_tokens=c_tokens,
            model=self._llm_config.model,
            finish_reason=_finish,
            tool_calls=tool_calls,
        )
    
    async def complete_messages(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: int = 500,
        temperature: float = 0.7,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
    ) -> LLMResponse:
        """
        多轮 messages 调用（支持 tool results 回传）。

        Args:
            messages: 完整的 messages 列表，可包含：
                - {"role": "system", "content": "..."}
                - {"role": "user", "content": "..."}
                - {"role": "assistant", "content": "...", "tool_calls": [...]}
                - {"role": "tool", "tool_call_id": "...", "content": "..."}
            max_tokens, temperature, tools, tool_choice: 同 complete()

        Returns:
            LLMResponse
        """
        for attempt in range(self._llm_config.max_retries):
            try:
                if attempt == 0:
                    logger.debug(
                        f"[llm] model={self._llm_config.model} "
                        f"backend={self._llm_config.backend.value} "
                        f"base_url={self._llm_config.base_url or 'default'} "
                        f"msgs={len(messages)} tools={len(tools) if tools else 0}"
                    )

                if self._llm_config.backend == LLMBackend.EVAL_PLATFORM:
                    response = await self._call_eval_platform_messages(
                        messages, max_tokens, temperature,
                        tools=tools, tool_choice=tool_choice,
                    )
                else:
                    response = await self._call_openai_messages(
                        messages, max_tokens, temperature,
                        tools=tools, tool_choice=tool_choice,
                    )

                self._total_calls += 1
                self._total_tokens += response.tokens_used

                logger.debug(
                    f"[llm] response: {len(response.content)} chars, "
                    f"tokens={response.tokens_used}, "
                    f"tool_calls={len(response.tool_calls) if response.tool_calls else 0}"
                )

                return response

            except Exception as e:
                self._errors += 1
                logger.warning(f"LLM call failed (attempt {attempt + 1}): {e}")
                if attempt < self._llm_config.max_retries - 1:
                    delay = self._llm_config.retry_delay * (attempt + 1)
                    await asyncio.sleep(delay)

        raise RuntimeError("LLM call failed after all retries")

    async def _call_openai_messages(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: int,
        temperature: float,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
    ) -> LLMResponse:
        """OpenAI 兼容接口 — 多轮 messages 版本。"""
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=self._llm_config.api_key,
            base_url=self._llm_config.base_url or None,
        )

        # 确保有 system 消息
        if not any(m.get("role") == "system" for m in messages):
            messages = [{"role": "system", "content": ""}] + messages

        kwargs: Dict[str, Any] = {
            "model": self._llm_config.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        if self._llm_config.extra_headers:
            kwargs["extra_headers"] = self._llm_config.extra_headers
        if self._llm_config.extra_body:
            kwargs["extra_body"] = self._llm_config.extra_body

        response = await client.chat.completions.create(**kwargs)

        msg, usage, _finish = self._extract_response(response)
        content = msg.content or ""
        tokens = usage.total_tokens if usage else 0
        p_tokens = usage.prompt_tokens if usage else 0
        c_tokens = usage.completion_tokens if usage else 0

        tool_calls_raw = getattr(msg, "tool_calls", None) or []
        tool_calls: Optional[List[Dict[str, Any]]] = None
        if tool_calls_raw:
            tool_calls = []
            for tc in tool_calls_raw:
                try:
                    fn = getattr(tc, "function", None)
                    tool_calls.append({
                        "id": getattr(tc, "id", "") or "",
                        "type": getattr(tc, "type", "function") or "function",
                        "function": {
                            "name": getattr(fn, "name", "") if fn else "",
                            "arguments": getattr(fn, "arguments", "") if fn else "",
                        },
                    })
                except Exception as e:
                    logger.warning(f"[llm] failed to serialize tool_call: {e}")

        return LLMResponse(
            content=content,
            tokens_used=tokens,
            prompt_tokens=p_tokens,
            completion_tokens=c_tokens,
            model=self._llm_config.model,
            finish_reason=_finish,
            tool_calls=tool_calls,
        )

    async def _call_eval_platform_messages(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: int,
        temperature: float,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
    ) -> LLMResponse:
        """蒸馏平台 — 多轮 messages 版本（标准协议，OpenAI 兼容）。"""
        from openai import AsyncOpenAI

        api_key = self._eval_platform_api_key()
        base_url = self._llm_config.base_url or "http://llm-api.model-eval.woa.com/v1"

        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url if base_url.endswith("/v1") else f"{base_url}/v1",
        )

        # 确保有 system 消息
        if not any(m.get("role") == "system" for m in messages):
            messages = [{"role": "system", "content": ""}] + messages

        kwargs: Dict[str, Any] = {
            "model": self._llm_config.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        if self._llm_config.extra_headers:
            kwargs["extra_headers"] = self._llm_config.extra_headers
        if self._llm_config.extra_body:
            kwargs["extra_body"] = self._llm_config.extra_body

        response = await client.chat.completions.create(**kwargs)

        msg, usage, _finish = self._extract_response(response)
        content = msg.content or ""
        tokens = usage.total_tokens if usage else 0
        p_tokens = usage.prompt_tokens if usage else 0
        c_tokens = usage.completion_tokens if usage else 0

        tool_calls_raw = getattr(msg, "tool_calls", None) or []
        tool_calls: Optional[List[Dict[str, Any]]] = None
        if tool_calls_raw:
            tool_calls = []
            for tc in tool_calls_raw:
                try:
                    fn = getattr(tc, "function", None)
                    tool_calls.append({
                        "id": getattr(tc, "id", "") or "",
                        "type": getattr(tc, "type", "function") or "function",
                        "function": {
                            "name": getattr(fn, "name", "") if fn else "",
                            "arguments": getattr(fn, "arguments", "") if fn else "",
                        },
                    })
                except Exception as e:
                    logger.warning(f"[llm] failed to serialize tool_call: {e}")

        return LLMResponse(
            content=content,
            tokens_used=tokens,
            prompt_tokens=p_tokens,
            completion_tokens=c_tokens,
            model=self._llm_config.model,
            finish_reason=_finish,
            tool_calls=tool_calls,
        )

    async def chat(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 500,
        temperature: float = 0.7,
        **kwargs
    ) -> LLMResponse:
        """
        多轮对话
        
        Args:
            messages: 消息列表 [{"role": "user/assistant", "content": "..."}]
            max_tokens: 最大 token 数
            temperature: 温度参数
        
        Returns:
            LLM 响应
        """
        # 将多轮消息转换为单个 prompt
        prompt_parts = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            prompt_parts.append(f"{role}: {content}")
        
        prompt = "\n".join(prompt_parts)
        
        return await self.complete(
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            **kwargs
        )
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            "total_calls": self._total_calls,
            "total_tokens": self._total_tokens,
            "errors": self._errors,
            "avg_tokens_per_call": (
                self._total_tokens / self._total_calls
                if self._total_calls > 0 else 0
            ),
            "error_rate": (
                self._errors / self._total_calls
                if self._total_calls > 0 else 0
            ),
        }
