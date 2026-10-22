"""集中式指令登记表（附加模块，不修改任何现有代码）。

用途：
1. 生成 QQ 官方指令面板（管理端自定义菜单，q.qq.com 后台配置）的清单：
   指令名 ≤ 8 中文字符、描述 ≤ 15 字、上限 12 条。
2. 未来 /指令 帮助文本多形态渲染的统一数据源。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class CommandSpec:
    name: str
    category: str
    summary: str
    usages: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    admin_only: bool = False
    qq_label: Optional[str] = None
    """指令面板中的按钮文案（≤8 中文字符），缺省时用 name。"""


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        "查询玩家",
        "查询",
        "查询玩家战绩（别名：查询战绩）",
        ("查询玩家 <玩家名> [赛季] [场次] [模式]",),
        ("查询玩家 小休比", "查询玩家 小休比 S10 20", "查询玩家 小休比 排位"),
    ),
    CommandSpec(
        "查询角色",
        "查询",
        "查询角色综合数据（可指定武器/段位）",
        ("查询角色 [武器] <角色名> [段位]",),
        ("查询角色 鼻荆", "查询角色 暗器 扎希尔 in1000"),
    ),
    CommandSpec(
        "角色评级",
        "查询",
        "查询全部角色评级排行（可筛选段位）",
        ("角色评级 [段位]",),
        ("角色评级", "角色评级 星陨"),
    ),
    CommandSpec(
        "排行榜",
        "查询",
        "查询永恒/半神分数线及前 5 名",
        ("永恒多少分 / 半神多少分 / 排行榜",),
        ("排行榜",),
    ),
    CommandSpec(
        "备注玩家",
        "备注",
        "为玩家设置备注名",
        ("备注玩家 <真实名> <备注名>",),
        ("备注玩家 小休比 休宝",),
    ),
    CommandSpec(
        "删除玩家备注",
        "备注",
        "删除玩家备注",
        ("删除玩家备注 <备注名>",),
        ("删除玩家备注 休宝",),
    ),
    CommandSpec(
        "创建小组",
        "小组",
        "创建新的空小组",
        ("创建小组 <小组名>",),
        ("创建小组 开黑队",),
    ),
    CommandSpec(
        "加入小组",
        "小组",
        "向小组中添加成员",
        ("加入小组 <小组名> <玩家名>",),
        ("加入小组 开黑队 小休比",),
    ),
    CommandSpec(
        "删除小组成员",
        "小组",
        "从小组中移除成员",
        ("删除小组成员 <小组名> <玩家名>",),
        ("删除小组成员 开黑队 小休比",),
    ),
    CommandSpec(
        "删除小组",
        "小组",
        "删除整个小组",
        ("删除小组 <小组名>",),
        ("删除小组 开黑队",),
    ),
    CommandSpec(
        "查询小组",
        "小组",
        "查询小组成员的 MMR/段位及平均分",
        ("查询小组 <小组名>",),
        ("查询小组 开黑队",),
    ),
)

CATEGORY_ORDER = ("查询", "备注", "小组", "管理")


def by_category() -> dict[str, list[CommandSpec]]:
    """按分类分组的指令表（顺序见 CATEGORY_ORDER）。"""
    result: dict[str, list[CommandSpec]] = {}
    for cat in CATEGORY_ORDER:
        result[cat] = [c for c in COMMANDS if c.category == cat]
    return result


def qq_panel_entries(limit: int = 12) -> list[CommandSpec]:
    """QQ 指令面板推荐配置项（≤12 条、指令名 ≤8 中文字符）。"""
    picked: list[CommandSpec] = []
    for cat in CATEGORY_ORDER:
        for c in by_category()[cat]:
            if len(picked) >= limit:
                break
            picked.append(c)
        if len(picked) >= limit:
            break
    return picked


def to_panel_items(limit: int = 12) -> list[dict]:
    """把指令登记表转成 QQ 指令面板 PanelItem 结构。

    约束：name ≤ 14 字符（约 7 个汉字）、desc ≤ 30 字符（约 15 个汉字），
    超限会被服务端以 40030013「超出数量限制」拒绝。
    type=command 时点击后 name 会填入聊天输入框（AstrBot 唤醒前缀会自动剥离）。
    """
    limit = min(limit, 20)  # 单个面板最多 20 个元素
    items = []
    for spec in qq_panel_entries(limit):
        desc = (spec.summary.split("（")[0] or spec.summary).strip()
        items.append(
            {
                "type": "command",
                "name": (spec.qq_label or spec.name)[:7],
                "desc": desc[:15],
                "only_admin": bool(spec.admin_only),
            }
        )
    return items


def render_panel_config(limit: int = 12) -> str:
    """生成可直接抄到 QQ 管理端指令面板的配置清单文本。"""
    lines = ["QQ 官方指令面板配置清单（指令名 ≤8 字、描述 ≤15 字）：", ""]
    for spec in qq_panel_entries(limit):
        label = spec.qq_label or spec.name
        lines.append(f"- 指令名：{label}")
        lines.append(f"  指令：{spec.usages[0] if spec.usages else spec.name}")
        desc = spec.summary.split("（")[0].split("：")[-1]
        lines.append(f"  描述：{desc[:15]}")
    return "\n".join(lines)
