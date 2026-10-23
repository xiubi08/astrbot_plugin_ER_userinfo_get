"""QQ 官方机器人：Markdown + 指令按钮消息发送（附加模块）。

- 通过 context.platform_manager 遍历平台实例，get_client() 取 botpy 客户端
- 键盘按钮 action.type=2（指令按钮）：点击自动把 data 填入输入框，enter=true 直接发送
- 全程 try/except 兜底：任何失败返回 False，由调用方降级（主结果消息不受影响）
"""
from __future__ import annotations

import random
from typing import Any

from .platform_compat import (
    QQ_OFFICIAL_NAMES,
    get_platform_name,
    get_qq_official_clients,
)

from astrbot.api import logger

try:
    import botpy  # noqa: F401  （存在即视为可用）

    _HAS_BOTPY = True
except Exception:
    _HAS_BOTPY = False


def available() -> bool:
    """当前环境是否具备 QQ 官方按钮发送能力。"""
    return _HAS_BOTPY


def _msg_type(event: Any) -> str:
    """识别消息场景：'group'（群聊）/ 'friend'（C2C 私聊）/ ''（不支持）。"""
    try:
        mt = event.get_message_type()
        raw = getattr(mt, "name", "") or str(mt)
    except Exception:
        raw = ""
    up = raw.upper()
    if "GROUP" in up:
        return "group"
    if "FRIEND" in up or "PRIVATE" in up or "C2C" in up or "DIRECT" in up:
        return "friend"
    return ""


def build_markdown(content: str) -> dict:
    """构造 markdown 消息体（botpy MarkdownPayload 为 TypedDict，传 dict 即可）。"""
    return {"content": content}


def build_keyboard(rows: list[list[Any]]) -> dict:
    """把按钮行转成 QQ 键盘 payload（botpy KeyboardPayload 结构）。

    rows: 每行一个按钮列表，元素为 (label, data) 元组或
    {"label":..,"data":..,"style":..,"enter":..,"reply":..,"click_limit":..}。
    enter=False 时点击仅填入输入框不自动发送；reply=True 时点击后引用本条消息。
    action.type=2 表示指令按钮；permission.type=2 表示所有人可点。
    注意：keyboard 必须包一层 {"content": {"rows": [...]}}（botpy KeyboardPayload），
    且每个按钮的 render_data.visited_label 与 action.unsupport_tips 为官方必填字段，
    缺失会导致渲染不出按钮。
    """
    button_rows = []
    for i, row in enumerate(rows):
        buttons = []
        for j, btn in enumerate(row):
            if isinstance(btn, dict):
                label = str(btn.get("label", ""))
                data = str(btn.get("data", ""))
                style = int(btn.get("style", 1))
                enter = bool(btn.get("enter", True))
                reply = bool(btn.get("reply", False))
                click_limit = btn.get("click_limit")
                visited = str(btn.get("visited_label", label))
                tips = str(btn.get("unsupport_tips", "当前客户端不支持该按钮喵"))
            else:
                label, data = str(btn[0]), str(btn[1])
                style = 1
                enter = True
                reply = False
                click_limit = None
                visited = label
                tips = "当前客户端不支持该按钮喵"
            action = {
                "type": 2,
                "permission": {"type": 2, "specify_role_ids": [], "specify_user_ids": []},
                "unsupport_tips": tips,
                "data": data,
                "enter": enter,
                "reply": reply,
                "at_bot_show_channel_list": False,
            }
            if click_limit is not None:
                action["click_limit"] = int(click_limit)
            buttons.append(
                {
                    "id": f"er_b_{i}_{j}",
                    "render_data": {"label": label, "visited_label": visited, "style": style},
                    "action": action,
                }
            )
        button_rows.append({"buttons": buttons})
    return {"content": {"rows": button_rows}}


def query_result_buttons(
    player_name: str,
    prev_command: Optional[str] = None,
    next_command: Optional[str] = None,
) -> list[list[Any]]:
    """玩家战绩查询出图后附带的按钮。

    - 帮助：打开指令帮助
    - 上一赛季 / 下一赛季：按当前查询赛季切换（命令由调用方计算，None 表示隐藏）
    - 备注玩家：填入「备注玩家 <真实名> 」到输入框（enter=False，等待用户补备注名）
    """
    row: list[Any] = [("帮助", "/帮助")]
    if prev_command:
        row.append(("上一赛季", prev_command))
    if next_command:
        row.append(("下一赛季", next_command))
    row.append({"label": "备注玩家", "data": f"/备注玩家 {player_name} ", "enter": False})
    return [row]


async def send_markdown_with_buttons(
    context: Any,
    event: Any,
    content: str,
    rows: list[list[Any]],
) -> bool:
    """向 QQ 官方平台发送一条 Markdown + 指令按钮消息。

    仅对 QQ 官方平台生效；非 QQ 平台、无客户端、发送失败均返回 False（调用方自行降级）。
    作为被动回复发送（带 msg_id + 随机 msg_seq），避免占用主动消息频次配额。
    """
    if not _HAS_BOTPY:
        return False
    if get_platform_name(event) not in QQ_OFFICIAL_NAMES:
        return False

    try:
        platform_id = event.get_platform_id()
    except Exception:
        platform_id = ""
    clients = get_qq_official_clients(context, prefer_id=platform_id)
    if not clients:
        logger.warning("[ER按钮] 未找到 QQ 官方平台客户端，跳过按钮消息")
        return False
    api = getattr(clients[0], "api", None)
    if api is None:
        return False

    markdown = build_markdown(content if content else "\u200B")  # 空内容用零宽字符占位，避免被服务端拒绝
    keyboard = build_keyboard(rows)
    mtype = _msg_type(event)
    # 被动回复：带上用户消息 msg_id + 随机 msg_seq，避免重复发送被拒
    msg_id = getattr(getattr(event, "message_obj", None), "message_id", None)
    msg_seq = random.randint(1, 10000)

    try:
        if mtype == "group":
            group_openid = getattr(getattr(event, "message_obj", None), "group_id", None)
            if not group_openid:
                logger.warning("[ER按钮] 群消息缺少 group_openid，跳过按钮消息")
                return False
            ret = await api.post_group_message(
                group_openid=group_openid,
                msg_type=2,
                markdown=markdown,
                keyboard=keyboard,
                msg_id=msg_id,
                msg_seq=msg_seq,
            )
        elif mtype == "friend":
            try:
                openid = event.get_sender_id()
            except Exception:
                openid = getattr(getattr(event, "message_obj", None), "sender_id", None)
            if not openid:
                logger.warning("[ER按钮] 私聊消息缺少 openid，跳过按钮消息")
                return False
            ret = await api.post_c2c_message(
                openid=openid,
                msg_type=2,
                markdown=markdown,
                keyboard=keyboard,
                msg_id=msg_id,
                msg_seq=msg_seq,
            )
        else:
            return False
    except Exception as e:
        logger.warning(f"[ER按钮] QQ 官方按钮消息发送失败，降级为纯文本: {e}")
        return False

    if ret is None:
        logger.warning("[ER按钮] QQ 官方按钮消息发送返回空，降级为纯文本")
        return False
    logger.info("[ER按钮] QQ 官方按钮消息已发送")
    return True
