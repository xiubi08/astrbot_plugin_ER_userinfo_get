"""QQ 官方指令面板管理（附加模块，不修改任何现有代码）。

通过官方 OpenAPI 查询 / 创建 / 修改 / 删除指令面板（见 机器人 > 自定义菜单与指令面板）：
- POST   /v2/panels                  创建面板
- GET    /v2/panels                  查询面板列表
- GET    /v2/panels/{panel_id}       查询面板详情
- PUT    /v2/panels/{panel_id}       修改面板（覆盖元素与备注，不影响已关联对象）
- DELETE /v2/panels/{panel_id}       删除面板（释放 20 个配额）
- PUT    /v2/panels/{panel_id}/target 修改生效范围（全量 / 指定用户、指定群）

域名：官方 20260810 起统一为 api.bot.qq.com，旧域名 api.sgroup.qq.com 作为回退
（仅网络层失败才换域名，业务错误码直接抛出）。

鉴权：AstrBot 创建 botpy 客户端时不传 appid/secret（client.api._token 为 None），
因此从适配器实例（client.platform.appid / client.platform.secret）取 appId + clientSecret，
调用官方接口 https://bots.qq.com/app/getAppAccessToken 换取 access_token（带本地缓存）。
上限约束：每机器人最多 20 个面板，每面板最多 20 个元素，操作接口 10 QPM，
元素 name ≤14 字符（约 7 汉字）、desc ≤30 字符（约 15 汉字）。
"""
from __future__ import annotations

import time
from typing import Any, Optional

from .platform_compat import get_qq_official_clients

from astrbot.api import logger

try:
    import aiohttp

    _HAS_AIOHTTP = True
except Exception:
    _HAS_AIOHTTP = False

# 指令面板备注标记：sync_panels 用它识别「属于本插件」的面板，避免重复创建
PANEL_REMARK = "ER战绩查询插件自动同步面板"

# 接口域名：官方 20260810 起统一为 api.bot.qq.com，旧域名保留为回退
BASE_URLS = ("https://api.bot.qq.com", "https://api.sgroup.qq.com")

# 上次调用成功的域名（优先复用，避免每次都先撞一次失败）
_ACTIVE_BASE: Optional[str] = None

TIMEOUT_SECONDS = 30


class PanelAPIError(RuntimeError):
    """指令面板 OpenAPI 调用失败。"""

    def __init__(self, code: Optional[int] = None, message: str = ""):
        self.code = code
        self.message = message
        super().__init__(f"code={code} {message}" if code else message)


def _get_token(client: Any) -> Any:
    """取 botpy HTTPClient 持有的 Token（app_id/secret/access_token）。"""
    api = getattr(client, "api", None)
    return getattr(api, "_token", None) if api is not None else None


# access_token 本地缓存：appid -> (access_token, 过期时间戳)，避免频繁换取触发限频
_ACCESS_TOKEN_CACHE: dict[str, tuple[str, float]] = {}

# 官方换取 access_token 的接口（appId + clientSecret）
ACCESS_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"


def _get_appid_secret(client: Any) -> tuple[str, str]:
    """获取 qq_official 平台的 appId / clientSecret。

    AstrBot 的 QQOfficialPlatformAdapter 以 botClient(platform) 挂在
    client.platform 上，直接持有 appid/secret；回退尝试 botpy Token。
    """
    platform = getattr(client, "platform", None)
    appid = getattr(platform, "appid", None)
    secret = getattr(platform, "secret", None)
    if appid and secret:
        return str(appid), str(secret)
    token = _get_token(client)
    if token is not None:
        t_appid = getattr(token, "app_id", None)
        t_secret = getattr(token, "secret", None)
        if t_appid and t_secret:
            return str(t_appid), str(t_secret)
    return "", ""


async def _get_access_token(appid: str, secret: str) -> str:
    """调用官方接口换取 access_token（过期前 60 秒提前刷新）。"""
    now = time.time()
    cached = _ACCESS_TOKEN_CACHE.get(appid)
    if cached and cached[1] > now:
        return cached[0]
    if not _HAS_AIOHTTP:
        raise PanelAPIError(message="环境缺少 aiohttp，无法获取 access_token")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                ACCESS_TOKEN_URL,
                json={"appId": appid, "clientSecret": secret},
                timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS),
            ) as resp:
                data = await resp.json()
    except Exception as e:
        logger.warning(f"[ER面板] 获取 access_token 失败: {e}")
        raise PanelAPIError(message=f"获取 access_token 失败: {e}") from e
    token = data.get("access_token") if isinstance(data, dict) else None
    if not token:
        raise PanelAPIError(message=f"获取 access_token 失败: {data}")
    expires_in = int(data.get("expires_in", 7200))
    _ACCESS_TOKEN_CACHE[appid] = (token, now + expires_in - 60)
    return token


