from __future__ import annotations

from pathlib import Path
from typing import Any, Optional
import time
import json
import re
import queue
import threading
import asyncio
import hashlib
import inspect
import functools
import contextlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig
from astrbot.core.star import StarTools

from .api_client import ERApiClient, EROpenApiClient, EROpenApiAuthError, EROpenApiError, EROpenApiUidInvalid
from .asset_manager import AssetManager
from .mapper import DataMapper
from .renderer import ERRenderer
from .get_image import main as refresh_data
from . import get_image
from .config import NETWORK, CACHE, LOCK, QUERY
from .command_registry import to_panel_items as registry_panel_items
from .platform_compat import QQ_OFFICIAL_NAMES, get_platform_name, get_qq_official_clients
from .qq_official_button import (
    available as qq_buttons_available,
    query_result_buttons,
    send_image_with_buttons,
    send_markdown_with_buttons,
)
from .qq_group_welcome import (
    install_hook as qq_welcome_install_hook,
    send_group_welcome,
)
from .qq_panel_manager import (
    PanelAPIError,
    delete_own_panels,
    delete_panel,
    format_panel_record,
    list_panels,
    sync_panels,
)

from astrbot.api import logger


# 资源更新进行中，其它本插件指令返回的占位提示
# 注意：所有通过 @filter.command / @filter.regex 注册的消息 handler 都必须加 @_resource_busy_guard，
# 否则更新资源期间该指令仍会执行。新增消息 handler 时请务必带上该装饰器。
_RESOURCE_UPDATING_NOTICE = "爱琳正在更新资源喵，请稍等喵~"

# QQ 官方「图文 + 按钮」单条消息的总超时（含素材上传与发送）
QQ_CARD_TIMEOUT = 45.0


def _resource_busy_guard(fn):
    """资源更新期间拦截本插件消息指令，返回固定提示而不是执行。"""

    if inspect.isasyncgenfunction(fn):
        @functools.wraps(fn)
        async def _gen_wrapper(self, event, *args, **kwargs):
            if getattr(self, "_resource_updating", False):
                yield event.plain_result(_RESOURCE_UPDATING_NOTICE)
                return
            async for it in fn(self, event, *args, **kwargs):
                yield it

        return _gen_wrapper

    @functools.wraps(fn)
    async def _coro_wrapper(self, event, *args, **kwargs):
        if getattr(self, "_resource_updating", False):
            return event.plain_result(_RESOURCE_UPDATING_NOTICE)
        return await fn(self, event, *args, **kwargs)

    return _coro_wrapper


# ============================================================
# 带超时的锁工具：避免无限制等待导致程序卡死
# ============================================================

class LockTimeoutError(RuntimeError):
    """获取锁超时异常"""
    pass


@contextlib.contextmanager
def acquire_timeout(lock: threading._RLock | threading.Lock | threading.Semaphore, timeout: float, lock_name: str = "lock"):
    """带超时的锁上下文管理器。超时后抛出 LockTimeoutError 而不是无限等待。"""
    acquired = lock.acquire(timeout=timeout)
    if not acquired:
        raise LockTimeoutError(f"获取锁 {lock_name} 超时 (timeout={timeout}s)，疑似存在死锁或任务卡住")
    try:
        yield
    finally:
        try:
            lock.release()
        except (RuntimeError, threading.ThreadError):
            # 某些情况下（如信号量）释放可能失败，忽略避免掩盖原始异常
            logger.warning(f"[ER战绩] 释放锁 {lock_name} 时检测到异常状态，已忽略")


# ============================================================
# 全局 TTL 缓存 & 查询去重
# ============================================================

class _TTLCache:
    """简单的线程安全TTL缓存，用于排行榜数据等"""

    def __init__(self):
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.RLock()
        self._lock_timeout = LOCK.ttl_cache_lock_timeout

    def get(self, key: str) -> Optional[Any]:
        try:
            with acquire_timeout(self._lock, self._lock_timeout, "ttl_cache_get"):
                item = self._data.get(key)
                if item is None:
                    return None
                expire_ts, value = item
                if time.time() > expire_ts:
                    self._data.pop(key, None)
                    return None
                return value
        except LockTimeoutError as e:
            logger.warning(f"[ER战绩] TTL缓存读取超时，降级为未命中: {e}")
            return None

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        try:
            with acquire_timeout(self._lock, self._lock_timeout, "ttl_cache_set"):
                # 容量保护
                if len(self._data) > CACHE.ttl_max_entries:
                    # 清理过期的；若还是太多，清空
                    now = time.time()
                    expired = [k for k, (ts, _) in self._data.items() if now > ts]
                    for k in expired:
                        self._data.pop(k, None)
                    if len(self._data) > CACHE.ttl_max_entries:
                        self._data.clear()
                self._data[key] = (time.time() + ttl_seconds, value)
        except LockTimeoutError as e:
            logger.warning(f"[ER战绩] TTL缓存写入超时，跳过本次缓存: {e}")


# 排行榜缓存：(season_key, server_name, team_mode) -> leaderboard_data，TTL 5分钟
_LEADERBOARD_CACHE = _TTLCache()

# 正在进行中的玩家查询 Future：避免同一玩家短时间重复请求
# key = cache_key，value = {evt, result, error, created_ts}
_PENDING_QUERIES: dict[str, "dict"] = {}
_PENDING_LOCK = threading.RLock()


def _cleanup_stale_pending() -> None:
    """清理过期的 pending 条目，避免崩溃后残留导致后续查询一直等待超时"""
    try:
        with acquire_timeout(_PENDING_LOCK, LOCK.pending_lock_timeout, "pending_cleanup"):
            now = time.time()
            stale_keys = [
                k for k, v in _PENDING_QUERIES.items()
                if now - v.get("created_ts", 0) > CACHE.pending_max_age
            ]
            for k in stale_keys:
                entry = _PENDING_QUERIES.pop(k, None)
                if entry:
                    entry["error"] = "pending_timeout_cleanup"
                    try:
                        entry["evt"].set()
                    except Exception:
                        pass
            if stale_keys:
                logger.warning(f"[ER战绩] 清理了 {len(stale_keys)} 个过期的 pending 查询条目")
    except LockTimeoutError as e:
        logger.warning(f"[ER战绩] 清理 pending 条目时获取锁超时: {e}")

# 查询结果缓存（图片路径）：避免相同参数在短时间内重复渲染
# key: cache_key, value: (expire_ts, image_path)
_RESULT_IMAGE_CACHE = _TTLCache()

# 游戏模式关键字映射：中文关键词 -> (matchingModeId, API matchingMode, 显示名)
MODE_KEYWORDS: dict[str, tuple[int, str, str]] = {
    "匹配": (2, "NORMAL", "匹配"),
    "排位": (3, "RANK", "排位"),
    "钴协议": (6, "COBALT", "钴协议"),
    "联盟": (8, "SQUAD_RUMBLE", "联盟"),
    "孤狼": (9, "LONE_WOLF", "孤狼"),
    "钴联盟": (11, "SQUAD_RUMBLE_COBALT", "钴联盟"),
}

# 默认模式（排位）
DEFAULT_MODE_ID = 3
DEFAULT_MODE_API = "ALL"
DEFAULT_MODE_LABEL = "排位"


