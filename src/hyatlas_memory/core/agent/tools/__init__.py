"""
HY Memory Agent Tools

通用的 LLM "工具"机制（OpenAI function-calling 风格）。当前 SDK 内部已不再
使用此机制做用户基础画像更新（由 extractor prompt + JSON `basic_info` 字段
直接表达，避免弱模型乱填）。本模块保留供未来其他 agent 场景使用。

核心组件：
- ToolDefinition: tool 的 schema 描述（OpenAI function calling 风格）
- ToolCall:       LLM 发起的一次 tool 调用请求
- ToolResult:     dispatcher 执行后的结果
- ToolHandler:    tool 的执行逻辑（抽象接口）
- ToolRegistry:   tool 注册表 + dispatcher

使用方式（自定义场景）::

    from .tools import ToolRegistry, ToolHandler, ToolDefinition, ToolResult

    class MyTool(ToolHandler):
        def definition(self) -> ToolDefinition:
            return ToolDefinition(name="my_tool", description="...", parameters={...})
        async def execute(self, arguments, context) -> ToolResult:
            return ToolResult(success=True, data={...})

    registry = ToolRegistry()
    registry.register(MyTool())

    # 传给 LLM
    tool_schemas = registry.openai_schemas()

    # LLM 返回 tool_calls 后
    for call in tool_calls:
        result = await registry.dispatch(call, context={...})
"""

from .base import (
    ToolDefinition,
    ToolCall,
    ToolResult,
    ToolHandler,
    ToolRegistry,
)

__all__ = [
    "ToolDefinition",
    "ToolCall",
    "ToolResult",
    "ToolHandler",
    "ToolRegistry",
]
