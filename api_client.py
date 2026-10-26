from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone, tzinfo
from email.utils import parsedate_to_datetime
from typing import Any, Optional
from urllib.parse import quote
import re
import json
import threading
import requests
import time

from .config import NETWORK, QUERY

from astrbot.api import logger

# 全局对外API并发限制：避免高峰期多个任务同时发起大量请求占满连接/带宽
_GLOBAL_API_SEMAPHORE = threading.Semaphore(NETWORK.global_api_semaphore)
_API_SEMAPHORE_TIMEOUT = NETWORK.api_semaphore_timeout

# 官方 Open API 固定参数（官方硬性限制，无需用户配置）
_OPEN_API_BASE_URL = "https://open-api.bser.io"
_OPEN_API_RATE = 1.0    # 每秒 1 次请求
_OPEN_API_BURST = 2.0   # 瞬时突发 2 个并发
_OPEN_API_BUCKET_TIMEOUT = 5.0  # 令牌获取超时（秒），超时降级放行


class _ApiSemaphoreTimeoutError(RuntimeError):
    """API 信号量获取超时异常"""
    pass


@contextmanager
def _acquire_api_sem():
    """带超时的 API 信号量获取。超时后抛出 _ApiSemaphoreTimeoutError。"""
    acquired = _GLOBAL_API_SEMAPHORE.acquire(timeout=_API_SEMAPHORE_TIMEOUT)
    if not acquired:
        raise _ApiSemaphoreTimeoutError(
            f"API 信号量获取超时 (timeout={_API_SEMAPHORE_TIMEOUT}s)"
        )
    try:
        yield
    finally:
        _GLOBAL_API_SEMAPHORE.release()