@register(
    "astrbot_plugin_er_profile",
    "椛椿, 小休比",
    "Eternal Return 战绩查询插件",
    "4.0.0",
)
class ERProfilePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)

        # 资源更新进行中标志：置位期间本插件其它指令返回占位提示
        self._resource_updating = False

        # 插件配置（_conf_schema.json）：QQ 按钮等附加模块开关读取于此
        self.plugin_config = config or {}

        # QQ 官方入群欢迎：已挂载钩子的客户端 id 集合（按实例去重）
        self._qq_welcome_hooked: set[int] = set()

        # ===== 路径 =====
        self.plugin_dir = Path(__file__).resolve().parent
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_er_profile")
        self.fonts_dir = self.plugin_dir / "data" / "fonts"
        self.output_dir = self.data_dir / "output"
        self.sync_cache_file = self.data_dir / "cache" / "player_sync_cache.json"
        self.sync_cache_file.parent.mkdir(parents=True, exist_ok=True)
        # 保护 sync_cache 读写（多线程并发触发同步时防止写覆盖丢数据）
        self._sync_cache_lock = threading.RLock()

        self.api = ERApiClient()

        # ===== 官方 Open API（可选）：AstrBot 面板配置了 key 则玩家数据走官方直查保证最新 =====
        self.official_api: Optional[EROpenApiClient] = None
        # 线程池内发现 uid 失效的玩家，放入线程安全队列供 async 层清理 KV 缓存（避免并发查询互相覆盖）
        self._official_uid_invalid: "queue.Queue[str]" = queue.Queue()
        open_api_key = ""
        if config:
            open_api_key = str(config.get("open_api_key", "") or "").strip()
        if open_api_key:
            try:
                self.official_api = EROpenApiClient(api_key=open_api_key)
                logger.info("[ER战绩] 已启用官方 Open API 直查玩家数据（保证最新段位/MMR）")
            except Exception as e:
                logger.warning(f"[ER战绩] 官方 Open API 客户端初始化失败，继续使用 dak.gg 方式: {e}")

        self.local_tz = datetime.now().astimezone().tzinfo
        self.time_sync_interval_seconds = QUERY.time_sync_interval
        self._time_lock = threading.RLock()
        self._time_lock_timeout = LOCK.time_lock_timeout
        self._network_time_base: datetime | None = None
        self._network_time_monotonic_base = 0.0
        self._last_time_sync_monotonic = 0.0
        # 启动 pending 清理后台线程
        self._start_pending_cleanup_task()

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._delete_output_files_older_than(days=QUERY.output_cleanup_days)
        self._start_output_cleanup_task()

        # ===== 核心模块 =====
        self.assets = AssetManager(self.data_dir, self.plugin_dir / "data")
        self.mapper = DataMapper(self.assets)
        self.renderer = ERRenderer(self.assets, self.output_dir, self.fonts_dir)

        # ===== 默认配置 =====
        self.default_match_count = QUERY.default_match_count
        self.max_match_count = QUERY.max_match_count
        # 先用兜底赛季，真实当前赛季由后台线程解析后覆盖（构造函数内禁止同步网络请求）
        self.default_season = QUERY.fallback_season
        self._start_initial_network_tasks()
        # QQ 官方：给 botpy 客户端挂载入群欢迎钩子（平台可能晚于插件就绪，后台重试）
        self._start_qq_welcome_hook_task()

    def _start_initial_network_tasks(self) -> None:
        """后台完成启动阶段的网络初始化（网络时间同步 + 当前赛季解析）。

        __init__ 运行在 AstrBot 事件循环线程上，同步 requests 会阻塞事件循环，
        因此统一放入独立守护线程执行；任一步失败都回退到本地时间 / 兜底赛季。
        """

        def _worker() -> None:
            try:
                self._sync_network_time(force=True)
            except Exception as e:
                logger.warning(f"[ER战绩] 后台同步网络时间失败，使用本地时间兜底: {e}")
            try:
                self.default_season = self._init_default_season()
            except Exception as e:
                logger.warning(
                    f"[ER战绩] 后台获取当前赛季失败，使用兜底赛季 {QUERY.fallback_season}: {e}"
                )

        threading.Thread(target=_worker, name="ER_init_network", daemon=True).start()



    def _format_season_display(self, season_key: str) -> str:
        if season_key.upper().startswith("PS"):
            num = season_key[2:]
            return f"S{num}季前赛"
        short = self.api.SEASON_SHORT.get(season_key, season_key)
        return short

    def _ensure_resources(self) -> str | None:
        if not self.assets.characters:
            return "游戏资源尚未下载，请先发送 /更新资源 完成资源初始化。"
        return None

    def _sync_network_time(self, force: bool = False) -> datetime:
        """同步网络时间。关键修复：网络IO在锁外执行，避免网络卡住时阻塞所有时间相关调用。
        使用 check-lock-check 模式，防止重复同步。
        """
        # ---- Phase 1: 快速路径，无锁检查是否需要同步 ----
        now_monotonic = time.monotonic()
        if (
            not force
            and self._network_time_base is not None
            and now_monotonic - self._last_time_sync_monotonic < self.time_sync_interval_seconds
        ):
            return self._network_time_base + timedelta(
                seconds=now_monotonic - self._network_time_monotonic_base
            )

        # ---- Phase 2: 获取锁后再次检查（避免多个线程同时发起同步） ----
        need_sync = False
        cached_result = None
        try:
            with acquire_timeout(self._time_lock, self._time_lock_timeout, "time_sync_check"):
                now_monotonic = time.monotonic()
                if (
                    not force
                    and self._network_time_base is not None
                    and now_monotonic - self._last_time_sync_monotonic < self.time_sync_interval_seconds
                ):
                    # 锁后检查发现已经被其他线程同步过了，直接返回
                    cached_result = self._network_time_base + timedelta(
                        seconds=now_monotonic - self._network_time_monotonic_base
                    )
                else:
                    # 确实需要同步，但不在锁内执行网络IO
                    need_sync = True
        except LockTimeoutError as e:
            logger.warning(f"[ER战绩] 时间同步检查获取锁超时，使用本地时间兜底: {e}")
            return datetime.now(self.local_tz)

        if cached_result is not None:
            return cached_result

        # ---- Phase 3: 在锁外执行网络请求（避免IO阻塞锁） ----
        if need_sync:
            try:
                now = self.api.get_network_time(self.local_tz)
                logger.info(f"[ER战绩] 网络时间同步成功: {now.isoformat()}")
            except Exception as e:
                logger.warning(f"[ER战绩] 获取网络时间失败，使用本地时间兜底: {e}")
                now = datetime.now(self.local_tz)

            # ---- Phase 4: 再次拿锁写入结果 ----
            synced_time = now
            try:
                with acquire_timeout(self._time_lock, self._time_lock_timeout, "time_sync_write"):
                    now_monotonic = time.monotonic()
                    # 只写入更新的同步，避免覆盖掉其他线程更准确的结果
                    if now_monotonic >= self._last_time_sync_monotonic:
                        self._network_time_base = synced_time
                        self._network_time_monotonic_base = now_monotonic
                        self._last_time_sync_monotonic = now_monotonic
            except LockTimeoutError as e:
                logger.warning(f"[ER战绩] 时间同步写入获取锁超时，结果未写入缓存: {e}")
            return synced_time

        # 理论上不会到这里，兜底
        return datetime.now(self.local_tz)

    def _network_now(self) -> datetime:
        return self._sync_network_time(force=False)

    def _sleep_until_daily_time(self, hour: int, minute: int = 0) -> None:
        while True:
            now = self._network_now()
            next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

            if now >= next_run:
                next_run = next_run + timedelta(days=1)

            sleep_seconds = max((next_run - now).total_seconds(), 1)

            if sleep_seconds <= self.time_sync_interval_seconds:
                time.sleep(sleep_seconds)
                return

            time.sleep(self.time_sync_interval_seconds)
            self._sync_network_time(force=True)

    def _init_default_season(self) -> str:
        """初始化默认赛季：优先从 API 获取，失败时使用 config 中的兜底值"""
        fallback = QUERY.fallback_season
        try:
            season = self.api.get_current_season_key()
            if season != fallback:
                logger.info(f"[ER战绩] 启动时获取当前赛季: {season} (兜底: {fallback})")
            else:
                logger.info(f"[ER战绩] 启动时获取当前赛季: {season}")
            return season
        except Exception as e:
            logger.warning(f"[ER战绩] 启动时获取当前赛季失败，使用兜底赛季 {fallback}: {e}")
            return fallback

    def _clear_output_dir(self) -> None:
        if not self.output_dir.exists():
            return

        for path in self.output_dir.iterdir():
            try:
                if path.is_file():
                    path.unlink()
            except Exception:
                pass

    def _start_output_cleanup_task(self) -> None:
        def worker():
            while True:
                self._sleep_until_daily_time(hour=4)

                try:
                    self._clear_output_dir()
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _start_pending_cleanup_task(self) -> None:
        """启动后台线程，定期清理过期的 pending 查询条目，防止死锁和条目残留"""
        def worker():
            while True:
                time.sleep(CACHE.pending_cleanup_interval)
                try:
                    _cleanup_stale_pending()
                except Exception as e:
                    logger.warning(f"[ER战绩] pending 清理任务异常: {e}")

        threading.Thread(target=worker, daemon=True, name="ER_pending_cleanup").start()

    def _delete_output_files_older_than(self, days: int = 1) -> None:
        if not self.output_dir.exists():
            return

        cutoff = time.time() - days * 24 * 60 * 60

        for path in self.output_dir.iterdir():
            try:
                if not path.is_file():
                    continue

                mtime = path.stat().st_mtime
                if mtime < cutoff:
                    path.unlink()
            except Exception:
                pass

    def _load_sync_cache(self) -> dict:
        if not self.sync_cache_file.exists():
            return {}
        try:
            with self._sync_cache_lock:
                return json.loads(self.sync_cache_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_sync_cache(self, data: dict) -> None:
        try:
            with self._sync_cache_lock:
                self.sync_cache_file.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        except Exception:
            pass

    def _record_sync_time(self, player_name: str) -> None:
        """原子记录玩家的同步CD时间：锁内完成 读→改→写，防止并发写覆盖丢数据。"""
        try:
            with self._sync_cache_lock:
                cache = self._load_sync_cache()
                cache[player_name] = time.time()
                self._save_sync_cache(cache)
        except Exception:
            pass

    def _should_skip_sync(self, player_name: str, ttl_seconds: int = CACHE.sync_skip_ttl) -> bool:
        cache = self._load_sync_cache()
        last_ts = float(cache.get(player_name, 0) or 0)
        return (time.time() - last_ts) < ttl_seconds

    def _in_dakgg_sync_cd(self, player_name: str, synced_at: Optional[float]) -> bool:
        """判断是否在同步 CD 内。

        两个信号任一在 CD 内即跳过：
        - 本地 cache：插件上次请求同步的时刻（防 RPC 重复触发）
        - dak.gg syncedAt：dak.gg 上次成功同步的时刻（感知外部同步，毫秒时间戳）
        """
        cd = CACHE.sync_skip_ttl
        if self._should_skip_sync(player_name):
            return True
        if synced_at is not None and synced_at > 0:
            return (time.time() * 1000 - synced_at) < cd * 1000
        return False

    def _trigger_dakgg_sync(self, player_name: str, synced_at: Optional[float] = None) -> None:
        """任意查询路径成功后，后台异步触发 dak.gg 同步（fire-and-forget）。

        传入 profile 的 syncedAt 时用它判断 CD（感知 dak.gg 真实同步状态）；未传入时回退插件本地 cache。
        """
        if self._in_dakgg_sync_cd(player_name, synced_at):
            return
        # 标记CD，避免并发重复触发
        self._record_sync_time(player_name)

        def _worker() -> None:
            try:
                self.api.sync_player_by_name(player_name)
            except Exception:
                pass

        threading.Thread(target=_worker, name="ER_dakgg_sync", daemon=True).start()

    @staticmethod
    def _friendly_query_error(error_msg: str) -> str:
        """把异常信息转换为符合机器人人设的友好明文提示（技术细节仅记录后台日志）。"""
        if "404" in error_msg or "Not Found" in error_msg:
            return "呜…没有找到相关数据喵，请核对玩家/角色名称后再试一次喵~"
        if "Failed to resolve host" in error_msg or "Name or service not known" in error_msg:
            return "网络解析失败喵，请稍后再试一下喵~"
        if "SSLEOFError" in error_msg or "SSL" in error_msg or "ConnectionError" in error_msg or "Timeout" in error_msg:
            return "网络连接不稳定喵，请稍后再试一下喵~"
        if "无法查找此玩家" in error_msg:
            return "呜…找不到这位玩家喵，请核对玩家ID后再试一次喵~"
        if "EROpenApiAuthError" in error_msg or "Key 无效" in error_msg or "Key 已过期" in error_msg:
            return "官方API Key 无效或已过期喵，请检查插件配置喵~"
        return "查询失败喵，请稍后再试一下喵~"

    @staticmethod
    def _parse_dtm_to_ts(value: Any) -> float:
        """把对局开始时间字符串（如 2026-08-29T02:50:22.022+0900）转成 epoch 秒，失败返回 0。"""
        if not value:
            return 0.0
        try:
            # 归一化时区 +0900 -> +09:00，兼容 Python < 3.11 的 fromisoformat
            s = re.sub(r"([+-]\d{2})(\d{2})\s*$", r"\1:\2", str(value))
            return datetime.fromisoformat(s).timestamp()
        except Exception:
            return 0.0

    @staticmethod
    def _latest_dtm_ts(matches: list[dict[str, Any]]) -> float:
        """取对局列表中最新一局的开始时间戳。"""
        ts = 0.0
        for m in matches:
            t = ERProfilePlugin._parse_dtm_to_ts(m.get("startDtm"))
            if t > ts:
                ts = t
        return ts

    def _sync_and_refetch_matches(
        self,
        player_name: str,
        resolved_season: str,
        mode_api: str,
        target_ts: float,
    ) -> Optional[dict[str, Any]]:
        """dak.gg 对局滞后时：触发同步 + 轮询重取，直到最新对局时间 >= target_ts 或超时。"""
        try:
            self.api.sync_player_by_name(player_name)
            # 记录同步CD，5分钟内不重复触发同步
            self._record_sync_time(player_name)
        except Exception:
            pass
        last: Optional[dict[str, Any]] = None
        for interval in NETWORK.sync_poll_intervals:
            if interval > 0:
                time.sleep(interval)
            try:
                last = self.api.get_matches(
                    player_name, season=resolved_season,
                    matching_mode=mode_api, team_mode="ALL", page=1,
                )
            except Exception:
                continue
            if self._latest_dtm_ts(last.get("matches", [])) >= target_ts:
                break
        return last

    # =========================
    # QQ 官方：入群欢迎（GROUP_ADD_ROBOT 事件被动回复）
    # =========================
    def _qq_welcome_enabled(self) -> bool:
        return bool(self.plugin_config and self.plugin_config.get("qq_group_welcome", True))

    def _qq_welcome_handler(self, client: Any):
        """构造 botpy 入群事件处理器（绑定到具体客户端实例）。"""

        async def _handler(event: Any) -> None:
            try:
                await self._on_group_add_robot(client, event)
            except Exception as e:
                logger.warning(f"[ER欢迎] 入群欢迎处理异常（已忽略）: {e}")

        # 标记来源：插件重载时据此替换旧实例的处理器
        _handler._er_welcome_handler = True  # type: ignore[attr-defined]
        return _handler

    async def _on_group_add_robot(self, client: Any, event: Any) -> None:
        """机器人被拉进群：发一条图文欢迎（不占用主动消息配额）。"""
        welcome_img = self.plugin_dir / "data" / "images" / "er_welcome.jpg"
        await send_group_welcome(
            client,
            event,
            welcome_img if welcome_img.is_file() else None,
        )

    def _install_qq_welcome_hooks(self) -> int:
        """给所有 QQ 官方客户端挂载 on_group_add_robot，返回本次新挂载数量。"""
        if not self._qq_welcome_enabled():
            return 0
        try:
            clients = get_qq_official_clients(self.context)
        except Exception:
            return 0
        installed = 0
        for client in clients:
            if id(client) in self._qq_welcome_hooked:
                continue
            if getattr(client, "on_group_add_robot", None) is not None:
                # 已有处理器（插件重载残留等），标记后不再处理
                self._qq_welcome_hooked.add(id(client))
                continue
            if qq_welcome_install_hook(client, self._qq_welcome_handler(client)):
                self._qq_welcome_hooked.add(id(client))
                installed += 1
                logger.info("[ER欢迎] 已挂载 QQ 官方入群欢迎钩子")
        return installed

    def _start_qq_welcome_hook_task(self) -> None:
        """后台重试挂载钩子：平台实例可能晚于插件初始化就绪。"""

        def _worker() -> None:
            for _ in range(24):
                try:
                    if self._install_qq_welcome_hooks():
                        return
                    if self._qq_welcome_hooked:
                        return
                except Exception as e:
                    logger.debug(f"[ER欢迎] 挂载入群欢迎钩子重试失败: {e}")
                time.sleep(5)

        threading.Thread(target=_worker, name="ER_qq_welcome_hook", daemon=True).start()

    # =========================
    # QQ 官方附加模块：图文 + 指令按钮单条消息
    # =========================
    def _qq_buttons_enabled(self) -> bool:
        return bool(self.plugin_config and self.plugin_config.get("qq_buttons_enabled", True))

    def _qq_inline_card_enabled(self) -> bool:
        return bool(self.plugin_config and self.plugin_config.get("qq_inline_card", True))

    async def _send_qq_profile_card(
        self,
        event: AstrMessageEvent,
        image_path: str,
        player_name: str,
        query_args: list[str],
    ) -> bool:
        """QQ 官方平台：把战绩图与指令按钮合成一条 markdown 图文消息发出。

        返回 True 表示已作为单条消息发送（调用方不要再单独发图片）；
        非 QQ 平台、开关关闭、素材上传或发送失败都返回 False，由调用方走
        「图片消息 + 按钮消息」的老路径。
        """
        try:
            if not (self._qq_buttons_enabled() and self._qq_inline_card_enabled()):
                return False
            if not qq_buttons_available():
                return False
            if get_platform_name(event) not in QQ_OFFICIAL_NAMES:
                return False
            # 平台可能是在插件加载后才添加的，这里顺手补挂入群欢迎钩子（已挂则直接返回）
            self._install_qq_welcome_hooks()
            prev_cmd, next_cmd = self._season_nav_commands(player_name, query_args)
            rows = query_result_buttons(player_name, prev_cmd, next_cmd)
            # 上传素材 + 发送需要网络往返，加超时避免拖住事件循环
            return bool(
                await asyncio.wait_for(
                    send_image_with_buttons(self.context, event, image_path, rows),
                    timeout=QQ_CARD_TIMEOUT,
                )
            )
        except asyncio.TimeoutError:
            logger.warning("[ER战绩] 图文消息发送超时（已降级为图片+按钮两条）")
            return False
        except Exception as e:
            logger.warning(f"[ER战绩] 图文消息发送失败（已降级）: {e}")
            return False

    async def _attach_qq_result_buttons(
        self,
        event: AstrMessageEvent,
        player_name: str,
        query_args: list[str],
    ) -> None:
        """QQ 官方平台：出图后追加「帮助/上一赛季/下一赛季/备注玩家」按钮。

        降级路径专用（图文单条消息发送失败时才会走到这里）：
        非 QQ 平台、开关关闭或发送失败均自动忽略，绝不影响主查询结果。
        """
        try:
            if not self._qq_buttons_enabled():
                return
            if not qq_buttons_available():
                return
            prev_cmd, next_cmd = self._season_nav_commands(player_name, query_args)
            rows = query_result_buttons(player_name, prev_cmd, next_cmd)
            await send_markdown_with_buttons(
                self.context,
                event,
                "",
                rows,
            )
        except Exception as e:
            # 附加按钮任何异常都只记日志，不影响主结果
            logger.warning(f"[ER战绩] 附加按钮发送失败（已忽略）: {e}")

    def _season_nav_commands(
        self,
        player_name: str,
        query_args: list[str],
    ) -> tuple[Optional[str], Optional[str]]:
        """计算「上一赛季/下一赛季」按钮指令；越界时返回 None（对应按钮隐藏）。

        赛季导航只覆盖 S1~S20 线性序列；PS 系列赛季不参与导航（两个按钮均隐藏）。
        规则：查询为 S1 时隐藏上一赛季；查询达到或超过当前赛季时隐藏下一赛季。
        非赛季参数（模式关键词、对局数）原样保留在切换指令尾部。
        """
        order = [f"S{i}" for i in range(1, 21)]
        current = self.api.SEASON_SHORT.get(self.default_season, order[-1])
        if current not in order:
            current = order[-1]
        current_idx = order.index(current)

        season_alias = None
        tail: list[str] = []
        for arg in query_args:
            au = arg.upper()
            if (au.startswith("S") and au[1:].isdigit()) or (
                au.startswith("PS") and au[2:].isdigit()
            ):
                season_alias = au
            else:
                tail.append(arg)
        tail_str = (" " + " ".join(tail)) if tail else ""
        if season_alias is None:
            season_alias = current
        if season_alias not in order:
            return None, None

        idx = order.index(season_alias)
        prev_cmd: Optional[str] = None
        next_cmd: Optional[str] = None
        if idx > 0:
            prev_cmd = f"/查询玩家 {player_name} {order[idx - 1]}{tail_str}"
        if idx < current_idx:
            next_cmd = f"/查询玩家 {player_name} {order[idx + 1]}{tail_str}"
        return prev_cmd, next_cmd

    # =========================
    # 主命令
    # =========================
    @filter.regex(r"^查询(玩家|战绩)\s+.+$")
    @_resource_busy_guard
    async def er_profile(
        self,
        event: AstrMessageEvent,
    ):
        """
        用法:
        查询玩家 小休比
        查询玩家 小休比 S10
        查询玩家 小休比 20
        查询玩家 小休比 S10 20
        查询玩家 小休比 排位
        查询玩家 小休比 排位 S10 20
        """

        message_str = event.message_str.strip()
        parts = message_str.split()
        parts = [p for p in parts if p]

        if len(parts) < 2:
            yield event.plain_result(
                "请输入玩家名，例如：\n"
                "查询玩家 小休比\n"
                "查询玩家 小休比 S10\n"
                "查询玩家 小休比 20\n"
                "查询玩家 小休比 S10 20\n"
                "查询玩家 小休比 排位 S10 20"
            )
            return

        raw_player_name = parts[1]
        player_name = await self._resolve_player_name(raw_player_name)

        err = self._ensure_resources()
        if err:
            yield event.plain_result(err)
            return

        season_key = None
        match_count = self.default_match_count
        mode_id = DEFAULT_MODE_ID
        mode_api = DEFAULT_MODE_API
        mode_label = DEFAULT_MODE_LABEL
        mode_explicit = False

        for arg in parts[2:]:
            arg_upper = arg.upper()
            if arg_upper in MODE_KEYWORDS:
                mode_id, mode_api, mode_label = MODE_KEYWORDS[arg_upper]
                mode_explicit = True
            elif (arg_upper.startswith("S") and arg_upper[1:].isdigit()) or (arg_upper.startswith("PS") and arg_upper[2:].isdigit()):
                season_key = arg_upper
            else:
                try:
                    match_count = int(arg)
                except ValueError:
                    pass

        if match_count < 10:
            match_count = 10
        elif match_count > self.max_match_count:
            match_count = self.max_match_count

        resolved_season = self.api.SEASON_ALIASES.get(season_key, season_key) if season_key else self.default_season
        season_display = self._format_season_display(season_key) if season_key else self._format_season_display(self.default_season)

        # 默认查询显示"所有模式"，指定模式时显示对应模式名
        query_mode_label = mode_label if mode_explicit else "所有模式"

        # ------ 结果缓存：120秒内相同参数直接返回已有图片 ------
        cache_key_raw = f"profile|{player_name.lower()}|{resolved_season}|{match_count}|{mode_api}"
        cache_key = hashlib.md5(cache_key_raw.encode("utf-8")).hexdigest()
        cached_image = _RESULT_IMAGE_CACHE.get(cache_key)
        if cached_image and Path(cached_image).exists():
            yield event.plain_result(
                f"正在查询 {player_name} 的 {season_display} 赛季 {query_mode_label}最近 {match_count} 场战绩喵"
            )
            if not await self._send_qq_profile_card(event, cached_image, player_name, parts[2:]):
                yield event.image_result(cached_image)
                await self._attach_qq_result_buttons(event, player_name, parts[2:])
            return

        # ------ 查询去重：相同参数正在处理时，等待先发起的查询结果 ------
        # 先尝试清理过期 pending，避免刚就撞到残留条目
        try:
            _cleanup_stale_pending()
        except Exception:
            pass

        pending: Optional[dict] = None
        wait_evt: Optional[threading.Event] = None
        is_first = False
        try:
            with acquire_timeout(_PENDING_LOCK, LOCK.pending_lock_timeout, "pending_check"):
                pending = _PENDING_QUERIES.get(cache_key)
                if pending is None:
                    # 第一个请求，创建等待事件（带 created_ts 用于过期清理）
                    wait_evt = threading.Event()
                    pending = {
                        "evt": wait_evt,
                        "result": None,
                        "error": None,
                        "created_ts": time.time(),
                    }
                    _PENDING_QUERIES[cache_key] = pending
                    is_first = True
                else:
                    wait_evt = pending["evt"]
                    is_first = False
        except LockTimeoutError as e:
            logger.warning(f"[ER战绩] 获取 pending 查询锁超时，降级为直接查询: {e}")
            # 拿锁失败时，直接执行查询（不做去重），避免卡死
            is_first = True
            pending = None
            wait_evt = None

        if not is_first and wait_evt is not None and pending is not None:
            yield event.plain_result(
                f"正在查询 {player_name} 的 {season_display} 赛季 {query_mode_label}最近 {match_count} 场战绩喵"
            )
            # 等待 pending 查询结果（阻塞等待放到线程，避免阻塞事件循环）
            signaled = await asyncio.to_thread(wait_evt.wait, CACHE.pending_wait_timeout)
            if signaled and pending.get("result") is not None and Path(pending["result"]).exists():
                if not await self._send_qq_profile_card(event, pending["result"], player_name, parts[2:]):
                    yield event.image_result(pending["result"])
                return
            # 超时或失败，走正常查询流程（自己再查一次）

        try:
            yield event.plain_result(
                f"正在查询 {player_name} 的 {season_display} 赛季 {query_mode_label}最近 {match_count} 场战绩喵"
            )

            # ===== 把同步IO+CPU密集的渲染丢到线程池，不阻塞事件循环 =====
            # 官方方案：仅当前赛季解析 userId 走官方直查（KV 缓存 → 一次 API）；
            # 历史赛季官方拿不到对应对局数据，直接跳过官方链路，只对 dak.gg 请求
            official_uid = None
            if self.official_api is not None and resolved_season == self.default_season:
                official_uid = await self._resolve_official_uid(player_name)

            result = await asyncio.to_thread(
                self._do_query_profile_sync,
                player_name,
                resolved_season,
                match_count,
                mode_id,
                mode_api,
                mode_label,
                official_uid,
            )

            # 官方查询发现缓存的 uid 已失效（玩家改名/删号），清理 KV 缓存，下次重新解析。
            # 队列存放并发查询中所有失效玩家，逐一出队清理（重复清理幂等，无害）；单个失败不影响其余
            while not self._official_uid_invalid.empty():
                try:
                    invalid_name = self._official_uid_invalid.get_nowait()
                except queue.Empty:
                    break
                try:
                    await self._invalidate_official_uid(invalid_name)
                except Exception as e:
                    logger.warning(f"[ER战绩] 清理玩家 {invalid_name} 的官方 userId 缓存失败: {e}")

            if isinstance(result, tuple) and result[0] == "no_season":
                yield event.plain_result(f"该玩家 {season_display} 赛季没有进行任何游戏喵")
                # 清理pending
                try:
                    with acquire_timeout(_PENDING_LOCK, LOCK.pending_lock_timeout, "pending_clear_noseason"):
                        cur = _PENDING_QUERIES.pop(cache_key, None)
                        if cur:
                            cur["error"] = "no_season"
                            try:
                                cur["evt"].set()
                            except Exception:
                                pass
                except LockTimeoutError as e:
                    logger.warning(f"[ER战绩] 清理 pending(no_season) 获取锁超时: {e}")
                return
            if isinstance(result, tuple) and result[0] == "error":
                raise Exception(result[1])

            image_path: str = result
            # 缓存图片结果
            _RESULT_IMAGE_CACHE.set(cache_key, image_path, CACHE.result_image_ttl)

            # 通知等待中的请求
            try:
                with acquire_timeout(_PENDING_LOCK, LOCK.pending_lock_timeout, "pending_notify_ok"):
                    cur = _PENDING_QUERIES.pop(cache_key, None)
                    if cur:
                        cur["result"] = image_path
                        try:
                            cur["evt"].set()
                        except Exception:
                            pass
            except LockTimeoutError as e:
                logger.warning(f"[ER战绩] 通知 pending(ok) 获取锁超时: {e}")

            if not await self._send_qq_profile_card(event, image_path, player_name, parts[2:]):
                yield event.image_result(image_path)
                await self._attach_qq_result_buttons(event, player_name, parts[2:])

        except Exception as e:
            # 通知等待中的请求：失败
            try:
                with acquire_timeout(_PENDING_LOCK, LOCK.pending_lock_timeout, "pending_notify_err"):
                    cur = _PENDING_QUERIES.pop(cache_key, None)
                    if cur:
                        cur["error"] = str(e)
                        try:
                            cur["evt"].set()
                        except Exception:
                            pass
            except LockTimeoutError as lock_err:
                logger.warning(f"[ER战绩] 通知 pending(error) 获取锁超时: {lock_err}")

            error_msg = str(e)
            # 技术细节记录后台日志，用户只看到友好明文
            logger.warning(f"[ER战绩] 玩家查询失败({player_name}): {error_msg}")
            yield event.plain_result(self._friendly_query_error(error_msg))

    def _do_query_profile_sync(
        self,
        player_name: str,
        resolved_season: str,
        match_count: int,
        mode_id: int = DEFAULT_MODE_ID,
        mode_api: str = DEFAULT_MODE_API,
        mode_label: str = DEFAULT_MODE_LABEL,
        official_uid: Optional[str] = None,
        return_data: bool = False,
        render_image: bool = True,
    ) -> Any:
        """【在线程池中执行】同步的玩家查询+映射+渲染流程

        并行优化策略：
        1. profile同步、matches拉取、leaderboard拉取 三者并行执行
           （旧版：串行累计等待，新版：取最长耗时）
        2. 同步内部使用 Check-Lock-Check 快速路径（~85%场景 <1s 返回）
           详见 api_client.sync_and_wait_for_profile 方法
        """
        # ===== 1. 并行请求数据：同步+matches+leaderboard 同时发起 =====
        lb_cache_key = f"{resolved_season}|seoul|SQUAD"
        need_sync = not self._should_skip_sync(player_name)

        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="ER_fetch") as pool:
            futures = {}

            # --- 任务A：获取玩家profile ---
            # 配置了官方 Open API key 且 async 层已解析到 userId 时走官方直查（最新，不触发同步轮询）；
            # 否则走 dak.gg 同步方式（需要时触发同步 + Check-Lock-Check 等待新数据）
            def _fetch_profile() -> tuple[dict[str, Any], bool, Optional[float]]:
                if self.official_api is not None and official_uid:
                    try:
                        profile_data, official_latest_ts = self._fetch_profile_official(
                            player_name, resolved_season, official_uid,
                            detect_games=(mode_api == "ALL"),
                        )
                        # 查了该玩家就后台触发 dak.gg 同步（用 syncedAt 判断 CD），保持 dak.gg 对局数据新鲜
                        synced_at = int((profile_data.get("player") or {}).get("syncedAt", 0) or 0) or None
                        self._trigger_dakgg_sync(player_name, synced_at)
                        return profile_data, True, official_latest_ts
                    except EROpenApiUidInvalid as e:
                        # 玩家已改名导致缓存的 userId 失效：清理缓存（async 层统一处理），本次降级 dak.gg
                        self._official_uid_invalid.put(player_name)
                        logger.info(f"[ER战绩] 官方 userId 已失效（玩家可能改名），本次降级为 dak.gg 查询: {e}")
                    except EROpenApiAuthError as e:
                        logger.info(f"[ER战绩] 官方 Open API Key 无效或已过期，本次降级为 dak.gg 查询: {e}")
                    except Exception as e:
                        logger.info(f"[ER战绩] 官方 Open API 查询失败，本次降级为 dak.gg 查询: {e}")
                if need_sync:
                    data, was_updated = self.api.sync_and_wait_for_profile(
                        player_name, season=resolved_season,
                    )
                    # 同步流程结束后，记录本次触发时间，保证5分钟内不重复触发
                    self._record_sync_time(player_name)
                    return data, was_updated, None
                else:
                    data = self.api.get_profile(player_name, season=resolved_season)
                    return data, True, None

            futures[pool.submit(_fetch_profile)] = "profile"

            # --- 任务B：获取战绩列表（与同步并行，同步等待的间隙就已完成）---
            def _fetch_matches() -> dict[str, Any]:
                return self.api.get_matches(
                    player_name,
                    season=resolved_season,
                    matching_mode=mode_api,
                    team_mode="ALL",
                    page=1,
                )

            futures[pool.submit(_fetch_matches)] = "matches"

            # --- 任务C：获取排行榜数据（缓存命中则秒回，不占线程）---
            def _fetch_leaderboard() -> dict[str, Any]:
                cached = _LEADERBOARD_CACHE.get(lb_cache_key)
                if cached is not None:
                    return cached
                data = self.api.get_leaderboard(
                    season_key=resolved_season,
                    server_name="seoul",
                    team_mode="SQUAD",
                )
                _LEADERBOARD_CACHE.set(lb_cache_key, data, CACHE.leaderboard_cache_ttl)
                return data

            futures[pool.submit(_fetch_leaderboard)] = "leaderboard"

            # 收集结果
            profile_data: Optional[dict] = None
            matches_data: Optional[dict] = None
            leaderboard_data: Optional[dict] = None
            sync_updated: bool = True
            official_latest_ts: Optional[float] = None

            for fut in as_completed(futures):
                tag = futures[fut]
                try:
                    result = fut.result()
                    if tag == "profile":
                        profile_data, sync_updated, official_latest_ts = result
                    elif tag == "matches":
                        matches_data = result
                    elif tag == "leaderboard":
                        leaderboard_data = result
                except Exception as e:
                    raise RuntimeError(f"并行获取 {tag} 数据失败: {e}") from e

        # 双保险：确保关键数据不为 None
        if profile_data is None or matches_data is None or leaderboard_data is None:
            raise RuntimeError("并行数据获取存在缺失，终止渲染")

        # ===== 官方方案：检测 dak.gg 对局是否滞后，滞后则同步+等待+重取 =====
        if official_latest_ts and official_latest_ts > 0:
            dakgg_ts = self._latest_dtm_ts(matches_data.get("matches", []))
            if official_latest_ts > dakgg_ts + 60:
                refreshed = self._sync_and_refetch_matches(
                    player_name, resolved_season, mode_api, official_latest_ts,
                )
                if refreshed is not None:
                    matches_data = refreshed

        cutoff_mmr = self.mapper._get_cutoff_mmr_values(leaderboard_data)

        # ===== 2. 检查赛季数据 =====
        season_data = self.mapper._get_season_data(profile_data, resolved_season)
        if season_data is None:
            return ("no_season",)

        # ===== 3. 数据映射 =====
        render_data = self.mapper.build_render_data(
            profile_data=profile_data,
            matches_data=matches_data,
            match_count=match_count,
            cutoff_mmr=cutoff_mmr,
            season_key=resolved_season,
            matching_mode_id=mode_id,
            mode_label=mode_label,
            sync_updated=sync_updated,
        )

        # ===== 3.5 首局队友详情（best-effort：取数/解析失败则静默跳过，不影响主流程） =====
        first_match_detail = None
        first_matches = render_data.get("matches") or []
        if first_matches:
            try:
                first_match_detail = self._build_first_match_members(player_name, matches_data)
            except Exception as e:
                logger.debug(f"[ER战绩] 获取首局队友详情失败: {e}")
        if first_match_detail:
            render_data["first_match_detail"] = first_match_detail
            # 首局卡及每位队友卡共用的伤害进度条基准 = 同队最高伤害
            try:
                team_max_damage = max(
                    (m["entry"].get("damage", 0) or 0) for m in first_match_detail["members"]
                ) or 0
            except Exception:
                team_max_damage = 0
            if team_max_damage > 0:
                for m in first_match_detail["members"]:
                    m["entry"]["team_max_damage"] = team_max_damage
                if first_matches:
                    first_matches[0]["team_max_damage"] = team_max_damage

        # ===== 4. 渲染图片（render内部已有信号量控制并发） =====
        image_path = self.renderer.render(render_data) if render_image else ""
        if return_data:
            return image_path, render_data
        return image_path

    def _build_first_match_members(
        self,
        player_name: str,
        matches_data: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """拉取战绩列表第一局的单局全员数据，组装队友信息。

        规则：
        - 孤狼/单人队伍不展示（该模式下无队友）；
        - 其它模式（四排/双排/钴协议等）按同 teamNumber 取出主玩家+全部队友；
        - 结果按 gameId 短 TTL 缓存，多人查同一局可复用；
        - 主玩家昵称在全员中匹配不到（改名等）时放弃展示。
        返回结构：{"members":[{entry, nickname, is_main}, ...], "mode_label", "rank", "game_id"}
        """
        raw_matches = (matches_data or {}).get("matches") or []
        if not raw_matches:
            return None
        first = raw_matches[0]

        # 排除无队友模式：孤狼(matchingMode=9) 与单人队伍(teamMode=1)
        try:
            matching_mode = int(first.get("matchingMode") or 0)
            team_mode = int(first.get("matchingTeamMode") or 0)
        except (ValueError, TypeError):
            return None
        if matching_mode == 9 or team_mode == 1:
            return None

        game_id = first.get("gameId")
        season_id = first.get("seasonId")
        if game_id is None or season_id is None:
            return None

        # 仅缓存原始单局详情响应（按 gameId）：不同玩家查同一局可复用接口数据，
        # 但主玩家定位/队友过滤必须按当前查询的玩家重新计算，不能缓存组装结果
        cache_raw = f"match_detail|{game_id}"
        cache_key = hashlib.md5(cache_raw.encode("utf-8")).hexdigest()
        detail = _LEADERBOARD_CACHE.get(cache_key)
        if detail is None:
            try:
                detail = self.api.get_match_detail(player_name, int(season_id), int(game_id))
            except Exception as e:
                logger.debug(f"[ER战绩] 首局详情接口请求失败(game={game_id}): {e}")
                return None
            _LEADERBOARD_CACHE.set(cache_key, detail, CACHE.leaderboard_cache_ttl)

        entries = detail.get("matches") or []
        if not entries:
            return None

        target = player_name.lower()
        main_entry = None
        for entry in entries:
            if str(entry.get("nickname", "") or "").lower() == target:
                main_entry = entry
                break
        if main_entry is None:
            return None

        team_no = main_entry.get("teamNumber")
        team = [e for e in entries if e.get("teamNumber") == team_no]
        if len(team) < 2:
            return None

        # 主玩家置顶，其余队友按排名先后排序
        team.sort(key=lambda e: (0 if e is main_entry else 1, int(e.get("gameRank", 0) or 0)))

        members = []
        for entry in team:
            try:
                mapped = self.mapper._build_single_match(entry)
            except Exception as e:
                logger.debug(f"[ER战绩] 单局队友战报映射失败: {e}")
                continue
            if not mapped:
                continue
            nickname = str(entry.get("nickname", "") or "")
            mapped["player_name"] = nickname
            members.append({
                "entry": mapped,
                "nickname": nickname,
                "is_main": entry is main_entry,
            })

        if len(members) < 2:
            return None

        return {
            "members": members,
            "game_id": game_id,
            "mode_label": str(members[0]["entry"].get("mode_label", "") or ""),
            "rank": int(members[0]["entry"].get("rank", 0) or 0),
            "escaped": bool(members[0]["entry"].get("escaped", False)),
        }

    async def _resolve_official_uid(self, player_name: str) -> Optional[str]:
        """从 KV 缓存解析官方 userId；缓存未命中时调官方 API 一次并写回 KV。

        返回 None 表示官方也解析不到该玩家（玩家不存在/异常），由查询流程降级为 dak.gg。
        KV key: player_official_ids = {昵称: {"uid": ..., "ts": 时间戳}}，30天有效。
        """
        kv = await self.get_kv_data("player_official_ids", {})
        cached = kv.get(player_name)
        now = time.time()
        if cached and now - cached.get("ts", 0) < 30 * 24 * 3600:
            return cached.get("uid")
        try:
            uid = await asyncio.to_thread(self.official_api.get_user_id, player_name)
        except Exception as e:
            logger.info(f"[ER战绩] 官方 Open API 解析玩家ID失败({player_name})，降级为 dak.gg 查询: {e}")
            return None
        kv[player_name] = {"uid": uid, "ts": now}
        await self.put_kv_data("player_official_ids", kv)
        return uid

    async def _invalidate_official_uid(self, player_name: str) -> None:
        """清除 KV 中失效的 nickname→userId 缓存（官方查不到该 uid 统计时调用，避免长期使用旧 uid）"""
        kv = await self.get_kv_data("player_official_ids", {})
        if player_name in kv:
            del kv[player_name]
            await self.put_kv_data("player_official_ids", kv)
            logger.info(f"[ER战绩] 已清除玩家 {player_name} 的官方 userId 缓存（uid 已失效）")

    def _fetch_profile_official(
        self,
        player_name: str,
        resolved_season: str,
        official_uid: str,
        detect_games: bool = True,
    ) -> tuple[dict[str, Any], float]:
        """官方方案：官方直查排位统计（最新）+ dak.gg profile 补结构/tierId。

        官方 stats/games 与 dak.gg profile 并行获取（官方令牌桶 burst=2 同时放行，省 1 秒串行等待）。
        detect_games=False（指定模式查询）时不查 user/games，返回 official_latest_ts=0，
        因为指定模式下官方全模式时间戳与 dak.gg 指定模式口径不一致，滞后检测无意义。
        返回 (profile_data, 官方最新对局时间戳)。games 失败不影响主流程。
        """
        season_id = self.api.SEASON_ID_MAP.get(resolved_season)
        if season_id is None:
            raise ValueError(f"无法映射赛季 {resolved_season} 到官方 seasonId")

        official_latest_ts = 0.0

        def _fetch_stats() -> list[dict[str, Any]]:
            return self.official_api.get_user_stats(official_uid, season_id, matching_mode=3)

        def _fetch_games() -> None:
            nonlocal official_latest_ts
            try:
                # 官方 user/games 覆盖最近对局（全模式，含休闲局），用于对局新鲜度检测
                games = self.official_api.get_user_games(official_uid)
                official_latest_ts = self._latest_dtm_ts(games)
            except EROpenApiUidInvalid:
                raise
            except Exception:
                pass

        def _fetch_dakgg_profile() -> dict[str, Any]:
            # dak.gg profile 提供完整渲染结构（tierId 等），不同步不轮询
            return self.api.get_profile(player_name, season=resolved_season)

        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="ER_official") as pool:
            f_stats = pool.submit(_fetch_stats)
            f_games = pool.submit(_fetch_games) if detect_games else None
            f_profile = pool.submit(_fetch_dakgg_profile)
            official_stats = f_stats.result()
            if f_games is not None:
                f_games.result()
            profile = f_profile.result()

        # dak.gg 对该玩家完全无数据时（首次查询），无法补结构/tierId，降级走同步流程
        if not profile.get("playerSeasons") and not profile.get("playerSeasonOverviews"):
            raise EROpenApiError(f"dak.gg 暂无玩家 {player_name} 数据，走同步流程")
        return self._merge_official_stats(profile, official_stats, season_id), official_latest_ts

    def _merge_official_stats(
        self,
        profile_data: dict[str, Any],
        official_stats: list[dict[str, Any]],
        season_id: int,
    ) -> dict[str, Any]:
        """用官方最新统计数据覆盖 dak.gg profile 中对应赛季排位(3)的统计字段。

        dak.gg 结构保留（tierId/tierGradeId/mmrStats 等），mmr/rank/对局统计改用官方值保证最新。
        官方缺失的字段（damageToPlayer 等）保留 dak.gg 原值。
        """
        if not profile_data or not official_stats:
            return profile_data

        for stats in official_stats:
            if int(stats.get("matchingMode", -1)) != 3:
                continue
            total_games = int(stats.get("totalGames", 0) or 0)
            total_wins = int(stats.get("totalWins", 0) or 0)

            overview_fields: dict[str, Any] = {
                "seasonId": int(stats.get("seasonId", season_id)),
                "matchingModeId": 3,
                "teamModeId": int(stats.get("matchingTeamMode", 3)),
                "mmr": int(stats.get("mmr", 0) or 0),
                "play": total_games,
                "win": total_wins,
                "teamKill": int(stats.get("totalTeamKills", 0) or 0),
                "playerKill": round(float(stats.get("averageKills", 0) or 0) * total_games),
                "playerAssistant": round(float(stats.get("averageAssistants", 0) or 0) * total_games),
                "monsterKill": round(float(stats.get("averageHunts", 0) or 0) * total_games),
                "place": round(float(stats.get("averageRank", 0) or 0) * total_games),
                "top2": round(float(stats.get("top2", 0) or 0) * total_games),
                "top3": round(float(stats.get("top3", 0) or 0) * total_games),
                "rank": {
                    "global": {
                        "rank": int(stats.get("rank", 0) or 0),
                        "rankSize": int(stats.get("rankSize", 0) or 0),
                    },
                    "local": {
                        "rank": int(stats.get("rank", 0) or 0),
                        "rankSize": int(stats.get("rankSize", 0) or 0),
                    },
                },
            }

            # 在 playerSeasonOverviews 中覆盖或新增排位(3)条目（保留 tierId 等 dak.gg 字段）
            overviews = profile_data.setdefault("playerSeasonOverviews", [])
            target: dict[str, Any] | None = None
            for ov in overviews:
                if (int(ov.get("seasonId", -1)) == season_id
                        and int(ov.get("matchingModeId", -1)) == 3
                        and int(ov.get("teamModeId", -1)) == 3):
                    target = ov
                    break
            if target is None:
                target = {}
                overviews.append(target)
            for k, v in overview_fields.items():
                target[k] = v

            # 官方常用英雄数据按 key 合并：覆盖官方有的字段（play/win/top3/place），
            # 保留 dak.gg 的 playerKill/playerAssistant/damageToPlayer（官方无场均字段，避免显示 0）
            char_stats = stats.get("characterStats") or []
            if char_stats:
                existing = {int(c.get("key", 0) or 0): c for c in target.get("characterStats", [])}
                merged: list[dict[str, Any]] = []
                for cs in char_stats:
                    c_key = int(cs.get("characterCode", 0) or 0)
                    c_games = int(cs.get("totalGames", 0) or 0)
                    item = dict(existing.get(c_key, {}))
                    item["key"] = c_key
                    item["play"] = c_games
                    item["win"] = int(cs.get("wins", 0) or 0)
                    item["top3"] = int(cs.get("top3", 0) or 0)
                    item["place"] = round(float(cs.get("averageRank", 0) or 0) * c_games)
                    item.setdefault("playerKill", 0)
                    item.setdefault("playerAssistant", 0)
                    item.setdefault("damageToPlayer", 0)
                    merged.append(item)
                    existing.pop(c_key, None)
                # dak.gg 有而官方没有的字符，保留 dak.gg 原数据
                merged.extend(existing.values())
                target["characterStats"] = merged

            # 同步覆盖 playerSeasons 中的 mmr（tierId/tierGradeId 保留 dak.gg 的）
            for ps in profile_data.setdefault("playerSeasons", []):
                if int(ps.get("seasonId", -1)) == season_id:
                    ps["mmr"] = int(stats.get("mmr", 0) or 0)
                    if "tierMmr" not in ps:
                        ps["tierMmr"] = int(stats.get("mmr", 0) or 0)

        return profile_data

    # =========================
    # 排行榜文本排版
    # =========================
    # 名次徽章：前 3 名用奖牌，4~5 名用数字键帽（避免行首 "4." 被 markdown 当成有序列表）
    _RANK_BADGES = ("🥇", "🥈", "🥉", "4️⃣", "5️⃣")

    @staticmethod
    def _md_escape(text: str) -> str:
        """转义 markdown 元字符。

        QQ 官方适配器会把纯文本当成 markdown 发送（msg_type=2），
        不转义时玩家名里的 * _ ~ ` 会变成斜体/删除线，行首 # 更会变成大标题。
        """
        out = text.replace("\\", "\\\\")
        for ch in ("*", "_", "~", "`", "[", "]", "#", ">"):
            out = out.replace(ch, "\\" + ch)
        return out

    def _format_leaderboard_text(
        self,
        mmr_values: dict[str, Any],
        top_players: list[dict[str, Any]],
        season_key: str,
    ) -> str:
        """把排行榜数据排成便于手机阅读的多行文本（不含 markdown 标题语法）。

        每个玩家占两行：名次 + 玩家名 / 数据摘要；玩家之间空一行，
        避免原来单行过长在手机上换行错乱，也避免用空格强行对齐（比例字体对不齐）。
        """
        season_display = self._format_season_display(season_key)
        immortal = int(mmr_values.get("Immortal_mmr", 0) or 0)
        titan = int(mmr_values.get("Titan_mmr", 0) or 0)

        lines = [
            f"🏆 排行榜　{season_display}",
            "",
            f"永恒分数线　{immortal:,} MMR",
            f"半神分数线　{titan:,} MMR",
        ]
        if not top_players:
            lines += ["", "暂无排行榜数据"]
            return "\n".join(lines)

        lines += ["", "───── 前 5 名 ─────"]
        for idx, player in enumerate(top_players, start=1):
            badge = (
                self._RANK_BADGES[idx - 1]
                if idx <= len(self._RANK_BADGES)
                else f"No.{int(player.get('rank') or idx)}"
            )
            name = str(player.get("player_name", "") or "未知玩家")
            lines.append("")
            lines.append(f"{badge} {name}")
            lines.append(
                f"{int(player.get('mmr') or 0):,} MMR · "
                f"胜率 {float(player.get('win_rate') or 0):.1f}% · "
                f"{int(player.get('games') or 0)} 场 · "
                f"均名 {float(player.get('avg_rank') or 0):.1f}"
            )
        return "\n".join(lines)

    @filter.regex(r"^(永恒多少分|半神多少分|排行榜)\s*$|^(永恒多少分|半神多少分|排行榜)\s+.+$")
    @_resource_busy_guard
    async def er_leaderboard(
        self,
        event: AstrMessageEvent,
    ):
        """
        用法:
        永恒多少分
        半神多少分
        排行榜
        """

        message_str = event.message_str.strip()
        parts = message_str.split()
        parts = [p for p in parts if p]

        season_key = self.default_season
        if len(parts) >= 2:
            arg = parts[1]
            arg_upper = arg.upper()
            if (arg_upper.startswith("S") and arg_upper[1:].isdigit()) or (arg_upper.startswith("PS") and arg_upper[2:].isdigit()):
                season_key = self.api.SEASON_ALIASES.get(arg_upper, arg_upper)

        lb_cache_key = f"{season_key}|seoul|SQUAD"
        try:
            # 先查缓存（也可以放在to_thread里，但本地dict读取很快）
            leaderboard_data = _LEADERBOARD_CACHE.get(lb_cache_key)
            if leaderboard_data is None:
                leaderboard_data = await asyncio.to_thread(
                    self.api.get_leaderboard,
                    page=1,
                    season_key=season_key,
                    server_name="seoul",
                    team_mode="SQUAD",
                )
                _LEADERBOARD_CACHE.set(lb_cache_key, leaderboard_data, CACHE.leaderboard_cache_ttl)

            mmr_values = self.mapper._get_cutoff_mmr_values(leaderboard_data)
            top_players = self.mapper._get_cutoff_player_rank(leaderboard_data, limit=5)

            result_text = self._format_leaderboard_text(mmr_values, top_players, season_key)
            if get_platform_name(event) in QQ_OFFICIAL_NAMES:
                # QQ 侧纯文本按 markdown 渲染，转义后玩家名不会被当成格式
                result_text = self._md_escape(result_text)
            yield event.plain_result(result_text)

        except Exception as e:
            error_msg = str(e)
            # 技术细节记录后台日志，用户只看到友好明文
            logger.warning(f"[ER战绩] 排行榜查询失败: {error_msg}")
            yield event.plain_result(self._friendly_query_error(error_msg))

    # =========================
    # 查看配置
    # =========================
    @filter.command("战绩配置")
    @_resource_busy_guard
    async def er_profile_config(self, event: AstrMessageEvent):
        yield event.plain_result(
            f"默认场次: {self.default_match_count}\n"
            f"最大场次: {self.max_match_count}\n"
            f"赛季: {self._format_season_display(self.default_season)}"
        )

    @filter.command("指令面板")
    @_resource_busy_guard
    async def er_qq_panel(
        self,
        event: AstrMessageEvent,
        action: str = "同步",
        arg2: str = "",
    ):
        """
        用法:
        /指令面板               同步面板（group + c2c，不存在则创建、存在则覆盖更新）
        /指令面板 同步          同上
        /指令面板 同步 本群     只让当前群生效（target_type=specific）
        /指令面板 列表          列出当前已生效的指令面板
        /指令面板 删除          删除本插件同步出来的面板
        /指令面板 删除 <id>     按 panel_id 删除指定面板
        """
        action = (action or "同步").strip()
        arg2 = (arg2 or "").strip()
        if action not in ("同步", "列表", "删除"):
            yield event.plain_result(
                "用法：/指令面板 同步 [本群]\n"
                "/指令面板 列表\n"
                "/指令面板 删除 [panel_id]"
            )
            return

        only_this_group = action == "同步" and arg2 in ("本群", "本群生效")
        if only_this_group and not self._panel_group_openids(event):
            yield event.plain_result("「本群」只能在 QQ 群里使用喵")
            return

        clients = get_qq_official_clients(self.context)
        if not clients:
            yield event.plain_result("当前未检测到 QQ 官方机器人平台，无法管理指令面板喵")
            return
        client = clients[0]

        try:
            if action == "同步":
                items = registry_panel_items()
                result = await sync_panels(
                    client,
                    scopes=("group", "c2c"),
                    items=items,
                    # 加上「本群」参数时，群面板只对当前群生效
                    group_openids=self._panel_group_openids(event) if only_this_group else None,
                )
                lines = [f"指令面板同步完成喵（共 {len(items)} 项指令）："]
                for scope, (status, pid) in result.items():
                    lines.append(f"- {scope} [{status}] {pid}")
                yield event.plain_result("\n".join(lines))
            elif action == "删除":
                if arg2:
                    await delete_panel(client, arg2)
                    yield event.plain_result(f"已删除指令面板 {arg2} 喵")
                else:
                    removed = await delete_own_panels(client)
                    if not removed:
                        yield event.plain_result("没有找到本插件同步的面板喵")
                    else:
                        lines = ["已删除以下面板喵："]
                        for scope, pid in removed:
                            lines.append(f"- {scope} | {pid}")
                        yield event.plain_result("\n".join(lines))
            else:
                lines = ["当前指令面板列表："]
                for scope in ("group", "c2c"):
                    records = await list_panels(client, scope)
                    if not records:
                        lines.append(f"- {scope}: 无")
                        continue
                    for rec in records:
                        lines.append(f"- {scope} | {format_panel_record(rec)}")
                yield event.plain_result("\n".join(lines))
        except PanelAPIError as e:
            logger.warning(f"[ER面板] 指令面板 API 失败: {e}")
            yield event.plain_result(f"指令面板 API 调用失败喵（{e}）")
        except Exception as e:
            logger.warning(f"[ER面板] 指令面板操作异常: {e}")
            yield event.plain_result("指令面板操作失败喵，请稍后再试~")

    @staticmethod
    def _panel_group_openids(event: AstrMessageEvent) -> Optional[list[str]]:
        """在群聊里同步面板时返回当前群 openid，让面板只对该群生效。

        私聊触发时返回 None（面板对全部群/用户生效）。
        """
        try:
            if get_platform_name(event) not in QQ_OFFICIAL_NAMES:
                return None
            group_id = getattr(getattr(event, "message_obj", None), "group_id", None)
            if group_id:
                return [str(group_id)]
        except Exception:
            pass
        return None
    @filter.command("备注列表")
    @_resource_busy_guard
    async def list_all_aliases(self, event: AstrMessageEvent):
        player_aliases = await self._get_player_aliases()
        character_aliases = await self._get_character_aliases()
        weapon_aliases = await self._get_weapon_aliases()

        lines = ["备注列表："]

        if player_aliases:
            lines.append("\n玩家备注：")
            for alias, real in sorted(player_aliases.items()):
                lines.append(f"  {alias} → {real}")
        else:
            lines.append("\n玩家备注：无")

        if character_aliases:
            lines.append("\n角色备注：")
            for alias, real in sorted(character_aliases.items()):
                lines.append(f"  {alias} → {real}")
        else:
            lines.append("\n角色备注：无")

        if weapon_aliases:
            lines.append("\n武器备注：")
            for alias, real in sorted(weapon_aliases.items()):
                lines.append(f"  {alias} → {real}")
        else:
            lines.append("\n武器备注：无")

        yield event.plain_result("\n".join(lines))

    # =========================
    # 指令帮助
    # =========================
    @filter.command("指令", alias={"帮助"})
    @_resource_busy_guard
    async def er_help(self, event: AstrMessageEvent):
        text = (
            "──── 指令说明 ────\n"
            "\n"
            "◆ 查询玩家战绩\n"
            " · 查询玩家 <玩家名>\n"
            " · 查询玩家 <玩家名> <模式>\n"
            " · 查询玩家 <玩家名> <赛季>\n"
            " · 查询玩家 <玩家名> <场次>\n"
            " · 查询玩家 <玩家名> <模式> <赛季> <场次>\n"
            "   例：查询玩家 小休比 排位 S10 20\n"
            "   季前赛用PS，如：查询玩家 小休比 PS10\n"
            "   模式：排位/匹配/钴协议/联盟/孤狼/钴联盟\n"
            "   *除玩家名以外均为可选项\n"
            "   *默认查询全部模式当前赛季战绩\n"
            "\n"
            "◆ 查询角色数据\n"
            " · 查询角色 <角色名>\n"
            " · 查询角色 <武器> <角色名>\n"
            " · 查询角色 <角色名> <段位>\n"
            " · 查询角色 <武器> <角色名> <段位>\n"
            "   例：查询角色 暗器 扎希尔 in1000\n"
            "   段位：修罗/灭钻/星陨/无瑕/in1000\n"
            "\n"
            "◆ 角色评级排行\n"
            " · 角色评级\n"
            " · 角色评级 <段位>\n"
            "   例：角色评级 灭钻\n"
            "   段位：修罗/灭钻/星陨/无瑕/永恒/金魄/银烙/铜魂/铁阎/全段位\n"
            "\n"
            "◆ 分数线与排行榜\n"
            " · 永恒多少分 / 半神多少分\n"
            " · 排行榜\n"
            " · 排行榜 <赛季>\n"
            "\n"
            "◆ 备注功能\n"
            " · 备注玩家 <真实名> <备注名>\n"
            " · 删除玩家备注 <备注名>\n"
            " · 备注角色 <真实名> <备注名>\n"
            " · 删除角色备注 <备注名>\n"
            "\n"
            "◆ 小组功能\n"
            " · 创建小组 <小组名>\n"
            " · 加入小组 <小组名> <玩家名>\n"
            " · 删除小组成员 <小组名> <玩家名>\n"
            " · 删除小组 <小组名>\n"
            " · 查询小组 <小组名>\n"
            " · 查询小组 <小组名> <赛季>\n"
        )
        if get_platform_name(event) in QQ_OFFICIAL_NAMES:
            # QQbot 模式：帮助文字与按钮整合为同一条消息（官方「消息携带 keyboard」方案）
            # markdown 内容中 * 需要转义，避免被渲染成斜体
            sent = await send_markdown_with_buttons(
                self.context,
                event,
                text.replace("*", "\\*"),
                [[{"label": "如何开启无@回复", "data": "/无@唤醒教程", "reply": True}]],
            )
            if not sent:
                yield event.plain_result(text)
            return
        yield event.plain_result(text)

    @filter.command("无@唤醒教程")
    @_resource_busy_guard
    async def er_full_mode_tutorial(self, event: AstrMessageEvent):
        yield event.plain_result(
            "──── 无@唤醒教程 ────\n"
            "群主在 QQ 群内开启「允许机器人接收全部消息」后，\n"
            "群内成员无需 @ 机器人即可直接发送指令，例如：查询玩家 小休比。\n"
            "设置方法见下图（需群主操作）\n"
        )
        tutorial_img = self.plugin_dir / "data" / "images" / "qq_full_mode_tutorial.png"
        if tutorial_img.is_file():
            yield event.image_result(str(tutorial_img))

    # =========================
    # 备注功能
    # =========================
    async def _get_player_aliases(self) -> dict[str, str]:
        data = await self.get_kv_data("player_aliases", {})
        return data if isinstance(data, dict) else {}

    async def _set_player_aliases(self, aliases: dict[str, str]):
        await self.put_kv_data("player_aliases", aliases)

    async def _get_character_aliases(self) -> dict[str, str]:
        data = await self.get_kv_data("character_aliases", {})
        return data if isinstance(data, dict) else {}

    async def _set_character_aliases(self, aliases: dict[str, str]):
        await self.put_kv_data("character_aliases", aliases)

    async def _resolve_player_name(self, name: str) -> str:
        aliases = await self._get_player_aliases()
        return aliases.get(name, name)

    async def _resolve_character_name(self, name: str) -> str:
        aliases = await self._get_character_aliases()
        return aliases.get(name, name)

    @filter.regex(r"^备注玩家\s+.+\s+.+$")
    @_resource_busy_guard
    async def alias_player(self, event: AstrMessageEvent):
        message_str = event.message_str.strip()
        parts = message_str.split()
        parts = [p for p in parts if p]

        if len(parts) != 3:
            yield event.plain_result(
                "用法：备注玩家 <真实玩家名> <备注名>\n"
                "示例：备注玩家 小休比 休比"
            )
            return

        real_name = parts[1]
        alias = parts[2]

        player_aliases = await self._get_player_aliases()

        if alias in player_aliases:
            yield event.plain_result(f"备注名「{alias}」已被占用喵")
            return

        player_aliases[alias] = real_name
        await self._set_player_aliases(player_aliases)

        yield event.plain_result(f"已添加备注：{alias} → {real_name}喵")

    @filter.regex(r"^删除玩家备注\s+.+$")
    @_resource_busy_guard
    async def unalias_player(self, event: AstrMessageEvent):
        message_str = event.message_str.strip()
        parts = message_str.split()
        parts = [p for p in parts if p]

        if len(parts) != 2:
            yield event.plain_result(
                "用法：删除玩家备注 <备注名>\n"
                "示例：删除玩家备注 休比"
            )
            return

        alias = parts[1]
        aliases = await self._get_player_aliases()

        if alias not in aliases:
            yield event.plain_result(f"未找到备注「{alias}」")
            return

        real_name = aliases.pop(alias)
        await self._set_player_aliases(aliases)
        yield event.plain_result(f"已删除备注：{alias} → {real_name}喵")

    @filter.regex(r"^备注角色\s+.+\s+.+$")
    @_resource_busy_guard
    async def alias_character(self, event: AstrMessageEvent):
        message_str = event.message_str.strip()
        parts = message_str.split()
        parts = [p for p in parts if p]

        if len(parts) != 3:
            yield event.plain_result(
                "用法：备注角色 <真实角色名> <备注名>\n"
                "示例：备注角色 扎希尔 扎哥"
            )
            return

        real_name = parts[1]
        alias = parts[2]

        character_aliases = await self._get_character_aliases()

        if alias in character_aliases:
            yield event.plain_result(f"备注名「{alias}」已被角色「{character_aliases[alias]}」占用喵")
            return

        character_aliases[alias] = real_name
        await self._set_character_aliases(character_aliases)

        yield event.plain_result(f"已添加备注：{alias} → {real_name}喵")

    @filter.regex(r"^删除角色备注\s+.+$")
    @_resource_busy_guard
    async def unalias_character(self, event: AstrMessageEvent):
        message_str = event.message_str.strip()
        parts = message_str.split()
        parts = [p for p in parts if p]

        if len(parts) != 2:
            yield event.plain_result(
                "用法：删除角色备注 <备注名>\n"
                "示例：删除角色备注 扎哥"
            )
            return

        alias = parts[1]
        aliases = await self._get_character_aliases()

        if alias not in aliases:
            yield event.plain_result(f"未找到备注「{alias}」")
            return

        real_name = aliases.pop(alias)
        await self._set_character_aliases(aliases)
        yield event.plain_result(f"已删除备注：{alias} → {real_name}喵")

    async def _get_weapon_aliases(self) -> dict[str, str]:
        data = await self.get_kv_data("weapon_aliases", {})
        return data if isinstance(data, dict) else {}

    async def _set_weapon_aliases(self, aliases: dict[str, str]):
        await self.put_kv_data("weapon_aliases", aliases)

    async def _resolve_weapon_name(self, name: str) -> str:
        aliases = await self._get_weapon_aliases()
        return aliases.get(name, name)

    @filter.regex(r"^备注武器\s+.+\s+.+$")
    @_resource_busy_guard
    async def alias_weapon(self, event: AstrMessageEvent):
        message_str = event.message_str.strip()
        parts = message_str.split()
        parts = [p for p in parts if p]

        if len(parts) != 3:
            yield event.plain_result(
                "用法：备注武器 <真实武器名> <备注名>\n"
                "示例：备注武器 投掷 扔"
            )
            return

        real_name = parts[1]
        alias = parts[2]

        weapon_aliases = await self._get_weapon_aliases()

        if alias in weapon_aliases:
            yield event.plain_result(f"备注名「{alias}」已被武器「{weapon_aliases[alias]}」占用喵")
            return

        weapon_aliases[alias] = real_name
        await self._set_weapon_aliases(weapon_aliases)

        yield event.plain_result(f"已添加备注：{alias} → {real_name}喵")

    @filter.regex(r"^删除武器备注\s+.+$")
    @_resource_busy_guard
    async def unalias_weapon(self, event: AstrMessageEvent):
        message_str = event.message_str.strip()
        parts = message_str.split()
        parts = [p for p in parts if p]

        if len(parts) != 2:
            yield event.plain_result(
                "用法：删除武器备注 <备注名>\n"
                "示例：删除武器备注 扔"
            )
            return

        alias = parts[1]
        aliases = await self._get_weapon_aliases()

        if alias not in aliases:
            yield event.plain_result(f"未找到备注「{alias}」")
            return

        real_name = aliases.pop(alias)
        await self._set_weapon_aliases(aliases)
        yield event.plain_result(f"已删除备注：{alias} → {real_name}喵")

    # =========================
    # 小组功能
    # =========================
    async def _get_groups(self) -> dict[str, list[str]]:
        data = await self.get_kv_data("groups", {})
        return data if isinstance(data, dict) else {}

    async def _set_groups(self, groups: dict[str, list[str]]):
        await self.put_kv_data("groups", groups)

    @filter.regex(r"^创建小组\s+.+$")
    @_resource_busy_guard
    async def create_group(self, event: AstrMessageEvent):
        message_str = event.message_str.strip()
        parts = message_str.split()
        parts = [p for p in parts if p]

        if len(parts) != 2:
            yield event.plain_result("用法：创建小组 <小组名>")
            return

        group_name = parts[1]
        groups = await self._get_groups()

        if group_name in groups:
            yield event.plain_result(f"小组「{group_name}」已存在喵")
            return

        groups[group_name] = []
        await self._set_groups(groups)
        yield event.plain_result(f"已创建小组「{group_name}」喵")

    @filter.regex(r"^加入小组\s+.+\s+.+$")
    @_resource_busy_guard
    async def add_group_member(self, event: AstrMessageEvent):
        message_str = event.message_str.strip()
        parts = message_str.split()
        parts = [p for p in parts if p]

        if len(parts) != 3:
            yield event.plain_result("用法：加入小组 <小组名> <玩家名>")
            return

        group_name = parts[1]
        member_name = parts[2]

        groups = await self._get_groups()

        if group_name not in groups:
            yield event.plain_result(f"小组「{group_name}」不存在喵")
            return

        members = groups[group_name]
        if member_name in members:
            yield event.plain_result(f"玩家「{member_name}」已在小组「{group_name}」中")
            return

        members.append(member_name)
        groups[group_name] = members
        await self._set_groups(groups)
        yield event.plain_result(f"已将「{member_name}」加入小组「{group_name}」喵")

    @filter.regex(r"^删除小组成员\s+.+\s+.+$")
    @_resource_busy_guard
    async def remove_group_member(self, event: AstrMessageEvent):
        message_str = event.message_str.strip()
        parts = message_str.split()
        parts = [p for p in parts if p]

        if len(parts) != 3:
            yield event.plain_result("用法：删除小组成员 <小组名> <玩家名>")
            return

        group_name = parts[1]
        member_name = parts[2]

        groups = await self._get_groups()

        if group_name not in groups:
            yield event.plain_result(f"小组「{group_name}」不存在喵")
            return

        members = groups[group_name]
        if member_name not in members:
            yield event.plain_result(f"玩家「{member_name}」不在小组「{group_name}」中")
            return

        members.remove(member_name)
        groups[group_name] = members
        await self._set_groups(groups)
        yield event.plain_result(f"已将「{member_name}」从小组「{group_name}」中移除喵")

    @filter.regex(r"^删除小组\s+.+$")
    @_resource_busy_guard
    async def delete_group(self, event: AstrMessageEvent):
        message_str = event.message_str.strip()
        parts = message_str.split()
        parts = [p for p in parts if p]

        if len(parts) != 2:
            yield event.plain_result("用法：删除小组 <小组名>")
            return

        group_name = parts[1]
        groups = await self._get_groups()

        if group_name not in groups:
            yield event.plain_result(f"小组「{group_name}」不存在喵")
            return

        del groups[group_name]
        await self._set_groups(groups)
        yield event.plain_result(f"已删除小组「{group_name}」喵")

    @filter.regex(r"^查询小组\s+.+$")
    @_resource_busy_guard
    async def query_group(self, event: AstrMessageEvent):
        message_str = event.message_str.strip()
        parts = message_str.split()
        parts = [p for p in parts if p]

        if len(parts) < 2 or len(parts) > 3:
            yield event.plain_result("用法：查询小组 <小组名> [赛季]\n示例：查询小组 测试队 S10")
            return

        group_name = parts[1]
        raw_season_key = parts[2] if len(parts) == 3 else None

        # 赛季解析：与 er_profile 保持一致 → 先转大写，再查 SEASON_ALIASES，保证后续能命中 mapper.SEASON_ID_MAP
        season_key: Optional[str] = None
        if raw_season_key is not None:
            arg = raw_season_key.upper()
            if (arg.startswith("S") and arg[1:].isdigit()) or (arg.startswith("PS") and arg[2:].isdigit()):
                season_key = arg
            else:
                season_key = arg  # 比如用户直接输入 SEASON_19
        resolved_season = self.api.SEASON_ALIASES.get(season_key, season_key) if season_key else self.default_season
        season_display_input = season_key or self.default_season
        season_display = self._format_season_display(season_display_input)

        groups = await self._get_groups()

        if group_name not in groups:
            yield event.plain_result(f"小组「{group_name}」不存在喵")
            return

        members = groups[group_name]
        if not members:
            yield event.plain_result(f"小组「{group_name}」暂无成员喵")
            return

        resolved_members = []
        for m in members:
            resolved = await self._resolve_player_name(m)
            resolved_members.append(resolved)

        yield event.plain_result(f"正在查询小组「{group_name}」的 {len(resolved_members)} 名成员 {season_display} 赛季战绩喵")

        # 并发查询所有成员：把 season 显式传入，避免闭包意外
        async def _query_one(member: str, season: str):
            try:
                def _sync_work():
                    profile_data = self.api.get_profile(member, season=season)
                    # 查了该玩家就后台触发 dak.gg 同步（用 syncedAt 判断 CD），保持小组查询数据新鲜
                    synced_at = int((profile_data.get("player") or {}).get("syncedAt", 0) or 0) or None
                    self._trigger_dakgg_sync(member, synced_at)
                    # 位置参数显式传全：(profile_data, cutoff_mmr, season_key)，避免关键字参数歧义
                    player_data = self.mapper._build_player(profile_data, None, season)
                    if player_data.get("has_ranked_data", False):
                        mmr = int(player_data.get("mmr", 0))
                        tier_name = player_data.get("tier_name", "")
                        return (member, mmr, tier_name, True)
                    else:
                        return (member, 0, "段位未鉴定", False)
                return await asyncio.to_thread(_sync_work)
            except Exception:
                return (member, 0, "查询失败", False)

        raw_results = await asyncio.gather(*(_query_one(m, resolved_season) for m in resolved_members))
        results = list(raw_results)

        total_mmr = 0
        valid_count = 0
        for _, mmr, _, valid in results:
            if valid:
                total_mmr += mmr
                valid_count += 1

        results.sort(key=lambda x: x[1], reverse=True)

        lines = [f"【小组「{group_name}」{season_display} 战绩】\n"]
        for member, mmr, tier, valid in results:
            if valid:
                lines.append(f"  {member}  MMR: {mmr}  ({tier})")
            else:
                lines.append(f"  {member}  {tier}")

        if valid_count > 0:
            avg_mmr = total_mmr // valid_count
            lines.append(f"\n平均MMR（有效排位）: {avg_mmr}")
            lines.append(f"有效排位人数: {valid_count}/{len(resolved_members)}")

        yield event.plain_result("\n".join(lines))

    @filter.command("小组列表")
    @_resource_busy_guard
    async def list_groups(self, event: AstrMessageEvent):
        groups = await self._get_groups()

        if not groups:
            yield event.plain_result("当前没有任何小组")
            return

        lines = ["当前小组列表："]
        for group_name, members in sorted(groups.items()):
            member_names = ", ".join(members) if members else "（空）"
            lines.append(f"\n  「{group_name}」: {member_names}")

        yield event.plain_result("\n".join(lines))

    # =========================
    # 检查并更新资源
    # =========================
    @filter.command("更新资源")
    @_resource_busy_guard
    async def er_update_resources(self, event: AstrMessageEvent):
        self._resource_updating = True
        try:
            yield event.plain_result("开始检查资源差异...")

            get_image.set_paths(self.data_dir)

            # ===== 检查赛季是否变更 =====
            try:
                new_season = await asyncio.to_thread(self.api.get_current_season_key)
                if new_season != self.default_season:
                    logger.info(f"[ER战绩] 赛季已更新: {self.default_season} -> {new_season}")
                    self.default_season = new_season
                    yield event.plain_result(f"赛季已更新: {self._format_season_display(new_season)}")
            except Exception as e:
                logger.warning(f"[ER战绩] 赛季检查失败，继续使用当前赛季 {self.default_season}: {e}")

            # 网络与比对放后台线程，避免阻塞 AstrBot 事件循环
            diff_info = await asyncio.to_thread(get_image.check_resource_diff, False)

            category_names = {
                "json": "JSON数据",
                "characters": "角色头像",
                "character_skins": "角色皮肤",
                "items": "物品图标",
                "tiers": "段位图标",
                "tiers_full": "段位大图",
                "tactical_skills": "战术技能",
                "trait_skills": "特性技能",
                "trait_skill_groups": "特性分组",
                "masteries": "武器熟练度",
                "skills": "角色技能",
                "character_tier_badges": "角色段位评级"
            }

            result = ["资源差异统计："]
            total_ok = 0
            total_changed = 0
            total_missing = 0

            for cat_key, cat_name in category_names.items():
                stats = diff_info["category_stats"][cat_key]
                ok = stats["ok"]
                changed = stats["changed"]
                missing = stats["missing"]
                total = ok + changed + missing

                total_ok += ok
                total_changed += changed
                total_missing += missing

                if total > 0:
                    result.append(f"{cat_name}：{ok} 正常 | {changed} 变更 | {missing} 丢失 | {total} 总计")

            result.append(f"\n总计：{total_ok} 正常 | {total_changed} 变更 | {total_missing} 丢失 | {total_ok + total_changed + total_missing} 总计")

            yield event.plain_result("\n".join(result))

            if total_changed > 0 or total_missing > 0:
                yield event.plain_result("开始更新资源...")

                upd = await asyncio.to_thread(get_image.update_diff_resources, diff_info, True)

                await asyncio.to_thread(self.assets.reload)

                done_lines = ["资源更新完成！"]
                if upd.get("download") or upd.get("adopt") or upd.get("fail"):
                    done_lines.append(
                        f"变更图片处理：实际重下 {upd.get('download', 0)} 张 | "
                        f"内容未变仅更新URL {upd.get('adopt', 0)} 张 | 失败 {upd.get('fail', 0)} 张"
                    )
                if diff_info["image_files"]["missing"]:
                    done_lines.append(f"待补缺失图片 {len(diff_info['image_files']['missing'])} 张（详见后台日志）")
                yield event.plain_result("\n".join(done_lines))
            else:
                yield event.plain_result("所有资源都是最新的，无需更新。")

        except Exception as e:
            logger.warning(f"[ER战绩] 更新资源时发生错误: {e}")
            yield event.plain_result("资源更新失败喵，请稍后再试一下喵~")
        finally:
            self._resource_updating = False

    @filter.regex(r"^查询角色\s+.+$")
    @_resource_busy_guard
    async def er_character_stats(
        self,
        event: AstrMessageEvent,
    ):
        """
        用法:
        查询角色 鼻荆
        查询角色 暗器 扎希尔
        查询角色 鼻荆 in1000
        查询角色 暗器 扎希尔 in1000
        """

        message_str = event.message_str.strip()
        raw_parts = message_str.split()
        raw_parts = [p for p in raw_parts if p]

        if len(raw_parts) < 2:
            yield event.plain_result(
                "请输入角色名，例如：\n"
                "查询角色 鼻荆\n"
                "查询角色 暗器 扎希尔\n"
                "查询角色 鼻荆 in1000"
            )
            return

        raw_parts = raw_parts[1:]

        err = self._ensure_resources()
        if err:
            yield event.plain_result(err)
            return

        character_name = ""
        weapon_name = None
        tier = "diamond_plus"

        tier_aliases = {
            "in1000": "in1000",
            "in100": "in1000",
            "星陨": "meteorite_plus",
            "星陨+": "meteorite_plus",
            "灭钻": "diamond_plus",
            "灭钻+": "diamond_plus",
            "无瑕": "mithril_plus",
            "无瑕+": "mithril_plus",
            "修罗": "asura_plus",
            "修罗+": "asura_plus",
        }

        # 段位名的拼音键，用于识别同音错别字（如 无暇 → 无瑕），避免被误当成角色名报"未找到角色"
        tier_pinyin_keys: dict[str, str] = {}
        for _tier_name, _tier_value in tier_aliases.items():
            _pkey = "".join(self.assets._to_pinyin_key(_tier_name))
            if _pkey:
                tier_pinyin_keys.setdefault(_pkey, _tier_value)

        parts = list(raw_parts)
        tier_parts = []
        for p in parts:
            lower = p.lower()
            if lower in tier_aliases or p in tier_aliases:
                tier_parts.append(p)
            elif lower.startswith("in"):
                tier_parts.append(p)
            else:
                # 段位同音错别字（如 无暇→无瑕）；若该词同时是角色名则按角色处理，不当作段位
                _pkey = "".join(self.assets._to_pinyin_key(p))
                if _pkey and _pkey in tier_pinyin_keys and not self.assets.find_character_by_name(p):
                    tier_parts.append(p)

        for tp in tier_parts:
            base = tp.lower()
            if base in tier_aliases:
                tier = tier_aliases[base]
            elif base.startswith("in"):
                tier = "in1000"
            else:
                tier = tier_pinyin_keys.get("".join(self.assets._to_pinyin_key(tp)), tier)
            parts.remove(tp)

        if len(parts) >= 2:
            weapon_name = parts[0]
            character_name = parts[1]
        elif len(parts) == 1:
            character_name = parts[0]
        else:
            yield event.plain_result("请输入角色名，例如：/查询角色 鼻荆")
            return

        if not character_name:
            yield event.plain_result("请输入角色名，例如：/查询角色 鼻荆")
            return

        character_name = await self._resolve_character_name(character_name)
        if weapon_name:
            weapon_name = await self._resolve_weapon_name(weapon_name)

        tier_display_map = {
            "diamond_plus": "灭钻+",
            "meteorite_plus": "星陨+",
            "in1000": "in 1000",
            "mithril_plus": "无瑕+",
            "asura_plus": "修罗+",
        }
        tier_display = tier_display_map.get(tier, tier)
        query_hint = (
            f"正在查询 {weapon_name} {character_name} 的 {tier_display} 段位数据喵"
            if weapon_name
            else f"正在查询 {character_name} 的 {tier_display} 段位数据喵"
        )

        # 角色缓存 key
        char_cache_raw = f"char|{character_name.lower()}|{weapon_name or ''}|{tier}"
        char_cache_key = hashlib.md5(char_cache_raw.encode("utf-8")).hexdigest()
        cached_char_img = _RESULT_IMAGE_CACHE.get(char_cache_key)
        if cached_char_img and Path(cached_char_img).exists():
            yield event.plain_result(query_hint)
            yield event.image_result(cached_char_img)
            return

        try:
            char_matches = self.assets.find_character_by_name(character_name)
            if not char_matches:
                yield event.plain_result(f"未找到角色「{character_name}」，请检查角色名是否正确。")
                return

            char_display_name = char_matches[0].get("name", character_name)

            # 提示中使用拼音匹配到的真实角色名（而非用户输入）
            if char_display_name != character_name:
                query_hint = (
                    f"正在查询 {weapon_name} {char_display_name} 的 {tier_display} 段位数据喵"
                    if weapon_name
                    else f"正在查询 {char_display_name} 的 {tier_display} 段位数据喵"
                )

            weapon_display = ""
            mastery_id = None
            if weapon_name:
                mastery_matches = self.assets.find_mastery_by_name(weapon_name)
                if not mastery_matches:
                    yield event.plain_result(f"未找到武器「{weapon_name}」，请检查武器名是否正确。")
                    return
                weapon_display = mastery_matches[0].get("name", weapon_name)
                mastery_id = mastery_matches[0].get("id")

            yield event.plain_result(query_hint)

            # 把同步的API请求+渲染放到线程池
            def _sync_char_work() -> str | None:
                cs_cache_key = f"char_stats|{tier}"
                stats_data = _LEADERBOARD_CACHE.get(cs_cache_key)
                if stats_data is None:
                    stats_data = self.api.get_character_stats(tier=tier)
                    _LEADERBOARD_CACHE.set(cs_cache_key, stats_data, CACHE.leaderboard_cache_ttl)

                recommendations = None
                if char_matches:
                    try:
                        char_key = char_matches[0].get("key", "")
                        if char_key:
                            rec_cache_key = f"char_rec|{char_key}|{tier}|{mastery_id or ''}"
                            cached_rec = _LEADERBOARD_CACHE.get(rec_cache_key)
                            if cached_rec is not None:
                                recommendations = cached_rec
                                logger.info(f"[ER角色] 从缓存获取推荐数据: {char_key}, weapon={mastery_id}")
                            else:
                                recommendations = self.api.get_character_recommendations(
                                    char_key,
                                    tier=tier,
                                    weapon=mastery_id,
                                )
                                rec_keys = list(recommendations.keys()) if recommendations else []
                                logger.info(f"[ER角色] 获取推荐数据: char={char_key}, 字段={rec_keys}")
                                if recommendations:
                                    _LEADERBOARD_CACHE.set(rec_cache_key, recommendations, CACHE.leaderboard_cache_ttl)
                    except Exception as e:
                        logger.warning(f"[ER角色] 获取推荐数据异常: {e}")
                        recommendations = None

                render_data = self.mapper.build_character_stats_render_data(
                    stats_data=stats_data,
                    character_name=character_name,
                    weapon_name=weapon_name,
                    tier=tier,
                    assets=self.assets,
                    recommendations=recommendations,
                )

                if not render_data:
                    return "__NO_DATA__"

                return self.renderer.render_character_stats(render_data)

            result = await asyncio.to_thread(_sync_char_work)

            if result == "__NO_DATA__":
                if weapon_name:
                    yield event.plain_result(f"未找到角色「{char_display_name}」使用武器「{weapon_display}」的数据。")
                else:
                    yield event.plain_result(f"未找到角色「{char_display_name}」的数据。")
                return

            _RESULT_IMAGE_CACHE.set(char_cache_key, result, CACHE.result_image_ttl)
            yield event.image_result(result)

        except Exception as e:
            error_msg = str(e)
            # 技术细节记录后台日志，用户只看到友好明文
            logger.warning(f"[ER战绩] 角色查询失败: {error_msg}")
            yield event.plain_result(self._friendly_query_error(error_msg))

    @filter.regex(r"^角色评级(?:\s+.+)?$")
    @_resource_busy_guard
    async def er_character_ranking(self, event: AstrMessageEvent):
        """
        用法:
        角色评级
        角色评级 灭钻
        角色评级 星陨
        """
        message_str = event.message_str.strip()
        parts = [p for p in message_str.split() if p]

        tier_aliases = {
            "灭钻": "diamond_plus",
            "灭钻+": "diamond_plus",
            "星陨": "meteorite_plus",
            "星陨+": "meteorite_plus",
            "无瑕": "mithril_plus",
            "无瑕+": "mithril_plus",
            "修罗": "asura_plus",
            "修罗+": "asura_plus",
            "永恒": "in1000",
            "in1000": "in1000",
            "in100": "in1000",
            "金魄": "gold",
            "银烙": "silver",
            "铜魂": "bronze",
            "铁阎": "iron",
            "全段位": "all",
        }

        tier = "diamond_plus"
        if len(parts) >= 2:
            arg = parts[1]
            lower = arg.lower()
            if lower in tier_aliases:
                tier = tier_aliases[lower]
            elif lower.startswith("in"):
                tier = "in1000"
            elif re.fullmatch(r"[a-z0-9_]+", lower):
                # 英文段位值直接透传，兼容未来新增段位
                tier = lower
            else:
                yield event.plain_result(
                    "未知段位喵，可选：灭钻/星陨/无瑕/修罗/永恒(in1000)/金魄/银烙/铜魂/铁阎/全段位"
                )
                return

        err = self._ensure_resources()
        if err:
            yield event.plain_result(err)
            return

        tier_display_map = {
            "diamond_plus": "灭钻+",
            "meteorite_plus": "星陨+",
            "mithril_plus": "无瑕+",
            "asura_plus": "修罗+",
            "in1000": "in 1000",
            "gold": "金魄",
            "silver": "银烙",
            "bronze": "铜魂",
            "iron": "铁阎",
            "all": "全段位",
        }
        tier_display = tier_display_map.get(tier, tier)

        cache_raw = f"char_rank|{tier}"
        cache_key = hashlib.md5(cache_raw.encode("utf-8")).hexdigest()
        cached_img = _RESULT_IMAGE_CACHE.get(cache_key)
        if cached_img and Path(cached_img).exists():
            yield event.plain_result(f"正在查询 {tier_display} 段位的角色评级排行喵")
            yield event.image_result(cached_img)
            return

        yield event.plain_result(f"正在查询 {tier_display} 段位的角色评级排行喵")

        def _sync_rank_work() -> str | None:
            cs_cache_key = f"char_stats|{tier}"
            stats_data = _LEADERBOARD_CACHE.get(cs_cache_key)
            if stats_data is None:
                stats_data = self.api.get_character_stats(tier=tier)
                _LEADERBOARD_CACHE.set(cs_cache_key, stats_data, CACHE.leaderboard_cache_ttl)

            render_data = self.mapper.build_character_ranking_data(
                stats_data=stats_data,
                assets=self.assets,
                tier=tier,
            )
            if not render_data.get("ranking"):
                return "__NO_DATA__"
            return self.renderer.render_character_ranking(render_data)

        try:
            result = await asyncio.to_thread(_sync_rank_work)
        except Exception as e:
            error_msg = str(e)
            logger.warning(f"[ER战绩] 角色评级查询失败: {error_msg}")
            yield event.plain_result(self._friendly_query_error(error_msg))
            return

        if result == "__NO_DATA__":
            yield event.plain_result(
                f"呜…没有找到 {tier_display} 段位的角色评级数据喵，请稍后再试喵~"
            )
            return

        _RESULT_IMAGE_CACHE.set(cache_key, result, CACHE.character_ranking_ttl)
        yield event.image_result(result)

    # =========================
    # LLM Tool：查询数据供模型分析
    # =========================

    async def _send_llm_image(self, event: AstrMessageEvent, image_path: Any) -> None:
        """Send a rendered card directly; failures must not block the text result."""
        if not image_path:
            return
        try:
            path = Path(str(image_path))
            if not path.exists():
                return
            await event.send(MessageChain().file_image(str(path)))
        except Exception as e:
            logger.warning(f"[ER LLM] 发送图片失败: {e}")

    def _format_profile_llm_text(
        self,
        data: dict[str, Any],
        season_display: str,
        mode_label: str,
    ) -> str:
        player = data.get("player", {}) or {}
        summary = data.get("summary", {}) or {}
        lines = [
            f"【永恒轮回玩家战绩】{player.get('name', '')} · {season_display} · {mode_label}",
            f"等级 {player.get('level', 0)} | MMR {player.get('mmr', 0)} | 段位 {player.get('tier_name', '未鉴定')}",
            (
                f"场次 {summary.get('play', 0)} | 胜率 {summary.get('win_rate', 0)}% | "
                f"TOP2 {summary.get('top2_rate', 0)}% | TOP3 {summary.get('top3_rate', 0)}% | "
                f"平均排名 {summary.get('avg_rank', 0)}"
            ),
            (
                f"平均击杀 {summary.get('avg_kill', 0)} | 平均助攻 {summary.get('avg_assist', 0)} | "
                f"平均 TK {summary.get('avg_tk', 0)} | 平均伤害 {summary.get('avg_damage', 0)}"
            ),
            (
                f"平均狩猎 {summary.get('avg_monster_kill', 0)} | "
                f"平均荣誉值 {summary.get('avg_vf_credit', 0)} | "
                f"平均视野贡献 {summary.get('avg_view_contribution', 0)}"
            ),
        ]

        heroes = data.get("common_heroes") or []
        if heroes:
            hero_text = []
            for hero in heroes[:5]:
                hero_text.append(
                    f"{hero.get('character_name', '')} "
                    f"{hero.get('play', 0)}场/{hero.get('win_rate', 0)}%"
                )
            lines.append("常用英雄：" + "；".join(hero_text))

        matches = data.get("matches") or []
        if matches:
            lines.append("最近对局：")
            for match in matches[:5]:
                rank = match.get("rank", 0)
                rank_text = "胜利" if rank == 1 else f"#{rank}"
                if match.get("mode_label") in ("钴协议", "钴联盟") and rank in (1, 2):
                    rank_text = "胜利" if rank == 1 else "失败"
                mmr_change = int(match.get("mmr_change", 0) or 0)
                rp_text = (
                    f"RP {match.get('mmr_before', 0)}+{mmr_change}" if mmr_change >= 0
                    else f"RP {match.get('mmr_before', 0)}{mmr_change}"
                )
                lines.append(
                    f"- {match.get('mode_label', '')} {rank_text} | "
                    f"{match.get('character_name', '')} | "
                    f"TK/K/A {match.get('team_kill', 0)}/{match.get('kills', 0)}/{match.get('assists', 0)} | "
                    f"伤害 {match.get('damage', 0)} | KDA {match.get('kda', 0)} | "
                    f"{rp_text if match.get('is_rank_match') else '路径ID ' + str(match.get('path_id', 0))}"
                )
        lines.append("请只根据以上数据做简短分析，不要编造未提供的数据。")
        return "\n".join(lines)

    def _format_character_llm_text(self, data: dict[str, Any]) -> str:
        lines = [
            f"【永恒轮回角色数据】{data.get('character_name', '')} · "
            f"{data.get('weapon_name', '')} · {data.get('tier', '')}",
            (
                f"样本 {data.get('count', 0)} 场 | 胜率 {data.get('win_rate', 0)}% | "
                f"TOP3 {data.get('top3_rate', 0)}% | 平均排名 {data.get('avg_rank', 0)}"
            ),
            (
                f"平均击杀 {data.get('avg_kill', 0)} | 平均 RP {data.get('avg_rp', 0)} | "
                f"选择率 {data.get('pick_rate', 0)}%"
            ),
            f"平均伤害 {data.get('avg_damage', 0)} | 平均 TK {data.get('avg_tk', 0)}",
        ]
        builds = data.get("skill_builds") or []
        if builds:
            lines.append("推荐技能：" + "；".join(
                f"{b.get('key', '')} 选择率{b.get('selection_rate', 0)}% 胜率{b.get('win_rate', 0)}%"
                for b in builds[:3]
            ))
        tactical = data.get("tactical_skills") or []
        if tactical:
            lines.append("推荐战术：" + "；".join(
                f"{t.get('name', '')} {t.get('selection_rate', 0)}%"
                for t in tactical[:3]
            ))
        traits = data.get("trait_builds") or []
        if traits:
            lines.append("推荐潜能：" + "；".join(
                f"{t.get('core', {}).get('name', '')} {t.get('selection_rate', 0)}%"
                for t in traits[:3]
            ))
        lines.append("请只根据以上数据做简短分析，不要编造未提供的数据。")
        return "\n".join(lines)

    def _format_ranking_llm_text(self, data: dict[str, Any], tier_display: str) -> str:
        ranking = data.get("ranking") or []
        lines = [
            f"【永恒轮回角色评级】{tier_display} · 共 {len(ranking)} 项 · "
            f"样本 {data.get('tier_count', 0)} 场",
            "按 tierScore 排序，仅列前 15 项：",
        ]
        for index, item in enumerate(ranking[:15], start=1):
            lines.append(
                f"{index}. {item.get('character_name', '')} "
                f"[{item.get('tier_badge', '')}] "
                f"tierScore {item.get('tier_score', 0)} | "
                f"{item.get('count', 0)}场 | "
                f"伤害 {item.get('avg_damage', 0)} | "
                f"RP {item.get('avg_mmr_gain', 0)}"
            )
        lines.append("请只根据以上数据做简短分析，不要编造未提供的数据。")
        return "\n".join(lines)

    @filter.llm_tool(name="er_player_report")
    async def llm_player_report(
        self,
        event: AstrMessageEvent,
        player: str,
        season: str = "",
        mode: str = "",
        match_count: int = 10,
    ):
        """查询永恒轮回玩家战绩并返回文本数据，供模型分析。

        Args:
            player(string): 玩家昵称
            season(string): 可选赛季，例如 S10、S11；留空使用当前赛季
            mode(string): 可选模式：排位、匹配、钴协议、联盟、孤狼、钴联盟；留空为所有模式
            match_count(number): 最近对局数量，10-20，默认 10
        """
        try:
            season_key = season.strip().upper() or self.default_season
            resolved_season = self.api.SEASON_ALIASES.get(season_key, season_key)
            season_display = self._format_season_display(season_key)
            mode_id, mode_api, mode_label = DEFAULT_MODE_ID, DEFAULT_MODE_API, DEFAULT_MODE_LABEL
            query_mode_label = "所有模式"
            mode_key = mode.strip().upper()
            if mode_key:
                if mode_key in MODE_KEYWORDS:
                    mode_id, mode_api, mode_label = MODE_KEYWORDS[mode_key]
                    query_mode_label = mode_label
                else:
                    english_modes = {
                        "RANK": "排位", "NORMAL": "匹配", "COBALT": "钴协议",
                        "SQUAD_RUMBLE": "联盟", "LONE_WOLF": "孤狼",
                        "SQUAD_RUMBLE_COBALT": "钴联盟",
                    }
                    if mode_key in english_modes:
                        mode_id, mode_api, mode_label = MODE_KEYWORDS[english_modes[mode_key]]
                        query_mode_label = mode_label
            count = max(10, min(20, int(match_count or 10)))
            official_uid = None
            if self.official_api is not None and resolved_season == self.default_season:
                official_uid = await self._resolve_official_uid(player)
            result = await asyncio.to_thread(
                self._do_query_profile_sync,
                player,
                resolved_season,
                count,
                mode_id,
                mode_api,
                mode_label,
                official_uid,
                True,
            )
            if not isinstance(result, tuple):
                return f"查询失败：{result}"
            image_path, data = result
            await self._send_llm_image(event, image_path)
            return self._format_profile_llm_text(data, season_display, query_mode_label)
        except Exception as e:
            logger.warning(f"[ER LLM] 玩家查询失败: {e}")
            return f"查询失败：{self._friendly_query_error(str(e))}"

    @filter.llm_tool(name="er_character_report")
    async def llm_character_report(
        self,
        event: AstrMessageEvent,
        character: str,
        weapon: str = "",
        tier: str = "diamond_plus",
    ):
        """查询永恒轮回角色数据并返回文本，供模型分析。

        Args:
            character(string): 角色名或备注
            weapon(string): 可选武器名；留空使用角色最常用武器
            tier(string): 段位筛选，默认 diamond_plus
        """
        try:
            tier_map = {
                "灭钻": "diamond_plus", "灭钻+": "diamond_plus",
                "星陨": "meteorite_plus", "星陨+": "meteorite_plus",
                "无瑕": "mithril_plus", "无瑕+": "mithril_plus",
                "修罗": "asura_plus", "修罗+": "asura_plus",
                "in1000": "in1000", "前1000": "in1000",
            }
            tier_key = tier_map.get(tier.strip(), tier.strip() or "diamond_plus")
            char_matches = self.assets.find_character_by_name(character)
            if not char_matches:
                return f"未找到角色「{character}」。"
            char_name = char_matches[0].get("name", character)
            mastery_id = None
            if weapon.strip():
                mastery_matches = self.assets.find_mastery_by_name(weapon)
                if not mastery_matches:
                    return f"未找到武器「{weapon}」。"
                mastery_id = mastery_matches[0].get("id")

            def _sync() -> Any:
                stats_data = _LEADERBOARD_CACHE.get(f"char_stats|{tier_key}")
                if stats_data is None:
                    stats_data = self.api.get_character_stats(tier=tier_key)
                    _LEADERBOARD_CACHE.set(f"char_stats|{tier_key}", stats_data, CACHE.leaderboard_cache_ttl)
                recommendations = None
                try:
                    char_key = char_matches[0].get("key", "")
                    if char_key:
                        recommendations = self.api.get_character_recommendations(
                            char_key, tier=tier_key, weapon=mastery_id,
                        )
                except Exception as e:
                    logger.warning(f"[ER LLM] 角色推荐获取失败: {e}")
                data = self.mapper.build_character_stats_render_data(
                    stats_data=stats_data,
                    character_name=character,
                    weapon_name=weapon or None,
                    tier=tier_key,
                    assets=self.assets,
                    recommendations=recommendations,
                )
                if not data:
                    return None
                path = self.renderer.render_character_stats(data)
                return path, data

            result = await asyncio.to_thread(_sync)
            if result is None:
                return f"未找到 {char_name} 在 {tier_key} 的数据。"
            image_path, data = result
            await self._send_llm_image(event, image_path)
            return self._format_character_llm_text(data)
        except Exception as e:
            logger.warning(f"[ER LLM] 角色查询失败: {e}")
            return f"查询失败：{self._friendly_query_error(str(e))}"

    @filter.llm_tool(name="er_character_ranking_report")
    async def llm_character_ranking_report(
        self,
        event: AstrMessageEvent,
        tier: str = "diamond_plus",
    ):
        """查询永恒轮回角色评级排行并返回文本，供模型分析。

        Args:
            tier(string): 段位筛选，默认 diamond_plus
        """
        try:
            tier_map = {
                "灭钻": "diamond_plus", "灭钻+": "diamond_plus",
                "星陨": "meteorite_plus", "星陨+": "meteorite_plus",
                "无瑕": "mithril_plus", "无瑕+": "mithril_plus",
                "修罗": "asura_plus", "修罗+": "asura_plus",
                "in1000": "in1000", "前1000": "in1000",
            }
            tier_key = tier_map.get(tier.strip(), tier.strip() or "diamond_plus")
            tier_display_map = {
                "diamond_plus": "灭钻+", "meteorite_plus": "星陨+",
                "mithril_plus": "无瑕+", "asura_plus": "修罗+", "in1000": "in 1000",
            }
            tier_display = tier_display_map.get(tier_key, tier_key)

            def _sync() -> Any:
                stats_data = _LEADERBOARD_CACHE.get(f"char_stats|{tier_key}")
                if stats_data is None:
                    stats_data = self.api.get_character_stats(tier=tier_key)
                    _LEADERBOARD_CACHE.set(f"char_stats|{tier_key}", stats_data, CACHE.leaderboard_cache_ttl)
                data = self.mapper.build_character_ranking_data(
                    stats_data=stats_data, assets=self.assets, tier=tier_key,
                )
                if not data.get("ranking"):
                    return None
                return self.renderer.render_character_ranking(data), data

            result = await asyncio.to_thread(_sync)
            if result is None:
                return f"没有找到 {tier_display} 的角色评级数据。"
            image_path, data = result
            await self._send_llm_image(event, image_path)
            return self._format_ranking_llm_text(data, tier_display)
        except Exception as e:
            logger.warning(f"[ER LLM] 角色评级查询失败: {e}")
            return f"查询失败：{self._friendly_query_error(str(e))}"

    def _resolve_llm_mode(self, mode: str) -> tuple[int, str, str, str]:
        mode_id, mode_api, mode_label = DEFAULT_MODE_ID, DEFAULT_MODE_API, DEFAULT_MODE_LABEL
        query_label = "所有模式"
        key = mode.strip().upper()
        if key:
            if key in MODE_KEYWORDS:
                mode_id, mode_api, mode_label = MODE_KEYWORDS[key]
                query_label = mode_label
            else:
                english = {
                    "RANK": "排位", "NORMAL": "匹配", "COBALT": "钴协议",
                    "SQUAD_RUMBLE": "联盟", "LONE_WOLF": "孤狼",
                    "SQUAD_RUMBLE_COBALT": "钴联盟",
                }
                if key in english:
                    mode_id, mode_api, mode_label = MODE_KEYWORDS[english[key]]
                    query_label = mode_label
        return mode_id, mode_api, mode_label, query_label

    def _compact_profile_text(
        self,
        data: dict[str, Any],
        label: str,
        season_display: str,
        mode_label: str,
    ) -> str:
        p = data.get("player", {}) or {}
        s = data.get("summary", {}) or {}
        lines = [
            f"{label}：{p.get('name', '')} · {season_display} · {mode_label}",
            f"MMR {p.get('mmr', 0)} | 段位 {p.get('tier_name', '未鉴定')} | 场次 {s.get('play', 0)} | 胜率 {s.get('win_rate', 0)}%",
            f"TOP2 {s.get('top2_rate', 0)}% | TOP3 {s.get('top3_rate', 0)}% | 平均排名 {s.get('avg_rank', 0)}",
            f"平均击杀 {s.get('avg_kill', 0)} | 助攻 {s.get('avg_assist', 0)} | TK {s.get('avg_tk', 0)} | 伤害 {s.get('avg_damage', 0)}",
        ]
        heroes = data.get("common_heroes") or []
        if heroes:
            lines.append("常用英雄：" + "；".join(
                f"{h.get('character_name', '')} {h.get('play', 0)}场/{h.get('win_rate', 0)}%"
                for h in heroes[:5]
            ))
        return "\n".join(lines)

    @filter.llm_tool(name="er_leaderboard_report")
    async def llm_leaderboard_report(
        self,
        event: AstrMessageEvent,
        season: str = "",
    ):
        """查询永恒轮回排行榜与永恒/半神分数线。

        Args:
            season(string): 可选赛季，例如 S10；留空使用当前赛季
        """
        try:
            season_key = season.strip().upper() or self.default_season
            resolved = self.api.SEASON_ALIASES.get(season_key, season_key)
            season_display = self._format_season_display(season_key)
            cache_key = f"{resolved}|seoul|SQUAD"
            data = _LEADERBOARD_CACHE.get(cache_key)
            if data is None:
                data = await asyncio.to_thread(
                    self.api.get_leaderboard,
                    page=1,
                    season_key=resolved,
                    server_name="seoul",
                    team_mode="SQUAD",
                )
                _LEADERBOARD_CACHE.set(cache_key, data, CACHE.leaderboard_cache_ttl)
            mmr = self.mapper._get_cutoff_mmr_values(data)
            top = self.mapper._get_cutoff_player_rank(data, limit=10)
            lines = [
                f"【永恒轮回排行榜】{season_display} · SQUAD",
                f"永恒分数线 {mmr.get('Immortal_mmr', 0):,} | 半神分数线 {mmr.get('Titan_mmr', 0):,}",
            ]
            for player in top:
                lines.append(
                    f"#{player.get('rank', 0)} {player.get('player_name', '')} | "
                    f"MMR {player.get('mmr', 0)} | 胜率 {player.get('win_rate', 0):.1f}% | "
                    f"{player.get('games', 0)}场 | 平均排名 {player.get('avg_rank', 0):.1f}"
                )
            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"[ER LLM] 排行榜查询失败: {e}")
            return f"查询失败：{self._friendly_query_error(str(e))}"

    @filter.llm_tool(name="er_patch_notes")
    async def llm_patch_notes(
        self,
        event: AstrMessageEvent,
        page: int = 1,
        size: int = 5,
    ):
        """查询永恒轮回最近的版本更新/热修复公告。

        Args:
            page(number): 页码，默认 1
            size(number): 数量，默认 5
        """
        try:
            data = await asyncio.to_thread(
                self.api.get_patch_notes,
                page=max(1, int(page or 1)),
                size=max(1, min(10, int(size or 5))),
            )
            notes = data.get("patchNotes") or []
            if not notes:
                return "没有找到版本公告。"
            lines = [f"【永恒轮回版本公告】共 {data.get('count', 0)} 条"]
            for note in notes:
                lines.append(
                    f"- {note.get('patchDate', '')} | {note.get('patchType', '')} | "
                    f"{note.get('title', '')} | {note.get('href', '')}"
                )
            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"[ER LLM] 版本公告查询失败: {e}")
            return f"查询失败：{self._friendly_query_error(str(e))}"

    @filter.llm_tool(name="er_esports_recent")
    async def llm_esports_recent(
        self,
        event: AstrMessageEvent,
        code: str = "",
        page: int = 1,
        size: int = 10,
    ):
        """查询近期永恒轮回赛事对局。

        Args:
            code(string): 可选赛事/阶段代码，例如 0112 表示 ER Masters S12
            page(number): 页码，默认 1
            size(number): 数量，默认 10
        """
        try:
            data = await asyncio.to_thread(
                self.api.get_tournament_recent_games,
                page=max(1, int(page or 1)),
                size=max(1, min(20, int(size or 10))),
                code=code.strip() or None,
            )
            infos = data.get("gameInfos") or []
            if not infos:
                return "没有找到赛事对局。"
            lines = [f"【永恒轮回赛事对局】共 {data.get('count', 0)} 场"]
            for info in infos:
                created = datetime.fromtimestamp(int(info.get("createdAt", 0)) / 1000)
                chars = []
                for game in (info.get("games") or [])[:6]:
                    name = self.assets.get_character_name(int(game.get("characterNum", 0) or 0))
                    if name:
                        chars.append(name)
                lines.append(
                    f"- {created.strftime('%Y-%m-%d %H:%M')} | code {info.get('code', '')} | "
                    f"gameId {info.get('gameId', '')} | 选手角色：{', '.join(chars)}"
                )
            lines.append("如需某场详细数据，可调用 er_esports_match 并传入 gameId。")
            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"[ER LLM] 赛事列表查询失败: {e}")
            return f"查询失败：{self._friendly_query_error(str(e))}"

    @filter.llm_tool(name="er_esports_match")
    async def llm_esports_match(
        self,
        event: AstrMessageEvent,
        game_id: int,
    ):
        """查询一场赛事的队伍得分、选手阵容和技能加点。

        Args:
            game_id(number): 赛事对局 gameId
        """
        try:
            data = await asyncio.to_thread(self.api.get_tournament_game_detail, int(game_id))
            scores = data.get("scoreInfos") or []
            games = data.get("games") or []
            lines = [f"【赛事对局 {game_id}】"]
            for score in sorted(scores, key=lambda x: x.get("rankScore", 0), reverse=True)[:8]:
                lines.append(
                    f"队伍 {score.get('teamNumber', '')} | 排名分 {score.get('rankScore', 0)} | "
                    f"击杀分 {score.get('killScore', 0)}"
                )
            lines.append("选手数据（按伤害前 12）：")
            for game in sorted(games, key=lambda x: x.get("damageToPlayer", 0), reverse=True)[:12]:
                lines.append(
                    f"- {game.get('nickname', '')} | "
                    f"{self.assets.get_character_name(int(game.get('characterNum', 0) or 0))} | "
                    f"排名 #{game.get('gameRank', 0)} | "
                    f"K/A {game.get('playerKill', 0)}/{game.get('playerAssistant', 0)} | "
                    f"伤害 {game.get('damageToPlayer', 0)}"
                )
            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"[ER LLM] 赛事对局查询失败: {e}")
            return f"查询失败：{self._friendly_query_error(str(e))}"

    @filter.llm_tool(name="er_compare_players")
    async def llm_compare_players(
        self,
        event: AstrMessageEvent,
        player_a: str,
        player_b: str,
        season: str = "",
        mode: str = "",
    ):
        """对比两个永恒轮回玩家的战绩数据。

        Args:
            player_a(string): 第一个玩家昵称
            player_b(string): 第二个玩家昵称
            season(string): 可选赛季，例如 S10；留空使用当前赛季
            mode(string): 可选模式：排位、匹配、钴协议等；留空为所有模式
        """
        try:
            season_key = season.strip().upper() or self.default_season
            resolved = self.api.SEASON_ALIASES.get(season_key, season_key)
            season_display = self._format_season_display(season_key)
            mode_id, mode_api, mode_label, query_label = self._resolve_llm_mode(mode)
            results = []
            for name in (player_a, player_b):
                result = await asyncio.to_thread(
                    self._do_query_profile_sync,
                    name,
                    resolved,
                    10,
                    mode_id,
                    mode_api,
                    mode_label,
                    None,
                    True,
                    False,
                )
                results.append(result)
            lines = [f"【玩家对比】{season_display} · {query_label}"]
            for index, result in enumerate(results):
                if not isinstance(result, tuple):
                    return f"查询 {player_a if index == 0 else player_b} 失败。"
                _, data = result
                label = "玩家A" if index == 0 else "玩家B"
                lines.append(self._compact_profile_text(data, label, season_display, query_label))
                lines.append("")
            lines.append("请根据以上数据比较两人的段位、稳定性、输出和近期状态，不要编造。")
            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"[ER LLM] 玩家对比失败: {e}")
            return f"查询失败：{self._friendly_query_error(str(e))}"

    @filter.llm_tool(name="er_match_report")
    async def llm_match_report(
        self,
        event: AstrMessageEvent,
        player: str,
        game_id: int,
        season: str = "",
    ):
        """查询某一局的全员详细战报。

        Args:
            player(string): 要查询的玩家昵称
            game_id(number): 对局 gameId
            season(string): 可选赛季，例如 S10；留空使用当前赛季
        """
        try:
            season_key = season.strip().upper() or self.default_season
            resolved = self.api.SEASON_ALIASES.get(season_key, season_key)
            season_id = self.mapper.SEASON_ID_MAP.get(resolved)
            if season_id is None:
                return f"无法识别赛季 {resolved}。"
            data = await asyncio.to_thread(
                self.api.get_match_detail,
                player,
                int(season_id),
                int(game_id),
            )
            entries = data.get("matches") or []
            if not entries:
                return "没有找到该局数据。"
            lines = [f"【单局战报】{player} · gameId {game_id} · {season_key}"]
            entries = sorted(entries, key=lambda e: (int(e.get("teamNumber", 0) or 0), int(e.get("gameRank", 0) or 0)))
            for entry in entries:
                lines.append(
                    f"队伍{entry.get('teamNumber', '')} #{entry.get('gameRank', '')} | "
                    f"{entry.get('nickname', '')} | "
                    f"{self.assets.get_character_name(int(entry.get('characterNum', 0) or 0))} | "
                    f"K/A/D {entry.get('playerKill', 0)}/{entry.get('playerAssistant', 0)}/{entry.get('playerDeaths', 0)} | "
                    f"伤害 {entry.get('damageToPlayer', 0)}"
                )
            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"[ER LLM] 单局战报查询失败: {e}")
            return f"查询失败：{self._friendly_query_error(str(e))}"

    @filter.llm_tool(name="er_character_leaderboard")
    async def llm_character_leaderboard(
        self,
        event: AstrMessageEvent,
        character: str,
        season: str = "",
        sort_type: str = "TIER",
    ):
        """查询某个角色的玩家排行榜。

        Args:
            character(string): 角色名或备注
            season(string): 可选赛季，例如 S10；留空使用当前赛季
            sort_type(string): 排序方式 TIER、WIN_RATE、MATCH_COUNT，默认 TIER
        """
        try:
            matches = self.assets.find_character_by_name(character)
            if not matches:
                return f"未找到角色「{character}」。"
            char = matches[0]
            season_key = season.strip().upper() or self.default_season
            resolved = self.api.SEASON_ALIASES.get(season_key, season_key)
            data = await asyncio.to_thread(
                self.api.get_character_leaderboard,
                char.get("key", ""),
                season_key=resolved,
                team_mode="SQUAD",
                sort_type=sort_type.strip().upper() or "TIER",
                page=1,
            )
            rankings = data.get("rankings") or []
            if not rankings:
                return f"没有找到 {char.get('name', character)} 的排行榜数据。"
            lines = [f"【角色排行榜】{char.get('name', character)} · {self._format_season_display(season_key)}"]
            for index, row in enumerate(rankings[:10], start=1):
                picks = int(row.get("pickCount", 0) or 0)
                wins = int(row.get("winCount", 0) or 0)
                lines.append(
                    f"{index}. {row.get('nickname', '')} | MMR {row.get('mmr', 0)} | "
                    f"{picks}场 | 胜率 {(wins / picks * 100) if picks else 0:.1f}% | "
                    f"平均排名 {row.get('avgPlacement', 0):.2f}"
                )
            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"[ER LLM] 角色排行榜查询失败: {e}")
            return f"查询失败：{self._friendly_query_error(str(e))}"
