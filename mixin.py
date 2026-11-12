import asyncio
import inspect
import re
import time
from typing import Any, Dict, List, Set

import httpx
from astrbot.api import logger


PERIOD_PRESETS: Dict[str, Set[int]] = {
    "上午": {1, 2, 3, 4},
    "下午": {5, 6, 7, 8},
    "晚上": {9, 10},
    "全天": {1, 2, 3, 4, 5, 6, 7, 8, 9, 10},
}


async def _maybe_await(func, *args, **kwargs):
    res = func(*args, **kwargs)
    if inspect.isawaitable(res):
        return await res
    return res


class ZZUClassroomMixin:
    """负责 ZZU.Py 登录鉴权与空教室数据解析的 Mixin"""

    def _init_classroom_state(self) -> None:
        self._cached_token: str = ""
        self._token_expire_at: float = 0.0
        self._eas_client: Any = None

    def _parse_target_periods(self, period_text: str) -> Set[int]:
        clean = (period_text or "全天").strip()
        for key, val in PERIOD_PRESETS.items():
            if key in clean:
                return set(val)
        nums = [int(x) for x in re.findall(r"\d+", clean)]
        if len(nums) == 2 and "-" in clean and nums[0] <= nums[1]:
            return set(range(nums[0], nums[1] + 1))
        if nums:
            return {n for n in nums if 1 <= n <= 12}
        return set(PERIOD_PRESETS["全天"])

    async def _acquire_eas_token(self, force_refresh: bool = False) -> str:
        manual_token = str(self.config.get("manual_eas_token", "")).strip()
        if manual_token and not force_refresh:
            return manual_token

        now = time.time()
        if not force_refresh and self._cached_token and now < self._token_expire_at:
            return self._cached_token

        student_id = str(self.config.get("student_id", "")).strip()
        password = str(self.config.get("password", "")).strip()
        if not student_id or not password:
            raise ValueError("插件配置中尚未填写学号或密码，请先在 AstrBot 管理面板配置")

        token = await asyncio.to_thread(self._sync_zzupy_login, student_id, password)
        if not token:
            raise RuntimeError("通过 ZZU.Py 登录成功但未能提取到教务 Token")

        self._cached_token = token
        self._token_expire_at = now + 1800
        return token

    def _sync_zzupy_login(self, student_id: str, password: str) -> str:
        # 优先适配 ZZU.Py 的 CASClient + UndergradEASClient 架构
        try:
            from zzupy.cas import CASClient
            from zzupy.eas import UndergradEASClient

            cas = CASClient(student_id, password)
            cas.login()
            eas = UndergradEASClient(cas)
            eas.login()
            self._eas_client = eas
            for attr in ("token", "jwt_token", "access_token"):
                val = getattr(eas, attr, None)
                if val:
                    return str(val)
            if hasattr(eas, "session") and hasattr(eas.session, "headers"):
                for hdr in ("token", "X-Id-Token", "Authorization"):
                    if hdr in eas.session.headers:
                        return str(eas.session.headers[hdr])
        except ImportError:
            pass

        # 兼容新版 zzupy 顶层 ZZUClient 封装
        import zzupy

        client_cls = getattr(zzupy, "ZZUClient", None)
        if client_cls is None:
            raise RuntimeError("当前环境的 zzupy 版本未找到 CASClient 或 ZZUClient")

        client = client_cls(student_id, password)
        client.login()
        eas_mod = getattr(client, "eas", client)
        if hasattr(eas_mod, "login"):
            eas_mod.login()
        self._eas_client = eas_mod
        for attr in ("token", "jwt_token", "access_token"):
            val = getattr(eas_mod, attr, None)
            if val:
                return str(val)
        raise RuntimeError("未能从 ZZUClient 实例读取教务鉴权令牌")

    def _extract_occupied_units(self, room_item: Dict[str, Any]) -> Set[int]:
        occupied: Set[int] = set()
        for key in ("occupyUnits", "occupiedUnits", "busyUnits", "lessons"):
            raw = room_item.get(key)
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, int):
                        occupied.add(item)
                    elif isinstance(item, str) and item.isdigit():
                        occupied.add(int(item))
                    elif isinstance(item, dict):
                        start = int(item.get("startUnit", item.get("startNode", 0)) or 0)
                        end = int(item.get("endUnit", item.get("endNode", start)) or start)
                        if start > 0 and end >= start:
                            occupied.update(range(start, end + 1))
            elif isinstance(raw, str):
                # 兼容 0100110000 形式的节次位图或逗号分隔字符串
                stripped = raw.strip()
                if stripped and set(stripped) <= {"0", "1"} and len(stripped) >= 8:
                    for idx, ch in enumerate(stripped, start=1):
                        if ch == "1":
                            occupied.add(idx)
                else:
                    occupied.update(int(x) for x in re.findall(r"\d+", stripped))
        return occupied

    def _match_building(self, room_name: str, building_kw: str) -> bool:
        norm_room = re.sub(r"[\s_\-教学楼]", "", room_name).upper()
        norm_kw = re.sub(r"[\s_\-教学楼]", "", building_kw).upper()
        return norm_kw in norm_room

    async def fetch_and_filter_rooms(
        self, date_str: str, building_kw: str, period_text: str
    ) -> str:
        target_units = self._parse_target_periods(period_text)
        base_url = str(self.config.get("eas_base_url", "")).rstrip("/")
        api_path = "/" + str(self.config.get("room_api_path", "")).lstrip("/")
        url = f"{base_url}{api_path}"

        token = await self._acquire_eas_token(force_refresh=False)
        payload = {
            "date": date_str,
            "buildingName": building_kw,
            "campusName": str(self.config.get("default_campus", "主校区")),
            "pageIndex": 1,
            "pageSize": 200,
        }

        async with httpx.AsyncClient(timeout=15.0, verify=False) as http_client:
            for attempt in range(2):
                headers = {
                    "token": token,
                    "X-Id-Token": token,
                    "Content-Type": "application/json",
                    "User-Agent": "ZZUPocket-AstrBot/1.0",
                }
                resp = await http_client.post(url, json=payload, headers=headers)
                if resp.status_code in (401, 403) and attempt == 0:
                    logger.info("教务令牌已过期，正在通过 ZZU.Py 重新登录刷新")
                    token = await self._acquire_eas_token(force_refresh=True)
                    continue
                resp.raise_for_status()
                body = resp.json()
                break

        records: List[Dict[str, Any]] = []
        if isinstance(body, list):
            records = body
        elif isinstance(body, dict):
            data_node = body.get("data", body.get("result", body.get("rows", [])))
            if isinstance(data_node, list):
                records = data_node
            elif isinstance(data_node, dict):
                records = data_node.get("list", data_node.get("records", data_node.get("items", [])))

        if not records:
            return f"未查到 {date_str} 教学楼「{building_kw}」的教室数据，请核对楼栋名称或接口返回格式"

        full_free_rooms: List[str] = []
        partial_free_rooms: List[str] = []

        for item in records:
            room_name = str(
                item.get("roomName")
                or item.get("name")
                or item.get("classroomName")
                or item.get("code")
                or ""
            ).strip()
            if not room_name or not self._match_building(room_name, building_kw):
                continue

            occupied = self._extract_occupied_units(item)
            free_in_target = sorted(target_units - occupied)
            if len(free_in_target) == len(target_units):
                seats = item.get("seats", item.get("capacity", ""))
                seat_tag = f"({seats}座)" if seats else ""
                full_free_rooms.append(f"{room_name}{seat_tag}")
            elif len(free_in_target) >= 2:
                units_desc = ",".join(str(u) for u in free_in_target)
                partial_free_rooms.append(f"{room_name}(空第{units_desc}节)")

        sorted_target = sorted(target_units)
        unit_label = f"第{sorted_target[0]}-{sorted_target[-1]}节" if sorted_target else period_text
        lines = [f"【{date_str} {building_kw} {period_text}（{unit_label}）空教室查询结果】"]

        if full_free_rooms:
            lines.append(f"全时段空闲（共 {len(full_free_rooms)} 间）：" + "、".join(full_free_rooms[:40]))
        else:
            lines.append("该时段暂无全程空闲的教室")

        if not full_free_rooms and partial_free_rooms:
            lines.append("部分节次空闲参考：" + "、".join(partial_free_rooms[:20]))

        return "\n".join(lines)
