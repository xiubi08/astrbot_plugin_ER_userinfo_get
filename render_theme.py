"""永恒轮回战绩卡 · 渲染主题层（render_theme.py）

设计约束
--------
1. 只改「画什么颜色 / 什么光效」，不动任何坐标：所有卡片的 x / y / 宽 / 高
   仍然由 renderer.py 的布局代码决定，主题层只负责填充、描边、发光、阴影、文字配色。
2. 纯 Pillow 实现（不引入 numpy / 不增加依赖），与现有渲染技术栈一致。
3. 默认主题 ``legacy`` 的每个取值都与改动前 renderer.py 里的硬编码值逐项相同，
   因此不配置时输出像素级不变（可用像素 diff 验证）。

用法
----
    # config.py -> RenderConfig
    theme: str = "neon_night"   # legacy | neon_night | aurora_glass | luminous_light

渲染器在 __init__ 里调用 resolve_theme(RENDER.theme) 拿到 ThemeStyle，
所有绘制分支都读 self.style；未知主题名自动回退 legacy，不会抛异常。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, fields
from typing import Dict, Optional, Tuple

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

RGBA = Tuple[int, int, int, int]
RGB = Tuple[int, int, int]


# ============================================================
# 主题数据
# ============================================================
@dataclass(frozen=True)
class ThemeStyle:
    """一套主题的全部可调参数。默认值 = 旧版粉白配色（像素级兼容）。"""

    name: str = "legacy"
    label: str = "经典粉白（原始）"

    # ---- 画布背景 ----
    bg: RGB = (255, 255, 255)
    bg_top: Optional[RGB] = None          # 竖渐变上端，None = 纯色
    bg_bottom: Optional[RGB] = None
    bg_blobs: tuple = ()                  # ((cx,cy,d,color,alpha), ...) 相对画布比例
    canvas_vignette: int = 0              # 画布四周压暗程度（0=关）

    # ---- 主卡片 ----
    card_fill: RGBA = (255, 240, 245, 255)
    card_fill_bottom: Optional[RGBA] = None
    card_radius: int = 18
    card_rim: int = 0                     # 内侧 1px 高光（玻璃质感），0=关
    card_border: RGBA = (255, 200, 210, 255)
    card_border_bottom: Optional[RGBA] = None
    card_border_w: int = 2
    card_glow: Optional[RGBA] = None
    card_glow_blur: int = 0
    card_glow_gain: float = 1.0
    card_shadow: Optional[RGBA] = None
    card_shadow_blur: int = 0
    card_shadow_dy: int = 0

    # ---- 内层子卡片 ----
    inner_card_fill: RGBA = (255, 250, 252, 255)
    inner_card_radius: int = 12
    inner_card_border: Optional[RGBA] = None

    # ---- 文字 ----
    text: RGB = (30, 30, 30)
    sub_text: RGB = (120, 120, 120)
    muted: RGB = (160, 160, 160)

    # ---- 语义色 ----
    red: RGB = (220, 60, 60)
    blue: RGB = (83, 147, 202)
    gold: RGB = (200, 150, 40)
    gold_dim: RGB = (200, 150, 40)
    accent: RGB = (255, 100, 130)
    stale: RGB = (200, 80, 100)
    rank_23: RGB = (255, 180, 90)
    divider: RGB = (255, 200, 210)
    dot: RGB = (255, 150, 180)

    # ---- MMR 折线图 ----
    chart_grid: RGB = (200, 200, 200)
    chart_label: RGB = (130, 130, 130)
    chart_line: RGB = (193, 132, 83)
    chart_line2: Optional[RGB] = None
    chart_dot: Optional[RGB] = None
    chart_glow: Optional[RGBA] = None
    chart_area: Optional[RGBA] = None

    # ---- 头像框 ----
    avatar_glow: RGB = (255, 95, 131)
    avatar_glow2: Optional[RGB] = None
    avatar_line: RGBA = (255, 150, 180, 255)
    avatar_line2: Optional[RGBA] = None   # 内圈描边色（None=与外圈同色）
    avatar_radius_ratio: float = 0.0      # 0=直角，0.5=正圆
    avatar_border_w: int = 2
    avatar_glow_blur: int = 8
    avatar_glow_gain: float = 1.8
    avatar_line_inset: int = 5            # 内描边内缩（0=不画内描边）

    # ---- 装备图标 ----
    item_glow_gain: float = 1.6
    item_glow_alpha: int = 255                # 外发光不透明度（0-255）
    item_slot_fill: Optional[RGBA] = None     # 装备格底色（None=透明）
    item_glow_blur: int = 5
    item_border_w: int = 3
    item_radius: int = 6
    item_icon_radius: int = 0
    item_w: int = 52                          # 装备格宽度
    item_h: int = 40                          # 装备格高度
    item_gap: int = 64                        # 装备横向间距（上下两排共用）
    item_row_gap: int = 52                    # 上下两排的垂直间距
    item_right_inset: int = 250               # 装备栏到卡片右边缘的距离

    # ---- 猫爪水印 ----
    paw_opacity: float = 1.0
    paw_tint: Optional[RGB] = None

    # ---- 首局队友面板 ----
    team_panel: RGBA = (255, 255, 255, 255)
    team_header: RGBA = (255, 238, 244, 255)
    team_divider: RGBA = (255, 215, 225, 255)
    team_row_divider: Optional[RGBA] = None   # 一体化队友行的 1px 分割线（None=用 team_divider）
    team_card_fill: RGBA = (255, 255, 255, 255)
    team_integrated: bool = False   # 主玩家+队友合并成一张卡，队友无边框，只用细分割线
    team_lead_bar: Optional[RGBA] = None      # 主体左条（强调色）
    team_lead_bar2: Optional[RGBA] = None     # 主体左条渐变末端（None=纯色）
    team_lead_wash: Optional[RGBA] = None     # 主体行底色（很淡的强调色，None=不铺）
    row_inset: int = 0                        # 战绩行内容整体右移（px），让左条离卡边远一点
    hero_header: bool = False                 # 常用英雄：列名提到卡顶一行，行内只写数值
    item_stat_chips: bool = False             # 角色统计：物品统计用「分组 + 芯片」版式
    tier_art_fit: bool = False                # 头部段位图按比例缩放（竖版段位大图不拉伸）
    tier_icon_size: int = 0                   # 段位图标边长（0=96）
    tier_name_size: int = 0                   # 段位名字号（0=font_big）
    tier_name_gap: int = 10                   # 段位名相对图标底部的上移量
    tier_rank_size: int = 0                   # 排名文字字号（0=font_tiny）
    tier_rank_y: int = 138                    # 排名第一行相对卡片顶的偏移
    tier_rank_gap: int = 24                   # 两行排名的行距
    card_gap: int = 0                         # 卡片之间的垂直间距（0=沿用 PADDING）
    header_h: int = 0                         # 总览卡片高度（0=沿用 RENDER.header_height）
    header_gap: int = 0                       # 总览与统计卡片之间的间距（0=沿用 card_gap）
    section_header: Optional[RGBA] = None     # 标题底：左端色（None=不画）
    section_header2: Optional[RGBA] = None    # 标题底：右端色（None=同左端）
    chart_w: int = 550                        # 折线图宽度
    chart_h: int = 145                        # 折线图高度
    chart_inset_x: int = 215                  # 折线图距卡片左边的偏移
    chart_radius: int = 16                    # 折线图面板/边框圆角
    chart_panel: Optional[RGBA] = None        # 折线图底板（None=不画）
    chart_panel2: Optional[RGBA] = None       # 折线图底板下端色
    chart_frame: Optional[RGBA] = None        # 折线图边框左/上端色（None=不画）
    chart_frame2: Optional[RGBA] = None       # 折线图边框下端色
    chart_frame_w: int = 2
    chart_frame_blur: int = 6
    chart_frame_gain: float = 1.5
    chart_glow_alpha: int = 110
    team_lead_bar_w: int = 3
    team_row_divider_strong: Optional[RGBA] = None  # 主体与次体之间的分界线（None=同 team_row_divider）
    team_sub_bar: Optional[RGBA] = None       # 次体左条（淡色细）
    team_sub_bar_w: int = 2
    team_sub_opacity: float = 1.0             # 次体整行不透明度（<1 表示降权）
    team_sub_inset: int = 0                   # 次体左右额外内缩（px），表达从属关系
    team_sub_scale: float = 0                 # 次体缩放比例（0=按缩进缩放；1.0=与主体同尺寸）
    team_sub_wash: Optional[RGBA] = None      # 次体行底色（略微压暗，None=不铺）
    mmr_muted: Optional[RGB] = None           # 次体 RP 增减的灰色（None=sub_text）

    # ---- 角色评级排行 ----
    rank_card_top: RGBA = (255, 240, 245, 255)
    rank_card_bottom: RGBA = (255, 255, 255, 255)
    rank_weapon_dot: RGB = (30, 30, 30)

    # ---- 战绩卡零件 ----
    weapon_chip: RGBA = (30, 30, 30, 255)
    badge_fill: RGBA = (245, 245, 245, 255)
    badge_outline: RGBA = (60, 60, 60, 255)
    badge_text: RGB = (20, 20, 20)
    dmg_track: RGBA = (240, 232, 235, 255)
    dmg_fill: RGBA = (255, 100, 130, 255)
    dmg_fill2: Optional[RGBA] = None

    # ---- 角色页 ----
    char_row_bg: RGBA = (255, 248, 251, 255)
    char_row_bg2: RGBA = (255, 252, 253, 255)
    char_row_border: RGBA = (255, 200, 210, 255)
    char_slot_empty: RGBA = (80, 80, 80, 255)
    char_slot_empty2: RGBA = (50, 50, 50, 255)
    char_slot_icon: RGBA = (100, 100, 100, 255)
    char_divider: RGBA = (80, 80, 80, 255)
    char_col_divider: RGBA = (200, 180, 190, 255)
    sel_hi: RGB = (255, 140, 50)
    sel_mid: RGB = (220, 180, 80)
    win_hi: RGB = (255, 80, 80)
    win_mid: RGB = (220, 150, 80)
    win_lo: RGB = (100, 200, 100)
    skill_icon_bg: RGBA = (60, 60, 60, 255)
    skill_badge_fill: RGBA = (40, 40, 40, 255)
    skill_badge_outline: RGBA = (120, 120, 120, 255)
    skill_badge_text: RGB = (255, 255, 255)
    skill_sep: RGB = (160, 160, 160)

    @property
    def legacy(self) -> bool:
        """True = 完全走旧绘制分支（保证像素级不变）。"""
        return self.name == "legacy"


# ============================================================
# 精灵缓存（渐变 / 描边 / 发光 / 阴影 / 光斑）
# ============================================================
_SPRITES: Dict[tuple, Image.Image] = {}
_SPRITES_LOCK = threading.RLock()
_SPRITE_BUDGET = 512


def _rgba(c) -> RGBA:
    c = tuple(c)
    if len(c) == 3:
        return (int(c[0]), int(c[1]), int(c[2]), 255)
    return (int(c[0]), int(c[1]), int(c[2]), int(c[3]))


def _sprite(key: tuple, factory):
    with _SPRITES_LOCK:
        hit = _SPRITES.get(key)
    if hit is not None:
        return hit
    made = factory()
    with _SPRITES_LOCK:
        if len(_SPRITES) > _SPRITE_BUDGET:
            _SPRITES.clear()
        _SPRITES[key] = made
    return made


def _vgrad(w: int, h: int, top: RGBA, bottom: RGBA) -> Image.Image:
    """竖直渐变（带 alpha），按尺寸+端点缓存。"""
    if w <= 0 or h <= 0:
        return Image.new("RGBA", (max(1, w), max(1, h)), (0, 0, 0, 0))

    def build():
        # 先在 1×h 上生成渐变再横向拉伸，避免逐像素 python 循环
        ramp = Image.linear_gradient("L").resize((1, h), Image.BILINEAR)
        rgb = ImageOps.colorize(ramp, top[:3], bottom[:3]).convert("RGBA")
        rgb = rgb.resize((w, h), Image.BILINEAR)
        if top[3] != 255 or bottom[3] != 255:
            a = ramp.point(lambda v: int(round(top[3] + (bottom[3] - top[3]) * v / 255.0)))
            rgb.putalpha(a.resize((w, h), Image.BILINEAR))
        return rgb

    return _sprite(("vgrad", w, h, top, bottom), build)


def rounded_gradient(w: int, h: int, radius: int, top: RGBA, bottom: RGBA,
                     rim: int = 0) -> Optional[Image.Image]:
    """圆角卡片底色（渐变 + 抗锯齿圆角 + 可选内高光）。"""
    if w <= 0 or h <= 0:
        return None
    key = ("card", w, h, radius, _rgba(top), _rgba(bottom), rim)

    def build():
        ss = 2
        gw, gh = w * ss, h * ss
        grad = _vgrad(gw, gh, _rgba(top), _rgba(bottom)).copy()
        mask = Image.new("L", (gw, gh), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, gw - 1, gh - 1), radius=max(0, radius * ss), fill=255)
        grad.putalpha(ImageChops.multiply(grad.getchannel("A"), mask))
        if rim > 0:
            ImageDraw.Draw(grad).rounded_rectangle(
                (1, 1, gw - 2, gh - 2), radius=max(0, radius * ss - 2),
                outline=(255, 255, 255, rim), width=ss)
        return grad.resize((w, h), Image.LANCZOS)

    return _sprite(key, build)


def glow_layer(w: int, h: int, radius: int, color: RGBA,
               blur: int, gain: float, pad: int,
               width: int = 2, outside_only: bool = True):
    """外发光精灵，返回 (sprite, pad)。"""
    P = max(1, int(pad))
    key = ("glow", w, h, radius, _rgba(color), int(blur), float(gain), P, width, outside_only)

    def build():
        ss = 2
        gw, gh = (w + 2 * P) * ss, (h + 2 * P) * ss
        ox = oy = P * ss
        ring = Image.new("L", (gw, gh), 0)
        ImageDraw.Draw(ring).rounded_rectangle(
            (ox, oy, ox + w * ss - 1, oy + h * ss - 1),
            radius=max(0, radius * ss), outline=255, width=max(1, width * ss))
        m = ring.filter(ImageFilter.GaussianBlur(max(1, blur) * ss))
        m = m.point(lambda v: min(255, int(v * max(1.0, gain))))
        if outside_only:
            inner = Image.new("L", (gw, gh), 0)
            ins = max(1, width) * ss
            ImageDraw.Draw(inner).rounded_rectangle(
                (ox + ins, oy + ins, ox + w * ss - 1 - ins, oy + h * ss - 1 - ins),
                radius=max(0, radius * ss - ins), fill=255)
            m = ImageChops.subtract(m, inner)
        sprite = Image.new("RGBA", (gw, gh), _rgba(color)[:3] + (255,))
        a = _rgba(color)[3]
        if a < 255:
            m = ImageChops.multiply(m, Image.new("L", (gw, gh), a))
        sprite.putalpha(m)
        return sprite.resize((w + 2 * P, h + 2 * P), Image.LANCZOS)

    return _sprite(key, build), P


def border_layer(w: int, h: int, radius: int, width: int,
                 top: RGBA, bottom: RGBA,
                 glow: Optional[RGBA] = None, glow_blur: int = 0,
                 glow_gain: float = 1.0, pad: int = 0):
    """渐变描边精灵（可叠外发光），返回 (sprite, pad)。"""
    P = max(0, int(pad))
    if width <= 0 and glow is None:
        return None, 0
    key = ("border", w, h, radius, width, _rgba(top), _rgba(bottom),
           _rgba(glow) if glow else None, int(glow_blur), float(glow_gain), P)

    def build():
        ss = 3
        sprite = Image.new("RGBA", ((w + 2 * P) * ss, (h + 2 * P) * ss), (0, 0, 0, 0))

        if glow is not None:
            g, gp = glow_layer(w, h, radius, glow, max(2, glow_blur), glow_gain, P,
                               width=max(1, width))
            if gp == P:
                sprite = g.resize(sprite.size, Image.LANCZOS)
            else:  # 理论不会发生，兜底直接贴
                sprite = Image.new("RGBA", ((w + 2 * P) * ss, (h + 2 * P) * ss), (0, 0, 0, 0))
                sprite.alpha_composite(g.resize(((w + 2 * gp) * ss, (h + 2 * gp) * ss), Image.LANCZOS),
                                       (max(0, (gp - P)) * ss, max(0, (gp - P)) * ss))

        if width > 0:
            ox = oy = P * ss
            ring = Image.new("L", sprite.size, 0)
            ImageDraw.Draw(ring).rounded_rectangle(
                (ox, oy, ox + w * ss - 1, oy + h * ss - 1),
                radius=max(0, radius * ss), outline=255, width=max(1, width * ss))
            line = _vgrad(sprite.size[0], sprite.size[1], _rgba(top), _rgba(bottom)).copy()
            line.putalpha(ImageChops.multiply(line.getchannel("A"), ring))
            sprite = Image.alpha_composite(sprite, line)

        return sprite.resize((w + 2 * P, h + 2 * P), Image.LANCZOS)

    return _sprite(key, build), P


def shadow_layer(w: int, h: int, radius: int, color: RGBA, blur: int, dy: int):
    """柔和投影精灵，返回 (sprite, pad)。"""
    P = max(4, int(blur) * 2)
    key = ("shadow", w, h, radius, _rgba(color), int(blur), int(dy), P)

    def build():
        ss = 2
        gw, gh = (w + 2 * P) * ss, (h + 2 * P) * ss
        mask = Image.new("L", (gw, gh), 0)
        ox, oy = P * ss, (P + dy) * ss
        ImageDraw.Draw(mask).rounded_rectangle(
            (ox, oy, ox + w * ss - 1, oy + h * ss - 1),
            radius=max(0, radius * ss), fill=_rgba(color)[3])
        mask = mask.filter(ImageFilter.GaussianBlur(max(1, blur) * ss))
        sprite = Image.new("RGBA", (gw, gh), _rgba(color)[:3] + (255,))
        sprite.putalpha(mask)
        return sprite.resize((w + 2 * P, h + 2 * P), Image.LANCZOS)

    return _sprite(key, build), P


def blob_layer(diameter: int, color: RGB, alpha: int) -> Optional[Image.Image]:
    """柔光光斑（径向渐变），用于极光 / 霓虹底噪。"""
    d = max(8, int(diameter))
    key = ("blob", d, tuple(color), int(alpha))

    def build():
        ramp = Image.radial_gradient("L").resize((d, d), Image.BICUBIC)
        mask = ImageOps.invert(ramp).point(lambda v: int(v * alpha / 255.0))
        sprite = Image.new("RGBA", (d, d), tuple(color) + (255,))
        sprite.putalpha(mask)
        return sprite

    return _sprite(key, build)


def build_canvas(w: int, h: int, style: ThemeStyle) -> Image.Image:
    """按主题生成画布：底色渐变 + 光斑 + 可选暗角。"""
    top = _rgba(style.bg_top or style.bg)
    bottom = _rgba(style.bg_bottom or style.bg)
    if top == bottom:
        img = Image.new("RGBA", (w, h), top)
    else:
        img = _vgrad(w, h, top, bottom).copy()

    for blob in style.bg_blobs:
        try:
            cx, cy, d_ratio, color, alpha = blob
        except (TypeError, ValueError):
            continue
        d = int(d_ratio * w)
        sprite = blob_layer(d, color, alpha)
        if sprite is None:
            continue
        blend_at(img, sprite, int(cx * w - d / 2), int(cy * h - d / 2))

    if style.canvas_vignette > 0:
        v = blob_layer(int(w * 1.9), (0, 0, 0), style.canvas_vignette)
        if v is not None:
            blend_at(img, v, int(w / 2 - v.width / 2), int(h / 2 - v.height / 2))
    return img


def blend_at(img: Image.Image, sprite: Optional[Image.Image], x: int, y: int) -> None:
    """越界的 alpha 合成（负坐标 / 超出画布自动裁剪）。"""
    if sprite is None or img is None:
        return
    if img.mode != "RGBA":
        return
    sw, sh = sprite.size
    ix, iy = max(0, int(x)), max(0, int(y))
    ex, ey = min(img.width, int(x) + sw), min(img.height, int(y) + sh)
    if ex <= ix or ey <= iy:
        return
    piece = sprite.crop((ix - int(x), iy - int(y), ix - int(x) + (ex - ix), iy - int(y) + (ey - iy)))
    img.alpha_composite(piece, (ix, iy))


# ============================================================
# 主题预设
# ============================================================
LEGACY = ThemeStyle()

NEON_NIGHT = ThemeStyle(
    name="neon_night",
    label="方案一 · 暗夜霓虹 Neon Night",
    bg=(9, 12, 20),
    bg_top=(16, 21, 34),
    bg_bottom=(6, 9, 15),
    bg_blobs=(
        (0.08, 0.02, 1.20, (34, 211, 238), 40),
        (0.95, 0.10, 1.10, (139, 92, 246), 40),
        (0.02, 0.42, 0.95, (34, 211, 238), 10),
        (0.98, 0.62, 1.05, (236, 72, 153), 14),
        (0.45, 0.92, 1.15, (139, 92, 246), 12),
    ),
    card_fill=(20, 26, 42, 255),
    card_fill_bottom=(13, 18, 30, 255),
    card_radius=20,
    card_rim=22,
    card_border=(56, 189, 248, 255),
    card_border_bottom=(167, 139, 250, 255),
    card_border_w=1,
    card_glow=(34, 211, 238, 70),
    card_glow_blur=18,
    card_glow_gain=1.35,
    card_shadow=(0, 0, 0, 120),
    card_shadow_blur=18,
    card_shadow_dy=6,
    inner_card_fill=(31, 39, 60, 235),
    inner_card_radius=14,
    inner_card_border=(56, 189, 248, 40),
    text=(233, 240, 250),
    sub_text=(146, 163, 186),
    muted=(120, 136, 160),
    red=(251, 113, 133),
    blue=(96, 165, 250),
    gold=(250, 204, 21),
    gold_dim=(202, 138, 4),
    accent=(34, 211, 238),
    stale=(251, 146, 166),
    rank_23=(251, 191, 36),
    divider=(40, 52, 74),
    dot=(34, 211, 238),
    chart_grid=(34, 44, 64),
    chart_label=(120, 136, 160),
    chart_line=(34, 211, 238),
    chart_line2=(167, 139, 250),
    chart_dot=(236, 254, 255),
    chart_glow=(34, 211, 238, 150),
    chart_area=(34, 211, 238, 30),
    avatar_glow=(34, 211, 238),
    avatar_glow2=(167, 139, 250),
    avatar_line=(150, 234, 255, 200),
    avatar_radius_ratio=0.18,
    avatar_border_w=2,
    avatar_glow_blur=10,
    avatar_glow_gain=1.9,
    avatar_line_inset=3,
    item_glow_gain=1.7,
    item_glow_blur=7,
    item_border_w=2,
    item_radius=7,
    item_icon_radius=6,
    paw_opacity=0.05,
    paw_tint=(56, 189, 248),
    team_panel=(15, 21, 35, 255),
    team_header=(22, 30, 47, 255),
    team_divider=(40, 52, 74, 255),
    team_card_fill=(18, 24, 39, 255),
    rank_card_top=(26, 34, 54, 255),
    rank_card_bottom=(14, 19, 31, 255),
    rank_weapon_dot=(5, 8, 14),
    weapon_chip=(10, 15, 25, 255),
    badge_fill=(15, 23, 42, 255),
    badge_outline=(34, 211, 238, 255),
    badge_text=(224, 242, 254),
    dmg_track=(33, 43, 64, 255),
    dmg_fill=(34, 211, 238, 255),
    dmg_fill2=(167, 139, 250, 255),
    char_row_bg=(26, 33, 52, 255),
    char_row_bg2=(22, 29, 46, 255),
    char_row_border=(40, 52, 74, 255),
    char_slot_empty=(44, 56, 80, 255),
    char_slot_empty2=(32, 42, 60, 255),
    char_slot_icon=(56, 70, 96, 255),
    char_divider=(40, 52, 74, 255),
    char_col_divider=(52, 66, 92, 255),
    sel_hi=(251, 191, 36),
    sel_mid=(202, 138, 4),
    win_hi=(251, 113, 133),
    win_mid=(251, 146, 60),
    win_lo=(52, 211, 153),
    skill_icon_bg=(38, 48, 70, 255),
    skill_badge_fill=(15, 22, 38, 255),
    skill_badge_outline=(34, 211, 238, 255),
    skill_badge_text=(224, 242, 254),
    skill_sep=(120, 136, 160),
)

AURORA_GLASS = ThemeStyle(
    name="aurora_glass",
    label="方案二 · 极光玻璃 Aurora Glass",
    bg=(10, 10, 22),
    bg_top=(20, 17, 44),
    bg_bottom=(8, 7, 16),
    bg_blobs=(
        (0.12, 0.06, 1.10, (45, 212, 191), 52),
        (0.94, 0.10, 1.05, (139, 92, 246), 64),
        (0.02, 0.38, 1.05, (236, 72, 153), 26),
        (0.85, 0.55, 1.15, (56, 189, 248), 34),
        (0.35, 0.88, 1.30, (167, 139, 250), 40),
    ),
    card_fill=(25, 23, 46, 236),
    card_fill_bottom=(14, 12, 28, 236),
    card_radius=26,
    card_rim=44,
    card_border=(255, 255, 255, 92),
    card_border_bottom=(167, 139, 250, 56),
    card_border_w=1,
    card_glow=(167, 139, 250, 66),
    card_glow_blur=26,
    card_glow_gain=1.2,
    card_shadow=(0, 0, 0, 110),
    card_shadow_blur=24,
    card_shadow_dy=10,
    inner_card_fill=(255, 255, 255, 18),
    inner_card_radius=16,
    inner_card_border=(255, 255, 255, 26),
    text=(241, 240, 255),
    sub_text=(176, 174, 208),
    muted=(150, 148, 186),
    red=(251, 113, 133),
    blue=(125, 211, 252),
    gold=(253, 230, 138),
    gold_dim=(250, 204, 21),
    accent=(45, 212, 191),
    stale=(251, 146, 166),
    rank_23=(253, 230, 138),
    divider=(255, 255, 255, 26),
    dot=(45, 212, 191),
    chart_grid=(62, 58, 104, 255),
    chart_label=(168, 166, 200),
    chart_line=(45, 212, 191),
    chart_line2=(167, 139, 250),
    chart_dot=(255, 255, 255),
    chart_glow=(45, 212, 191, 140),
    chart_area=(45, 212, 191, 26),
    avatar_glow=(45, 212, 191),
    avatar_glow2=(167, 139, 250),
    avatar_line=(255, 255, 255, 110),
    avatar_radius_ratio=0.5,
    avatar_border_w=2,
    avatar_glow_blur=12,
    avatar_glow_gain=2.0,
    avatar_line_inset=0,
    item_glow_gain=2.0,
    item_glow_blur=9,
    item_border_w=2,
    item_radius=8,
    item_icon_radius=7,
    paw_opacity=0.05,
    paw_tint=(167, 139, 250),
    team_panel=(255, 255, 255, 20),
    team_header=(255, 255, 255, 34),
    team_divider=(255, 255, 255, 26),
    team_card_fill=(255, 255, 255, 10),
    rank_card_top=(28, 26, 52, 255),
    rank_card_bottom=(16, 14, 32, 255),
    rank_weapon_dot=(12, 10, 24),
    weapon_chip=(58, 56, 92, 255),
    badge_fill=(12, 10, 24, 255),
    badge_outline=(45, 212, 191, 255),
    badge_text=(240, 255, 252),
    dmg_track=(255, 255, 255, 22),
    dmg_fill=(45, 212, 191, 255),
    dmg_fill2=(167, 139, 250, 255),
    char_row_bg=(42, 40, 74, 255),
    char_row_bg2=(34, 32, 62, 255),
    char_row_border=(78, 74, 124, 255),
    char_slot_empty=(48, 46, 84, 255),
    char_slot_empty2=(34, 32, 62, 255),
    char_slot_icon=(58, 56, 92, 255),
    char_divider=(56, 52, 92, 255),
    char_col_divider=(46, 44, 82, 255),
    sel_hi=(253, 230, 138),
    sel_mid=(250, 204, 21),
    win_hi=(251, 113, 133),
    win_mid=(251, 146, 60),
    win_lo=(52, 211, 153),
    skill_icon_bg=(46, 44, 82, 255),
    skill_badge_fill=(12, 10, 24, 255),
    skill_badge_outline=(45, 212, 191, 255),
    skill_badge_text=(240, 255, 252),
    skill_sep=(150, 148, 186),
)

LUMINOUS_LIGHT = ThemeStyle(
    name="luminous_light",
    label="方案三 · 白昼柔光 Luminous Light",
    bg=(247, 248, 252),
    bg_top=(255, 255, 255),
    bg_bottom=(240, 242, 250),
    bg_blobs=(
        (0.04, 0.02, 1.05, (99, 102, 241), 26),
        (0.98, 0.08, 0.95, (236, 72, 153), 20),
        (0.02, 0.45, 0.95, (56, 189, 248), 12),
        (0.95, 0.75, 1.10, (139, 92, 246), 18),
    ),
    card_fill=(255, 255, 255, 255),
    card_fill_bottom=(252, 252, 255, 255),
    card_radius=22,
    card_rim=0,
    card_border=(226, 232, 240, 255),
    card_border_bottom=(226, 232, 240, 255),
    card_border_w=1,
    card_shadow=(15, 23, 42, 34),
    card_shadow_blur=18,
    card_shadow_dy=5,
    inner_card_fill=(246, 247, 252, 255),
    inner_card_radius=14,
    inner_card_border=(232, 235, 244, 190),
    text=(23, 29, 45),
    sub_text=(100, 116, 139),
    muted=(148, 163, 184),
    red=(244, 63, 94),
    blue=(59, 130, 246),
    gold=(217, 119, 6),
    gold_dim=(180, 83, 9),
    accent=(99, 102, 241),
    stale=(225, 29, 72),
    rank_23=(234, 88, 12),
    divider=(226, 232, 240),
    dot=(99, 102, 241),
    chart_grid=(226, 232, 240),
    chart_label=(148, 163, 184),
    chart_line=(99, 102, 241),
    chart_line2=(236, 72, 153),
    chart_dot=(255, 255, 255),
    chart_glow=(99, 102, 241, 60),
    chart_area=(99, 102, 241, 24),
    avatar_glow=(99, 102, 241),
    avatar_glow2=(236, 72, 153),
    avatar_line=(255, 255, 255, 210),
    avatar_radius_ratio=0.22,
    avatar_border_w=2,
    avatar_glow_blur=10,
    avatar_glow_gain=1.5,
    avatar_line_inset=4,
    item_glow_gain=1.3,
    item_glow_blur=6,
    item_border_w=2,
    item_radius=8,
    item_icon_radius=6,
    paw_opacity=0.30,
    paw_tint=None,
    team_panel=(250, 251, 255, 255),
    team_header=(243, 245, 251, 255),
    team_divider=(232, 235, 244, 255),
    team_card_fill=(255, 255, 255, 255),
    rank_card_top=(255, 255, 255, 255),
    rank_card_bottom=(247, 248, 253, 255),
    rank_weapon_dot=(23, 29, 45),
    weapon_chip=(23, 29, 45, 255),
    badge_fill=(255, 255, 255, 255),
    badge_outline=(99, 102, 241, 255),
    badge_text=(49, 46, 129),
    dmg_track=(238, 240, 246, 255),
    dmg_fill=(99, 102, 241, 255),
    dmg_fill2=(236, 72, 153, 255),
    char_row_bg=(246, 247, 252, 255),
    char_row_bg2=(252, 252, 255, 255),
    char_row_border=(232, 235, 244, 255),
    char_slot_empty=(226, 232, 240, 255),
    char_slot_empty2=(208, 213, 224, 255),
    char_slot_icon=(203, 213, 225, 255),
    char_divider=(226, 232, 240, 255),
    char_col_divider=(232, 235, 244, 255),
    sel_hi=(234, 88, 12),
    sel_mid=(217, 119, 6),
    win_hi=(244, 63, 94),
    win_mid=(234, 88, 12),
    win_lo=(22, 163, 74),
    skill_icon_bg=(238, 240, 246, 255),
    skill_badge_fill=(255, 255, 255, 255),
    skill_badge_outline=(203, 213, 225, 255),
    skill_badge_text=(23, 29, 45),
    skill_sep=(148, 163, 184),
)

SAKURA_GLOW = ThemeStyle(
    name="sakura_glow",
    label="方案四 · 樱花柔光 Sakura Glow",
    bg=(255, 248, 251),
    bg_top=(255, 253, 254),
    bg_bottom=(255, 241, 247),
    bg_blobs=(
        (0.06, 0.03, 1.05, (255, 170, 195), 40),
        (0.96, 0.09, 0.95, (255, 140, 175), 30),
        (0.03, 0.46, 0.90, (255, 205, 215), 22),
        (0.94, 0.72, 1.05, (255, 190, 150), 20),
        (0.42, 0.94, 1.10, (255, 165, 200), 24),
    ),
    card_fill=(255, 255, 255, 255),
    card_fill_bottom=(255, 246, 250, 255),
    card_radius=26,
    card_rim=26,
    card_border=(255, 108, 142, 255),
    card_border_bottom=(255, 186, 148, 255),
    card_border_w=2,
    card_glow=(255, 108, 142, 110),
    card_glow_blur=6,
    card_glow_gain=1.5,
    card_shadow=(240, 90, 130, 30),
    card_shadow_blur=20,
    card_shadow_dy=7,
    inner_card_fill=(255, 247, 251, 255),
    inner_card_radius=16,
    inner_card_border=(255, 228, 238, 210),
    text=(74, 44, 60),
    sub_text=(150, 112, 130),
    muted=(186, 158, 172),
    red=(240, 76, 110),
    blue=(112, 152, 224),
    gold=(240, 170, 60),
    gold_dim=(203, 128, 26),
    accent=(255, 100, 130),
    stale=(226, 74, 106),
    rank_23=(255, 160, 90),
    divider=(255, 222, 232),
    dot=(255, 150, 180),
    chart_grid=(255, 228, 237),
    chart_label=(176, 142, 156),
    chart_line=(255, 118, 148),
    chart_line2=(255, 176, 110),
    chart_dot=(255, 255, 255),
    chart_glow=(255, 118, 148, 96),
    chart_area=(255, 138, 168, 28),
    avatar_glow=(255, 138, 168),
    avatar_glow2=(255, 190, 150),
    avatar_line=(255, 158, 190, 255),
    avatar_line2=(255, 255, 255, 240),
    avatar_radius_ratio=0.4,
    avatar_border_w=2,
    avatar_glow_blur=13,
    avatar_glow_gain=2.2,
    avatar_line_inset=4,
    item_glow_gain=2.6,
    item_glow_blur=6,
    item_glow_alpha=215,
    item_border_w=3,
    item_radius=11,
    item_icon_radius=8,
    item_w=58,
    item_h=46,
    item_gap=70,
    item_row_gap=54,
    item_right_inset=224,
    item_slot_fill=(255, 250, 252, 235),
    paw_opacity=0.82,
    paw_tint=None,
    team_panel=(255, 252, 254, 255),
    team_header=(255, 240, 246, 255),
    team_divider=(255, 226, 236, 255),
    team_row_divider=(255, 198, 214, 255),
    team_card_fill=(255, 255, 255, 255),
    team_integrated=True,
    team_lead_bar=(255, 116, 146, 255),
    team_lead_bar2=(255, 172, 120, 255),
    team_lead_wash=(250, 212, 222, 42),
    team_lead_bar_w=5,
    team_sub_bar=None,
    team_sub_bar_w=0,
    team_sub_opacity=1.0,
    team_sub_inset=0,
    team_sub_scale=1.0,
    team_sub_wash=(255, 116, 146, 30),
    row_inset=26,
    mmr_muted=(150, 147, 154),
    hero_header=True,
    item_stat_chips=True,
    tier_art_fit=True,
    tier_icon_size=116,
    tier_name_size=24,
    tier_name_gap=6,
    tier_rank_size=14,
    tier_rank_y=150,
    tier_rank_gap=18,
    card_gap=12,
    header_h=204,
    header_gap=6,
    section_header=(255, 108, 142, 110),
    section_header2=(255, 190, 148, 110),
    chart_w=430,
    chart_h=158,
    chart_inset_x=318,
    chart_radius=16,
    chart_panel=(255, 255, 255, 190),
    chart_panel2=(255, 247, 251, 205),
    chart_frame=(255, 190, 210, 180),
    chart_frame2=(255, 170, 195, 180),
    chart_frame_blur=18,
    chart_frame_gain=1.12,
    chart_glow_alpha=22,
    team_row_divider_strong=(255, 170, 196, 255),
    rank_card_top=(255, 251, 253, 255),
    rank_card_bottom=(255, 238, 245, 255),
    rank_weapon_dot=(74, 44, 60),
    weapon_chip=(88, 62, 78, 255),
    badge_fill=(255, 255, 255, 255),
    badge_outline=(255, 168, 190, 255),
    badge_text=(198, 62, 96),
    dmg_track=(255, 232, 240, 255),
    dmg_fill=(255, 120, 150, 255),
    dmg_fill2=(255, 176, 110, 255),
    char_row_bg=(255, 248, 251, 255),
    char_row_bg2=(255, 252, 253, 255),
    char_row_border=(255, 214, 226, 255),
    char_slot_empty=(255, 232, 240, 255),
    char_slot_empty2=(255, 238, 244, 255),
    char_slot_icon=(242, 214, 226, 255),
    char_divider=(255, 222, 234, 255),
    char_col_divider=(255, 228, 238, 255),
    sel_hi=(245, 128, 40),
    sel_mid=(214, 158, 66),
    win_hi=(235, 70, 105),
    win_mid=(240, 132, 92),
    win_lo=(56, 176, 118),
    skill_icon_bg=(255, 232, 240, 255),
    skill_badge_fill=(255, 255, 255, 255),
    skill_badge_outline=(255, 182, 202, 255),
    skill_badge_text=(188, 58, 90),
    skill_sep=(192, 158, 172),
)

THEMES: Dict[str, ThemeStyle] = {
    "legacy": LEGACY,
    "neon_night": NEON_NIGHT,
    "aurora_glass": AURORA_GLASS,
    "luminous_light": LUMINOUS_LIGHT,
    "sakura_glow": SAKURA_GLOW,
}


def resolve_theme(name: Optional[str]) -> ThemeStyle:
    """按名字取主题；未知名字回退 legacy（绝不抛异常）。"""
    if not name:
        return LEGACY
    return THEMES.get(str(name).strip().lower(), LEGACY)


def theme_names() -> Tuple[str, ...]:
    return tuple(THEMES.keys())


def theme_label(name: str) -> str:
    return resolve_theme(name).label


if __name__ == "__main__":  # 自检：打印可用主题
    for _n, _t in THEMES.items():
        _changed = sum(1 for f in fields(_t) if getattr(_t, f.name) != getattr(LEGACY, f.name))
        print(f"{_n:16s} {_t.label:28s} 覆盖字段数={_changed}")
