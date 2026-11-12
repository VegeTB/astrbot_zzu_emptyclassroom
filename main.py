from __future__ import annotations

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

from .mixin import EmptyClassroomToolMixin

PLUGIN_NAME = "astrbot_zzu_emptyclassroom"
TOOL_NAMES = ["query_zzu_empty_classroom"]

TOOL_SCHEMAS = {
    "query_zzu_empty_classroom": {
        "type": "object",
        "properties": {
            "campus": {
                "type": "string",
                "description": "校区名称，可选值：主校区、南校区、北校区、东校区、南校区·河南音乐学院。若用户未指定则传空字符串。",
            },
            "building": {
                "type": "string",
                "description": "教学楼名称或编号（如：北1、北2、综合管理中心、南5等）。若用户未指定则传空字符串。",
            },
            "date": {
                "type": "string",
                "description": "查询日期，格式为 YYYY-MM-DD（如 2026-11-13）。若用户说“今天”、“明天”等请自行换算，未指定则传空字符串。",
            },
            "periods": {
                "type": "string",
                "description": "节次，如“1-2”、“3-4”、“5-6”、“7-8”、“9-10”、“上午”、“下午”、“晚上”、“全天”等。若用户未指定则传空字符串。",
            },
        },
        "required": [],
    }
}


@register(
    PLUGIN_NAME,
    "Vege",
    "郑州大学新教务空教室查询插件（直连 jwxt.zzu.edu.cn，支持工具调用、MFA与指令查询）",
    "2.0.0",
    "https://github.com/VegeTB/astrbot_zzu_emptyclassroom",
)
class ZzuEmptyClassroomPlugin(Star, EmptyClassroomToolMixin):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        try:
            super().__init__(context, config)
        except TypeError:
            super().__init__(context)
        self.config = config or {}
        self._init_zzu_runtime()
        self._ensure_llm_tools_registered()
        logger.info(f"{PLUGIN_NAME} 插件已加载。")

    @filter.on_astrbot_loaded()
    async def _on_astrbot_loaded(self):
        self._ensure_llm_tools_registered()

    def _ensure_llm_tools_registered(self) -> list:
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
    # LLM 工具
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
            campus(string): 校区名称，可选值：主校区、南校区、北校区、东校区、南校区·河南音乐学院。若用户未指定则传空字符串。
            building(string): 教学楼名称或编号（如：北1、北2、综合管理中心、南5等）。若用户未指定则传空字符串。
            date(string): 查询日期，格式为 YYYY-MM-DD（如 2026-11-13）。若用户说今天、明天等请自行换算，未指定则传空字符串。
            periods(string): 节次，如 1-2、3-4、上午、下午、晚上、全天 等。若用户未指定则传空字符串。
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
            return await self._query_empty_classroom_core(
                campus=campus_val,
                building=building_val,
                date=date_val,
                periods=periods_val,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[{PLUGIN_NAME}] 查询空教室失败: {exc}", exc_info=True)
            return f"查询空教室失败：{exc}"

    # ------------------------------------------------------------------ #
    # 聊天指令（查空教室 + 账号登录/MFA验证）
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
            if token in (
                "主校区",
                "南校区",
                "北校区",
                "东校区",
                "新校区",
                "老校区",
                "高新区",
                "医学院",
                "工学院",
                "南校区·河南音乐学院",
            ):
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

    @filter.command("空教室登录", alias={"zzulogin", "zzu登录"})
    async def cmd_zzu_login(self, event: AstrMessageEvent):
        """[管理员] 使用配置的学号密码登录郑大 CAS 并缓存 Token（若触发 MFA 则自动下发短信）"""
        if hasattr(event, "is_admin") and not event.is_admin() and not self._is_allowed(event):
            yield event.plain_result("仅管理员或白名单用户可执行登录。")
            return
        yield event.plain_result("🔐 正在尝试登录郑大统一身份认证，请稍候…")
        res = await self._interactive_cas_login()
        yield event.plain_result(res)

    @filter.command("空教室验证码", alias={"zzumfa", "zzu验证码"})
    async def cmd_zzu_mfa(self, event: AstrMessageEvent, code: str = ""):
        """[管理员] 提交郑大统一身份认证短信验证码以完成 MFA 登录"""
        if hasattr(event, "is_admin") and not event.is_admin() and not self._is_allowed(event):
            yield event.plain_result("仅管理员或白名单用户可执行验证。")
            return
        code = (code or "").strip()
        if not code:
            yield event.plain_result("用法：/空教室验证码 <短信验证码>")
            return
        res = await self._interactive_cas_verify_sms(code)
        yield event.plain_result(res)

    @filter.command("空教室帮助")
    async def cmd_empty_room_help(self, event: AstrMessageEvent):
        help_text = (
            "🏫 **郑州大学空教室查询帮助**\n"
            "------------------------\n"
            "1. **自然语言直接问**：\n"
            "   例如：“帮我查一下今天主校区北1第3-4节有什么空教室？”\n"
            "2. **指令查询**：\n"
            "   `/空教室 [校区] [教学楼] [日期] [节次]`\n"
            "   示例：\n"
            "   • `/空教室` （查默认校区当前/下一时段）\n"
            "   • `/空教室 主校区 北1 1-2`\n"
            "   • `/空教室 南校区 明天 上午`\n"
            "   • `/空教室 东校区 2026-11-13 5-6`\n"
            "3. **首次登录与 MFA 短信验证（管理员）**：\n"
            "   • 在插件配置填入 `zzu_account` 和 `zzu_password` 后发送 `/空教室登录`\n"
            "   • 若收到短信验证码，发送 `/空教室验证码 <6位数字>` 完成认证\n"
            "   • 认证成功后本地持久化 Token，后续无需重复登录（也不会踢掉手机端）"
        )
        yield event.plain_result(help_text)

    async def terminate(self):
        await self._close_zzu_runtime()
        logger.info(f"{PLUGIN_NAME} 插件已卸载。")