async def _build_headers(client: Any) -> dict[str, str]:
    """构造 QQ 官方 OpenAPI 请求头（Authorization: QQBot {access_token}）。"""
    appid, secret = _get_appid_secret(client)
    if not (appid and secret):
        raise PanelAPIError(
            message="无法获取 qq_official 平台的 appId/clientSecret，请检查平台配置"
        )
    access_token = await _get_access_token(appid, secret)
    return {
        "Authorization": f"QQBot {access_token}",
        "X-Union-Appid": appid,
        "Content-Type": "application/json",
    }


async def _request_once(
    base: str,
    headers: dict[str, str],
    method: str,
    path: str,
    query: Optional[dict],
    body: Optional[dict],
) -> dict:
    """对单个域名发起一次请求；网络层异常转为 code=None 的 PanelAPIError。"""
    url = base + path
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method,
                url,
                params=query,
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS),
            ) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    # 非 JSON 响应（404 HTML 等）按网络层错误处理，允许换域名重试
                    raise PanelAPIError(message=f"HTTP {resp.status} {path}") from None
    except Exception as e:
        logger.warning(f"[ER面板] 请求失败 {base}{path}: {e}")
        raise PanelAPIError(message=str(e)) from e
    if isinstance(data, dict) and data.get("code") not in (None, 0):
        message = str(data.get("message", ""))
        if data.get("limit") is not None:
            message = f"{message}（limit={data.get('limit')}）"
        raise PanelAPIError(
            code=data.get("code"),
            message=message,
        )
    return data


def _base_candidates() -> tuple[str, ...]:
    """优先使用上次成功的域名，其余作为回退。"""
    if _ACTIVE_BASE and _ACTIVE_BASE in BASE_URLS:
        return (_ACTIVE_BASE,) + tuple(b for b in BASE_URLS if b != _ACTIVE_BASE)
    return BASE_URLS


def _remember_base(base: str) -> None:
    global _ACTIVE_BASE
    if _ACTIVE_BASE != base:
        _ACTIVE_BASE = base
        logger.info(f"[ER面板] 指令面板接口域名已切换为 {base}")


async def _request(
    client: Any,
    method: str,
    path: str,
    query: Optional[dict] = None,
    body: Optional[dict] = None,
) -> dict:
    """发起带 QQBot 鉴权的请求；非 code=0 响应抛 PanelAPIError。

    官方已于 20260810 把接口域名统一为 api.bot.qq.com；旧域名仍作为回退，
    只有网络层失败才换域名重试，业务错误码（服务端已响应）直接抛出。
    """
    if not _HAS_AIOHTTP:
        raise PanelAPIError(message="环境缺少 aiohttp，无法调用指令面板 API")
    headers = await _build_headers(client)
    last_error: Optional[PanelAPIError] = None
    for base in _base_candidates():
        try:
            data = await _request_once(base, headers, method, path, query, body)
        except PanelAPIError as e:
            if e.code is not None:
                # 服务端已响应（业务错误），说明域名可用，不必换域名
                _remember_base(base)
                raise
            last_error = e
            continue
        _remember_base(base)
        return data
    assert last_error is not None
    raise last_error


async def list_panels(client: Any, scope: str, limit: int = 50) -> list[dict]:
    """分页拉取指定场景下的指令面板列表（第一页）。"""
    data = await _request(client, "GET", "/v2/panels", query={"scope": scope, "limit": limit})
    return list(data.get("records") or [])


async def get_panel(client: Any, panel_id: str) -> dict:
    """查询指定指令面板详情。"""
    return await _request(client, "GET", f"/v2/panels/{panel_id}")


async def create_panel(
    client: Any,
    scope: str,
    panel: dict,
    target_type: str = "all",
    user_openids: Optional[list[str]] = None,
    group_openids: Optional[list[str]] = None,
) -> str:
    """创建指令面板，返回 panel_id。"""
    body: dict[str, Any] = {
        "scope": scope,
        "target_type": target_type,
        "panel": panel,
    }
    if user_openids:
        body["user_openids"] = user_openids
    if group_openids:
        body["group_openids"] = group_openids
    data = await _request(client, "POST", "/v2/panels", body=body)
    return str(data.get("panel_id", ""))


