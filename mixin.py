from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

try:
    from zzupy.aio.app.auth import CASClient
    from zzupy.exception import MFAError
except Exception:  # noqa: BLE001
    CASClient = None
    MFAError = Exception

EAS_API_BASE = "https://jwxt.zzu.edu.cn/eams-micro-server/api/v1"

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
    "音乐学院": "南校区·河南音乐学院",
    "河南音乐学院": "南校区·河南音乐学院",
    "南校区·河南音乐学院": "南校区·河南音乐学院",
}

DEFAULT_CAMPUS_MAP = {
    "主校区": 2,
    "南校区": 3,
    "北校区": 4,
    "东校区": 5,
    "南校区·河南音乐学院": 6,
}

# 节次定义兜底表（来自实测 GET /room/system/course-units）
DEFAULT_UNITS: dict[int, tuple[int, int, str]] = {
    1: (800, 845, "08:00-08:45"),
    2: (855, 940, "08:55-09:40"),
    3: (1010, 1055, "10:10-10:55"),
    4: (1105, 1150, "11:05-11:50"),
    5: (1400, 1445, "14:00-14:45"),
    6: (1455, 1540, "14:55-15:40"),
    7: (1600, 1645, "16:00-16:45"),
    8: (1655, 1740, "16:55-17:40"),
    9: (1900, 1945, "19:00-19:45"),
    10: (1955, 2040, "19:55-20:40"),
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
    "晚上": "9-10",
    "晚自习": "9-10",
    "九十节": "9-10",
    "910节": "9-10",
    "9-10节": "9-10",
    "9-11": "9-10",
    "全天": "1-10",
    "1-11": "1-10",
}

BIG_BLOCKS = [(1, 2), (3, 4), (5, 6), (7, 8), (9, 10)]


class _ZzuGatewayInterceptError(RuntimeError):
    """校园网零信任网关拦截"""


class _ZzuAuthExpiredError(RuntimeError):
    """Token 过期或未认证"""


