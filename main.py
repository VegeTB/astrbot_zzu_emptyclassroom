from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .mixin import ZZUClassroomMixin


@register(
    "astrbot_plugin_zzu_classroom",
    "炫风好调花椰菜",
    "基于 ZZU.Py 的郑大空教室查询工具插件",
    "1.0.0",
)
class ZZUClassroomPlugin(ZZUClassroomMixin, Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._init_classroom_state()

    @filter.llm_tool(name="query_zzu_empty_classroom")
    async def query_zzu_empty_classroom(
        self,
        event: AstrMessageEvent,
        date: str,
        building: str,
        period: str = "全天",
    ) -> str:
        """查询郑州大学指定日期、教学楼和时段的空教室情况

        Args:
            date(string): 查询日期，格式为 YYYY-MM-DD，例如 2026-11-13
            building(string): 教学楼名称关键词，例如 南1、北2、东3、南核心
            period(string): 空闲时段要求，可选 上午、下午、晚上、全天，或具体节次如 5-8
        """
        try:
            return await self.fetch_and_filter_rooms(
                date_str=date,
                building_kw=building,
                period_text=period,
            )
        except Exception as exc:
            logger.warning(f"空教室查询执行异常: {exc}")
            return f"查询空教室时遇到问题：{exc}"

    @filter.command("查空教室")
    async def cmd_test_classroom(
        self,
        event: AstrMessageEvent,
        date: str,
        building: str,
        period: str = "下午",
    ):
        """手动调试指令：/查空教室 2026-11-13 南1 下午"""
        try:
            res = await self.fetch_and_filter_rooms(date, building, period)
            yield event.plain_result(res)
        except Exception as exc:
            yield event.plain_result(f"接口调试未通过：{exc}")
