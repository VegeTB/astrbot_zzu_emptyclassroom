import re
from typing import Any, Dict, List, Optional, Set
import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_zzu_classroom",
    "Pioneer",
    "查询指定日期与教学楼的空教室情况",
    "1.0.0",
    "https://github.com/3beuml/ZZUPocket",
)
class ZZUClassroomPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._cached_token: str = ""

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def terminate(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get_token(self, force_refresh: bool = False) -> str:
        configured_token = str(self.config.get("eas_token", "")).strip()
        if configured_token and not force_refresh:
            return configured_token

        if self._cached_token and not force_refresh:
            return self._cached_token

        username = str(self.config.get("username", "")).strip()
        password = str(self.config.get("password", "")).strip()
        if not username or not password:
            if configured_token:
                return configured_token
            raise ValueError("未配置教务 eas_token 或账号密码，请先在插件设置中填写")

        base_url = str(self.config.get("eas_base_url", "")).rstrip("/")
        login_ep = str(self.config.get("login_endpoint", "")).strip()
        proxy = str(self.config.get("proxy_url", "")).strip() or None
        url = f"{base_url}{login_ep}"

        session = await self._get_session()
        payload = {"username": username, "password": password}
        async with session.post(url, json=payload, proxy=proxy) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" in content_type:
                raise RuntimeError("登录请求返回了 HTML 页面，可能被校园零信任网关拦截")
            data = await resp.json(content_type=None)

        token = (
            data.get("token")
            or (data.get("data") or {}).get("token")
            or (data.get("result") or {}).get("token")
        )
        if not token:
            raise RuntimeError(f"登录未获取到有效令牌，服务端响应: {data}")

        self._cached_token = str(token)
        return self._cached_token

    @staticmethod
    def _parse_sections(raw: Any, total_sections: int) -> Set[int]:
        occupied: Set[int] = set()
        if raw is None:
            return occupied

        if isinstance(raw, int):
            for i in range(1, total_sections + 1):
                if raw & (1 << (i - 1)):
                    occupied.add(i)
            return occupied

        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, int):
                    occupied.add(item)
                elif isinstance(item, str):
                    occupied.update(ZZUClassroomPlugin._parse_sections(item, total_sections))
                elif isinstance(item, dict):
                    start = item.get("startSection") or item.get("ksjc") or item.get("start")
                    end = item.get("endSection") or item.get("jsjc") or item.get("end") or start
                    if start is not None and end is not None:
                        occupied.update(range(int(start), int(end) + 1))
            return occupied

        if isinstance(raw, str):
            text = raw.strip()
            if set(text) <= {"0", "1"} and len(text) >= 4:
                for idx, ch in enumerate(text[:total_sections], start=1):
                    if ch == "1":
                        occupied.add(idx)
                return occupied

            for part in re.split(r"[,，;；\s]+", text):
                if not part:
                    continue
                if "-" in part:
                    s_str, e_str = part.split("-", 1)
                    if s_str.isdigit() and e_str.isdigit():
                        occupied.update(range(int(s_str), int(e_str) + 1))
                elif part.isdigit():
                    occupied.add(int(part))
        return occupied

    @staticmethod
    def _format_sections(sections: Set[int]) -> str:
        if not sections:
            return "无空闲"
        sorted_secs = sorted(sections)
        ranges: List[str] = []
        start = prev = sorted_secs[0]
        for sec in sorted_secs[1:]:
            if sec == prev + 1:
                prev = sec
            else:
                ranges.append(f"{start}-{prev}" if start != prev else f"{start}")
                start = prev = sec
        ranges.append(f"{start}-{prev}" if start != prev else f"{start}")
        return ",".join(ranges) + "节"

    async def _fetch_classrooms(self, date: str, building: str) -> List[Dict[str, Any]]:
        base_url = str(self.config.get("eas_base_url", "")).rstrip("/")
        endpoint = str(self.config.get("classroom_endpoint", "")).strip()
        method = str(self.config.get("request_method", "GET")).upper().strip()
        header_name = str(self.config.get("token_header_name", "token")).strip()
        page_size = int(self.config.get("page_size", 50))
        proxy = str(self.config.get("proxy_url", "")).strip() or None

        token = await self._get_token()
        headers = {
            header_name: token,
            "Accept": "application/json",
            "User-Agent": "ZZUPocket-AstrBot/1.0",
        }

        url = f"{base_url}{endpoint}"
        session = await self._get_session()
        all_items: List[Dict[str, Any]] = []

        for page in range(1, 21):
            req_data = {
                "date": date,
                "building": building,
                "page": page,
                "pageNum": page,
                "pageSize": page_size,
            }
            req_kwargs: Dict[str, Any] = {"headers": headers, "proxy": proxy}
            if method == "POST":
                req_kwargs["json"] = req_data
            else:
                req_kwargs["params"] = req_data

            async with session.request(method, url, **req_kwargs) as resp:
                if resp.status == 401 and self.config.get("username"):
                    token = await self._get_token(force_refresh=True)
                    headers[header_name] = token
                    async with session.request(method, url, **req_kwargs) as retry_resp:
                        payload = await retry_resp.json(content_type=None)
                else:
                    content_type = resp.headers.get("Content-Type", "")
                    if "text/html" in content_type:
                        raise RuntimeError("查询接口返回了 HTML 登录页，请检查校园网代理或令牌有效性")
                    payload = await resp.json(content_type=None)

            container = payload.get("data") if isinstance(payload, dict) else payload
            if isinstance(container, dict):
                items = (
                    container.get("list")
                    or container.get("records")
                    or container.get("rows")
                    or container.get("items")
                    or []
                )
            elif isinstance(container, list):
                items = container
            else:
                items = []

            if not items:
                break

            all_items.extend(items)
            if len(items) < page_size:
                break

        return all_items

    async def _build_classroom_report(
        self, date: str, building: str, target_sections: str = ""
    ) -> str:
        total_sections = int(self.config.get("total_sections", 10))
        all_day_set = set(range(1, total_sections + 1))
        required_set = self._parse_sections(target_sections, total_sections)

        raw_rooms = await self._fetch_classrooms(date=date, building=building)
        if not raw_rooms:
            return f"未查询到 {date} 楼栋“{building}”的教室数据，请核对楼栋名称或接口参数映射"

        matched_lines: List[str] = []
        for room in raw_rooms:
            room_name = (
                room.get("roomName")
                or room.get("classroomName")
                or room.get("jsmc")
                or room.get("name")
                or "未知教室"
            )
            seat_count = room.get("seats") or room.get("capacity") or room.get("zws") or ""
            raw_occupied = (
                room.get("occupiedSections")
                or room.get("occupiedPeriods")
                or room.get("busySections")
                or room.get("zyjc")
                or room.get("occupy")
                or []
            )

            occupied_set = self._parse_sections(raw_occupied, total_sections)
            free_set = all_day_set - occupied_set

            if not free_set:
                continue
            if required_set and not required_set.issubset(free_set):
                continue

            seat_info = f"({seat_count}座)" if seat_count else ""
            free_desc = "全天空闲" if free_set == all_day_set else f"空闲:{self._format_sections(free_set)}"
            matched_lines.append(f"{room_name}{seat_info}: {free_desc}")

        if not matched_lines:
            cond = f"第 {target_sections} 节" if target_sections else "当日"
            return f"{date} {building} 在{cond}没有符合条件的空闲教室"

        header = f"查询结果（{date} | {building} | 共 {len(matched_lines)} 间可用）："
        return header + "\n" + "\n".join(matched_lines[:60])

    @filter.llm_tool(name="query_empty_classrooms")
    async def query_empty_classrooms(
        self,
        event: AstrMessageEvent,
        date: str,
        building: str,
        target_sections: str = "",
    ) -> str:
        '''查询指定日期和教学楼的空教室及空闲节次情况

        Args:
            date(string): 查询日期，格式为 YYYY-MM-DD（如 2026-11-13），若用户说今天或明天请先换算为具体日期
            building(string): 教学楼或楼栋名称（如 南核心一、北核心二、松园等）
            target_sections(string): 可选，指定需要空闲的节次范围，例如 1-2 或 3-4，不指定则留空字符串返回全天空闲情况
        '''
        try:
            return await self._build_classroom_report(
                date=date.strip(),
                building=building.strip(),
                target_sections=target_sections.strip(),
            )
        except Exception as exc:
            logger.error(f"空教室查询失败: {exc}")
            return f"查询空教室时出错：{exc}"

    @filter.command("空教室测试")
    async def test_classroom_cmd(
        self,
        event: AstrMessageEvent,
        date: str,
        building: str,
        target_sections: str = "",
    ):
        '''手动测试空教室接口连通性：/空教室测试 YYYY-MM-DD 楼栋 [节次]'''
        try:
            report = await self._build_classroom_report(date, building, target_sections)
            yield event.plain_result(report)
        except Exception as exc:
            logger.error(f"空教室测试指令异常: {exc}")
            yield event.plain_result(f"接口测试失败：{exc}")
