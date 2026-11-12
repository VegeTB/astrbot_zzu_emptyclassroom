from __future__ import annotations

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

from .mixin import EmptyClassroomToolMixin

PLUGIN_NAME = "astrbot_zzu_emptyclassroom"
TOOL_NAMES = ["query_zzu_empty_classroom"]

# 显式定义标准 JSON Schema，100% 兼容 Anthropic (input_schema) 与 OpenAI 协议
TOOL_SCHEMAS = {
    "query_zzu_empty_classroom": {
        "type": "object",
        "properties": {
            "campus": {
                "type": "string",
                "description": "校区名称，可选值：主校区、南校区、北校区、东校区。若用户未指定则传空字符串。",
            },
            "building": {
                "type": "string",
                "description": "教学楼名称或编号（如：综合管理中心、北1、南5等）。若用户未指定则传空字符串。",
            },
            "date": {
                "type": "string",
                "description": "查询日期，格式为 YYYY-MM-DD（如 2026-11-13）。若用户说“今天”、“明天”等请自行换算，未指定则传空字符串。",
            },
            "periods": {
                "type": "string",
                "description": "节次，如“1-2”、“3-4”、“上午”、“全天”等。若用户未指定则传空字符串。",
            },
        },
        "required": [],
    }
}


@register(
    PLUGIN_NAME,
    "Vege",
    "郑州大学空教室查询插件（支持自然语言调用工具与指令查询）",
    "1.1.0",
    "https://github.com/VegeTB/astrbot_zzu_emptyclassroom",
)
class ZzuEmptyClassroomPlugin(Star, EmptyClassroomToolMixin):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        try:
            super().__init__(context, config)
        except TypeError:
            super().__init__(context)
        self.config = config or {}
        self._ensure_llm_tools_registered()
        logger.info(f"{PLUGIN_NAME} 插件已加载。")

    @filter.on_astrbot_loaded()
    async def _on_astrbot_loaded(self):
        """在 AstrBot 所有模块加载完成后再次确保工具激活并写入标准 Schema。"""
        self._ensure_llm_tools_registered()

    def _ensure_llm_tools_registered(self) -> list:
        """对齐工具模块路径、写入标准 JSON Schema 并激活工具（保留原生 handler）。"""
        if not self.config.get("register_llm_tools", True):
            return []

        main_module_path = self.__class__.__module__
        plugin_pkg = main_module_path.rsplit(".", 1)[0]

        try:
            from astrbot.core.star.star_handler import star_handlers_registry

            for handler_meta in star_handlers_registry:
                if handler_meta.handler_module_path.startswith(plugin_pkg):
                    handler_meta.handler_module_path = main_module_path
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 修正 handler_registry 路径跳过: {exc}")

        matched_tools = []
        try:
            tool_mgr = self.context.get_llm_tool_manager()
            if tool_mgr and hasattr(tool_mgr, "func_list"):
                active_names = []
                for tool in tool_mgr.func_list:
                    t_name = getattr(tool, "name", "")
                    if t_name in TOOL_NAMES:
                        tool.handler_module_path = main_module_path
                        tool.active = True
                        # 保留 AstrBot 原生绑定，不手动覆盖 tool.handler，避免 parameter mismatch
                        if t_name in TOOL_SCHEMAS:
                            tool.parameters = dict(TOOL_SCHEMAS[t_name])
                        matched_tools.append(tool)
                        active_names.append(t_name)
                logger.info(f"[{PLUGIN_NAME}] 已确认激活并绑定 Schema 的 LLM 工具: {active_names}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 激活 LLM 工具时出现异常: {exc}")

        return matched_tools

    @filter.on_llm_request(priority=99999)
    async def _inject_tools_on_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """在请求发往 LLM 前强行注入工具到 req.func_tool，绕过人设数据库白名单过滤。"""
        if not self.config.get("register_llm_tools", True):
            return
        if not self._is_allowed(event):
            return

        try:
            tool_mgr = self.context.get_llm_tool_manager()
            if not tool_mgr:
                return

            if getattr(req, "func_tool", None) is None:
                from astrbot.core.provider.func_tool_manager import ToolSet

                req.func_tool = ToolSet()

            before_tools = []
            if hasattr(req.func_tool, "tools"):
                before_tools = [getattr(t, "name", str(t)) for t in req.func_tool.tools]
            elif hasattr(req.func_tool, "__iter__"):
                before_tools = [getattr(t, "name", str(t)) for t in req.func_tool]

            injected = []
            for t_name in TOOL_NAMES:
                existing = None
                if hasattr(req.func_tool, "get_tool"):
                    existing = req.func_tool.get_tool(t_name)
                elif t_name in before_tools:
                    existing = True

                if not existing:
                    tool_obj = None
                    if hasattr(tool_mgr, "get_func"):
                        tool_obj = tool_mgr.get_func(t_name)
                    if not tool_obj and hasattr(tool_mgr, "func_list"):
                        for f in tool_mgr.func_list:
                            if getattr(f, "name", "") == t_name:
                                tool_obj = f
                                break
                    if tool_obj:
                        tool_obj.active = True
                        if t_name in TOOL_SCHEMAS:
                            tool_obj.parameters = dict(TOOL_SCHEMAS[t_name])
                        if hasattr(req.func_tool, "add_tool"):
                            req.func_tool.add_tool(tool_obj)
                            injected.append(t_name)

            after_tools = []
            if hasattr(req.func_tool, "tools"):
                after_tools = [getattr(t, "name", str(t)) for t in req.func_tool.tools]
            elif hasattr(req.func_tool, "__iter__"):
                after_tools = [getattr(t, "name", str(t)) for t in req.func_tool]

            logger.info(
                f"[{PLUGIN_NAME}] LLM 请求工具诊断 -> 注入前: {before_tools} | 本次补齐注入: {injected} | 最终发往模型: {after_tools}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[{PLUGIN_NAME}] on_llm_request 注入工具失败: {exc}", exc_info=True)

    # ------------------------------------------------------------------ #
    # LLM 工具（直接定义在主类上，并用 **kwargs 兜底）
    # ------------------------------------------------------------------ #
    @filter.llm_tool(name="query_zzu_empty_classroom")
    async def query_zzu_empty_classroom(
        self,
        event: AstrMessageEvent,
        campus: str = None,
        building: str = None,
        date: str = None,
        periods: str = None,
        **kwargs,
    ):
        """查询郑州大学（ZZU）指定校区、教学楼、日期及节次的空教室信息。

        Args:
            campus(string): 校区名称，可选值：主校区、南校区、北校区、东校区。若用户未指定则传空字符串。
            building(string): 教学楼名称或编号（如：综合管理中心、北1、南5等）。若用户未指定则传空字符串。
            date(string): 查询日期，格式为 YYYY-MM-DD（如 2026-11-13）。若用户说今天、明天等请自行换算，未指定则传空字符串。
            periods(string): 节次，如 1-2、3-4、上午、全天 等。若用户未指定则传空字符串。
        """
        if not self.config.get("register_llm_tools", True):
            return "空教室查询工具未启用。"
        if not self._is_allowed(event):
            return "当前用户或群聊无权限使用空教室查询功能。"

        campus_val = str(campus if campus is not None else kwargs.get("campus") or "")
        building_val = str(building if building is not None else kwargs.get("building") or "")
        date_val = str(date if date is not None else kwargs.get("date") or "")
        periods_val = str(periods if periods is not None else kwargs.get("periods") or "")

        try:
            res = await self._query_empty_classroom_core(
                campus=campus_val,
                building=building_val,
                date=date_val,
                periods=periods_val,
            )
            return res
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[{PLUGIN_NAME}] 查询空教室失败: {exc}", exc_info=True)
            return f"查询空教室失败：{exc}"

    # ------------------------------------------------------------------ #
    # 聊天指令（保持原有逻辑不变）
    # ------------------------------------------------------------------ #
    @filter.command("空教室", alias={"emptyroom", "查空教室"})
    async def cmd_empty_classroom(self, event: AstrMessageEvent, args: GreedyStr = ""):
        """手动查询空教室指令。
        用法示例：
        /空教室
        /空教室 主校区 北1 1-2
        /空教室 南校区 明天 3-4
        """
        if not self._is_allowed(event):
            yield event.plain_result("你没有使用该指令的权限。")
            return

        text = (args or "").strip()
        tokens = text.split() if text else []

        campus = ""
        building = ""
        date = ""
        periods = ""

        for token in tokens:
            if token in ("主校区", "南校区", "北校区", "东校区", "新校区", "老校区"):
                campus = token
            elif self._looks_like_date(token):
                date = token
            elif self._looks_like_periods(token):
                periods = token
            else:
                building = f"{building} {token}".strip() if building else token

        try:
            res = await self._query_empty_classroom_core(
                campus=campus,
                building=building,
                date=date,
                periods=periods,
            )
            yield event.plain_result(res)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[ZZUEmptyClassroom] 指令查询空教室异常: {e}", exc_info=True)
            yield event.plain_result(f"查询空教室失败：{e}")

    @filter.command("空教室帮助")
    async def cmd_empty_room_help(self, event: AstrMessageEvent):
        """显示空教室查询插件使用帮助。"""
        help_text = (
            "🏫 **郑州大学空教室查询帮助**\n"
            "------------------------\n"
            "1. **自然语言直接问**：\n"
            "   例如：“帮我查一下今天主校区北1第3-4节有什么空教室？”\n"
            "2. **指令查询**：\n"
            "   `/空教室 [校区] [教学楼] [日期] [节次]`\n"
            "   示例：\n"
            "   • `/空教室` （查默认校区今天当前/全天）\n"
            "   • `/空教室 主校区 北1 1-2`\n"
            "   • `/空教室 南校区 明天 上午`\n"
            "   • `/空教室 东校区 2026-11-13 5-6`"
        )
        yield event.plain_result(help_text)

    async def terminate(self):
        logger.info(f"{PLUGIN_NAME} 插件已卸载。")