class ERApiClient:
    BASE_URL = "https://er.dakgg.io/api/v1"

    SEASON_ALIASES: dict[str, str] = {
        **{f"S{i}": f"SEASON_{i + 9}" for i in range(1, 21)},
        **{f"PS{i}": f"PRE_SEASON_{i + 9}" for i in range(1, 21)},
    }

    SEASON_SHORT: dict[str, str] = {
        **{f"SEASON_{i + 9}": f"S{i}" for i in range(1, 21)},
        **{f"PRE_SEASON_{i + 9}": f"PS{i}" for i in range(1, 21)},
    }

    SEASON_ID_MAP: dict[str, int] = {
        **{f"SEASON_{i + 9}": 2 * (i + 9) - 1 for i in range(1, 21)},
        **{f"PRE_SEASON_{i + 9}": 2 * (i + 9) - 2 for i in range(1, 21)},
    }

    def __init__(
        self,
        timeout: int = NETWORK.api_timeout,
        user_agent: str = NETWORK.user_agent,
    ):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "application/json,text/plain,*/*",
        })

    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """内置最多 3 次重试：网络层异常(超时/SSL/连接重置)与 5xx 指数退避重试，
        403/401 及其他 4xx 视为权限/限流/风控，立即停止重试。
        """
        # 支持传入完整 URL（如 leaderboard 的 v0 接口），否则拼接到 BASE_URL
        url = path if path.startswith(("http://", "https://")) else f"{self.BASE_URL}{path}"
        max_attempts = 3
        last_exc: Exception | None = None

        def _do_request() -> requests.Response:
            try:
                with _acquire_api_sem():
                    return self.session.get(url, params=params, timeout=self.timeout)
            except _ApiSemaphoreTimeoutError as e:
                logger.warning(f"[ER API] 信号量获取超时，降级为不限并发: {e}")
                return self.session.get(url, params=params, timeout=self.timeout)

        for attempt in range(1, max_attempts + 1):
            try:
                resp = _do_request()
                resp.raise_for_status()
                return resp.json()
            except requests.HTTPError as e:
                status = getattr(e.response, "status_code", 0)
                # 4xx（含 403/401 限流风控）：重试无意义，立即停止
                if 400 <= status < 500:
                    logger.warning(f"[ER API] 请求被拒绝(status={status}, path={path})，停止重试")
                    raise
                # 5xx：进入重试
                last_exc = e
                if attempt < max_attempts:
                    sleep_s = 2 ** (attempt - 1)
                    logger.warning(
                        f"[ER API] 服务器错误(status={status}, path={path})，"
                        f"第{attempt}/{max_attempts}次失败，{sleep_s}s后重试"
                    )
                    time.sleep(sleep_s)
            except (requests.Timeout, requests.ConnectionError) as e:
                # 网络层：超时/SSL断开/连接重置 → 重试
                last_exc = e
                if attempt < max_attempts:
                    sleep_s = 2 ** (attempt - 1)
                    logger.warning(
                        f"[ER API] 网络异常({type(e).__name__}, path={path})，"
                        f"第{attempt}/{max_attempts}次失败，{sleep_s}s后重试"
                    )
                    time.sleep(sleep_s)
            except Exception as e:
                # 其他未知异常：兜底重试
                last_exc = e
                if attempt < max_attempts:
                    sleep_s = 2 ** (attempt - 1)
                    logger.warning(
                        f"[ER API] 未知异常({type(e).__name__}: {e}, path={path})，"
                        f"第{attempt}/{max_attempts}次失败，{sleep_s}s后重试"
                    )
                    time.sleep(sleep_s)

        logger.warning(f"[ER API] 请求最终失败(path={path})，共尝试{max_attempts}次")
        raise last_exc

    def _resolve_season(self, season: Optional[str]) -> str:
        if not season:
            return self.get_current_season_key()
        return self.SEASON_ALIASES.get(season.upper(), season)

    def _fetch_network_time(self, url: str, target_tz: Optional[tzinfo] = None) -> datetime:
        """从指定 URL 获取网络时间（在信号量保护下调用）。"""
        resp = self.session.head(
            url,
            timeout=min(self.timeout, 10),
            allow_redirects=True,
        )
        date_header = resp.headers.get("Date")

        if not date_header:
            resp.close()
            resp = self.session.get(
                url,
                timeout=min(self.timeout, 10),
                stream=True,
            )
            date_header = resp.headers.get("Date")

        resp.close()

        if not date_header:
            raise RuntimeError(f"URL {url} 无 Date 响应头")

        network_time = parsedate_to_datetime(date_header)
        if network_time.tzinfo is None:
            network_time = network_time.replace(tzinfo=timezone.utc)

        return network_time.astimezone(target_tz) if target_tz else network_time.astimezone()

    def get_network_time(self, target_tz: Optional[tzinfo] = None) -> datetime:
        urls = [
            f"{self.BASE_URL}/data/seasons",
            "https://dak.gg",
            "https://www.cloudflare.com",
        ]
        last_error: Exception | None = None

        for url in urls:
            try:
                try:
                    with _acquire_api_sem():
                        return self._fetch_network_time(url, target_tz)
                except _ApiSemaphoreTimeoutError as e:
                    logger.warning(f"[ER API] 信号量获取超时，降级为不限并发: {e}")
                    return self._fetch_network_time(url, target_tz)
            except Exception as e:
                last_error = e

        raise RuntimeError(f"无法获取网络时间: {last_error}")

    def get_profile(self, player_name: str, season: Optional[str] = None) -> dict[str, Any]:
        return self._get(
            f"/players/{player_name}/profile",
            params={"season": self._resolve_season(season)},
        )
    
    def sync_player_by_name(self, player_name: str) -> dict[str, Any]:
        encoded_name = quote(player_name, safe="")
        url = f"https://er.dakgg.io/api/v0/rpc/player-sync/by-name/{encoded_name}"

        headers = {
            "User-Agent": self.session.headers.get("User-Agent", "Mozilla/5.0"),
            "Accept": "application/json,text/plain,*/*",
            "Referer": f"https://dak.gg/er/players/{encoded_name}",
            "Origin": "https://dak.gg",
        }

        try:
            with _acquire_api_sem():
                resp = self.session.get(url, headers=headers, timeout=self.timeout)
                resp.raise_for_status()
                try:
                    return resp.json()
                except Exception:
                    return {"ok": True, "text": resp.text}
        except _ApiSemaphoreTimeoutError as e:
            logger.warning(f"[ER API] 信号量获取超时，降级为不限并发: {e}")
            resp = self.session.get(url, headers=headers, timeout=self.timeout)
            resp.raise_for_status()
            try:
                return resp.json()
            except Exception:
                return {"ok": True, "text": resp.text}

    def sync_and_wait_for_profile(
        self,
        player_name: str,
        season: Optional[str] = None,
    ) -> tuple[dict[str, Any], bool]:
        """触发同步并等待数据更新完成。"""
        season = self._resolve_season(season)
        poll_intervals = NETWORK.sync_poll_intervals

        # ---- Step 1: 获取同步前的 profile，记录 syncedAt ----
        before_profile = self.get_profile(player_name, season=season)
        before_synced_at = int(before_profile.get("player", {}).get("syncedAt", 0) or 0)

        # ---- Step 2: 触发同步（通知 DAK.GG 去官方拉取最新数据）----
        self.sync_player_by_name(player_name)

        # ---- Step 3: Check-Lock-Check 渐进式轮询 ----
        last_profile = before_profile

        for idx, interval in enumerate(poll_intervals):
            if interval > 0:
                time.sleep(interval)

            current_profile = self.get_profile(player_name, season=season)
            current_synced_at = int(current_profile.get("player", {}).get("syncedAt", 0) or 0)
            last_profile = current_profile

            if idx == 0:
                if current_synced_at == before_synced_at and before_synced_at > 0:
                    logger.debug(f"[ER API] 玩家 {player_name} 数据已在同步CD内（syncedAt={before_synced_at}）")
                    return last_profile, True
            else:
                if current_synced_at > before_synced_at:
                    logger.debug(f"[ER API] 玩家 {player_name} 数据已更新（{idx}轮，syncedAt: {before_synced_at} → {current_synced_at}）")
                    return last_profile, True

        # 轮询超时：降级返回旧数据，标记 was_updated=False
        logger.warning(f"[ER API] 玩家 {player_name} 同步轮询超时（共{len(poll_intervals)}轮），返回旧数据")
        return last_profile, False

    def get_matches(
        self,
        player_name: str,
        season: Optional[str] = None,
        matching_mode: str = "RANK",
        team_mode: str = "ALL",
        page: int = 1,
    ) -> dict[str, Any]:
        return self._get(
            f"/players/{player_name}/matches",
            params={
                "season": self._resolve_season(season),
                "matchingMode": matching_mode,
                "teamMode": team_mode,
                "page": page,
            },
        )

    def get_union_teams(self, player_name: str, season: Optional[str] = None) -> dict[str, Any]:
        return self._get(
            f"/players/{player_name}/union-teams",
            params={"season": self._resolve_season(season)},
        )

    def get_match_detail(
        self,
        player_name: str,
        season_id: int,
        game_id: int,
    ) -> dict[str, Any]:
        """获取单局全员战报（dak.gg 战绩详情页数据源，无需鉴权）。

        season_id 使用该局记录中的数值赛季 ID（如 SEASON_20 → 39），
        返回包含该局所有参赛玩家完整战报的 matches[] 列表。
        供首局战绩的队友详情面板使用。
        """
        return self._get(
            f"/players/{player_name}/matches/{int(season_id)}/{int(game_id)}",
        )

    def get_leaderboard(
        self,
        page: int = 1,
        season_key: Optional[str] = None,
        server_name: str = "seoul",
        team_mode: str = "SQUAD",
        hl: str = "zh_CN"
    ) -> dict[str, Any]:
        season_key = self._resolve_season(season_key)
        # 复用 _get 内置重试（SSLEOFError/超时/连接重置 指数退避重试）
        return self._get(
            "https://er.dakgg.io/api/v0/leaderboard",
            params={
                "page": page,
                "seasonKey": season_key,
                "serverName": server_name,
                "teamMode": team_mode,
                "hl": hl,
            },
        )

    def get_seasons(self, hl: str = "zh_CN") -> list[dict[str, Any]]:
        # 复用 _get 内置重试
        return self._get("/data/seasons", params={"hl": hl}).get("seasons", [])

    def get_current_season_key(self) -> str:
        seasons = self.get_seasons()
        if not seasons:
            raise RuntimeError("无法获取赛季列表")
        for season in seasons:
            if season.get("isCurrent") is True:
                return season.get("key", "")
        raise RuntimeError("未找到当前赛季（isCurrent: true）")

    def get_character_stats(
        self,
        dt: int = QUERY.character_stats_dt,
        matching_mode: str = QUERY.default_matching_mode,
        team_mode: str = QUERY.default_team_mode,
        tier: str = QUERY.default_tier,
    ) -> dict[str, Any]:
        return self._get(
            "/character-stats",
            params={
                "dt": dt,
                "matchingMode": matching_mode,
                "teamMode": team_mode,
                "tier": tier,
            },
        )

    def get_character_recommendations(
        self,
        character_key: str,
        tier: str = QUERY.default_tier,
        weapon: str | None = None,
        matching_mode: str = QUERY.default_matching_mode,
        team_mode: str = QUERY.default_team_mode,
        dt: int = QUERY.character_stats_dt,
        hl: str = "zh_CN",
    ) -> dict[str, Any]:
        """Fetch character recommendation data from the analysis page's __NEXT_DATA__.

        weapon: 指定武器（mastery id/key），推荐数据按该武器返回；为空时取使用次数最多的武器。

        Returns dict with keys: skillBuildStats, traitStats, tacticalSkillStats, itemStats
        """
        url = f"https://dak.gg/er/characters/{character_key}"
        params = {
            "hl": hl,
            "tab": "introduction",
            "teamMode": team_mode,
            "matchingMode": matching_mode,
            "tier": tier,
            "dt": dt,
        }
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }

        max_attempts = 3
        html = ""
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                resp = requests.get(url, params=params, headers=headers, timeout=self.timeout)
                resp.raise_for_status()
                html = resp.text
                last_exc = None
                break
            except requests.HTTPError as e:
                status = getattr(e.response, "status_code", 0)
                # 403/401 判定为权限/限流/风控：不重试，立即退出
                if status in (403, 401):
                    logger.warning(
                        f"[ER角色] 推荐数据请求被拒绝(status={status}, char={character_key})，停止重试"
                    )
                    last_exc = e
                    break
                # 4xx 其他（如404）：也无意义重试
                if 400 <= status < 500:
                    logger.debug(
                        f"[ER角色] 推荐数据请求客户端错误(status={status}, char={character_key})，停止重试"
                    )
                    last_exc = e
                    break
                # 5xx：进入重试
                last_exc = e
                if attempt < max_attempts:
                    sleep_s = 2 ** (attempt - 1)  # 1s, 2s 指数退避
                    logger.warning(
                        f"[ER角色] 推荐数据服务器错误(status={status}, char={character_key})，"
                        f"第{attempt}/{max_attempts}次失败，{sleep_s}s后重试"
                    )
                    time.sleep(sleep_s)
            except (requests.Timeout, requests.ConnectionError) as e:
                # 网络层：超时/连接重置 → 重试
                last_exc = e
                if attempt < max_attempts:
                    sleep_s = 2 ** (attempt - 1)
                    logger.warning(
                        f"[ER角色] 推荐数据网络异常({type(e).__name__}, char={character_key})，"
                        f"第{attempt}/{max_attempts}次失败，{sleep_s}s后重试"
                    )
                    time.sleep(sleep_s)
            except Exception as e:
                # 其他未知异常：也尝试重试，最后一次失败后退出
                last_exc = e
                if attempt < max_attempts:
                    sleep_s = 2 ** (attempt - 1)
                    logger.warning(
                        f"[ER角色] 推荐数据未知异常({type(e).__name__}: {e}, char={character_key})，"
                        f"第{attempt}/{max_attempts}次失败，{sleep_s}s后重试"
                    )
                    time.sleep(sleep_s)

        if last_exc is not None or not html:
            logger.warning(
                f"[ER角色] 推荐数据获取最终失败(char={character_key})，共尝试{max_attempts}次，"
                f"最后错误: {last_exc}"
            )
            return {}

        nd = self._extract_next_data(html)
        queries = (
            nd.get("props", {})
            .get("pageProps", {})
            .get("dehydratedState", {})
            .get("queries", [])
        )

        target_query = None
        for q in queries:
            qk = q.get("queryKey", [])
            if isinstance(qk, list) and qk and str(qk[0]) == "getCharacterStatistics":
                # queryKey: ["getCharacterStatistics", charKey, dt, ...]，需匹配指定 dt
                qk_dt = qk[2] if len(qk) > 2 else None
                try:
                    qk_dt = int(qk_dt)
                except (TypeError, ValueError):
                    qk_dt = None
                if qk_dt == dt:
                    target_query = q
                    break
                # 兜底：未找到精确 dt 时保留第一个匹配项
                if target_query is None:
                    target_query = q

        if not target_query:
            return {}

        data = target_query.get("state", {}).get("data", {})
        snapshot = data.get("characterDetailStatSnapshot", {})
        detail_stat = snapshot.get("characterDetailStat", {})

        weapon_stats_list = detail_stat.get("weaponStats", [])
        if not weapon_stats_list:
            return {}

        # 优先按指定武器匹配，匹配不到时回退为使用次数最多的武器
        best_ws = None
        if weapon is not None:
            for ws in weapon_stats_list:
                ws_key = ws.get("key")
                if str(ws_key) == str(weapon):
                    best_ws = ws
                    break
        if best_ws is None:
            best_ws = max(weapon_stats_list, key=lambda w: w.get("count", 0))

        result = {}
        for field in ["skillBuildStats", "traitStats", "tacticalSkillStats", "itemStats"]:
            val = best_ws.get(field)
            if val:
                result[field] = val

        tier_val = best_ws.get("tier", "")
        if tier_val:
            result["tier"] = tier_val

        return result

    @staticmethod
    def _extract_next_data(html: str) -> dict[str, Any]:
        m = re.search(
            r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            html,
            flags=re.DOTALL,
        )
        if not m:
            return {}
        json_str = m.group(1).strip()
        try:
            return json.loads(json_str)
        except Exception:
            return {}

    def get_all_matches(
        self,
        player_name: str,
        season: Optional[str] = None,
        matching_mode: str = "ALL",
        team_mode: str = "ALL",
        max_pages: int = NETWORK.max_match_pages,
    ) -> list[dict[str, Any]]:
        season = self._resolve_season(season)
        all_matches: list[dict[str, Any]] = []

        for page in range(1, max_pages + 1):
            data = self.get_matches(
                player_name=player_name,
                season=season,
                matching_mode=matching_mode,
                team_mode=team_mode,
                page=page,
            )
            matches = data.get("matches", [])
            if not matches:
                break
            all_matches.extend(matches)

        return all_matches

    def get_patch_notes(self, page: int = 1, size: int = 5) -> dict[str, Any]:
        """Recent Eternal Return patch notes."""
        return self._get("/patch-notes", params={"page": page, "size": size})

    def get_tournament_recent_games(
        self,
        page: int = 1,
        size: int = 20,
        code: Optional[str] = None,
    ) -> dict[str, Any]:
        """Recent esports tournament games, optionally filtered by code prefix."""
        params: dict[str, Any] = {"page": page, "size": size}
        if code:
            params["code"] = code
        return self._get("/tournament/recent-games", params=params)

    def get_tournament_game_detail(self, game_id: int) -> dict[str, Any]:
        """Full score/skill/player detail for one tournament game."""
        return self._get("/tournament/games", params={"id": int(game_id)})

    def get_character_leaderboard(
        self,
        character_key: str,
        season_key: Optional[str] = None,
        team_mode: str = "SQUAD",
        sort_type: str = "TIER",
        page: int = 1,
    ) -> dict[str, Any]:
        """Character-specific player leaderboard."""
        return self._get(
            f"https://er.dakgg.io/api/v0/leaderboard/characters/{quote(str(character_key), safe='')}",
            params={
                "seasonKey": self._resolve_season(season_key),
                "teamMode": team_mode,
                "sortType": sort_type,
                "page": page,
                "hl": "zh_CN",
            },
        )