class EmptyClassroomToolMixin:
    """郑州大学新教务空教室查询核心逻辑（直连 eams-micro-server + zzupy CAS Token 持久化）。"""

    def _init_zzu_runtime(self) -> None:
        self._data_dir = Path(get_astrbot_plugin_data_path()) / "astrbot_zzu_emptyclassroom"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._token_file = self._data_dir / "cas_token.json"
        self._auth_lock = asyncio.Lock()
        self._pending_mfa_cas = None
        self._pending_mfa_client = None

        # 内存字典与查询缓存
        self._campus_cache: tuple[float, dict[str, int]] | None = None
        self._building_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}
        self._units_cache: tuple[float, dict[int, tuple[int, int, str]]] | None = None
        self._rooms_cache: dict[tuple[str, int, tuple[int, ...]], tuple[float, list[dict[str, Any]]]] = {}

    async def _close_zzu_runtime(self) -> None:
        if self._pending_mfa_cas is not None:
            try:
                await self._pending_mfa_cas.close()
            except Exception:  # noqa: BLE001
                pass
            self._pending_mfa_cas = None
            self._pending_mfa_client = None

    # ------------------------------------------------------------------ #
    # 权限与入参解析
    # ------------------------------------------------------------------ #
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

    def _parse_periods_to_units(
        self, periods: str, date_str: str, units_map: dict[int, tuple[int, int, str]]
    ) -> tuple[list[int], str]:
        """将用户输入节次解析为节次编号列表与可读标签。"""
        max_unit = max(units_map.keys()) if units_map else 10
        p = (periods or "").strip()

        if not p:
            today_str = datetime.now().strftime("%Y-%m-%d")
            if date_str == today_str:
                now_hhmm = int(datetime.now().strftime("%H%M"))
                for a, b in BIG_BLOCKS:
                    end_hhmm = units_map.get(b, (0, 2040, ""))[1]
                    if now_hhmm <= end_hhmm:
                        return [a, b], f"当前/下节({a}-{b}节)"
                return [1, 2], "今日课程已结束(参考1-2节)"
            return [1, 2], "默认1-2节"

        mapped = PERIOD_ALIAS_MAP.get(p, p).rstrip("节")
        selected: list[int] = []
        for part in mapped.split(","):
            part = part.strip()
            if not part:
                continue
            m = re.match(r"^(\d{1,2})-(\d{1,2})$", part)
            if m:
                start_u = max(1, int(m.group(1)))
                end_u = min(max_unit, int(m.group(2)))
                if start_u <= end_u:
                    selected.extend(range(start_u, end_u + 1))
            elif part.isdigit():
                u = int(part)
                if 1 <= u <= max_unit:
                    selected.append(u)

        unique_units = sorted(set(selected))
        if not unique_units:
            return [1, 2], f"{p}(未识别，按1-2节查)"
        return unique_units, f"第{p}节" if p.isdigit() or "-" in p else p

    # ------------------------------------------------------------------ #
    # Token 持久化与 zzupy CAS 认证维护（避免频繁账密登录踢下线）
    # ------------------------------------------------------------------ #
    def _load_saved_tokens(self) -> tuple[str, str]:
        cfg_ut = str(self.config.get("user_token") or "").strip()
        cfg_rt = str(self.config.get("refresh_token") or "").strip()
        if cfg_ut:
            return cfg_ut, cfg_rt
        if self._token_file.exists():
            try:
                data = json.loads(self._token_file.read_text(encoding="utf-8"))
                return str(data.get("user_token") or "").strip(), str(data.get("refresh_token") or "").strip()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ZZUEmptyClassroom] 读取缓存 Token 失败: {exc}")
        return "", ""

    def _save_tokens(self, user_token: str, refresh_token: str) -> None:
        try:
            payload = {
                "user_token": user_token,
                "refresh_token": refresh_token,
                "updated_at": datetime.now().isoformat(),
            }
            self._token_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ZZUEmptyClassroom] 保存 Token 失败: {exc}")

    async def _get_valid_token(self, force_refresh: bool = False) -> str:
        """优先复用本地/配置 Token；仅在无效或强制刷新时尝试 CAS 续期或登录。"""
        async with self._auth_lock:
            ut, rt = self._load_saved_tokens()
            if ut and not force_refresh:
                return ut

            account = str(self.config.get("zzu_account") or "").strip()
            password = str(self.config.get("zzu_password") or "").strip()
            device_id = str(self.config.get("device_id") or "astrbot_zzu_emptyclassroom").strip()

            if CASClient is None:
                if ut:
                    return ut
                raise RuntimeError("未检测到 zzupy 库，且未配置可用的 user_token。")

            # 1. 如果已有 token 且只是触发了刷新，优先用 set_token 尝试无密码续期
            if ut and rt and force_refresh:
                cas = CASClient(account or "", password or "")
                try:
                    cas.set_device(device_id)
                    cas.set_token(ut, rt)
                    if cas.logged_in and cas.user_token:
                        self._save_tokens(cas.user_token, cas.refresh_token or rt)
                        return cas.user_token
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[ZZUEmptyClassroom] Token 无感续期失败，准备降级账密登录: {exc}")
                finally:
                    await cas.close()

            # 2. 若配置了账密，尝试 CAS 登录
            if not account or not password:
                if ut and not force_refresh:
                    return ut
                raise RuntimeError(
                    "未配置有效 Token 或郑大学号密码。请在插件设置中填入 user_token，或填入 zzu_account 与 zzu_password 后发送 /空教室登录。"
                )

            cas = CASClient(account, password)
            try:
                cas.set_device(device_id)
                await cas.login(force_login=force_refresh)
                if cas.user_token:
                    self._save_tokens(cas.user_token, cas.refresh_token or "")
                    return cas.user_token
                raise RuntimeError("CAS 登录成功但未返回 user_token。")
            except MFAError as exc:
                raise RuntimeError(
                    "触发了郑大统一认证 MFA 短信校验，请管理员在群聊或私聊发送 `/空教室登录` 获取短信并完成验证。"
                ) from exc
            finally:
                await cas.close()

    async def _interactive_cas_login(self) -> str:
        account = str(self.config.get("zzu_account") or "").strip()
        password = str(self.config.get("zzu_password") or "").strip()
        device_id = str(self.config.get("device_id") or "astrbot_zzu_emptyclassroom").strip()

        if CASClient is None:
            return "❌ 当前环境未安装 zzupy 库。"
        if not account or not password:
            return "⚠️ 请先在插件配置中填写 zzu_account（学号）和 zzu_password（密码）。"

        await self._close_zzu_runtime()
        cas = CASClient(account, password)
        cas.set_device(device_id)
        try:
            await cas.login(force_login=True)
            if cas.user_token:
                self._save_tokens(cas.user_token, cas.refresh_token or "")
                await cas.close()
                return "✅ 郑大统一认证登录成功！Token 已持久化保存，现在可以直接查询空教室。"
            await cas.close()
            return "⚠️ 登录返回但未获得 user_token。"
        except MFAError:
            try:
                mfa = CASClient.MFAClient(cas)
                await mfa.send_sms()
                self._pending_mfa_cas = cas
                self._pending_mfa_client = mfa
                return (
                    "📱 检测到新设备需要短信 MFA 二次验证，已尝试向你的绑定手机发送验证码！\n"
                    "请在收到短信后发送：`/空教室验证码 <6位验证码>`"
                )
            except Exception as mfa_exc:  # noqa: BLE001
                await cas.close()
                return f"❌ 需要 MFA 验证但发送短信失败：{mfa_exc}"
        except Exception as exc:  # noqa: BLE001
            await cas.close()
            return f"❌ 登录失败：{exc}"

    async def _interactive_cas_verify_sms(self, code: str) -> str:
        if not self._pending_mfa_cas or not self._pending_mfa_client:
            return "⚠️ 当前没有等待验证的登录会话，请先发送 `/空教室登录`。"
        cas = self._pending_mfa_cas
        mfa = self._pending_mfa_client
        try:
            await mfa.verify_sms(code)
            await cas.login(force_login=True)
            if cas.user_token:
                self._save_tokens(cas.user_token, cas.refresh_token or "")
                return "✅ MFA 短信验证并登录成功！Token 已安全缓存，后续查空教室将直接复用（建议顺便去安全中心设为可信设备）。"
            return "⚠️ 验证通过但未获取到 user_token，请重试 `/空教室登录`。"
        except Exception as exc:  # noqa: BLE001
            return f"❌ 验证码校验失败：{exc}"
        finally:
            await self._close_zzu_runtime()

    # ------------------------------------------------------------------ #
    # 教务 API 底层请求（含 302 零信任检测 & 401 自动重试一次）
    # ------------------------------------------------------------------ #
    async def _eas_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        timeout = float(self.config.get("api_timeout", 15) or 15)
        url = f"{EAS_API_BASE}{path}"

        for attempt in range(2):
            token = await self._get_valid_token(force_refresh=(attempt == 1))
            headers = {
                "Authorization": token,
                "Content-Type": "application/json;charset=UTF-8",
                "Accept": "application/json",
            }
            cookies = {"userToken": token}

            # trust_env=False 绕过 Clash 代理；follow_redirects=False 精准识别 nox 302 拦截
            async with httpx.AsyncClient(
                timeout=timeout,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                if method.upper() == "GET":
                    resp = await client.get(url, params=params, headers=headers, cookies=cookies)
                else:
                    resp = await client.post(url, json=json_body, headers=headers, cookies=cookies)

            # 1. 检测 302 零信任网关 / CAS 跳转
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location", "")
                if "nox" in loc.lower() or "portal" in loc.lower():
                    raise _ZzuGatewayInterceptError(
                        f"请求教务系统被校园网零信任网关（nox 认证门户）302 拦截（跳转至 {loc}），请先在服务器或同网段环境完成网关认证。"
                    )
                if attempt == 0:
                    continue
                raise _ZzuAuthExpiredError(f"教务接口发生重定向 ({resp.status_code} -> {loc})，Token 可能已失效。")

            # 2. 检测 401 未认证 / Token 过期
            if resp.status_code == 401:
                if attempt == 0:
                    logger.info("[ZZUEmptyClassroom] 教务返回 401，尝试刷新/重登 Token 后重试一次...")
                    continue
                raise _ZzuAuthExpiredError("教务系统返回 401（Token 已失效且自动刷新失败），请重新 `/空教室登录` 或更新 user_token。")

            if resp.status_code != 200:
                raise RuntimeError(f"教务接口 {path} 返回状态码 {resp.status_code}：{resp.text[:200]}")

            try:
                payload = resp.json()
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"教务接口 {path} 未返回合法 JSON（可能被网关拦截）：{resp.text[:150]}") from exc

            if isinstance(payload, dict) and payload.get("result") not in (None, 0):
                raise RuntimeError(f"教务接口返回错误：{payload.get('message') or payload.get('msg') or payload}")

            return payload.get("data", payload) if isinstance(payload, dict) else payload

        raise RuntimeError("教务接口请求重试后仍失败。")

    # ------------------------------------------------------------------ #
    # 字典拉取与缓存（校区 / 楼栋 / 节次时刻表）
    # ------------------------------------------------------------------ #
    async def _get_campus_map(self) -> dict[str, int]:
        now = time.time()
        if self._campus_cache and now - self._campus_cache[0] < 86400:
            return self._campus_cache[1]
        try:
            data = await self._eas_request("GET", "/room/place/campus")
            items = data if isinstance(data, list) else (data.get("list") or data.get("rows") or [])
            mapping: dict[str, int] = {}
            for item in items:
                if isinstance(item, dict) and item.get("id") is not None:
                    c_name = str(item.get("nameZh") or item.get("name") or "").strip()
                    if c_name:
                        mapping[c_name] = int(item["id"])
            if mapping:
                self._campus_cache = (now, mapping)
                return mapping
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ZZUEmptyClassroom] 拉取校区列表失败，使用兜底映射: {exc}")
        return dict(DEFAULT_CAMPUS_MAP)

    async def _get_buildings(self, campus_id: int) -> list[dict[str, Any]]:
        now = time.time()
        cached = self._building_cache.get(campus_id)
        if cached and now - cached[0] < 86400:
            return cached[1]
        # 文档注明 campusAssoc 为必填参数
        data = await self._eas_request("GET", "/room/place/building", params={"campusAssoc": campus_id})
        items = data if isinstance(data, list) else (data.get("list") or data.get("rows") or data.get("data") or [])
        buildings = [b for b in items if isinstance(b, dict) and b.get("id") is not None]
        self._building_cache[campus_id] = (now, buildings)
        return buildings

    async def _get_course_units(self) -> dict[int, tuple[int, int, str]]:
        now = time.time()
        if self._units_cache and now - self._units_cache[0] < 86400:
            return self._units_cache[1]
        try:
            data = await self._eas_request("GET", "/room/system/course-units")
            items = data if isinstance(data, list) else (data.get("list") or data.get("units") or [])
            units: dict[int, tuple[int, int, str]] = {}
            for idx, item in enumerate(items, start=1):
                if not isinstance(item, dict):
                    continue
                unit_no = int(item.get("indexNo") or item.get("unit") or item.get("sort") or item.get("index") or idx)
                s_val = self._parse_hhmm(item.get("startTime") or item.get("startTimeString"))
                e_val = self._parse_hhmm(item.get("endTime") or item.get("endTimeString"))
                if s_val is not None and e_val is not None:
                    s_str = item.get("startTimeString") or f"{s_val // 100:02d}:{s_val % 100:02d}"
                    e_str = item.get("endTimeString") or f"{e_val // 100:02d}:{e_val % 100:02d}"
                    units[unit_no] = (s_val, e_val, f"{s_str}-{e_str}")
            if units:
                self._units_cache = (now, units)
                return units
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[ZZUEmptyClassroom] 拉取节次表失败，使用兜底时刻表: {exc}")
        return dict(DEFAULT_UNITS)

    def _match_buildings(self, building_query: str, buildings: list[dict[str, Any]]) -> tuple[list[int], list[str]]:
        """模糊匹配教学楼名，返回 (id列表, 名称列表)。"""
        q = (building_query or "").strip()
        if not q:
            return [], []

        def b_name(b: dict[str, Any]) -> str:
            return str(b.get("nameZh") or b.get("name") or b.get("code") or "").strip()

        def norm(s: str) -> str:
            return re.sub(r"[\s_\-教学楼]", "", s.lower())

        # 1. 精确匹配
        exact = [b for b in buildings if b_name(b).lower() == q.lower() or str(b.get("code") or "").lower() == q.lower()]
        if exact:
            return [int(b["id"]) for b in exact], [b_name(b) for b in exact]

        # 2. 归一化精确匹配 (如 "北1楼" -> "北1")
        nq = norm(q)
        norm_exact = [b for b in buildings if norm(b_name(b)) == nq]
        if norm_exact:
            return [int(b["id"]) for b in norm_exact], [b_name(b) for b in norm_exact]

        # 3. 包含匹配
        partial = [b for b in buildings if nq in norm(b_name(b)) or norm(b_name(b)) in nq]
        if partial:
            return [int(b["id"]) for b in partial], [b_name(b) for b in partial]

        return [], []

    # ------------------------------------------------------------------ #
    # 分页拉取教室占用表 + 本地反算空闲教室
    # ------------------------------------------------------------------ #
    async def _fetch_all_rooms(
        self, date_str: str, campus_id: int, building_ids: list[int]
    ) -> list[dict[str, Any]]:
        ttl = max(1, int(self.config.get("cache_minutes", 15) or 15)) * 60
        cache_key = (date_str, campus_id, tuple(sorted(building_ids)))
        now = time.time()

        # 顺便清理过期缓存
        for k in list(self._rooms_cache.keys()):
            if now - self._rooms_cache[k][0] >= ttl:
                self._rooms_cache.pop(k, None)

        if cache_key in self._rooms_cache:
            return self._rooms_cache[cache_key][1]

        all_rooms: list[dict[str, Any]] = []
        page = 1
        page_size = 100
        max_pages = 30

        while page <= max_pages:
            body = {
                "currentPage": page,
                "pageSize": page_size,
                "campusAssoc": campus_id,
                "buildingIds": building_ids,
                "roomTypeIds": [],
                "floors": [],
                "minSeat": "",
                "maxSeat": "",
                "date": date_str,
            }
            data = await self._eas_request("POST", "/room/place/rooms", json_body=body)

            page_items: list[dict[str, Any]] = []
            total = None
            if isinstance(data, list):
                page_items = data
            elif isinstance(data, dict):
                for key in ("list", "rows", "records", "items", "content", "data"):
                    if isinstance(data.get(key), list):
                        page_items = data[key]
                        break
                total = data.get("total") or data.get("totalCount") or data.get("totalElements")

            if not page_items:
                break

            all_rooms.extend([r for r in page_items if isinstance(r, dict)])

            if len(page_items) < page_size:
                break
            if total is not None and len(all_rooms) >= int(total):
                break
            page += 1

        self._rooms_cache[cache_key] = (now, all_rooms)
        return all_rooms

    def _parse_hhmm(self, val: Any) -> int | None:
        if val is None or val == "":
            return None
        if isinstance(val, int):
            return val
        s = str(val).strip()
        if s.isdigit():
            return int(s)
        m = re.match(r"^(\d{1,2}):(\d{2})", s)
        if m:
            return int(m.group(1)) * 100 + int(m.group(2))
        return None

    def _is_unit_free(
        self,
        occupations: list[dict[str, Any]],
        unit_start: int,
        unit_end: int,
    ) -> bool:
        for occ in occupations:
            if not isinstance(occ, dict):
                continue
            occ_s = self._parse_hhmm(occ.get("startTime") or occ.get("startTimeString"))
            occ_e = self._parse_hhmm(occ.get("endTime") or occ.get("endTimeString"))
            if occ_s is None or occ_e is None:
                continue
            # 区间严格相交判定
            if occ_s < unit_end and unit_start < occ_e:
                return False
        return True

    async def _query_empty_classroom_core(
        self,
        campus: str = "",
        building: str = "",
        date: str = "",
        periods: str = "",
    ) -> str:
        norm_campus = self._normalize_campus(campus)
        norm_date = self._normalize_date(date)
        norm_building = (building or "").strip()

        # 1. 获取校区 ID
        campus_map = await self._get_campus_map()
        campus_id = campus_map.get(norm_campus)
        if campus_id is None:
            for c_name, c_id in campus_map.items():
                if norm_campus in c_name or c_name in norm_campus:
                    campus_id = c_id
                    norm_campus = c_name
                    break
        if campus_id is None:
            return f"⚠️ 未找到校区「{norm_campus}」，可选校区有：{', '.join(campus_map.keys())}"

        # 2. 解析教学楼 ID
        building_ids: list[int] = []
        matched_b_names: list[str] = []
        if norm_building:
            buildings = await self._get_buildings(campus_id)
            building_ids, matched_b_names = self._match_buildings(norm_building, buildings)
            if not building_ids:
                avail = [str(b.get("nameZh") or b.get("name") or "") for b in buildings[:20]]
                return f"⚠️ 在【{norm_campus}】未找到教学楼「{norm_building}」。\n可选楼栋参考：{', '.join(avail)}"

        # 3. 获取节次表与解析查询节次
        units_map = await self._get_course_units()
        target_units, period_label = self._parse_periods_to_units(periods, norm_date, units_map)

        # 4. 分页拉取教室占用表
        rooms = await self._fetch_all_rooms(norm_date, campus_id, building_ids)

        # 5. 反算空闲教室（排除 virtual 虚拟教室；不筛选 enabled/seatsForLesson）
        free_rooms: list[tuple[str, str]] = []
        for room in rooms:
            if room.get("virtual") is True:
                continue
            r_name = str(room.get("nameZh") or room.get("code") or room.get("name") or "").strip()
            if not r_name or "不安排" in r_name:
                continue

            occs = room.get("roomOccupationInfoVms") or []
            all_target_free = True
            for u in target_units:
                u_s, u_e, _ = units_map.get(u, DEFAULT_UNITS.get(u, (0, 0, "")))
                if u_s and u_e and not self._is_unit_free(occs, u_s, u_e):
                    all_target_free = False
                    break

            if all_target_free:
                # 顺便算一下今天各大节的空闲概况，给用户更实用的参考
                free_blocks = []
                for a, b in BIG_BLOCKS:
                    a_s = units_map.get(a, DEFAULT_UNITS[a])[0]
                    b_e = units_map.get(b, DEFAULT_UNITS[b])[1]
                    if self._is_unit_free(occs, a_s, b_e):
                        free_blocks.append(f"{a}-{b}")
                block_desc = "全天空闲" if len(free_blocks) == len(BIG_BLOCKS) else f"今日空:{','.join(free_blocks)}节"
                free_rooms.append((r_name, block_desc))

        # 排序
        free_rooms.sort(key=lambda x: x[0])

        bldg_display = ", ".join(matched_b_names) if matched_b_names else (norm_building or "全部教学楼")
        header = (
            f"🏫 **郑州大学空教室查询结果**\n"
            f"📅 日期：{norm_date} | 校区：{norm_campus}\n"
            f"🏢 楼栋：{bldg_display} | ⏰ 时段：{period_label}"
        )

        if not free_rooms:
            return f"{header}\n\n😭 该时段暂无符合条件的空闲教室。"

        max_display = int(self.config.get("max_rooms_display", 30) or 30)
        lines = [header, f"共找到 **{len(free_rooms)}** 间空闲教室："]
        for r_name, desc in free_rooms[:max_display]:
            lines.append(f"• **{r_name}** ({desc})")

        if len(free_rooms) > max_display:
            lines.append(f"... 其余 {len(free_rooms) - max_display} 间教室已省略（建议指定具体教学楼，或在配置中调大 max_rooms_display）。")

        return "\n".join(lines)