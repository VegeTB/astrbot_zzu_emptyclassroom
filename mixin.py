from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

CAMPUS_ALIAS_MAP = {
    "主校区": "主校区",
    "新校区": "主校区",
    "高新区": "主校区",
    "柳林": "主校区",
    "南校区": "南校区",
    "老校区": "南校区",
    "医学院": "东校区",
    "东校区": "东校区",
    "北校区": "北校区",
    "工学院": "北校区",
}

PERIOD_ALIAS_MAP = {
    "上午": "1-4",
    "早八": "1-2",
    "第一节": "1-2",
    "一二节": "1-2",
    "12节": "1-2",
    "1-2节": "1-2",
    "三四节": "3-4",
    "34节": "3-4",
    "3-4节": "3-4",
    "下午": "5-8",
    "五六节": "5-6",
    "56节": "5-6",
    "5-6节": "5-6",
    "七八节": "7-8",
    "78节": "7-8",
    "7-8节": "7-8",
    "晚上": "9-11",
    "晚自习": "9-11",
    "九十节": "9-10",
    "910节": "9-10",
    "9-10节": "9-10",
    "全天": "1-11",
}


class EmptyClassroomToolMixin:
    """郑州大学空教室查询核心逻辑（工具方法已统一置于 main.py）。"""

    def _is_allowed(self, event: AstrMessageEvent) -> bool:
        allowed_users = self.config.get("allowed_users") or []
        allowed_groups = self.config.get("allowed_groups") or []

        if allowed_users:
            sender_id = str(event.get_sender_id())
            if sender_id not in [str(u) for u in allowed_users]:
                return False

        if allowed_groups:
            group_id = ""
            if hasattr(event, "message_obj") and getattr(event.message_obj, "group_id", None):
                group_id = str(event.message_obj.group_id)
            if group_id and group_id not in [str(g) for g in allowed_groups]:
                return False

        return True

    def _looks_like_date(self, token: str) -> bool:
        if token in ("今天", "今日", "明天", "明日", "后天"):
            return True
        return bool(re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$", token))

    def _looks_like_periods(self, token: str) -> bool:
        if token in PERIOD_ALIAS_MAP:
            return True
        return bool(re.match(r"^\d{1,2}(?:-\d{1,2})?(?:,\d{1,2}(?:-\d{1,2})?)*节?$", token))

    def _normalize_campus(self, campus: str) -> str:
        c = (campus or "").strip()
        if not c:
            return str(self.config.get("default_campus", "主校区") or "主校区").strip()
        return CAMPUS_ALIAS_MAP.get(c, c)

    def _normalize_date(self, date_str: str) -> str:
        d = (date_str or "").strip()
        today = datetime.now()
        if not d or d in ("今天", "今日"):
            return today.strftime("%Y-%m-%d")
        if d in ("明天", "明日"):
            return (today + timedelta(days=1)).strftime("%Y-%m-%d")
        if d == "后天":
            return (today + timedelta(days=2)).strftime("%Y-%m-%d")

        m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$", d)
        if m:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        return d

    def _normalize_periods(self, periods: str) -> str:
        p = (periods or "").strip()
        if not p:
            return ""
        if p in PERIOD_ALIAS_MAP:
            return PERIOD_ALIAS_MAP[p]
        return p.rstrip("节")

    async def _query_empty_classroom_core(
        self,
        campus: str = "",
        building: str = "",
        date: str = "",
        periods: str = "",
    ) -> str:
        base_url = str(self.config.get("api_base_url", "http://127.0.0.1:8000") or "").strip().rstrip("/")
        if not base_url:
            return "插件未配置有效的空教室服务接口地址（api_base_url）。"

        timeout = float(self.config.get("api_timeout", 15) or 15)
        norm_campus = self._normalize_campus(campus)
        norm_date = self._normalize_date(date)
        norm_periods = self._normalize_periods(periods)
        norm_building = (building or "").strip()

        params: dict[str, Any] = {
            "campus": norm_campus,
            "date": norm_date,
        }
        if norm_building:
            params["building"] = norm_building
        if norm_periods:
            params["periods"] = norm_periods

        url = f"{base_url}/api/v1/empty-classrooms"
        logger.debug(f"[ZZUEmptyClassroom] 请求 {url} params={params}")

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                return f"空教室查询接口返回异常状态码：{resp.status_code}，内容：{resp.text[:200]}"
            payload = resp.json()

        return self._format_result(payload, norm_campus, norm_building, norm_date, norm_periods)

    def _format_result(
        self,
        payload: Any,
        campus: str,
        building: str,
        date_str: str,
        periods: str,
    ) -> str:
        if isinstance(payload, dict):
            if payload.get("code") not in (None, 0, 200) and not payload.get("ok", True):
                return f"空教室接口返回错误：{payload.get('message') or payload.get('msg') or payload}"
            data = payload.get("data", payload)
        else:
            data = payload

        rooms: list[Any] = []
        if isinstance(data, list):
            rooms = data
        elif isinstance(data, dict):
            for key in ("rooms", "classrooms", "items", "list", "results"):
                if isinstance(data.get(key), list):
                    rooms = data[key]
                    break

        header = f"🏫 郑州大学空教室查询结果\n📅 日期：{date_str} | 校区：{campus}"
        if building:
            header += f" | 教学楼：{building}"
        if periods:
            header += f" | 节次：{periods}"

        if not rooms:
            return f"{header}\n\n暂无符合条件的空教室记录。"

        max_display = int(self.config.get("max_rooms_display", 30) or 30)
        lines = [header, f"共找到 {len(rooms)} 间空教室："]
        for room in rooms[:max_display]:
            if isinstance(room, str):
                lines.append(f"• {room}")
            elif isinstance(room, dict):
                r_name = room.get("room") or room.get("name") or room.get("classroom") or room.get("roomName") or "未知教室"
                r_bldg = room.get("building") or room.get("buildingName") or ""
                r_cap = room.get("capacity") or room.get("seats") or ""
                r_per = room.get("periods") or room.get("freePeriods") or ""
                parts = []
                if r_bldg and r_bldg not in str(r_name):
                    parts.append(str(r_bldg))
                parts.append(str(r_name))
                desc = " ".join(parts)
                extras = []
                if r_cap:
                    extras.append(f"座位数:{r_cap}")
                if r_per:
                    extras.append(f"空闲节次:{r_per}")
                if extras:
                    desc += f" ({', '.join(extras)})"
                lines.append(f"• {desc}")

        if len(rooms) > max_display:
            lines.append(f"... 其余 {len(rooms) - max_display} 间教室已省略（可在配置中调大 max_rooms_display）。")

        return "\n".join(lines)