class _TokenBucket:
    """令牌桶限速器：官方 Open API 限制为 1 req/s、瞬时突发 2 个并发"""

    def __init__(self, rate: float, capacity: float):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, timeout: float = 30.0) -> bool:
        """获取一个令牌。超时返回 False（由调用方降级放行）。"""
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(0.1, deadline - time.monotonic()))


class EROpenApiError(RuntimeError):
    """官方 Open API 请求错误"""


class EROpenApiAuthError(EROpenApiError):
    """官方 Open API Key 无效或已过期"""


class EROpenApiNotFound(EROpenApiError):
    """官方 Open API 资源不存在(404)"""


class EROpenApiUidInvalid(EROpenApiError):
    """官方 Open API 返回的 userId 已失效（玩家可能已改名）"""


class EROpenApiClient:
    """官方 Open API 客户端（无缓存中间层，保证数据最新）。

    限速：令牌桶 1 req/s、突发 2 次（官方限制）。
    Key 无效（401/403）抛出 EROpenApiAuthError，由调用方降级处理。
    仅用于玩家数据查询（user/nickname + user/stats 排位模式），对局数据仍走 dak.gg。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = _OPEN_API_BASE_URL,
        rate: float = _OPEN_API_RATE,
        burst: float = _OPEN_API_BURST,
        timeout: int = NETWORK.api_timeout,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self._bucket = _TokenBucket(rate, burst)
        self._bucket_timeout = _OPEN_API_BUCKET_TIMEOUT
        self.session = requests.Session()
        self.session.headers.update({
            "x-api-key": api_key,
            "Accept": "application/json",
        })

    def _request(self, path: str) -> dict[str, Any]:
        """带令牌桶限速 + 最多3次重试的官方请求；认证失败抛 EROpenApiAuthError。"""
        acquired = self._bucket.acquire(self._bucket_timeout)
        if not acquired:
            logger.warning(f"[ER 官方API] 令牌获取超时({self._bucket_timeout}s)，降级放行")

        url = f"{self.base_url}{path}"
        max_attempts = 3
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                resp = self.session.get(url, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
                code = data.get("code")
                if code != 200:
                    msg = str(data.get("message", ""))
                    if code == 401 or "unauthorized" in msg.lower():
                        raise EROpenApiAuthError(f"官方 Open API Key 无效或已过期: {msg}")
                    raise EROpenApiError(f"官方 Open API 返回错误(code={code}, message={msg})")
                return data
            except EROpenApiAuthError:
                # 认证失败：重试无意义，立即停止
                raise
            except EROpenApiError:
                raise
            except requests.HTTPError as e:
                status = getattr(e.response, "status_code", 0)
                # 401/403：视为 Key 无效，立即停止
                if status in (401, 403):
                    raise EROpenApiAuthError(f"官方 Open API 认证失败(status={status})") from e
                # 404：资源不存在（对 uid 类接口表示 userId 已失效），单独抛以便调用方区分
                if status == 404:
                    raise EROpenApiNotFound(f"官方 Open API 资源不存在(status=404, path={path})") from e
                # 4xx 其他：重试无意义
                if 400 <= status < 500:
                    raise EROpenApiError(f"官方 Open API 请求被拒绝(status={status}, path={path})") from e
                # 5xx：进入重试
                last_exc = e
                if attempt < max_attempts:
                    sleep_s = 2 ** (attempt - 1)
                    logger.warning(
                        f"[ER 官方API] 服务器错误(status={status}, path={path})，"
                        f"第{attempt}/{max_attempts}次失败，{sleep_s}s后重试"
                    )
                    time.sleep(sleep_s)
            except (requests.Timeout, requests.ConnectionError) as e:
                # 网络层：超时/SSL断开/连接重置 → 重试
                last_exc = e
                if attempt < max_attempts:
                    sleep_s = 2 ** (attempt - 1)
                    logger.warning(
                        f"[ER 官方API] 网络异常({type(e).__name__}, path={path})，"
                        f"第{attempt}/{max_attempts}次失败，{sleep_s}s后重试"
                    )
                    time.sleep(sleep_s)
            except Exception as e:
                last_exc = e
                if attempt < max_attempts:
                    sleep_s = 2 ** (attempt - 1)
                    logger.warning(
                        f"[ER 官方API] 未知异常({type(e).__name__}: {e}, path={path})，"
                        f"第{attempt}/{max_attempts}次失败，{sleep_s}s后重试"
                    )
                    time.sleep(sleep_s)

        logger.warning(f"[ER 官方API] 请求最终失败(path={path})，共尝试{max_attempts}次")
        raise last_exc or EROpenApiError(f"官方 Open API 请求失败(path={path})")

    def get_user_id(self, nickname: str) -> str:
        """通过昵称获取玩家 userId（UID 字符串）"""
        data = self._request(f"/v1/user/nickname?query={quote(nickname)}")
        user = data.get("user") or {}
        uid = user.get("userId") or user.get("userNum")
        if not uid:
            raise EROpenApiError(f"官方 Open API 未返回玩家ID(nickname={nickname})")
        return str(uid)

    def get_user_stats(self, user_id: str, season_id: int, matching_mode: int = 3) -> list[dict[str, Any]]:
        """获取玩家指定赛季/模式的统计（V2）。默认固定排位(matching_mode=3)保证段位最新。

        官方公告：每次查昵称会返回不同 userId，昵称不变时旧 userId 长期有效；玩家改名后旧 userId 返回 404。
        此处将 404 转为 EROpenApiUidInvalid，供调用方清理缓存并重新解析昵称。
        """
        try:
            data = self._request(f"/v2/user/stats/uid/{user_id}/{season_id}/{matching_mode}")
        except EROpenApiNotFound as e:
            raise EROpenApiUidInvalid(f"userId {user_id} 已失效（玩家可能已改名）") from e
        return data.get("userStats") or []

    def get_user_games(self, user_id: str) -> list[dict[str, Any]]:
        """获取玩家最近对局列表（官方 V1，最近约10场全模式），用于对局新鲜度检测。"""
        try:
            data = self._request(f"/v1/user/games/uid/{user_id}")
        except EROpenApiNotFound as e:
            raise EROpenApiUidInvalid(f"userId {user_id} 已失效（玩家可能已改名）") from e
        return data.get("userGames") or []
