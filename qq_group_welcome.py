"""QQ 官方机器人：入群欢迎消息（附加模块，不修改任何现有代码）。

机器人被拉进群时 QQ 会推送 ``GROUP_ADD_ROBOT`` 事件，官方允许用事件 id
做「被动回复」（发消息时传 ``event_id`` 而不是 ``msg_id``），因此可以在
进群当下主动发一条欢迎消息，且不占用主动消息频次、不受群主「关闭主动消息」影响。

AstrBot 的 QQ 适配器不处理该事件，所以这里直接给 botpy 客户端挂 ``on_group_add_robot``
（botpy 的派发逻辑就是 ``getattr(client, "on_" + event)``）。

注意：``event_id`` 有有效期（过期报 40034026），必须收到后立刻发送。
"""
from __future__ import annotations

import time
from typing import Any, Optional

from .qq_media_image import build_image_markdown, upload_image
from .qq_official_button import build_keyboard

from astrbot.api import logger

try:
    import botpy  # noqa: F401  （存在即视为可用）

    _HAS_BOTPY = True
except Exception:
    _HAS_BOTPY = False

# 欢迎文案：图片在前作文首图，正文说明能做什么、怎么用
WELCOME_TEXT = (
    "**大家好喵，我是爱琳，一只可以帮你查永恒轮回资料的好猫！**\n\n"
    "直接发消息就能查喵：\n"
    "· 查询玩家　小休比\n"
    "· 查询角色　扎希尔\n"
    "· 排行榜\n\n"
    "不想 @ 我的话，点下面的「无@唤醒教程」，群主开启后就能直接发指令喵～"
)

# 同一群的欢迎冷却（秒）：防止重复拉入 / 事件重推导致刷屏
WELCOME_COOLDOWN = 600

_last_welcome_at: dict[str, float] = {}


def welcome_buttons() -> list[list[Any]]:
    """欢迎消息底部的三个按钮：查询示例 / 指令帮助 / 无@唤醒教程。"""
    return [
        [
            {"label": "查询示例", "data": "/查询玩家 小休比"},
            {"label": "指令帮助", "data": "/指令"},
        ],
        [
            {"label": "无@唤醒教程", "data": "/无@唤醒教程", "reply": False},
        ],
    ]


def build_welcome_content(url: Optional[str], image_path: Optional[str]) -> str:
    """拼装欢迎 markdown：有图放首图，无图只发文字。"""
    if url and image_path:
        try:
            img = build_image_markdown(url, image_path, alt="爱琳", max_width=600)
            return f"{img}\n\n{WELCOME_TEXT}"
        except Exception:
            pass
    return WELCOME_TEXT


async def send_group_welcome(
    client: Any,
    event: Any,
    image_path: Optional[str] = None,
) -> bool:
    """向刚加入的群发送一条图文欢迎消息（被动回复入群事件）。

    返回 True 表示已发出；非 QQ 客户端、事件缺字段、发送失败均返回 False。
    """
    if not _HAS_BOTPY:
        return False

    group_openid = getattr(event, "group_openid", None)
    event_id = getattr(event, "event_id", None)
    if not (group_openid and event_id):
        logger.warning("[ER欢迎] 入群事件缺少 group_openid/event_id，跳过欢迎消息")
        return False

    group_key = str(group_openid)
    now = time.time()
    last = _last_welcome_at.get(group_key)
    if last and now - last < WELCOME_COOLDOWN:
        logger.info("[ER欢迎] 该群刚发过欢迎消息，跳过（冷却中）")
        return False

    url = None
    if image_path:
        try:
            url = await upload_image(client, str(image_path))
        except Exception as e:
            logger.warning(f"[ER欢迎] 欢迎图上传失败，改为纯文字欢迎: {e}")
            url = None

    content = build_welcome_content(url, str(image_path) if (url and image_path) else None)
    markdown: dict[str, Any] = {"content": content}
    if url:
        # 图片转存失败时直接报错，而不是发出没图的卡片
        markdown["force_verify_image_resource"] = True

    api = getattr(client, "api", None)
    if api is None:
        return False
    try:
        ret = await api.post_group_message(
            group_openid=group_openid,
            msg_type=2,
            markdown=markdown,
            keyboard=build_keyboard(welcome_buttons()),
            event_id=event_id,
        )
    except Exception as e:
        logger.warning(f"[ER欢迎] 入群欢迎消息发送失败（已忽略）: {e}")
        return False

    if ret is None:
        logger.warning("[ER欢迎] 入群欢迎消息发送返回空")
        return False
    _last_welcome_at[group_key] = now
    logger.info("[ER欢迎] 已发送入群欢迎消息")
    return True


def install_hook(client: Any, handler: Any) -> bool:
    """给 botpy 客户端挂载 on_group_add_robot 处理器。

    返回 True 表示已挂载/已替换。插件重载时旧处理器仍绑定旧实例，
    因此带本模块标记的旧处理器会被新实例的处理器覆盖。
    """
    try:
        current = getattr(client, "on_group_add_robot", None)
        if current is not None:
            if getattr(current, "_er_welcome_handler", False):
                setattr(client, "on_group_add_robot", handler)
                return True
            return False
        setattr(client, "on_group_add_robot", handler)
        return True
    except Exception as e:
        logger.warning(f"[ER欢迎] 挂载入群欢迎钩子失败: {e}")
        return False


__all__ = [
    "WELCOME_COOLDOWN",
    "WELCOME_TEXT",
    "build_welcome_content",
    "install_hook",
    "send_group_welcome",
    "welcome_buttons",
]