async def update_panel(client: Any, panel_id: str, panel: dict) -> int:
    """修改指令面板元素与备注，返回新版本号。"""
    data = await _request(client, "PUT", f"/v2/panels/{panel_id}", body={"panel": panel})
    return int(data.get("version", 0))


async def delete_panel(client: Any, panel_id: str) -> dict:
    """删除指令面板（释放 20 个面板配额）。"""
    return await _request(client, "DELETE", f"/v2/panels/{panel_id}")


async def update_panel_target(
    client: Any,
    panel_id: str,
    target_type: str = "all",
    user_openids: Optional[list[str]] = None,
    group_openids: Optional[list[str]] = None,
) -> dict:
    """修改指令面板的生效范围（全量 / 指定用户、指定群）。

    注意：target_type=all 是全局配置，不能带关联对象；specific 最多各 20 个。
    """
    body: dict[str, Any] = {"target_type": target_type}
    if target_type == "specific":
        if user_openids:
            body["user_openids"] = list(user_openids)[:20]
        if group_openids:
            body["group_openids"] = list(group_openids)[:20]
    return await _request(client, "PUT", f"/v2/panels/{panel_id}/target", body=body)


async def sync_panels(
    client: Any,
    scopes: tuple[str, ...] = ("group", "c2c"),
    items: Optional[list[dict]] = None,
    remark: str = PANEL_REMARK,
    group_openids: Optional[list[str]] = None,
) -> dict[str, tuple[str, str]]:
    """把指令面板同步到指定场景（不存在则创建，已存在则覆盖更新）。

    group_openids 非空时：group 场景的面板改为「仅这些群生效」（target_type=specific），
    已存在的面板会额外调用修改关联对象接口。

    返回 {scope: (created|updated, panel_id)}。
    """
    items = items or []
    results: dict[str, tuple[str, str]] = {}
    for scope in scopes:
        target_type = "specific" if (scope == "group" and group_openids) else "all"
        records = await list_panels(client, scope)
        mine = [r for r in records if r.get("panel", {}).get("remark") == remark]
        if mine:
            panel_id = str(mine[0]["panel_id"])
            await update_panel(client, panel_id, {"items": items, "remark": remark})
            if target_type == "specific":
                try:
                    await update_panel_target(
                        client, panel_id, target_type, group_openids=group_openids
                    )
                except PanelAPIError as e:
                    logger.warning(f"[ER面板] 修改面板 {panel_id} 生效范围失败: {e}")
            results[scope] = ("updated", panel_id)
        else:
            panel_id = await create_panel(
                client,
                scope,
                {"items": items, "remark": remark},
                target_type=target_type,
                group_openids=group_openids if target_type == "specific" else None,
            )
            results[scope] = ("created", panel_id)
    return results


async def delete_own_panels(
    client: Any,
    scopes: tuple[str, ...] = ("group", "c2c"),
    remark: str = PANEL_REMARK,
) -> list[tuple[str, str]]:
    """删除本插件同步出来的面板（按 remark 识别），返回 [(scope, panel_id)]。"""
    removed: list[tuple[str, str]] = []
    for scope in scopes:
        records = await list_panels(client, scope)
        for record in records:
            if record.get("panel", {}).get("remark") != remark:
                continue
            panel_id = str(record.get("panel_id", ""))
            if not panel_id:
                continue
            try:
                await delete_panel(client, panel_id)
                removed.append((scope, panel_id))
            except PanelAPIError as e:
                logger.warning(f"[ER面板] 删除面板 {panel_id} 失败: {e}")
    return removed


def format_panel_record(record: dict) -> str:
    """把面板记录格式化为一行可读文本（供列表命令展示）。"""
    panel = record.get("panel", {}) or {}
    return (
        f"panel_id={record.get('panel_id', '')} "
        f"scope={record.get('scope', '')} "
        f"target={record.get('target_type', '')} "
        f"items={len(panel.get('items') or [])} "
        f"updated_at={record.get('updated_at', '')}"
    )


__all__ = [
    "BASE_URLS",
    "PANEL_REMARK",
    "PanelAPIError",
    "create_panel",
    "delete_own_panels",
    "delete_panel",
    "format_panel_record",
    "get_panel",
    "get_qq_official_clients",
    "list_panels",
    "sync_panels",
    "update_panel",
    "update_panel_target",
]
