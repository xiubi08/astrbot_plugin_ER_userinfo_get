"""平台兼容工具（附加模块，不修改任何现有代码）。

- 探测消息事件所属平台（qq_official / aiocqhttp 等）
- 遍历 AstrBot 全部平台实例（context.platform_manager），通过 get_client()
  获取 QQ 官方机器人（botpy）底层客户端，供按钮消息发送使用。
"""
from __future__ import annotations

from typing import Any

try:
    from astrbot.api import logger
except Exception:  # 非 AstrBot 环境下退化为标准 logging
    import logging
    logger = logging.getLogger(__name__)

# QQ 官方机器人适配器（botpy 底层）的平台名
QQ_OFFICIAL_NAMES = frozenset({"qq_official", "qq_official_webhook"})
AIOCQHTTP_NAMES = frozenset({"aiocqhttp"})


def get_platform_name(event: Any) -> str:
    """获取消息事件所属平台名（如 qq_official / aiocqhttp）。"""
    try:
        meta = getattr(event, "platform_meta", None) or getattr(event, "platform", None)
        name = getattr(meta, "name", None)
        if name:
            return str(name)
    except Exception:
        pass
    try:
        return str(event.get_platform_name())
    except Exception:
        return ""


def is_qq_official(event: Any) -> bool:
    """事件是否来自 QQ 官方机器人平台。"""
    return get_platform_name(event) in QQ_OFFICIAL_NAMES


def is_aiocqhttp(event: Any) -> bool:
    """事件是否来自 OneBot v11（aiocqhttp）平台。"""
    return get_platform_name(event) in AIOCQHTTP_NAMES


def _inst_meta(inst: Any) -> Any:
    try:
        return inst.meta()
    except Exception:
        return None


def _inst_name(inst: Any) -> str:
    meta = _inst_meta(inst)
    try:
        return str(getattr(meta, "name", "") or "")
    except Exception:
        return ""


def _inst_id(inst: Any) -> str:
    meta = _inst_meta(inst)
    try:
        return str(getattr(meta, "id", "") or "")
    except Exception:
        return ""


def _get_insts(context: Any) -> list[Any]:
    """获取 AstrBot 全部平台实例列表（兼容 platform_insts 属性 / get_insts() 方法）。"""
    try:
        pm = getattr(context, "platform_manager", None)
        if pm is None:
            return []
        insts = getattr(pm, "platform_insts", None)
        if insts is None:
            insts = getattr(pm, "get_insts", None)
            if callable(insts):
                insts = insts()
        return list(insts or [])
    except Exception as e:
        logger.debug(f"[ER平台] 获取平台实例列表失败: {e}")
        return []


def _inst_client(inst: Any) -> Any:
    """获取平台实例的底层客户端（优先 get_client()，回退 client 属性）。"""
    try:
        getter = getattr(inst, "get_client", None)
        if callable(getter):
            return getter()
    except Exception:
        pass
    try:
        return getattr(inst, "client", None)
    except Exception:
        return None


def find_insts(context: Any, names: set[str]) -> list[Any]:
    """在全部平台实例中筛选指定平台名的实例列表。"""
    return [i for i in _get_insts(context) if _inst_name(i) in names]


def get_qq_official_clients(context: Any, prefer_id: str = "") -> list[Any]:
    """获取 QQ 官方机器人平台的底层 botpy 客户端列表。

    prefer_id: 事件平台 id（event.get_platform_id()），匹配的实例排在前面。
    """
    pairs = []
    for inst in find_insts(context, QQ_OFFICIAL_NAMES):
        client = _inst_client(inst)
        if client is not None:
            pairs.append((_inst_id(inst), client))
    if prefer_id:
        pairs.sort(key=lambda p: p[0] != prefer_id)
    return [client for _, client in pairs]
