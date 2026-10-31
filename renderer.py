from __future__ import annotations
import uuid
import time
import threading
import contextlib
from functools import lru_cache
from pathlib import Path
from typing import Any, List, Optional
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

from .config import RENDER, LOCK
from .render_theme import (
    ThemeStyle,
    blend_at,
    border_layer,
    build_canvas,
    glow_layer,
    resolve_theme,
    rounded_gradient,
    shadow_layer,
)

from astrbot.api import logger


# ============================================================
# 带超时的锁工具：避免无限制等待导致渲染线程卡死
# ============================================================

class _RendererLockTimeoutError(RuntimeError):
    """渲染锁获取超时异常"""
    pass


@contextlib.contextmanager
def _acquire_timeout(lock, timeout: float, lock_name: str = "lock"):
    """带超时的锁上下文管理器，超时后抛出异常而非无限等待。"""
    acquired = lock.acquire(timeout=timeout)
    if not acquired:
        raise _RendererLockTimeoutError(
            f"获取渲染锁 {lock_name} 超时 (timeout={timeout}s)，疑似死锁或任务卡住"
        )
    try:
        yield
    finally:
        try:
            lock.release()
        except (RuntimeError, threading.ThreadError):
            logger.warning(f"[ER渲染] 释放锁 {lock_name} 时检测到异常状态，已忽略")


# 缓存用的哨兵对象，用于区分"缓存了None"和"没有缓存"
_SENTINEL_NONE = object()
# 预加载的猫爪原图（只从磁盘加载一次）
_PAW_BASE_ICON: Optional[Image.Image] = None
# RLock + 超时，防止死锁
_PAW_BASE_LOCK = threading.RLock()
_PAW_BASE_LOCK_TIMEOUT = LOCK.paw_base_lock_timeout

# 全局并发绘制池（用于提高CPU利用率）
_DRAWING_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="er_draw_thread")

class ERRenderer:
    # 并发渲染控制：限制同时进行的渲染任务数，避免CPU占满
    _render_semaphore = threading.Semaphore(RENDER.max_render_concurrency)
    # 信号量获取超时（秒）：渲染任务卡住时避免无限等待
    _RENDER_SEMAPHORE_TIMEOUT = LOCK.render_semaphore_timeout
    # 图标缓存：key = (path_str, size_w, size_h, method)
    _icon_cache: dict[tuple[str, int, int, str], Optional[Image.Image]] = {}
    _icon_cache_lock = threading.RLock()
    _ICON_CACHE_LOCK_TIMEOUT = LOCK.icon_cache_lock_timeout
    # 预渲染的猫爪水印（按尺寸+旋转+透明度缓存）
    _paw_watermark_cache: dict[tuple[int, int, float], Optional[Image.Image]] = {}
    _paw_cache_lock = threading.RLock()
    _PAW_CACHE_LOCK_TIMEOUT = LOCK.paw_cache_lock_timeout
    # 角色卡片渐变底色模板：key = (w, h)（固定尺寸，一次生成全局复用）
    _card_gradient_cache: dict[tuple[int, int], Image.Image] = {}
    _card_gradient_cache_lock = threading.RLock()
    # 武器圆形底图精灵：key = (radius, color)
    _dot_sprite_cache: dict[tuple[int, tuple[int, int, int]], Image.Image] = {}
    _dot_sprite_cache_lock = threading.RLock()
    # 发光边框精灵：key = (size, glow_color, border_color, radius, border, blur, pad, outside_only, gain)
    _glow_frame_cache: dict[tuple, Image.Image] = {}
    _glow_frame_cache_lock = threading.RLock()
    # 排行榜外部API数据缓存（main.py中使用，这里提供锁和缓存变量的引用）

    def __init__(self, assets, output_dir: Path, fonts_dir: Path):
        self.assets = assets
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.data_dir = self.output_dir.parent
        self.fonts_dir = Path(fonts_dir)

        self.W = RENDER.canvas_width
        self.PADDING = RENDER.padding

        self.HEADER_H = RENDER.header_height
        self.SUMMARY_H = RENDER.summary_height
        self.HEROES_H = RENDER.heroes_height
        self.MATCH_H = RENDER.match_height

        # MiSans 字体分层
        self.font_title = self._load_named_font("MiSans-Bold", RENDER.font_title_size)
        self.font_big = self._load_named_font("MiSans-Bold", RENDER.font_big_size)

        self.font = self._load_named_font("MiSans-Semibold", RENDER.font_module_size)
        self.font_small = self._load_named_font("MiSans-Medium", RENDER.font_body_size)

        self.font_tiny = self._load_named_font("MiSans-Regular", RENDER.font_small_size)
        self.font_mini = self._load_named_font("MiSans-Regular", RENDER.font_mini_size)

        # 韩文字体分层
        self.kr_font_title = self._load_named_font("NotoSansKR-Bold", RENDER.font_title_size)
        self.kr_font_big = self._load_named_font("NotoSansKR-Bold", RENDER.font_big_size)
        self.kr_font = self._load_named_font("NotoSansKR-SemiBold", RENDER.font_module_size)
        self.kr_font_small = self._load_named_font("NotoSansKR-Medium", RENDER.font_body_size)
        self.kr_font_tiny = self._load_named_font("NotoSansKR-Regular", RENDER.font_small_size)
        self.kr_font_mini = self._load_named_font("NotoSansKR-Regular", RENDER.font_mini_size)
        self.has_kr_font = self._font_file_exists("NotoSansKR-Bold")

        # ===== 白色主题 =====
        self.bg = RENDER.bg
        self.card = RENDER.bg
        self.card_2 = (255, 250, 252)

        self.text = (30, 30, 30)
        self.sub_text = (120, 120, 120)

        self.red = (220, 60, 60)
        self.blue = (83, 147, 202)

        self.gold = (200, 150, 40)

        self.white = (30, 30, 30)
        self.gray_line = (210, 210, 210)

        # 淡粉边框
        self.pink_border = RENDER.pink_border


        # ===== 主题层：配色 / 光效全部由 RENDER.theme 决定，布局坐标不动 =====
        self.style: ThemeStyle = resolve_theme(getattr(RENDER, "theme", "legacy"))
        if not self.style.legacy:
            # 把旧代码里直接引用 self.xxx 的颜色指向主题色，避免逐行改调用点
            self.text = self.style.text
            self.sub_text = self.style.sub_text
            self.red = self.style.red
            self.blue = self.style.blue
            self.gold = self.style.gold
            self.bg = self.style.bg
            self.card = self.style.card_fill[:3]
            self.card_2 = self.style.inner_card_fill[:3]
            self.pink_border = self.style.card_border[:3]
            self.gray_line = self.style.divider

    # =========================
    # 对外入口
    # =========================
    def render(self, data: dict[str, Any]) -> str:
        """渲染入口。使用带超时的信号量，避免渲染异常时所有线程卡死等待。"""
        try:
            with _acquire_timeout(
                ERRenderer._render_semaphore,
                ERRenderer._RENDER_SEMAPHORE_TIMEOUT,
                "render_semaphore",
            ):
                return self._render_inner(data)
        except _RendererLockTimeoutError as e:
            # 信号量获取超时：有可能前3个渲染任务全部卡住，
            # 这里降级为不限制并发直接渲染，避免用户请求彻底失败
            logger.warning(f"[ER渲染] 并发渲染信号量等待超时，降级为直接渲染: {e}")
            return self._render_inner(data)

    def _render_inner(self, data: dict[str, Any]) -> str:
        matches = data.get("matches", [])
        match_count = len(matches)

        # 首局队友详情面板（可选）：高度需在整图布局中预留，首局卡与后续卡之间插入
        first_detail = data.get("first_match_detail")
        detail_h = self._first_match_detail_h(first_detail) if first_detail else 0
        # 面板上移贴紧首局卡（-FLUSH_UP），并与第二局卡留出间隔（+AFTER_GAP）
        if detail_h > 0:
            detail_h += ERRenderer._TEAM_AFTER_GAP - ERRenderer._TEAM_FLUSH_UP

        heroes = data.get("common_heroes", [])
        hero_count = len(heroes)
        heroes_block_h = self._common_heroes_h(hero_count)

        FOOTER_H = RENDER.footer_height

        # 卡片之间用更紧凑的主题间距（统计/总览区更贴）
        gap = int(getattr(self.style, "card_gap", 0) or self.PADDING)
        # 总览卡片高度/它与统计卡片之间的间距都可按主题覆盖
        hdr_h = int(getattr(self.style, "header_h", 0) or self.HEADER_H)
        gap_head = int(getattr(self.style, "header_gap", 0) or gap)
        total_h = (
            self.PADDING
            + hdr_h
            + gap_head
            + self.SUMMARY_H
            + gap
            + heroes_block_h
            + gap
            + match_count * self.MATCH_H
            + detail_h
            + self.PADDING
            + FOOTER_H
        )

        img = self._new_canvas(total_h)
        draw = ImageDraw.Draw(img)

        mode_label = data.get("mode_label", "")

        # ========= 阶段1：绘制所有卡片填充（最底层） =========
        y_positions = {
            "header": self.PADDING,
            "summary": self.PADDING + hdr_h + gap_head,
            "common_heroes": self.PADDING + hdr_h + gap_head + self.SUMMARY_H + gap,
        }
        y_positions["matches"] = y_positions["common_heroes"] + heroes_block_h + gap

        future_header = _DRAWING_POOL.submit(
            self._draw_header, img, draw, data.get("player", {}), y_positions["header"], phase="fill"
        )
        future_summary = _DRAWING_POOL.submit(
            self._draw_summary, draw, data.get("summary", {}), y_positions["summary"], phase="fill", mode_label=mode_label
        )
        future_heroes = _DRAWING_POOL.submit(
            self._draw_common_heroes, img, draw, data.get("common_heroes", []), y_positions["common_heroes"], phase="fill", mode_label=mode_label
        )
        future_matches = _DRAWING_POOL.submit(
            self._draw_matches, img, draw, data.get("matches", []), y_positions["matches"], phase="fill",
            first_extra_h=detail_h, detail=first_detail,
        )

        # 等待所有填充任务完成
        future_header.result()
        future_summary.result()
        future_heroes.result()
        future_matches.result()

        # ========= 阶段2：猫爪水印（中间层 - 在卡片之上、文字之下） =========
        self._add_cat_paw_watermark(img)

        # ========= 阶段3：绘制边框 + 文字 + 图片（最顶层） =========
        future_header_top = _DRAWING_POOL.submit(
            self._draw_header, img, draw, data.get("player", {}), y_positions["header"], phase="top"
        )
        future_summary_top = _DRAWING_POOL.submit(
            self._draw_summary, draw, data.get("summary", {}), y_positions["summary"], phase="top", mode_label=mode_label
        )
        future_heroes_top = _DRAWING_POOL.submit(
            self._draw_common_heroes, img, draw, data.get("common_heroes", []), y_positions["common_heroes"], phase="top", mode_label=mode_label
        )
        future_matches_top = _DRAWING_POOL.submit(
            self._draw_matches, img, draw, data.get("matches", []), y_positions["matches"], phase="top",
            first_extra_h=detail_h, detail=first_detail,
        )

        future_header_top.result()
        future_summary_top.result()
        future_heroes_top.result()
        future_matches_top.result()

        # ========= 阶段4：页脚与辅助文字绘制 =========
        footer_text = "Powered by 椛椿&小休比"
        footer_font = self._load_named_font("MiSans-Regular", 16)
        bbox = draw.textbbox((0, 0), footer_text, font=footer_font)
        tw = bbox[2] - bbox[0]
        footer_x = (self.W - tw) / 2
        footer_y = y_positions["matches"] + match_count * self.MATCH_H + detail_h + 10
        draw.text(
            (footer_x, footer_y),
            footer_text,
            font=footer_font,
            fill=self.sub_text,
        )

        help_text = "输入 /指令 查询所有指令使用方法"
        help_font = self._load_named_font("MiSans-Regular", 14)
        help_bbox = draw.textbbox((0, 0), help_text, font=help_font)
        help_tw = help_bbox[2] - help_bbox[0]
        help_x = (self.W - help_tw) / 2
        help_y = footer_y + 22
        draw.text(
            (help_x, help_y),
            help_text,
            font=help_font,
            fill=self.sub_text,
        )

        # ========= 阶段5：保存图片 =========
        output_path = self.output_dir / f"er_profile_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.png"
        img = self._finish_canvas(img)
        img.convert("RGB").save(output_path, "PNG", optimize=False)
        
        return str(output_path)

    # =========================
    # 字体
    # =========================
    # 字体瘦身：仅保留 中文 Bold(玩家名) + Chinese Semibold(其他) + 韩文 Bold。
    # 其余被移除的权重统一映射到保留档，保证所有调用点都能命中字体文件、不退化到系统字体。
    _WEIGHT_ALIASES = {
        "MiSans-Medium": "MiSans-Semibold",
        "MiSans-Regular": "MiSans-Semibold",
        "NotoSansKR-Medium": "NotoSansKR-Bold",
        "NotoSansKR-Regular": "NotoSansKR-Bold",
        "NotoSansKR-SemiBold": "NotoSansKR-Bold",
    }

    def _canonical_font_stem(self, font_stem: str) -> str:
        return self._WEIGHT_ALIASES.get(font_stem, font_stem)

    def _load_named_font(self, font_stem: str, size: int):
        font_stem = self._canonical_font_stem(font_stem)
        candidates = [
            f"{font_stem}.ttf",
            f"{font_stem}.otf",
            f"{font_stem}.ttc",
        ]
        for name in candidates:
            path = self.fonts_dir / name
            if path.exists():
                try:
                    return ImageFont.truetype(str(path), size)
                except Exception:
                    pass
        return self._load_font(size)

    def _font_file_exists(self, font_stem: str) -> bool:
        candidates = [
            f"{font_stem}.ttf",
            f"{font_stem}.otf",
            f"{font_stem}.ttc",
        ]
        for name in candidates:
            if (self.fonts_dir / name).exists():
                return True
        return False

    def _load_font(self, size: int):
        candidates = [
            "msyh.ttc",
            "msyh.ttf",
            "Microsoft YaHei.ttf",
            "simhei.ttf",
            "simsun.ttc",
            "SourceHanSansCN-Regular.otf",
            "SourceHanSansSC-Regular.otf",
            "NotoSansCJKsc-Regular.otf",
        ]
        for name in candidates:
            font_path = self.fonts_dir / name
            if font_path.exists():
                try:
                    return ImageFont.truetype(str(font_path), size)
                except Exception:
                    pass
        try:
            return ImageFont.truetype("arial.ttf", size)
        except Exception:
            return ImageFont.load_default()

    @staticmethod
    def _has_korean(text: str) -> bool:
        if not text:
            return False
        for ch in text:
            code = ord(ch)
            if 0xAC00 <= code <= 0xD7A3 or 0x1100 <= code <= 0x11FF or 0x3130 <= code <= 0x318F:
                return True
        return False

    def _pick_font(self, text: str, base_font, kr_font):
        if self.has_kr_font and self._has_korean(text):
            return kr_font
        return base_font

    def _font_for_size(self, text: str, size: int) -> Any:
        if self._has_korean(text):
            kr = self._load_named_font("NotoSansKR-Regular", size)
            if kr is not None:
                return kr
        return self._load_named_font("MiSans-Regular", size)

    # =========================
    # 顶部
    # =========================
    def _draw_chart_panel(self, img: Image.Image, x: int, y: int, w: int, h: int) -> None:
        """折线图底板：白色柔光面板，让曲线从卡片底上"浮"起来。"""
        st = self.style
        top = getattr(st, "chart_panel", None)
        if top is None:
            return
        bottom = getattr(st, "chart_panel2", None) or top
        radius = int(getattr(st, "chart_radius", 16))
        sprite = rounded_gradient(w, h, radius, tuple(top[:4]), tuple(bottom[:4]))
        if sprite is not None:
            blend_at(img, sprite, x, y)

    def _draw_chart_frame(self, img: Image.Image, x: int, y: int, w: int, h: int) -> None:
        """折线图边框：强调色→暖色的渐变描边 + 柔和外发光（复合主题）。"""
        st = self.style
        c1 = getattr(st, "chart_frame", None)
        if c1 is None:
            return
        c2 = getattr(st, "chart_frame2", None) or c1
        radius = int(getattr(st, "chart_radius", 16))
        pad = 10
        sprite, p = border_layer(
            w, h, radius, int(getattr(st, "chart_frame_w", 2)),
            tuple(c1[:4]), tuple(c2[:4]),
            glow=tuple(c1[:3]) + (int(getattr(st, "chart_glow_alpha", 110)),),
            glow_blur=int(getattr(st, "chart_frame_blur", 6)),
            glow_gain=float(getattr(st, "chart_frame_gain", 1.5)),
            pad=pad,
        )
        blend_at(img, sprite, x - p, y - p)

    def _draw_header(self, img: Image.Image, draw: ImageDraw.ImageDraw, player: dict, y: int, phase: str = "all") -> int:
        x1 = self.PADDING
        x2 = self.W - self.PADDING
        header_h = int(getattr(self.style, "header_h", 0) or 190)
        y2 = y + header_h

        if phase in ("all", "fill"):
            self._draw_card(draw, x1, y, x2, y2, only_fill=True)
        if phase in ("all", "top"):
            self._draw_card(draw, x1, y, x2, y2, only_outline=True)

        if phase == "fill":
            return y2 + 12

        # ===== 左侧基础信息 =====
        name_x = x1 + 18
        name_y = y + 18

        player_name = player.get("name", "")
        draw.text(
            (name_x, name_y),
            player_name,
            font=self._pick_font(player_name, self.font_title, self.kr_font_title),
            fill=self.text,
        )

        draw.text(
            (name_x + 4, name_y + 58),
            f"Lv.{int(player.get('level', 0) or 0)}",
            font=self.font_small,
            fill=self.sub_text,
        )


        has_ranked_data = player.get("has_ranked_data", True)

        if has_ranked_data:
            draw.text(
                (name_x, name_y + 100),
                f"MMR: {int(player.get('mmr', 0) or 0)}",
                font=self.font_big,
                fill=self.text,
            )
        else:
            draw.text(
                (name_x, name_y + 100),
                "段位未鉴定",
                font=self.font_big,
                fill=self.sub_text,
            )

        immortal_mmr = player.get("Immortal_mmr", 0) or player.get("cutoff_mmr", {}).get("Immortal_mmr", 0)
        titan_mmr = player.get("Titan_mmr", 0) or player.get("cutoff_mmr", {}).get("Titan_mmr", 0)
        if has_ranked_data and (immortal_mmr > 0 or titan_mmr > 0):
            draw.text(
                (name_x, name_y + 140),
                f"永恒: {immortal_mmr:,} | 半神: {titan_mmr:,}",
                font=self.font_tiny,
                fill=self.sub_text,
            )

        # ===== 中间折线图 =====
        chart_x = x1 + int(getattr(self.style, "chart_inset_x", 215))
        chart_y = y + 12
        chart_w = int(getattr(self.style, "chart_w", 550))
        chart_h = int(getattr(self.style, "chart_h", 145))

        mmr_chart = player.get("mmr_chart", [])
        if has_ranked_data and mmr_chart:
            self._draw_chart_panel(img, chart_x - 6, chart_y - 6, chart_w + 20, chart_h + 32)
            try:
                self._draw_mmr_chart(
                    img=img,
                    draw=draw,
                    data=mmr_chart,
                    x=chart_x,
                    y=chart_y,
                    w=chart_w,
                    h=chart_h,
                )
            except TypeError:
                try:
                    self._draw_mmr_chart(
                        img=img,
                        draw=draw,
                        player=player,
                        x=chart_x,
                        y=chart_y,
                        w=chart_w,
                        h=chart_h,
                    )
                except Exception:
                    pass
            except Exception:
                pass
            self._draw_chart_frame(img, chart_x - 6, chart_y - 6, chart_w + 20, chart_h + 32)

        # ===== 右侧段位图标 =====
        tier_icon_path = player.get("tier_full") or player.get("tier_icon", "")
        tier_icon_size = int(getattr(self.style, "tier_icon_size", 0) or 96)
        tier_icon_x = x2 - 190
        tier_icon_y = y + 12

        icon_center_x = tier_icon_x + tier_icon_size / 2

        if has_ranked_data and tier_icon_path:
            if getattr(self.style, "tier_art_fit", False):
                # 段位大图是竖版（100x127），按比例装进 96 的框再水平居中
                tier_icon = self._load_icon_fixed(tier_icon_path, (tier_icon_size, tier_icon_size))
                if tier_icon:
                    self._paste_icon(
                        img, tier_icon,
                        (tier_icon_x + (tier_icon_size - tier_icon.size[0]) // 2, tier_icon_y),
                    )
            else:
                tier_icon = self._load_icon(tier_icon_path, (tier_icon_size, tier_icon_size))
                if tier_icon:
                    self._paste_icon(img, tier_icon, (tier_icon_x, tier_icon_y))

        # ===== 段位名称 =====
        tier_name = player.get("tier_name", "")

        if tier_name:
            _name_sz = int(getattr(self.style, "tier_name_size", 0) or 0)
            tier_font = (self._load_named_font("MiSans-Bold", _name_sz)
                         if _name_sz else self.font_big)
            bbox = draw.textbbox((0, 0), tier_name, font=tier_font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]

            tier_text_x = int(icon_center_x - text_w / 2)
            tier_text_y = (tier_icon_y + tier_icon_size
                           - int(getattr(self.style, "tier_name_gap", 10)))

            draw.text(
                (tier_text_x, tier_text_y),
                tier_name,
                font=tier_font,
                fill=self.text,
            )

            # ===== 未更新战绩标记 =====
            if not player.get("sync_updated", True):
                stale_font = self.font_mini
                stale_text = "未更新战绩"
                stale_bbox = draw.textbbox((0, 0), stale_text, font=stale_font)
                stale_w = stale_bbox[2] - stale_bbox[0]
                stale_x = int(icon_center_x - stale_w / 2)
                stale_y = tier_text_y + text_h + 4
                draw.text(
                    (stale_x, stale_y),
                    stale_text,
                    font=stale_font,
                    fill=self.style.stale,
                )

        # ===== 排名 =====
        global_rank = int(player.get("rank_global", 0) or 0)
        global_percent = float(player.get("rank_global_percent", 0.0) or 0.0)
        local_rank = int(player.get("rank_local", 0) or 0)
        local_percent = float(player.get("rank_local_percent", 0.0) or 0.0)

        _rank_sz = int(getattr(self.style, "tier_rank_size", 0) or 0)
        rank_font = (self._load_named_font("MiSans-Regular", _rank_sz)
                     if _rank_sz else self.font_tiny)

        global_text = f"Global {global_rank:,}名 (上位 {global_percent:.2f}%)" if global_rank > 0 else ""
        local_text = f"Asia {local_rank:,}名 (上位 {local_percent:.2f}%)" if local_rank > 0 else ""

        rank_y1 = y + int(getattr(self.style, "tier_rank_y", 138))
        rank_gap = int(getattr(self.style, "tier_rank_gap", 24))

        if global_text:
            g_bbox = draw.textbbox((0, 0), global_text, font=rank_font)
            g_w = g_bbox[2] - g_bbox[0]
            g_x = int(icon_center_x - g_w / 2)

            draw.text(
                (g_x, rank_y1),
                global_text,
                font=rank_font,
                fill=self.sub_text,
            )

        if local_text:
            l_bbox = draw.textbbox((0, 0), local_text, font=rank_font)
            l_w = l_bbox[2] - l_bbox[0]
            l_x = int(icon_center_x - l_w / 2)

            draw.text(
                (l_x, rank_y1 + rank_gap),
                local_text,
                font=rank_font,
                fill=self.sub_text,
            )

        return y2 + 12

    def _draw_mmr_chart(self, img: Image.Image, draw: ImageDraw.ImageDraw, data: list[dict], x: int, y: int, w: int, h: int) -> None:
        if not data or len(data) < 2:
            return

        values = []
        labels = []
        for item in data:
            try:
                values.append(float(item.get("value", 0)))
                labels.append(str(item.get("label", "")))
            except Exception:
                continue

        if len(values) < 2:
            return

        pad_left = 52
        pad_right = 10
        pad_top = 8
        pad_bottom = 2

        chart_x1 = x + pad_left
        chart_y1 = y + pad_top
        chart_x2 = x + w - pad_right
        chart_y2 = y + h - pad_bottom

        chart_w = chart_x2 - chart_x1
        chart_h = chart_y2 - chart_y1

        if chart_w <= 0 or chart_h <= 0:
            return

        v_min = min(values)
        v_max = max(values)

        if v_min == v_max:
            v_min -= 1
            v_max += 1

        span = v_max - v_min
        v_min -= span * 0.08
        v_max += span * 0.08
        span = v_max - v_min

        grid_count = 5
        label_font = self.font_mini

        for i in range(grid_count):
            ratio = i / (grid_count - 1)
            yy = chart_y1 + chart_h * ratio
            value = v_max - span * ratio

            draw.line(
                [(chart_x1, yy), (chart_x2, yy)],
                fill=self.style.chart_grid,
                width=1,
            )

            text = f"{int(round(value)):,}"
            bbox = draw.textbbox((0, 0), text, font=label_font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]

            draw.text(
                (chart_x1 - tw - 8, yy - th / 2),
                text,
                font=label_font,
                fill=self.style.chart_label,
            )

        points = []
        n = len(values)
        for i, v in enumerate(values):
            if n == 1:
                px = chart_x1 + chart_w / 2
            else:
                px = chart_x1 + chart_w * i / (n - 1)

            py = chart_y2 - ((v - v_min) / span) * chart_h
            points.append((px, py))

        if not self.style.legacy:
            self._draw_mmr_series_themed(img, draw, points, x, y, w, h, chart_y2)
        else:
            draw.line(points, fill=(193, 132, 83), width=4)

            r = 5
            for px, py in points:
                draw.ellipse((px - r, py - r, px + r, py + r), fill=(193, 132, 83))

        for (px, _py), label in zip(points, labels):
            bbox = draw.textbbox((0, 0), label, font=label_font)
            tw = bbox[2] - bbox[0]

            draw.text(
                (px - tw / 2, chart_y2 + 6),
                label,
                font=label_font,
                fill=self.style.chart_label,
            )

    # =========================
    # 总览
    # =========================
    def _draw_summary(self, draw: ImageDraw.ImageDraw, summary: dict, y: int, phase: str = "all", mode_label: str = "") -> int:
        x1 = self.PADDING
        x2 = self.W - self.PADDING
        y2 = y + self.SUMMARY_H
        if phase in ("all", "fill"):
            self._draw_card(draw, x1, y, x2, y2, only_fill=True)
            simg = ERRenderer._card_img(draw)
            if simg is not None:
                self._draw_section_header(simg, x1 + 1, y + 1, x2 - 1, 32)
        if phase in ("all", "top"):
            self._draw_card(draw, x1, y, x2, y2, only_outline=True)
        if phase == "fill":
            return y2

        # 标题
        title = f"{mode_label}统计" if mode_label and mode_label != "排位" else "RANK统计"
        draw.text(
            (x1 + 18, y + 8),
            title,
            font=self.font_small,
            fill=self.text,
        )

        stats = [
            ("平均TK", f"{summary.get('avg_tk', 0)}"),
            ("胜率", f"{summary.get('win_rate', 0)}%"),
            ("游戏场次", f"{summary.get('play', 0)}"),

            ("平均击杀", f"{summary.get('avg_kill', 0)}"),
            ("TOP 2", f"{summary.get('top2_rate', 0)}%"),
            ("平均伤害", f"{summary.get('avg_damage', 0)}"),

            ("平均助攻", f"{summary.get('avg_assist', 0)}"),
            ("TOP 3", f"{summary.get('top3_rate', 0)}%"),
            ("平均排名", f"#{summary.get('avg_rank', 0)}"),

            ("平均狩猎野生动物", f"{summary.get('avg_monster_kill', 0)}"),
            ("平均获得荣誉值", f"{int(summary.get('avg_vf_credit', 0) or 0):,}"),
            ("平均视野贡献", f"{summary.get('avg_view_contribution', 0)}"),
        ]

        start_x = x1 + 18
        start_y = y + 36
        col_w = 330
        row_h = 36

        dot_color = self.style.dot

        for i, (label, value) in enumerate(stats):
            col = i % 3
            row = i // 3

            text_x = start_x + col * col_w
            text_y = start_y + row * row_h

            dot_x = text_x
            dot_y = text_y + 6
            draw.ellipse(
                (dot_x, dot_y, dot_x + 8, dot_y + 8),
                fill=dot_color,
            )

            draw.text(
                (text_x + 14, text_y),
                f"{label}：{value}",
                font=self.font_small,
                fill=self.text,
            )

        return y2

    # =========================
    # 常用英雄
    # =========================
    # ---------- 卡片标题底（柔光渐变） ----------
    def _draw_section_header(self, img: Image.Image, x1: int, y: int, x2: int,
                             height: int) -> None:
        """标题底：横向强调色→暖色渐变，再自上而下淡出，只贴合卡片顶部圆角。"""
        st = self.style
        left = getattr(st, "section_header", None)
        if left is None:
            return
        right = getattr(st, "section_header2", None) or left
        w = x2 - x1 + 1
        h = int(height)
        if w <= 2 or h <= 2:
            return
        ramp = Image.linear_gradient("L").resize((w, 1), Image.BILINEAR)
        base = ImageOps.colorize(ramp, tuple(left[:3]), tuple(right[:3])).convert("RGBA")
        base = base.resize((w, h), Image.BILINEAR)
        # 横向柔光：左侧起亮 → 约 35% 处最亮 → 向右衰减；竖向只留很轻的衰减
        peak = float(left[3])
        def _h_alpha(v):
            t = v / 255.0
            if t < 0.35:
                k = 0.25 + 0.75 * (t / 0.35)
            else:
                k = max(0.0, 1.0 - 0.75 * (t - 0.35) / 0.65)
            return int(round(peak * k))
        alpha = ramp.point(_h_alpha).resize((w, h), Image.BILINEAR)
        vfade = Image.linear_gradient("L").resize((1, h), Image.BILINEAR)
        vfade = vfade.point(lambda v: int(round(255 * (1.0 - 0.35 * v / 255.0))))
        alpha = ImageChops.multiply(alpha, vfade.resize((w, h), Image.BILINEAR))
        base.putalpha(alpha)
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, w - 1, h - 1), radius=max(0, int(st.card_radius)), fill=255)
        base.putalpha(ImageChops.multiply(base.getchannel("A"), mask))
        blend_at(img, base, x1, y)

    # ---------- 常用英雄（列头同行版） ----------
    _HERO_ROW_H = 64
    _HERO_AVATAR = 50

    def _common_heroes_h(self, n: int) -> int:
        if n <= 0:
            return 120
        if self.style.hero_header:
            return 48 + n * ERRenderer._HERO_ROW_H + 16
        return 48 + n * 92 + 20

    def _draw_common_heroes_themed(self, img: Image.Image, draw: ImageDraw.ImageDraw,
                                   heroes: List[dict], y: int, phase: str = "all",
                                   mode_label: str = "") -> int:
        """列名与「常用英雄」标题同一行，数据行只写数值。"""
        n = len(heroes)
        row_h = ERRenderer._HERO_ROW_H
        x1 = self.PADDING
        x2 = self.W - self.PADDING
        block_h = self._common_heroes_h(n)
        y2 = y + block_h
        is_rank = (mode_label == "排位")
        start_y = y + 48

        if phase in ("all", "fill"):
            self._draw_card(draw, x1, y, x2, y2, only_fill=True)
            himg = ERRenderer._card_img(draw)
            if himg is not None:
                self._draw_section_header(himg, x1 + 1, y + 1, x2 - 1, 44)
        if phase in ("all", "top"):
            self._draw_card(draw, x1, y, x2, y2, only_outline=True)
        if phase == "fill":
            return y2

        draw.text((x1 + 20, y + 12), "常用英雄", font=self.font, fill=self.text)

        # 行与行之间只用一条细分割线（不再做卡片）
        line_color = getattr(self.style, "team_row_divider", None) or self.style.divider
        for i in range(1, n):
            ly = start_y + i * row_h - 3
            blend_at(img, Image.new("RGBA", ((x2 - x1) - 56, 1), line_color), x1 + 28, ly)

        # 列名与标题同一行（右侧按数值列对齐）
        cols = [
            (x1 + 250, "场次 / 胜率"),
            (x1 + 400, "场均排名"),
            (x1 + 540, "平均击杀 / 助攻"),
            (x1 + 720, "平均伤害"),
        ]
        if is_rank:
            cols.append((x1 + 880, "RP"))
        for cx, label in cols:
            draw.text((cx, y + 26), label, font=self.font_tiny,
                      fill=self.sub_text, anchor="lm")

        avatar = ERRenderer._HERO_AVATAR
        inset = 4
        inner = avatar - 2 * inset
        for i, hero in enumerate(heroes):
            ry1 = start_y + i * row_h
            mid = ry1 + (row_h - 6) // 2
            ax = x1 + 24
            ay = mid - avatar // 2

            self._draw_char_border(img, draw, ax, ay, avatar)
            icon = self._load_character_icon(hero.get("character_icon", ""), (inner, inner))
            self._paste_icon_rounded(img, icon, (ax + inset, ay + inset),
                                     self._avatar_radius(inner))

            draw.text((x1 + 88, mid), str(hero.get("character_name", "")),
                      font=self.font_small, fill=self.text, anchor="lm")
            draw.text((x1 + 250, mid),
                      f"{hero.get('play', 0)}场 | {hero.get('win_rate', 0)}%",
                      font=self.font_small, fill=self.text, anchor="lm")
            draw.text((x1 + 400, mid), f"{hero.get('avg_rank', 0)}",
                      font=self.font_small, fill=self.text, anchor="lm")
            draw.text((x1 + 540, mid),
                      f"K {hero.get('avg_kill', 0)} / A {hero.get('avg_assist', 0)}",
                      font=self.font_small, fill=self.text, anchor="lm")
            draw.text((x1 + 720, mid), f"{hero.get('avg_damage', 0)}",
                      font=self.font_small, fill=self.text, anchor="lm")
            if is_rank:
                gain = hero.get("mmr_gain", 0)
                if gain > 0:
                    txt, col = f"+{gain}", self.red
                elif gain < 0:
                    txt, col = f"{gain}", self.blue
                else:
                    txt, col = f"{gain}", self.text
                draw.text((x1 + 880, mid), txt, font=self.font_small,
                          fill=col, anchor="lm")
        return y2

    def _apply_team_damage_scale(self, lead_match: dict, detail: dict) -> None:
        """首局伤害条的分母 = 首局队伍三人（本人 + 队友）输出的最大值。"""
        members = detail.get("members") or []
        damages = []
        for m in members:
            entry = m.get("entry") or {}
            dmg = int(entry.get("damage", 0) or 0)
            if dmg > 0:
                damages.append(dmg)
        if not damages:
            return
        team_max = max(damages)
        lead_match["team_max_damage"] = team_max
        for m in members:
            entry = m.get("entry")
            if isinstance(entry, dict):
                entry["team_max_damage"] = team_max

    def _draw_common_heroes(self, img: Image.Image, draw: ImageDraw.ImageDraw, heroes: List[dict], y: int, phase: str = "all", mode_label: str = "") -> int:
        if self.style.hero_header:
            return self._draw_common_heroes_themed(img, draw, heroes, y, phase, mode_label)
        hero_count = len(heroes)
        row_h = 82
        block_h = 48 + hero_count * row_h + 16 if hero_count > 0 else 120

        x1 = self.PADDING
        x2 = self.W - self.PADDING
        y2 = y + block_h
        if phase in ("all", "fill"):
            self._draw_card(draw, x1, y, x2, y2, only_fill=True)
            # fill 阶段也绘制内卡片填充，让水印处于所有卡片填充之上
            start_y = y + 48
            for i in range(hero_count):
                row_y1 = start_y + i * row_h
                row_y2 = row_y1 + row_h - 8
                self._draw_inner_card(draw, x1 + 14, row_y1, x2 - 14, row_y2)
        if phase in ("all", "top"):
            self._draw_card(draw, x1, y, x2, y2, only_outline=True)
        if phase == "fill":
            return y2

        draw.text((x1 + 20, y + 12), "常用英雄", font=self.font, fill=self.text)

        start_y = y + 48

        # 收集所有需要并行加载的图标任务
        icon_tasks = []
        icon_positions = []  # 存储每个英雄图标的绘制位置
        for i, hero in enumerate(heroes):
            row_y1 = start_y + i * row_h
            row_y2 = row_y1 + row_h - 8

            if i < hero_count - 1:
                line_y = row_y2 + 4
                draw.line(
                    [(x1 + 30, line_y), (x2 - 30, line_y)],
                    fill=self.style.divider,
                    width=2,
                )

            is_rank = (mode_label == "排位")

            avatar_x = x1 + 24
            avatar_y = row_y1 + 12

            name_x = x1 + 92
            play_x = x1 + 250
            rank_x = x1 + 400
            ka_x = x1 + 540
            dmg_x = x1 + 720
            mmr_x = x1 + 880

            top_y = row_y1 + 12
            sub_y = row_y1 + 40

            hero_avatar_size = 58
            hero_avatar_inset = 5
            icon_size = hero_avatar_size - 2 * hero_avatar_inset
            
            # 记录图标加载任务及其位置
            character_icon_path = hero.get("character_icon", "")
            icon_tasks.append((i, character_icon_path, (icon_size, icon_size)))
            icon_positions.append((avatar_x + hero_avatar_inset, avatar_y + hero_avatar_inset))

            self._draw_char_border(img, draw, avatar_x, avatar_y, hero_avatar_size)

            draw.text(
                (name_x, top_y),
                hero.get("character_name", ""),
                font=self.font_small,
                fill=self.text,
            )
            draw.text(
                (name_x, sub_y),
                "英雄",
                font=self.font_tiny,
                fill=self.sub_text,
            )

            draw.text(
                (play_x, top_y),
                f"{hero.get('play', 0)}场 | {hero.get('win_rate', 0)}%",
                font=self.font_small,
                fill=self.text,
            )
            draw.text(
                (play_x, sub_y),
                "场次 / 胜率",
                font=self.font_tiny,
                fill=self.sub_text,
            )

            draw.text(
                (rank_x, top_y),
                f"{hero.get('avg_rank', 0)}",
                font=self.font_small,
                fill=self.text,
            )
            draw.text(
                (rank_x, sub_y),
                "场均排名",
                font=self.font_tiny,
                fill=self.sub_text,
            )

            draw.text(
                (ka_x, top_y),
                f"K {hero.get('avg_kill', 0)} / A {hero.get('avg_assist', 0)}",
                font=self.font_small,
                fill=self.text,
            )
            draw.text(
                (ka_x, sub_y),
                "平均击杀 / 助攻",
                font=self.font_tiny,
                fill=self.sub_text,
            )

            draw.text(
                (dmg_x, top_y),
                f"{hero.get('avg_damage', 0)}",
                font=self.font_small,
                fill=self.text,
            )
            draw.text(
                (dmg_x, sub_y),
                "平均伤害",
                font=self.font_tiny,
                fill=self.sub_text,
            )

            if is_rank:
                mmr_gain = hero.get('mmr_gain', 0)
                if mmr_gain > 0:
                    mmr_text = f"+{mmr_gain}"
                    mmr_color = self.red
                elif mmr_gain < 0:
                    mmr_text = f"{mmr_gain}"
                    mmr_color = self.blue
                else:
                    mmr_text = f"{mmr_gain}"
                    mmr_color = self.text

                draw.text(
                    (mmr_x, top_y),
                    mmr_text,
                    font=self.font_small,
                    fill=mmr_color,
                )
                draw.text(
                    (mmr_x, sub_y),
                    "RP",
                    font=self.font_tiny,
                    fill=self.sub_text,
                )

        # 并行加载所有图标
        if icon_tasks:
            futures = {}
            for idx, path, size in icon_tasks:
                futures[idx] = _DRAWING_POOL.submit(self._load_character_icon, path, size)
            
            # 等待并绘制所有加载完成的图标
            for idx, future in futures.items():
                icon = future.result()
                if icon:
                    pos = icon_positions[idx]
                    self._paste_icon_rounded(img, icon, pos, self._avatar_radius(58))

        return y2

    # =========================
    # 战绩列表
    # =========================
    def _draw_matches(self, img: Image.Image, draw: ImageDraw.ImageDraw, matches: List[dict], y: int, phase: str = "all",
                      first_extra_h: int = 0, detail: Optional[dict] = None) -> int:
        """绘制战绩列表。首局后若有队友详情面板（detail），其下方战绩整体下移 first_extra_h。"""
        # 一体化模式：首局卡向下延伸，把「主玩家 + 队友」包进同一张卡（只留外框）
        extra = 0
        if detail is not None and matches and self.style.team_integrated:
            extra = self._first_match_detail_h(detail)
            # 首局伤害条分母 = 队伍三人输出最大值（不是整卡最大值）
            self._apply_team_damage_scale(matches[0], detail)
        futures = {}
        for idx, match in enumerate(matches):
            row_y = y + idx * self.MATCH_H + (first_extra_h if idx >= 1 else 0)
            futures[_DRAWING_POOL.submit(
                self._draw_single_match, img, draw, match, row_y, phase=phase,
                card_extra_h=(extra if idx == 0 else 0),
            )] = idx

        # 等待所有战绩卡绘制完成（按区域隔离，可安全并行）
        for future in futures:
            future.result()

        # 首局卡下方绘制队友详情（上移贴紧首局卡底部）
        if detail is not None and matches:
            self._draw_match_teammates(img, draw, detail, y + self.MATCH_H - ERRenderer._TEAM_FLUSH_UP, phase=phase)

        return y + len(matches) * self.MATCH_H + first_extra_h

    # ============================================================
    # 首局队友详情（淡粉铺底外层面板 + 白色带边框的队友战绩卡）
    # ============================================================
    _TEAM_CAPTION_H = 30
    """面板顶部标题栏高度"""
    _TEAM_HEADER_EXTRA = 16
    """标题栏下方：间隙(6) + 分割线(2) + 间隙(8)"""
    _TEAM_CARD_GAP = 6
    """队友卡之间的垂直间隙"""
    _TEAM_BOTTOM_PAD = 8
    """面板底部留白"""
    _TEAM_PANEL_INSET = 8
    """外层面板相对主卡的水平内缩"""
    _TEAM_FLUSH_UP = 8
    """面板上移量：贴紧首局卡底部（首局卡与下一行原有 8px 间隙）"""
    _TEAM_AFTER_GAP = 10
    """面板底部与第二局卡之间的间隔"""
    _TEAM_INT_DIV_GAP = 8
    """一体化模式：队友行与细分割线之间的间距"""
    _TEAM_INT_BOTTOM_PAD = 10
    """一体化模式：最后一行队友与卡底之间的距离"""
    _TEAM_SUB_CROP_TOP = 10
    """一体化模式：队友行上下各裁掉的像素（压掉行内多余留白）"""
    _TEAM_SUB_CROP_BOTTOM = 10

    def _teammate_scaled_size(self) -> tuple[int, int]:
        """队友卡等比缩放后的 (宽, 高)。

        一体化模式：宽度贴齐主卡内缩 28px，队友直接落在主卡内部；
        旧模式：宽度贴齐淡粉面板（略窄 12px）。
        两者缩放比 = 目标宽 / 原卡宽，高度按同一比例计算，保证等比缩小。
        """
        card_w = self.W - 2 * self.PADDING
        row_h = self.MATCH_H
        if self.style.team_integrated:
            indent = int(getattr(self.style, "team_sub_inset", 0))
            sub_scale = float(getattr(self.style, "team_sub_scale", 0) or 0)
            row_h = (self.MATCH_H
                     - ERRenderer._TEAM_SUB_CROP_TOP
                     - ERRenderer._TEAM_SUB_CROP_BOTTOM)
            if sub_scale > 0:
                # 指定比例：1.0 即与主体同尺寸（100% 不缩放）
                scaled_w = max(1, int(round(card_w * sub_scale)))
                slot_h = max(1, int(round(row_h * min(1.0, sub_scale))))
                return scaled_w, slot_h
            scaled_w = max(1, card_w - 28 - 2 * indent)
        else:
            panel_w = card_w - 2 * ERRenderer._TEAM_PANEL_INSET
            scaled_w = max(1, panel_w - 12)
        scale = scaled_w / card_w
        slot_h = max(1, int(round(row_h * scale)))
        return scaled_w, slot_h

    def _teammate_slot_h(self) -> int:
        """单张队友卡缩放后的占位高度（等比，与宽度同缩放比）。"""
        return self._teammate_scaled_size()[1]

    def _first_match_detail_h(self, detail: dict) -> int:
        """队友详情块高度。

        旧模式 = 面板（标题栏+分割区）+ (n-1) 张缩小队友卡（含间隙）+ 底部留白；
        一体化模式 = (n-1) 行「细分割线 + 队友行」+ 底部留白（没有标题栏/面板）。
        """
        n = len(detail.get("members") or [])
        if n < 2:
            return 0
        if self.style.team_integrated:
            rows = n - 1
            div_block = 1 + 2 * ERRenderer._TEAM_INT_DIV_GAP
            return rows * (div_block + self._teammate_slot_h()) + ERRenderer._TEAM_INT_BOTTOM_PAD
        cards_h = (n - 1) * self._teammate_slot_h() + (n - 2) * ERRenderer._TEAM_CARD_GAP
        return (
            ERRenderer._TEAM_CAPTION_H
            + ERRenderer._TEAM_HEADER_EXTRA
            + cards_h
            + ERRenderer._TEAM_BOTTOM_PAD
        )

    _ROLE_BAR_H = 112
    """主/次体左侧色条高度（行内垂直居中）"""

    def _draw_lead_wash(self, img: Image.Image, x1: int, y: int, x2: int, y2: int,
                        square_bottom: bool = False) -> None:
        """主体行底色：极淡的强调色，顶部圆角贴合卡片；一体化时底部保持直角。"""
        st = self.style
        w = x2 - x1 - 1
        h = y2 - y - 1
        if w <= 0 or h <= 0:
            return
        r = max(0, int(st.card_radius) - 2)
        color = st.team_lead_wash
        # 自上而下淡出，避免在主卡里再出现一块"小卡片"
        fade = (color[0], color[1], color[2], 0)
        sprite = rounded_gradient(w, h, r, color, fade)
        if sprite is None:
            return
        blend_at(img, sprite, x1 + 1, y + 1)

    def _draw_rank_bar(self, img: Image.Image, x: int, row_y: int,
                       color, width: int) -> None:
        """名次色左条：主色取自名次文字，下端过渡到主题暖色（样式与主条一致）。"""
        st = self.style
        warm = tuple((getattr(st, "team_lead_bar2", None) or (255, 172, 120))[:3])
        c = tuple(color)[:3]
        end = tuple(int(c[k] * 0.60 + warm[k] * 0.40) for k in range(3))
        h = ERRenderer._ROLE_BAR_H
        y0 = row_y + (self.MATCH_H - 8 - h) // 2
        sprite = rounded_gradient(max(1, int(width)), h, max(1, int(width) // 2),
                                  c + (255,), end + (255,))
        if sprite is not None:
            blend_at(img, sprite, x, y0)

    def _draw_role_bar(self, img: Image.Image, x: int, row_y: int,
                       color: tuple, width: int) -> None:
        """行首竖向色条：主体粗且饱和，次体细且淡，用来一眼区分主次。"""
        h = ERRenderer._ROLE_BAR_H
        y0 = row_y + (self.MATCH_H - 8 - h) // 2
        c2 = None
        if color is self.style.team_lead_bar:
            c2 = getattr(self.style, "team_lead_bar2", None)
        sprite = rounded_gradient(max(1, int(width)), h, max(1, int(width) // 2),
                                  color, c2 or color)
        blend_at(img, sprite, x, y0)

    def _draw_team_divider(self, img: Image.Image, draw: ImageDraw.ImageDraw,
                           x1: int, x2: int, y: int, color=None) -> None:
        """一体化模式下的队友分割线：1px，左右各内缩 20px，颜色取主题 divider。"""
        st = self.style
        left, right = x1 + 20, x2 - 20
        if right <= left:
            return
        if st.legacy:
            draw.rectangle((left, y, right, y), fill=(255, 215, 225))
        else:
            line_color = color or getattr(st, "team_row_divider", None) or st.team_divider
            blend_at(img, Image.new("RGBA", (right - left + 1, 1), line_color), left, y)

    def _draw_match_teammates(self, img: Image.Image, draw: ImageDraw.ImageDraw, detail: dict, y: int, phase: str = "all") -> None:
        """首局队友：两种呈现方式。

        一体化（team_integrated）：队友直接落在首局卡内部，没有各自的卡边框/底色，
        主玩家与队友、队友与队友之间只用一条 1px 分割线，整块共用外框；
        旧模式：淡粉面板 + 白色带边框的缩小队友卡。
        """
        members = detail.get("members") or []
        n = len(members)
        if n < 2:
            return
        cap = ERRenderer._TEAM_CAPTION_H
        x1 = self.PADDING
        x2 = self.W - self.PADDING

        # 队友卡等比缩放尺寸（宽度贴齐容器，高度同比例）
        scaled_w, slot_h = self._teammate_scaled_size()

        if self.style.team_integrated:
            if phase not in ("all", "top"):
                return
            row_y = y + ERRenderer._TEAM_INT_DIV_GAP
            paste_x = x1 + (x2 - x1 - scaled_w) // 2
            last = len(members) - 2
            strong = getattr(self.style, "team_row_divider_strong", None)
            for i, m in enumerate(members[1:]):
                self._draw_team_divider(img, draw, x1, x2, row_y,
                                        color=(strong if (i == 0 and strong) else None))
                row_y += 1 + ERRenderer._TEAM_INT_DIV_GAP
                # 无卡：透明白底渲染，直接叠在主卡同款渐变上
                tmp = Image.new("RGBA", (self.W, self.MATCH_H), (0, 0, 0, 0))
                tmp_draw = ImageDraw.Draw(tmp)
                self._draw_single_match(
                    tmp, tmp_draw, m["entry"], 0, phase="all",
                    with_card=False, show_meta=False, show_rank_mode=False, role="sub",
                )
                card = tmp.crop((x1, ERRenderer._TEAM_SUB_CROP_TOP, x2,
                                 self.MATCH_H - ERRenderer._TEAM_SUB_CROP_BOTTOM))
                card = card.resize((scaled_w, slot_h), Image.LANCZOS)
                op = float(self.style.team_sub_opacity)
                if op < 0.999:
                    card.putalpha(card.getchannel("A").point(lambda v, o=op: int(v * o)))
                img.paste(card, (paste_x, row_y), card)
                row_y += slot_h
                if i < last:
                    row_y += ERRenderer._TEAM_INT_DIV_GAP
            return

        # 外层面板：比主卡左右各内缩 8px，淡粉色铺底
        inset = ERRenderer._TEAM_PANEL_INSET
        px1 = x1 + inset
        px2 = x2 - inset
        panel_w = px2 - px1
        panel_y2 = y + self._first_match_detail_h(detail)

        if phase in ("all", "fill"):
            # 面板白色铺底（圆角与主卡一致）
            if self.style.legacy:
                draw.rounded_rectangle((px1, y, px2, panel_y2), radius=18, fill=(255, 255, 255))
                # 仅头部标题栏保留淡粉色（顶部随面板圆角，底部补直角）
                draw.rounded_rectangle((px1, y, px2, y + cap), radius=18, fill=(255, 238, 244))
                draw.rectangle((px1, y + cap - 18, px2, y + cap), fill=(255, 238, 244))
            else:
                st = self.style
                pimg = ERRenderer._card_img(draw)
                pw = px2 - px1 + 1
                ph = panel_y2 - y + 1
                blend_at(pimg, rounded_gradient(pw, ph, st.card_radius, st.team_panel, st.team_panel), px1, y)
                blend_at(pimg, rounded_gradient(pw, cap, st.card_radius, st.team_header, st.team_header), px1, y)
                blend_at(pimg, Image.new("RGBA", (pw, 18), st.team_header), px1, y + cap - 18)

        if phase in ("all", "top"):
            # 面板边框（与主卡同款 2px 淡粉）
            if self.style.legacy:
                draw.rounded_rectangle((px1, y, px2, panel_y2), radius=18, outline=self.pink_border, width=2)
            else:
                st = self.style
                pimg = ERRenderer._card_img(draw)
                sprite, pad = border_layer(px2 - px1 + 1, panel_y2 - y + 1, st.card_radius,
                                           int(st.card_border_w), st.card_border,
                                           st.card_border_bottom or st.card_border, pad=0)
                blend_at(pimg, sprite, px1 - pad, y - pad)
            # 标题栏下方分割线
            div_y = y + cap + 6
            if self.style.legacy:
                draw.rectangle((px1, div_y, px2, div_y + 2), fill=(255, 215, 225))
            else:
                blend_at(ERRenderer._card_img(draw),
                         Image.new("RGBA", (px2 - px1 + 1, 2), self.style.team_divider),
                         px1, div_y)
            # 头部仅保留下拉箭头与右侧副信息（排名 · 模式）
            draw.text((px1 + 14, y + 6), "▼", font=self.font_small, fill=self.text)
            sub = f"{'逃生' if detail.get('escaped') else '#' + str(detail.get('rank', 0))} · {detail.get('mode_label', '')}"
            sb = draw.textbbox((0, 0), sub, font=self.font_tiny)
            draw.text((px2 - 14 - (sb[2] - sb[0]), y + 8), sub, font=self.font_tiny, fill=self.sub_text)

        if phase not in ("all", "top"):
            return

        row_y = y + cap + ERRenderer._TEAM_HEADER_EXTRA
        paste_x = px1 + (panel_w - scaled_w) // 2
        for m in members[1:]:
            # 在与主玩家同尺寸的透明画布上渲染战绩卡（白色铺底+边框、无时间/服务器/ID），
            # 再整体缩小贴入淡粉面板，保证内部布局与主玩家完全一致
            tmp = Image.new("RGBA", (self.W, self.MATCH_H), (0, 0, 0, 0))
            tmp_draw = ImageDraw.Draw(tmp)
            self._draw_single_match(
                tmp, tmp_draw, m["entry"], 0, phase="all",
                card_fill=self.style.team_card_fill, show_meta=False, show_rank_mode=False,
            )
            card = tmp.crop((x1, 0, x2, self.MATCH_H))
            card = card.resize((scaled_w, slot_h), Image.LANCZOS)
            img.paste(card, (paste_x, row_y), card)
            row_y += slot_h + ERRenderer._TEAM_CARD_GAP

    def _draw_single_match(self, img: Image.Image, draw: ImageDraw.ImageDraw, match: dict, y: int, phase: str = "all", with_card: bool = True, card_fill=None, show_meta: bool = True, show_rank_mode: bool = True, card_extra_h: int = 0, role: str = "lead") -> int:
        x1 = self.PADDING
        x2 = self.W - self.PADDING
        # card_extra_h：一体化模式下首局卡向下延伸，把队友行一起框住
        y2 = y + self.MATCH_H - 8 + card_extra_h

        if with_card:
            if phase in ("all", "fill"):
                self._draw_card(draw, x1, y, x2, y2, only_fill=True, fill=card_fill)
            if phase in ("all", "top"):
                self._draw_card(draw, x1, y, x2, y2, only_outline=True)

        # 主次分层：主体铺极淡强调色底（左条在名次文字画完后按名次色绘制）；次体靠缩进 + 缩小
        ins = int(getattr(self.style, "row_inset", 0) or 0)
        if (role == "lead" and getattr(self.style, "team_lead_wash", None) is not None
                and phase in ("all", "top")):
            self._draw_lead_wash(img, x1, y, x2, y + self.MATCH_H - 8,
                                 square_bottom=card_extra_h > 0)

        if phase == "fill":
            return y + self.MATCH_H

        rank = int(match.get("rank", 0) or 0)
        kills = int(match.get("kills", 0) or 0)
        deaths = int(match.get("deaths", 0) or 0)
        assists = int(match.get("assists", 0) or 0)
        team_kill = int(match.get("team_kill", 0) or 0)
        damage = int(match.get("damage", 0) or 0)
        mmr_after = match.get("mmr_after")
        mmr_change = int(match.get("mmr_change", 0) or 0)

        is_rank_match = bool(match.get("is_rank_match", False))
        mode_label = str(match.get("mode_label", "") or "")

        tkka_text = f"{team_kill}/{kills}/{assists}"

        if deaths == 0:
            kda_value = round(kills + assists, 2)
        else:
            kda_value = round((kills + assists) / max(deaths, 1), 2)

        avatar_x = x1 + 14 + ins
        if show_rank_mode:
            avatar_y = y + 32
        else:
            # 队友卡：头像+角色名整体基于卡片上下居中
            avatar_y = y + max(0, (self.MATCH_H - 8 - 72 - 14) // 2)

        time_x = x1 + 12 + ins
        time_y = y + 6

        rank_x = x1 + 92 + ins
        rank_y = y + 34

        change_x = x1 + 150 + ins
        change_y = y + 38

        stat_x = x1 + 92 + ins
        stat_y = y + 66

        label_x = x1 + 92 + ins
        label_y = y + 100

        icon_x = x1 + 225 + ins
        icon_y = y + 28
        trait_row_y = y + 82

        damage_x = x1 + 360 + ins
        kda_x = x1 + 505 + ins
        rp_x = x1 + 625 + ins

        value_y = y + 42
        sub_y = y + 82

        item_x = x2 - int(getattr(self.style, "item_right_inset", 250))
        item_y = y + 22

        # ===== 左侧头像 =====
        avatar_size = 72
        avatar_inset = 5
        icon_size = avatar_size - 2 * avatar_inset
        hero_icon = self._load_character_icon(match.get("character_icon", ""), (icon_size, icon_size))
        if hero_icon:
            self._paste_icon_rounded(img, hero_icon, (avatar_x + avatar_inset, avatar_y + avatar_inset),
                                     self._avatar_radius(avatar_size))

        self._draw_char_border(img, draw, avatar_x, avatar_y, avatar_size)

        # ===== 头像下方：实验体名字 =====
        character_name = str(match.get("character_name", "") or "")
        if character_name:
            name_font = self.font_tiny
            bbox = draw.textbbox((0, 0), character_name, font=name_font)
            text_w = bbox[2] - bbox[0]

            name_x = int(avatar_x + avatar_size / 2 - text_w / 2)
            name_y = avatar_y + avatar_size + 1

            draw.text(
                (name_x, name_y),
                character_name,
                font=name_font,
                fill=self.sub_text,
            )

        # ===== 时间 / 服务器 / 对局ID（队友卡不显示） =====
        if show_meta:
            time_text = match.get("start_time", "")
            draw.text(
                (time_x, time_y),
                time_text,
                font=self.font_tiny,
                fill=self.sub_text,
            )

            server_name = match.get("server_name", "")
            game_id = match.get("game_id", "")
            if server_name:
                time_bbox = draw.textbbox((0, 0), time_text, font=self.font_tiny)
                time_w = time_bbox[2] - time_bbox[0]
                draw.text(
                    (time_x + time_w + 8, time_y),
                    server_name,
                    font=self.font_tiny,
                    fill=self.sub_text,
                )
                if game_id:
                    server_bbox = draw.textbbox((0, 0), server_name, font=self.font_tiny)
                    server_w = server_bbox[2] - server_bbox[0]
                    draw.text(
                        (time_x + time_w + server_w + 16, time_y),
                        f"ID:{game_id}",
                        font=self.font_tiny,
                        fill=self.sub_text,
                    )
            elif game_id:
                time_bbox = draw.textbbox((0, 0), time_text, font=self.font_tiny)
                time_w = time_bbox[2] - time_bbox[0]
                draw.text(
                    (time_x + time_w + 8, time_y),
                    f"ID:{game_id}",
                    font=self.font_tiny,
                    fill=self.sub_text,
                )

        # ===== 排名 / 模式（队友卡不显示，TKKA 改为居中大字） =====
        if not show_rank_mode:
            # 队友卡：玩家名为主视觉（加粗），TKKA 缩小为次级；超长玩家名截断省略
            player_name = str(match.get("player_name", "") or "")
            # 按文字内容选择粗体字体：韩文用 NotoSansKR-Bold，其余用 MiSans-Bold
            if self._has_korean(player_name):
                name_font = self._load_named_font("NotoSansKR-Bold", 22) or self._load_font(22)
            else:
                name_font = self._load_named_font("MiSans-Bold", 22) or self.font_small
            # 名称可用宽度：到武器图标列前留 8px，上限 120px
            max_name_w = min(120, icon_x - stat_x - 8)
            display_name = player_name
            nb = draw.textbbox((0, 0), display_name, font=name_font)
            if nb[2] - nb[0] > max_name_w and display_name:
                while display_name and nb[2] - nb[0] > max_name_w - 12:
                    display_name = display_name[:-1]
                    nb = draw.textbbox((0, 0), display_name + "…", font=name_font)
                display_name += "…"
                nb = draw.textbbox((0, 0), display_name, font=name_font)
            name_h = nb[3] - nb[1]
            tkka_font = self.font_small
            vb = draw.textbbox((0, 0), tkka_text, font=tkka_font)
            value_h = vb[3] - vb[1]
            lb = draw.textbbox((0, 0), "TK / K / A", font=self.font_tiny)
            label_h = lb[3] - lb[1]
            card_h = self.MATCH_H - 8
            total = name_h + 4 + value_h + 4 + label_h
            block_y = y + max(0, (card_h - total) // 2)
            draw.text((stat_x, block_y - nb[1]), display_name, font=name_font, fill=self.text)
            draw.text((stat_x, block_y + name_h + 4 - vb[1]), tkka_text, font=tkka_font, fill=self.sub_text)
            draw.text((stat_x, block_y + name_h + 4 + value_h + 4 - lb[1]), "TK / K / A", font=self.font_tiny, fill=self.sub_text)
        else:
            # 钴协议/钴联盟特殊处理
            is_cobalt_mode = mode_label in ("钴协议", "钴联盟")

            if is_cobalt_mode:
                if rank == 1:
                    rank_text = "胜利"
                    rank_color = self.red
                elif rank == 2:
                    rank_text = "失败"
                    rank_color = self.blue
                else:
                    rank_text = f"#{rank}"
                    rank_color = self.text
            else:
                # 普通模式：逃生成功显示「逃生」，否则显示排名数字
                escaped = bool(match.get("escaped", False))
                if escaped:
                    rank_text = "逃生"
                    rank_color = self.gold
                else:
                    rank_text = f"#{rank}"
                    rank_color = self.text
                    if rank == 1:
                        rank_color = self.gold
                    elif rank in (2, 3):
                        rank_color = self.style.rank_23

            if is_cobalt_mode and rank in (1, 2):
                rank_font = self.font
            elif not is_cobalt_mode and match.get("escaped", False):
                rank_font = self.font
            else:
                rank_font = self.font_big
            draw.text((rank_x, rank_y), rank_text, font=rank_font, fill=rank_color)
            # 左条主色跟随名次文字颜色（无特殊名次时仍用主题强调色），渐变样式不变
            if (role == "lead" and self.style.team_lead_bar is not None
                    and phase in ("all", "top")):
                # 第 4 名以后 / 钴协议未获胜等不优秀成绩：左条用灰色
                good = (rank <= 3) or (is_cobalt_mode and rank == 1)
                if good:
                    bar_c = rank_color
                    if tuple(bar_c)[:3] == tuple(self.text)[:3]:
                        bar_c = self.style.team_lead_bar
                else:
                    bar_c = (getattr(self.style, "mmr_muted", None)
                             or self.style.sub_text)
                self._draw_rank_bar(img, x1 + max(8, ins - 4), y, bar_c,
                                    int(self.style.team_lead_bar_w))

            # ===== 游戏模式 =====
            draw.text(
                (change_x, change_y),
                mode_label or "匹配",
                font=self.font_small,
                fill=self.text,
            )

            # ===== TK / K / A =====
            draw.text((stat_x, stat_y), tkka_text, font=self.font_big, fill=self.text)
            draw.text((label_x, label_y), "TK / K / A", font=self.font_tiny, fill=self.sub_text)

        # ===== 第一行：武器 + 战术技能 =====
        draw.rounded_rectangle(
            (icon_x, icon_y, icon_x + 36, icon_y + 36),
            radius=8,
            fill=self.style.weapon_chip,
        )
        weapon_icon = self._load_icon(match.get("weapon_icon", ""), (28, 28))
        if weapon_icon:
            self._paste_icon(img, weapon_icon, (icon_x + 4, icon_y + 4))

        tactical = match.get("tactical_skill", {})
        tactical_icon = self._load_icon(tactical.get("icon", ""), (34, 34))
        if tactical_icon:
            tactical_pos = (icon_x + 48, icon_y + 1)
            self._paste_icon(img, tactical_icon, tactical_pos)

            tactical_level = int(match.get("tactical_skill_level", 0) or 0)
            if tactical_level > 0:
                badge_x = tactical_pos[0] - 2
                badge_y = tactical_pos[1] + 20

                draw.rounded_rectangle(
                    (badge_x, badge_y, badge_x + 16, badge_y + 16),
                    radius=4,
                    fill=self.style.badge_fill,
                    outline=self.style.badge_outline,
                    width=1,
                )
                draw.text(
                    (badge_x + 4, badge_y - 1),
                    str(tactical_level),
                    font=self.font_mini if hasattr(self, "font_mini") else self._load_font(12),
                    fill=self.style.badge_text,
                )

        # ===== 第二行：主天赋图标 + 副组类型图标 =====
        core_icon = self._load_icon(match.get("trait_core_icon", ""), (30, 30))
        if core_icon:
            self._paste_icon(img, core_icon, (icon_x + 3, trait_row_y))

        second_group_icon = self._load_icon(match.get("trait_second_group_icon", ""), (30, 30))
        if second_group_icon:
            self._paste_icon(img, second_group_icon, (icon_x + 51, trait_row_y))

        # ===== 伤害 =====
        # 第一局若存在队友详情：长进度条为主视觉，条纵向贴卡片中线；
        # 伤害数字与“伤害”标签在条正下方，并以进度条水平中心为基准左右居中。
        # 其余战绩维持原「数字 + 标签」展示，保证信息口径不变
        team_max_damage = int(match.get("team_max_damage", 0) or 0)
        has_dmg_bar = team_max_damage > 0 and damage > 0
        if has_dmg_bar:
            bar_w = 128
            bar_h = 12
            bar_x = damage_x
            # 条中心 = 卡片纵向中心
            bar_y = y + (self.MATCH_H - 8) // 2 - bar_h // 2
            pct = max(0.0, min(100.0, damage / team_max_damage * 100.0))
            if self.style.legacy:
                draw.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + bar_h),
                                       radius=bar_h // 2, fill=(240, 232, 235))
            else:
                st = self.style
                blend_at(img, rounded_gradient(bar_w, bar_h, bar_h // 2, st.dmg_track, st.dmg_track),
                         bar_x, bar_y)
            fill_w = bar_w * pct / 100.0
            if fill_w > 0:
                if self.style.legacy:
                    draw.rounded_rectangle((bar_x, bar_y, bar_x + fill_w, bar_y + bar_h),
                                           radius=bar_h // 2, fill=(255, 100, 130))
                else:
                    st = self.style
                    fw = max(2, int(round(fill_w)))
                    blend_at(img, rounded_gradient(fw, bar_h, bar_h // 2, st.dmg_fill, st.dmg_fill),
                             bar_x, bar_y)
            # 数字在条上方，标签贴于条下方，均相对进度条中心左右居中
            cx = bar_x + bar_w / 2
            dmg_font = self.font_small
            num_text = f"{damage}"
            nb = draw.textbbox((0, 0), num_text, font=dmg_font)
            draw.text((cx - (nb[2] - nb[0]) / 2, bar_y - (nb[3] - nb[1]) - 6 - nb[1]), num_text, font=dmg_font, fill=self.text)
            lb = draw.textbbox((0, 0), "伤害", font=self.font_tiny)
            draw.text((cx - (lb[2] - lb[0]) / 2, sub_y - lb[1]), "伤害", font=self.font_tiny, fill=self.sub_text)
        else:
            draw.text((damage_x, value_y), f"{damage}", font=self.font_big, fill=self.text)
            draw.text((damage_x, sub_y), "伤害", font=self.font_tiny, fill=self.sub_text)

        # ===== KDA =====
        draw.text((kda_x, value_y), f"{kda_value:.2f}", font=self.font_big, fill=self.text)
        draw.text((kda_x, sub_y), "KDA", font=self.font_tiny, fill=self.sub_text)

        # ===== 排位模式：MMR / 其他模式：起始地点 =====
        is_rank_mode = is_rank_match or mode_label == "排位"
        is_cobalt_mode = mode_label in ("钴协议", "钴联盟")
        
        if is_cobalt_mode:
            # 钴协议/钴联盟模式：不渲染任何内容
            pass
        elif is_rank_mode:
            # 排位模式：显示 MMR
            mmr_before = int(match.get("mmr_before", 0) or 0)
            mmr_change = int(match.get("mmr_change", 0) or 0)
            
            mmr_before_text = f"{mmr_before:,}"
            mmr_before_bbox = draw.textbbox((0, 0), mmr_before_text, font=self.font_big)
            mmr_before_w = mmr_before_bbox[2] - mmr_before_bbox[0]
            
            if role == "sub":
                # 队友（次体）的 RP 增减统一灰色，让主体的红/蓝更突出
                mmr_change_text = f"+{mmr_change}" if mmr_change > 0 else f"{mmr_change}"
                mmr_change_color = (getattr(self.style, "mmr_muted", None)
                                    or self.sub_text)
            elif mmr_change >= 0:
                mmr_change_text = f"+{mmr_change}"
                mmr_change_color = self.red
            elif mmr_change < 0:
                mmr_change_text = f"{mmr_change}"
                mmr_change_color = self.blue
            else:
                mmr_change_text = f"{mmr_change}"
                mmr_change_color = self.text
            
            mmr_change_bbox = draw.textbbox((0, 0), mmr_change_text, font=self.font_big)
            mmr_change_w = mmr_change_bbox[2] - mmr_change_bbox[0]
            
            gap = 10
            start_x = kda_x + 100
            
            draw.text((start_x, value_y), mmr_before_text, font=self.font_big, fill=self.text)
            
            draw.text((start_x + mmr_before_w, value_y), mmr_change_text, font=self.font_big, fill=mmr_change_color)
            
            draw.text((start_x, sub_y), "RP", font=self.font_tiny, fill=self.sub_text)
        else:
            path_id = int(match.get("path_id", 0) or 0)

            if path_id > 0:
                top_text = str(path_id)
                bottom_text = "路径ID"
                top_font = self.font_big
                bottom_font = self.font_tiny
            else:
                top_text = "私人路径"
                bottom_text = "路径ID"
                top_font = self.font_small
                bottom_font = self.font_tiny

            top_bbox = draw.textbbox((0, 0), top_text, font=top_font)
            bottom_bbox = draw.textbbox((0, 0), bottom_text, font=bottom_font)

            top_w = top_bbox[2] - top_bbox[0]
            bottom_w = bottom_bbox[2] - bottom_bbox[0]

            center_x = rp_x + 40

            top_x = int(center_x - top_w / 2)
            bottom_x = int(center_x - bottom_w / 2)

            draw.text((top_x, value_y), top_text, font=top_font, fill=self.text)
            draw.text((bottom_x, sub_y), bottom_text, font=bottom_font, fill=self.sub_text)

        # ===== 装备：3上2下，下排居中 =====
        items = match.get("items", [])[:5]

        item_w = int(getattr(self.style, "item_w", 52))
        item_h = int(getattr(self.style, "item_h", 40))
        item_padding = 5

        top_gap_x = int(getattr(self.style, "item_gap", 64))
        bottom_gap_x = top_gap_x
        top_y = item_y
        bottom_y = item_y + int(getattr(self.style, "item_row_gap", 52))

        top_row_width = top_gap_x * 2
        top_center_x = item_x + top_row_width / 2

        bottom_row_width = bottom_gap_x
        bottom_start_x = int(top_center_x - bottom_row_width / 2)

        positions = [
            (item_x + 0 * top_gap_x, top_y),
            (item_x + 1 * top_gap_x, top_y),
            (item_x + 2 * top_gap_x, top_y),
            (bottom_start_x, bottom_y),
            (bottom_start_x + bottom_gap_x, bottom_y),
        ]

        for i, item in enumerate(items):
            # 字典形式的装备会缺失部分部位，按实际部位下标定位，避免错位
            try:
                slot = int(item.get("slot", i))
            except (TypeError, ValueError):
                slot = i
            if slot < 0 or slot >= len(positions):
                continue

            ix, iy = positions[slot]

            item_grade = item.get("grade", "")
            frame_color = self._get_item_bg_color(item_grade)
            # 格子底色：压住卡片渐变/水印，图标不再透出背景
            self._draw_item_slot_bg(img, ix, iy, item_w, item_h)

            item_icon = self._load_icon_fixed(item.get("icon", ""), (item_w - item_padding * 2, item_h - item_padding * 2))
            if item_icon:
                icon_w, icon_h = item_icon.size
                icon_x = ix + (item_w - icon_w) // 2
                icon_y = iy + (item_h - icon_h) // 2
                self._paste_icon_rounded(img, item_icon, (icon_x, icon_y),
                                         int(self.style.item_icon_radius))

            self._draw_item_glow_frame(img, ix, iy, item_w, item_h, frame_color)

        return y + self.MATCH_H


    # =========================
    # 主题绘制（仅非 legacy 主题走这里；坐标与旧逻辑完全一致）
    # =========================
    def _new_canvas(self, height: int) -> Image.Image:
        """画布：legacy 纯色；主题模式为渐变底 + 柔光光斑。"""
        if self.style.legacy:
            return Image.new("RGBA", (self.W, height), self.bg + (255,))
        return build_canvas(self.W, height, self.style)

    def _finish_canvas(self, img: Image.Image) -> Image.Image:
        """出图前的收尾钩子（保留给后续加暗角 / 噪点等）。"""
        return img

    @staticmethod
    def _card_img(draw) -> Optional[Image.Image]:
        img = getattr(draw, "_image", None)
        if isinstance(img, Image.Image) and img.mode == "RGBA":
            return img
        return None

    def _avatar_radius(self, size: int) -> int:
        if self.style.legacy:
            return 0
        ratio = max(0.0, min(0.5, float(self.style.avatar_radius_ratio)))
        return int(round(size * ratio))

    def _paste_icon_rounded(self, img: Image.Image, icon: Optional[Image.Image],
                            pos: tuple[int, int], radius: int = 0) -> None:
        """贴图标；radius>0 时把方形图标裁成圆角（视觉形状跟主题一致）。"""
        if icon is None:
            return
        if radius <= 0:
            img.paste(icon, pos, icon)
            return
        mask = Image.new("L", icon.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, icon.size[0] - 1, icon.size[1] - 1), radius=radius, fill=255)
        img.paste(icon, pos, ImageChops.multiply(icon.getchannel("A"), mask))

    def _draw_item_slot_bg(self, img: Image.Image, x: int, y: int, w: int, h: int) -> None:
        """装备格底色：挡住卡片渐变与猫爪水印，避免图标透出背景元素。"""
        st = self.style
        fill = getattr(st, "item_slot_fill", None)
        if fill is None:
            return
        blend_at(img, rounded_gradient(w, h, int(st.item_radius),
                                       tuple(fill[:4]), tuple(fill[:4])), x, y)

    def _draw_card_themed(self, draw, x1, y1, x2, y2, only_fill, only_outline, fill):
        img = ERRenderer._card_img(draw)
        if img is None:
            return
        st = self.style
        w = x2 - x1 + 1
        h = y2 - y1 + 1
        if w <= 0 or h <= 0:
            return
        r = int(st.card_radius)

        if not only_outline:
            if st.card_shadow is not None:
                sprite, pad = shadow_layer(w, h, r, st.card_shadow,
                                           st.card_shadow_blur, st.card_shadow_dy)
                blend_at(img, sprite, x1 - pad, y1 - pad)
            if st.card_glow is not None:
                sprite, pad = glow_layer(w, h, r, st.card_glow, st.card_glow_blur,
                                         st.card_glow_gain, int(st.card_glow_blur) + 4,
                                         width=max(1, int(st.card_border_w)), outside_only=True)
                blend_at(img, sprite, x1 - pad, y1 - pad)
            if fill is not None:
                f = tuple(fill)
                top = (f[0], f[1], f[2], f[3] if len(f) > 3 else st.card_fill[3])
                bottom = top
            else:
                top = st.card_fill
                bottom = st.card_fill_bottom or st.card_fill
            blend_at(img, rounded_gradient(w, h, r, top, bottom, st.card_rim), x1, y1)

        if not only_fill and st.card_border_w > 0:
            sprite, pad = border_layer(w, h, r, int(st.card_border_w), st.card_border,
                                       st.card_border_bottom or st.card_border, pad=0)
            blend_at(img, sprite, x1 - pad, y1 - pad)

    def _draw_inner_card_themed(self, draw, x1, y1, x2, y2):
        img = ERRenderer._card_img(draw)
        if img is None:
            return
        st = self.style
        w = x2 - x1 + 1
        h = y2 - y1 + 1
        if w <= 0 or h <= 0:
            return
        r = int(st.inner_card_radius)
        blend_at(img, rounded_gradient(w, h, r, st.inner_card_fill, st.inner_card_fill), x1, y1)
        if st.inner_card_border is not None:
            sprite, pad = border_layer(w, h, r, 1, st.inner_card_border,
                                       st.inner_card_border, pad=0)
            blend_at(img, sprite, x1 - pad, y1 - pad)

    def _draw_char_border_themed(self, img, draw, x, y, size):
        st = self.style
        r = self._avatar_radius(size)
        pad = max(8, int(st.avatar_glow_blur) * 2)
        glow = tuple(st.avatar_glow[:3]) + (150,)
        sprite, p = border_layer(size, size, r, int(st.avatar_border_w),
                                 st.avatar_line, st.avatar_line,
                                 glow=glow, glow_blur=st.avatar_glow_blur,
                                 glow_gain=st.avatar_glow_gain, pad=pad)
        blend_at(img, sprite, x - p, y - p)
        if st.avatar_line_inset > 0:
            ins = int(st.avatar_line_inset)
            layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            ImageDraw.Draw(layer).rounded_rectangle(
                (ins, ins, size - 1 - ins, size - 1 - ins),
                radius=max(0, r - ins),
                outline=getattr(st, "avatar_line2", None) or st.avatar_line, width=1)
            blend_at(img, layer, x, y)

    def _draw_item_glow_frame_themed(self, img, x, y, w, h, color):
        st = self.style
        c = tuple(color)[:3]
        # 描边：品质色在上、向下过渡到主题暖色（和左侧主条同一套主色调）
        warm = tuple((getattr(st, "team_lead_bar2", None) or (255, 172, 120))[:3])
        # 上端提亮（柔光），下端向主题暖色过渡（主色调统一）
        top = tuple(int(c[k] * 0.55 + 255 * 0.45) for k in range(3))
        bottom = tuple(int(c[k] * 0.62 + warm[k] * 0.38) for k in range(3))
        # 高亮低发散：短半径 + 高强度，光晕只贴着描边一小圈
        pad = max(8, int(st.item_glow_blur) * 3)
        sprite, p = border_layer(w, h, int(st.item_radius), int(st.item_border_w),
                                 top + (255,), bottom + (255,), glow=c + (int(getattr(st, "item_glow_alpha", 255)),),
                                 glow_blur=st.item_glow_blur,
                                 glow_gain=st.item_glow_gain, pad=pad)
        blend_at(img, sprite, x - p, y - p)

    def _draw_mmr_series_themed(self, img, draw, points, x, y, w, h, chart_y2):
        """折线主体：面积渐变 + 折线外发光 + 渐变描边 + 光点（只模糊图表局部）。"""
        st = self.style
        if len(points) < 2:
            return
        c1 = tuple(st.chart_line)
        c2 = tuple(st.chart_line2 or st.chart_line)
        local = [(px - x, py - y) for px, py in points]

        if st.chart_area is not None:
            area = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            poly = local + [(local[-1][0], chart_y2 - y), (local[0][0], chart_y2 - y)]
            ImageDraw.Draw(area).polygon(poly, fill=tuple(st.chart_area))
            fade = Image.linear_gradient("L").resize((w, h), Image.BILINEAR)
            alpha = ImageChops.multiply(area.getchannel("A"), ImageOps.invert(fade))
            area.putalpha(alpha)
            blend_at(img, area, x, y)

        if st.chart_glow is not None:
            halo = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            ImageDraw.Draw(halo).line(local, fill=tuple(st.chart_glow), width=7, joint="curve")
            halo = halo.filter(ImageFilter.GaussianBlur(5))
            blend_at(img, halo, x, y)

        segs = max(1, len(points) - 1)
        for i in range(len(points) - 1):
            t = i / segs
            seg = tuple(int(c1[k] + (c2[k] - c1[k]) * t) for k in range(3))
            draw.line([points[i], points[i + 1]], fill=seg, width=4)

        r = 5
        dot = tuple(st.chart_dot or c1)
        for px, py in points:
            draw.ellipse((px - r, py - r, px + r, py + r), fill=dot)
            draw.ellipse((px - r, py - r, px + r, py + r), outline=c1, width=1)

    # =========================
    # 工具
    # =========================
    @staticmethod
    def _cached_icon(
        path: str,
        size: tuple[int, int],
        method: str,
    ) -> Optional[Image.Image]:
        """带内存缓存的图标加载。method: 'resize' | 'fixed' | 'crop' | 'face_crop'
        所有锁操作均带超时，超时后降级为无缓存直接加载，避免卡死。
        """
        if not path:
            return None
        p = Path(path)
        if not p.exists():
            return None
        path_str = str(p.resolve())
        sw, sh = size
        key = (path_str, sw, sh, method)

        # ---- Phase 1: 带超时读取缓存 ----
        cache_hit = False
        cached_result: Optional[Any] = None
        try:
            with _acquire_timeout(
                ERRenderer._icon_cache_lock,
                ERRenderer._ICON_CACHE_LOCK_TIMEOUT,
                "icon_cache_read",
            ):
                cached = ERRenderer._icon_cache.get(key)
                if cached is not None:
                    cache_hit = True
                    if cached is _SENTINEL_NONE:
                        cached_result = None
                    else:
                        cached_result = cached.copy()
        except _RendererLockTimeoutError as e:
            logger.warning(f"[ER渲染] 图标缓存读取锁超时，降级为直接加载: {e}")
            # 拿锁失败时，跳过缓存直接加载（cache_hit 保持 False）

        if cache_hit:
            return cached_result

        # ---- Phase 2: 无锁状态下加载处理图像（IO+CPU都在锁外） ----
        try:
            img = Image.open(p).convert("RGBA")
            if method == "resize":
                img = img.resize(size, Image.Resampling.LANCZOS)
            elif method == "crop":
                target_w, target_h = size
                src_w, src_h = img.size
                scale = max(target_w / src_w, target_h / src_h)
                new_w = int(src_w * scale)
                new_h = int(src_h * scale)
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                left = (new_w - target_w) // 2
                top = (new_h - target_h) // 2
                right = left + target_w
                bottom = top + target_h
                img = img.crop((left, top, right, bottom))
            elif method == "fixed":
                w, h = img.size
                max_w, max_h = size
                scale = min(max_w / w, max_h / h)
                new_w = int(w * scale)
                new_h = int(h * scale)
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            elif method == "face_crop":
                target_w, target_h = size
                src_w, src_h = img.size
                scale = max(target_w / src_w, target_h / src_h)
                new_w = int(src_w * scale)
                new_h = int(src_h * scale)
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                left = (new_w - target_w) // 2
                top = int((new_h - target_h) * 0.25)
                right = left + target_w
                bottom = top + target_h
                img = img.crop((left, top, right, bottom))
            result = img
        except Exception:
            result = None

        # ---- Phase 3: 带超时写回缓存 ----
        try:
            with _acquire_timeout(
                ERRenderer._icon_cache_lock,
                ERRenderer._ICON_CACHE_LOCK_TIMEOUT,
                "icon_cache_write",
            ):
                # 限制缓存容量，避免无限增长（简单的LRU：满时清空一半）
                if len(ERRenderer._icon_cache) > RENDER.icon_cache_max_size:
                    items = list(ERRenderer._icon_cache.keys())
                    for k in items[: len(items) // 2]:
                        ERRenderer._icon_cache.pop(k, None)
                ERRenderer._icon_cache[key] = result if result is not None else _SENTINEL_NONE
        except _RendererLockTimeoutError as e:
            logger.warning(f"[ER渲染] 图标缓存写入锁超时，跳过本次缓存: {e}")

        return result.copy() if result is not None else None

    def _load_icon_crop_center(self, path: str, size: tuple[int, int]) -> Optional[Image.Image]:
        return ERRenderer._cached_icon(path, size, "crop")

    def _get_item_bg_color(self, grade: str) -> tuple[int, int, int]:
        grade = (grade or "").strip()

        grade_map = {
            "Common": (150, 150, 150),
            "Uncommon": (70, 120, 220),
            "Rare": (70, 120, 220),
            "Epic": (160, 90, 210),
            "Hero": (160, 90, 210),
            "Legend": (183, 145, 67),
            "Mythic": (180, 60, 60),
        }

        return grade_map.get(grade, (150, 150, 150))

    def _load_icon(self, path: str, size: tuple[int, int]) -> Optional[Image.Image]:
        return ERRenderer._cached_icon(path, size, "resize")

    def _load_icon_fixed(self, path: str, max_size: tuple[int, int]) -> Optional[Image.Image]:
        return ERRenderer._cached_icon(path, max_size, "fixed")

    def _load_icon_from_url(self, url: str, size: tuple[int, int]) -> Optional[Image.Image]:
        try:
            import requests as _req
            from io import BytesIO
            resp = _req.get(url, timeout=10)
            resp.raise_for_status()
            img = Image.open(BytesIO(resp.content)).convert("RGBA")
            sw, sh = size
            w, h = img.size
            scale = min(sw / w, sh / h)
            nw, nh = int(w * scale), int(h * scale)
            img = img.resize((nw, nh), Image.Resampling.LANCZOS)
            return img
        except Exception:
            return None

    def _load_character_icon(self, path: str, size: tuple[int, int]) -> Optional[Image.Image]:
        return ERRenderer._cached_icon(path, size, "face_crop")

    def _paste_icon(self, base_img: Image.Image, icon: Optional[Image.Image], pos: tuple[int, int]) -> None:
        if icon is None:
            return
        base_img.paste(icon, pos, icon)

    def _draw_card(self, draw, x1, y1, x2, y2, only_fill: bool = False, only_outline: bool = False, fill=None):
        if not self.style.legacy:
            # 主题模式：渐变底色 + 渐变描边 + 外发光 / 投影（坐标不变）
            self._draw_card_themed(draw, x1, y1, x2, y2, only_fill, only_outline, fill)
            return
        if only_outline:
            draw.rounded_rectangle(
                (x1, y1, x2, y2),
                radius=18,
                outline=self.pink_border,
                width=2,
            )
            return
        if only_fill:
            draw.rounded_rectangle(
                (x1, y1, x2, y2),
                radius=18,
                fill=fill if fill is not None else self.card,
            )
            return
        draw.rounded_rectangle(
            (x1, y1, x2, y2),
            radius=18,
            fill=self.card,
            outline=self.pink_border,
            width=2,
        )

    def _draw_inner_card(self, draw: ImageDraw.ImageDraw, x1: int, y1: int, x2: int, y2: int, only_fill: bool = False) -> None:
        if not self.style.legacy:
            self._draw_inner_card_themed(draw, x1, y1, x2, y2)
            return
        draw.rounded_rectangle((x1, y1, x2, y2), radius=12, fill=self.card_2)

    @classmethod
    def _glow_frame_sprite(
        cls,
        size: tuple[int, int],
        glow_color: tuple[int, int, int],
        border_color: Optional[tuple[int, int, int]] = None,
        *,
        radius: int = 0,
        border: int = 2,
        blur: int = 6,
        pad: int = 14,
        outside_only: bool = False,
        gain: float = 2.4,
    ) -> Image.Image:
        """生成透明中心、带外发光和清晰描边的中空边框精灵。"""
        border_color = border_color or glow_color
        key = (size, glow_color, border_color, radius, border, blur, pad, outside_only, float(gain))
        with cls._glow_frame_cache_lock:
            cached = cls._glow_frame_cache.get(key)
            if cached is not None:
                return cached

        supersample = 4
        w, h = size
        sw, sh = w + pad * 2, h + pad * 2
        W, H = sw * supersample, sh * supersample
        x0 = y0 = pad * supersample
        box = [x0, y0, x0 + w * supersample - 1, y0 + h * supersample - 1]

        ring = Image.new("L", (W, H), 0)
        ring_draw = ImageDraw.Draw(ring)
        if radius > 0:
            ring_draw.rounded_rectangle(
                box, radius=radius * supersample,
                outline=255, width=max(1, border * supersample),
            )
        else:
            ring_draw.rectangle(box, outline=255, width=max(1, border * supersample))

        mask = ring.filter(ImageFilter.GaussianBlur(blur * supersample))
        mask = mask.point(lambda a: min(255, int(a * gain)))

        if outside_only:
            inset = border * supersample
            inner = Image.new("L", (W, H), 0)
            inner_draw = ImageDraw.Draw(inner)
            inner_box = [
                x0 + inset, y0 + inset,
                x0 + w * supersample - 1 - inset,
                y0 + h * supersample - 1 - inset,
            ]
            if radius > 0:
                inner_draw.rounded_rectangle(
                    inner_box, radius=max(radius * supersample - inset, 0), fill=255,
                )
            else:
                inner_draw.rectangle(inner_box, fill=255)
            mask = ImageChops.subtract(mask, inner)

        glow = Image.new("RGBA", (W, H), glow_color + (255,))
        glow.putalpha(mask)

        line = Image.new("RGBA", (W, H), border_color + (255,))
        line.putalpha(ring)

        sprite = Image.alpha_composite(glow, line)
        sprite = sprite.resize((sw, sh), Image.LANCZOS)
        with cls._glow_frame_cache_lock:
            cls._glow_frame_cache[key] = sprite
        return sprite

    def _draw_item_glow_frame(self, img: Image.Image, x: int, y: int,
                              w: int, h: int, color: tuple[int, int, int]) -> None:
        """在装备图标外叠一层中空发光边框，中心保持透明。"""
        if not self.style.legacy:
            self._draw_item_glow_frame_themed(img, x, y, w, h, color)
            return
        pad = 12
        sprite = self._glow_frame_sprite(
            (w, h), color, color,
            radius=6, border=3, blur=5, pad=pad, outside_only=False, gain=1.6,
        )
        img.alpha_composite(sprite, (x - pad, y - pad))

    def _draw_char_border(self, img: Image.Image, draw: ImageDraw.ImageDraw,
                          x: int, y: int, size: int) -> None:
        """外发光双边框：外层2px淡粉 + 内层1px亮粉，光晕只落在框外。"""
        if not self.style.legacy:
            self._draw_char_border_themed(img, draw, x, y, size)
            return
        pad = 16
        sprite = self._glow_frame_sprite(
            (size, size), (255, 95, 131), self.pink_border,
            radius=0, border=2, blur=8, pad=pad, outside_only=True, gain=1.8,
        )
        img.alpha_composite(sprite, (x - pad, y - pad))
        inner_inset = 5
        draw.rectangle(
            (x + inner_inset, y + inner_inset, x + size - inner_inset, y + size - inner_inset),
            outline=(255, 150, 180),
            width=1,
        )

    def _add_gradient_border(self, img: Image.Image) -> None:
        """在图片四周添加向中心的极淡粉转白色渐变"""
        # 注：此方法当前未被 render 流程调用，保留定义以便后续使用
        w, h = img.size
        gradient_w = 100
        gradient = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        g_draw = ImageDraw.Draw(gradient)
        for i in range(gradient_w):
            ratio = i / gradient_w
            alpha = int(80 * (1 - ratio))
            pink_val = 255 - int(15 * ratio)
            color = (255, pink_val, pink_val, alpha)
            g_draw.rectangle([0, i, w, i + 1], fill=color)
            g_draw.rectangle([0, h - 1 - i, w, h - i], fill=color)
            g_draw.rectangle([i, 0, i + 1, h], fill=color)
            g_draw.rectangle([w - 1 - i, 0, w - i, h], fill=color)
        result = Image.alpha_composite(img, gradient)
        img.paste(result)

    @staticmethod
    def _get_paw_base(fonts_dir_parent: Path) -> Optional[Image.Image]:
        """只从磁盘加载一次猫爪原图，全局复用。
        关键修复：文件IO在锁外执行，避免磁盘IO卡住导致死锁。
        使用 check-lock-check 模式。
        """
        global _PAW_BASE_ICON
        # ---- Phase 1: 无锁快速路径 ----
        if _PAW_BASE_ICON is not None:
            if _PAW_BASE_ICON is _SENTINEL_NONE:
                return None
            return _PAW_BASE_ICON.copy()

        # ---- Phase 2: 拿锁后再次检查 ----
        need_load = False
        try:
            with _acquire_timeout(_PAW_BASE_LOCK, _PAW_BASE_LOCK_TIMEOUT, "paw_base_check"):
                if _PAW_BASE_ICON is not None:
                    # 其他线程已经加载好了
                    if _PAW_BASE_ICON is _SENTINEL_NONE:
                        return None
                    return _PAW_BASE_ICON.copy()
                need_load = True
        except _RendererLockTimeoutError as e:
            logger.warning(f"[ER渲染] 猫爪原图锁检查超时，降级为直接加载: {e}")
            # 拿锁失败时，直接从磁盘加载（不缓存），避免卡死
            cat_paw_path = fonts_dir_parent / "images" / "cat_paw.png"
            if cat_paw_path.exists():
                try:
                    return Image.open(str(cat_paw_path)).convert("RGBA")
                except Exception:
                    return None
            return None

        # ---- Phase 3: 锁外执行文件IO ----
        if need_load:
            cat_paw_path = fonts_dir_parent / "images" / "cat_paw.png"
            loaded_icon: Any = _SENTINEL_NONE
            if cat_paw_path.exists():
                try:
                    loaded_icon = Image.open(str(cat_paw_path)).convert("RGBA")
                except Exception:
                    loaded_icon = _SENTINEL_NONE

            # ---- Phase 4: 再次拿锁写入缓存 ----
            try:
                with _acquire_timeout(_PAW_BASE_LOCK, _PAW_BASE_LOCK_TIMEOUT, "paw_base_write"):
                    # 只在仍然为空时写入，避免覆盖其他线程更先写入的结果
                    if _PAW_BASE_ICON is None:
                        _PAW_BASE_ICON = loaded_icon
            except _RendererLockTimeoutError as e:
                logger.warning(f"[ER渲染] 猫爪原图写入锁超时，结果未写入缓存: {e}")

            if loaded_icon is _SENTINEL_NONE:
                return None
            return loaded_icon.copy()

        return None

    @staticmethod
    def _get_cached_paw(size: int, rotation: int, opacity: float,
                        tint: Optional[tuple] = None) -> Optional[Image.Image]:
        """按(尺寸,旋转,透明度)缓存处理好的猫爪水印。带超时保护。"""
        key = (int(size), int(rotation) % 360, int(opacity * 1000),
               tuple(tint[:3]) if tint else None)
        try:
            with _acquire_timeout(
                ERRenderer._paw_cache_lock,
                ERRenderer._PAW_CACHE_LOCK_TIMEOUT,
                "paw_cache_read",
            ):
                cached = ERRenderer._paw_watermark_cache.get(key)
                if cached is not None:
                    if cached is _SENTINEL_NONE:
                        return None
                    return cached.copy()
        except _RendererLockTimeoutError as e:
            logger.warning(f"[ER渲染] 猫爪水印缓存读取超时，跳过缓存: {e}")
        return _SENTINEL_NONE  # 外部区分：SENTINEL表示没查到，需要继续生成

    @staticmethod
    def _put_cached_paw(size: int, rotation: int, opacity: float,
                        value: Optional[Image.Image], tint: Optional[tuple] = None) -> None:
        """写入猫爪水印缓存。带超时保护，超时则放弃本次写入。"""
        key = (int(size), int(rotation) % 360, int(opacity * 1000),
               tuple(tint[:3]) if tint else None)
        try:
            with _acquire_timeout(
                ERRenderer._paw_cache_lock,
                ERRenderer._PAW_CACHE_LOCK_TIMEOUT,
                "paw_cache_write",
            ):
                # 容量保护
                if len(ERRenderer._paw_watermark_cache) > RENDER.paw_cache_max_size:
                    ERRenderer._paw_watermark_cache.clear()
                ERRenderer._paw_watermark_cache[key] = value if value is not None else _SENTINEL_NONE
        except _RendererLockTimeoutError as e:
            logger.warning(f"[ER渲染] 猫爪水印缓存写入超时，跳过本次缓存: {e}")

    def _add_cat_paw_watermark(self, img: Image.Image) -> None:
        """在图片上散布多个猫爪水印（带缓存，避免每次重复缩放/旋转/透明度处理）"""
        fonts_dir_parent = self.fonts_dir.parent
        paw_base = ERRenderer._get_paw_base(fonts_dir_parent)
        if paw_base is None:
            return

        # 主题化：整体透明度按主题缩放（暗色主题淡一点），可选统一着色
        tint = self.style.paw_tint
        if self.style.paw_opacity <= 0:
            return

        try:
            w, h = img.size
            watermarks = [
                {"x": w * 0.08, "y": h * 0.12, "size": 320, "opacity": 0.4, "rotation": -20},
                {"x": w * 0.92, "y": h * 0.2, "size": 280, "opacity": 0.35, "rotation": 25},
                {"x": w * 0.12, "y": h * 0.4, "size": 260, "opacity": 0.4, "rotation": 15},
                {"x": w * 0.88, "y": h * 0.5, "size": 300, "opacity": 0.3, "rotation": -30},
                {"x": w * 0.05, "y": h * 0.65, "size": 240, "opacity": 0.3, "rotation": 10},
                {"x": w * 0.95, "y": h * 0.75, "size": 290, "opacity": 0.45, "rotation": -15},
                {"x": w * 0.15, "y": h * 0.88, "size": 340, "opacity": 0.4, "rotation": 20},
                {"x": w * 0.5, "y": h * 0.05, "size": 220, "opacity": 0.25, "rotation": -5},
                {"x": w * 0.5, "y": h * 0.95, "size": 230, "opacity": 0.3, "rotation": 5},
                {"x": w * 0.35, "y": h * 0.25, "size": 200, "opacity": 0.2, "rotation": -25},
                {"x": w * 0.65, "y": h * 0.6, "size": 210, "opacity": 0.25, "rotation": 30},
            ]

            for wm in watermarks:
                size = int(wm["size"])
                rotation = int(wm["rotation"])
                opacity = float(wm["opacity"]) * self.style.paw_opacity

                processed = ERRenderer._get_cached_paw(size, rotation, opacity, tint)
                if processed is _SENTINEL_NONE:
                    # 缓存未命中：生成
                    try:
                        scaled = paw_base.resize((size, size), Image.Resampling.LANCZOS)
                        if rotation != 0:
                            scaled = scaled.rotate(rotation, expand=True, resample=Image.Resampling.BICUBIC)
                        alpha = scaled.split()[-1]
                        alpha = alpha.point(lambda p, o=opacity: int(p * o))
                        scaled.putalpha(alpha)
                        if tint is not None:
                            tinted = Image.new("RGBA", scaled.size, tuple(tint[:3]) + (255,))
                            tinted.putalpha(scaled.getchannel("A"))
                            scaled = tinted
                        processed = scaled
                        ERRenderer._put_cached_paw(size, rotation, opacity, processed, tint)
                    except Exception:
                        processed = None
                        ERRenderer._put_cached_paw(size, rotation, opacity, None, tint)

                if processed is None:
                    continue

                wm_w, wm_h = processed.size
                pos_x = int(wm["x"] - wm_w / 2)
                pos_y = int(wm["y"] - wm_h / 2)
                img.paste(processed, (pos_x, pos_y), processed)

        except Exception:
            pass

    # =========================
    # 角色评级排行
    # =========================
    @staticmethod
    def _tier_display_name(tier_label: str) -> str:
        m = {
            "diamond_plus": "灭钻+",
            "meteorite_plus": "星陨+",
            "mithril_plus": "无瑕+",
            "asura_plus": "修罗+",
            "in1000": "in 1000",
            "asura": "修罗",
            "gold": "金魄",
            "silver": "银烙",
            "bronze": "铜魂",
            "iron": "铁阎",
            "all": "全段位",
        }
        return m.get(tier_label, tier_label)

    def render_character_ranking(self, data: dict[str, Any]) -> str:
        """渲染入口。使用带超时的信号量，避免渲染异常时所有线程卡死等待。"""
        try:
            with _acquire_timeout(
                ERRenderer._render_semaphore,
                ERRenderer._RENDER_SEMAPHORE_TIMEOUT,
                "render_semaphore",
            ):
                return self._render_character_ranking_inner(data)
        except _RendererLockTimeoutError as e:
            logger.warning(f"[ER渲染] 并发渲染信号量等待超时，降级为直接渲染: {e}")
            return self._render_character_ranking_inner(data)

    def _render_character_ranking_inner(self, data: dict[str, Any]) -> str:
        ranking = data.get("ranking", [])
        n = len(ranking)

        COLS = 10
        CARD_W = 96
        CARD_H = 140
        GAP = 8
        HEADER_H = 100
        FOOTER_H = RENDER.footer_height

        rows = (n + COLS - 1) // COLS if n > 0 else 1
        grid_h = rows * CARD_H + (rows - 1) * GAP

        total_h = self.PADDING + HEADER_H + self.PADDING + grid_h + self.PADDING + FOOTER_H
        img = self._new_canvas(total_h)
        draw = ImageDraw.Draw(img)

        content_x = self.PADDING
        grid_top = self.PADDING + HEADER_H + self.PADDING

        cards = []
        for i, entry in enumerate(ranking):
            row = i // COLS
            col = i % COLS
            x = content_x + col * (CARD_W + GAP)
            y = grid_top + row * (CARD_H + GAP)
            cards.append((entry, x, y))

        # 阶段1：猫爪水印（铺在背景最底层）
        self._add_cat_paw_watermark(img)

        # 阶段2：卡片与标题底色（不透明底色充当遮罩，遮挡卡片内部的水印）
        for entry, x, y in cards:
            self._draw_ranking_card(img, draw, entry, x, y, CARD_W, CARD_H, phase="fill")
        self._draw_ranking_header(img, draw, data, self.PADDING, HEADER_H, phase="fill")

        # 阶段3：边框 + 文字 + 图标
        self._draw_ranking_header(img, draw, data, self.PADDING, HEADER_H, phase="top")
        for entry, x, y in cards:
            self._draw_ranking_card(img, draw, entry, x, y, CARD_W, CARD_H, phase="top")

        # 页脚
        footer_y = grid_top + grid_h + 10
        footer_text = "Powered by 椛椿&小休比"
        footer_font = self._load_named_font("MiSans-Regular", 16)
        fb = draw.textbbox((0, 0), footer_text, font=footer_font)
        draw.text(((self.W - (fb[2] - fb[0])) / 2, footer_y), footer_text, font=footer_font, fill=self.sub_text)

        help_text = "输入 /指令 查询所有指令使用方法"
        help_font = self._load_named_font("MiSans-Regular", 14)
        hb = draw.textbbox((0, 0), help_text, font=help_font)
        draw.text(((self.W - (hb[2] - hb[0])) / 2, footer_y + 22), help_text, font=help_font, fill=self.sub_text)

        output_path = self.output_dir / f"er_rank_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.png"
        img = self._finish_canvas(img)
        img.convert("RGB").save(output_path, "PNG", optimize=False)
        return str(output_path)

    def _draw_ranking_header(self, img: Image.Image, draw: ImageDraw.ImageDraw, data: dict, y: int, h: int, phase: str = "all") -> None:
        x1 = self.PADDING
        x2 = self.W - self.PADDING
        y2 = y + h

        if phase in ("all", "fill"):
            self._draw_card(draw, x1, y, x2, y2, only_fill=True)
        if phase in ("all", "top"):
            self._draw_card(draw, x1, y, x2, y2, only_outline=True)
        if phase == "fill":
            return

        tier_display = self._tier_display_name(data.get("tier", ""))
        total = len(data.get("ranking", []))
        tier_count = data.get("tier_count", 0)

        draw.text((x1 + 24, y + 16), "角色评级排行", font=self.font_big, fill=self.text)

        subtitle = f"{tier_display} · 共 {total} 项"
        if tier_count > 0:
            subtitle += f" · 样本 {tier_count:,} 场"
        draw.text((x1 + 24, y + 60), subtitle, font=self.font_small, fill=self.sub_text)

    def _fill_card_gradient(self, img: Image.Image, x1: int, y1: int, x2: int, y2: int) -> None:
        """用淡粉到白的垂直渐变填充圆角卡片底色。

        渐变模板按 (w, h) 缓存一次生成，后续卡片直接粘贴复用，避免逐卡重复逐行绘制。
        """
        w = x2 - x1 + 1
        h = y2 - y1 + 1
        if w <= 0 or h <= 0:
            return
        # 主题也纳入 key：同一进程内切换主题时不会复用旧配色的模板
        key = (w, h, self.style.name)

        # ---- Phase 1: 带超时读取缓存模板 ----
        try:
            with _acquire_timeout(
                ERRenderer._card_gradient_cache_lock,
                LOCK.icon_cache_lock_timeout,
                "card_gradient_cache_read",
            ):
                cached = ERRenderer._card_gradient_cache.get(key)
                if cached is not None:
                    img.paste(cached, (x1, y1), cached)
                    return
        except _RendererLockTimeoutError as e:
            logger.warning(f"[ER渲染] 渐变模板缓存读取锁超时，降级为直接生成: {e}")

        # ---- Phase 2: 锁外生成模板（透明圆角外区域）----
        template = self._build_card_gradient_template(w, h)

        # ---- Phase 3: 再次拿锁写入缓存（若已有则丢弃本次生成）----
        try:
            with _acquire_timeout(
                ERRenderer._card_gradient_cache_lock,
                LOCK.icon_cache_lock_timeout,
                "card_gradient_cache_write",
            ):
                if key not in ERRenderer._card_gradient_cache:
                    ERRenderer._card_gradient_cache[key] = template
        except _RendererLockTimeoutError as e:
            logger.warning(f"[ER渲染] 渐变模板缓存写入锁超时，跳过缓存: {e}")

        img.paste(template, (x1, y1), template)

    def _build_card_gradient_template(self, w: int, h: int) -> Image.Image:
        """生成带透明圆角的垂直渐变 RGBA 模板（圆角外 alpha=0）。"""
        top = self.style.rank_card_top[:3]
        bottom = self.style.rank_card_bottom[:3]
        gradient = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        gd = ImageDraw.Draw(gradient)
        for i in range(h):
            ratio = i / (h - 1) if h > 1 else 0.0
            r = round(top[0] + (bottom[0] - top[0]) * ratio)
            g = round(top[1] + (bottom[1] - top[1]) * ratio)
            b = round(top[2] + (bottom[2] - top[2]) * ratio)
            gd.line((0, i, w - 1, i), fill=(r, g, b, 255))
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1),
                                               radius=int(self.style.card_radius), fill=255)
        gradient.putalpha(mask)
        return gradient

    @staticmethod
    def _draw_circle_aa(img: Image.Image, cx: int, cy: int, radius: int, color: tuple) -> None:
        """绘制抗锯齿实心圆（超采样），按 (radius, color) 缓存精灵，避免逐卡重复超采样。"""
        key = (radius, tuple(color))

        # ---- Phase 1: 带超时读取缓存精灵 ----
        try:
            with _acquire_timeout(
                ERRenderer._dot_sprite_cache_lock,
                LOCK.icon_cache_lock_timeout,
                "dot_sprite_cache_read",
            ):
                cached = ERRenderer._dot_sprite_cache.get(key)
                if cached is not None:
                    img.paste(cached, (cx - radius, cy - radius), cached)
                    return
        except _RendererLockTimeoutError as e:
            logger.warning(f"[ER渲染] 圆形底图缓存读取锁超时，降级为直接生成: {e}")

        # ---- Phase 2: 锁外超采样生成精灵 ----
        scale = 4
        big = radius * 2 * scale
        layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        ImageDraw.Draw(layer).ellipse((0, 0, big - 1, big - 1), fill=tuple(color) + (255,))
        layer = layer.resize((radius * 2, radius * 2), Image.Resampling.LANCZOS)

        # ---- Phase 3: 再次拿锁写入缓存 ----
        try:
            with _acquire_timeout(
                ERRenderer._dot_sprite_cache_lock,
                LOCK.icon_cache_lock_timeout,
                "dot_sprite_cache_write",
            ):
                if key not in ERRenderer._dot_sprite_cache:
                    ERRenderer._dot_sprite_cache[key] = layer
        except _RendererLockTimeoutError as e:
            logger.warning(f"[ER渲染] 圆形底图缓存写入锁超时，跳过缓存: {e}")

        img.paste(layer, (cx - radius, cy - radius), layer)

    def _draw_ranking_card(self, img: Image.Image, draw: ImageDraw.ImageDraw, entry: dict, x: int, y: int, w: int, h: int, phase: str = "all") -> None:
        if phase in ("all", "fill"):
            self._fill_card_gradient(img, x, y, x + w, y + h)
        if phase in ("all", "top"):
            self._draw_card(draw, x, y, x + w, y + h, only_outline=True)
        if phase == "fill":
            return

        AVATAR_SZ = 60
        AVATAR_INSET = 5
        BADGE_SZ = 26
        WEAPON_SZ = 18

        avatar_x = x + (w - AVATAR_SZ) // 2
        avatar_y = y + 18
        icon_inner = AVATAR_SZ - 2 * AVATAR_INSET

        char_icon = self._load_character_icon(entry.get("character_icon", ""), (icon_inner, icon_inner))
        if char_icon:
            self._paste_icon_rounded(img, char_icon, (avatar_x + AVATAR_INSET, avatar_y + AVATAR_INSET),
                                     self._avatar_radius(AVATAR_SZ))
        self._draw_char_border(img, draw, avatar_x, avatar_y, AVATAR_SZ)

        cx = x + w // 2

        # 评级徽章角标（盖在头像右上角）
        badge_x = avatar_x + AVATAR_SZ - BADGE_SZ + 6
        badge_y = avatar_y - 6
        badge_icon = self._load_icon(entry.get("tier_badge_path", ""), (BADGE_SZ, BADGE_SZ))
        if badge_icon:
            self._paste_icon(img, badge_icon, (badge_x, badge_y))

        # 武器图标角标（盖在头像右下角）
        weapon_x = avatar_x + AVATAR_SZ - WEAPON_SZ + 6
        weapon_y = avatar_y + AVATAR_SZ - WEAPON_SZ + 6
        weapon_icon = self._load_icon(entry.get("weapon_icon", ""), (WEAPON_SZ, WEAPON_SZ))
        if weapon_icon:
            # 武器为白色透明 PNG，加抗锯齿黑色圆形底图使其在淡色卡片上清晰可见
            dot_r = WEAPON_SZ // 2 + 4
            cx_w = weapon_x + WEAPON_SZ // 2
            cy_w = weapon_y + WEAPON_SZ // 2
            self._draw_circle_aa(img, cx_w, cy_w, dot_r, self.style.rank_weapon_dot)
            self._paste_icon(img, weapon_icon, (weapon_x, weapon_y))

        sub_font = self._load_named_font("MiSans-Medium", 12)

        # 平均伤害
        dmg_text = f"伤害 {entry.get('avg_damage', 0):,}"
        db = draw.textbbox((0, 0), dmg_text, font=sub_font)
        draw.text((cx - (db[2] - db[0]) / 2, y + 88), dmg_text, font=sub_font, fill=self.sub_text)

        # 平均加减分
        rp = entry.get("avg_mmr_gain", 0)
        rp_text = f"RP {rp:+.1f}"
        rp_color = self.style.accent if rp >= 0 else self.blue
        rb = draw.textbbox((0, 0), rp_text, font=sub_font)
        draw.text((cx - (rb[2] - rb[0]) / 2, y + 108), rp_text, font=sub_font, fill=rp_color)

    def render_character_stats(self, data: dict[str, Any]) -> str:
        """渲染入口。使用带超时的信号量，避免渲染异常时所有线程卡死等待。"""
        try:
            with _acquire_timeout(
                ERRenderer._render_semaphore,
                ERRenderer._RENDER_SEMAPHORE_TIMEOUT,
                "render_semaphore",
            ):
                return self._render_character_stats_inner(data)
        except _RendererLockTimeoutError as e:
            logger.warning(f"[ER渲染] 并发渲染信号量等待超时，降级为直接渲染: {e}")
            return self._render_character_stats_inner(data)

    def _render_character_stats_inner(self, data: dict[str, Any]) -> str:
        FOOTER_H = RENDER.footer_height

        skill_builds = data.get("skill_builds", [])
        tactical_skills = data.get("tactical_skills", [])
        trait_builds = data.get("trait_builds", [])
        item_stats = data.get("item_stats", [])

        has_skills = len(skill_builds) > 0
        has_tactical = len(tactical_skills) > 0
        has_traits = len(trait_builds) > 0
        has_items = len(item_stats) > 0

        HEADER_H = 44
        SEPARATOR_H = 34
        CONTENT_PAD = 14

        skills_h = 0
        if has_skills or has_tactical:
            skill_rows = min(len(skill_builds), 5) if has_skills else 0
            tactical_rows = min(len(tactical_skills), 6) if has_tactical else 0
            max_rows = max(skill_rows, tactical_rows)
            row_h = 50
            skills_h = 84 + max_rows * row_h
        traits_h = 0
        if has_traits:
            card_h = 288
            traits_h = HEADER_H + card_h + CONTENT_PAD
        items_h = 0
        if has_items:
            items_h = self._char_items_h(item_stats)

        total_h = (
            self.PADDING
            + 220
            + self.PADDING
            + 460
            + (self.PADDING + skills_h if (has_skills or has_tactical) else 0)
            + (self.PADDING + traits_h if has_traits else 0)
            + (self.PADDING + items_h if has_items else 0)
            + self.PADDING
            + FOOTER_H
        )

        img = self._new_canvas(total_h)
        draw = ImageDraw.Draw(img)

        y_positions = {
            "char_header": self.PADDING,
            "char_stats": self.PADDING + 220 + self.PADDING,
        }

        cur_y = y_positions["char_stats"] + 460 + self.PADDING

        if has_skills or has_tactical:
            y_positions["skills_and_tactical"] = cur_y
            cur_y += skills_h + self.PADDING
        if has_traits:
            y_positions["trait_builds"] = cur_y
            cur_y += traits_h + self.PADDING
        if has_items:
            y_positions["item_stats"] = cur_y
            cur_y += items_h + self.PADDING

        futures_fill = []
        futures_fill.append(_DRAWING_POOL.submit(
            self._draw_char_header, img, draw, data, y_positions["char_header"], phase="fill"
        ))
        futures_fill.append(_DRAWING_POOL.submit(
            self._draw_char_stats, img, draw, data, y_positions["char_stats"], phase="fill"
        ))

        if has_skills or has_tactical:
            futures_fill.append(_DRAWING_POOL.submit(
                self._draw_char_skills_tactical_row, img, draw, data, y_positions["skills_and_tactical"], skills_h, phase="fill"
            ))
        if has_traits:
            futures_fill.append(_DRAWING_POOL.submit(
                self._draw_char_trait_builds, img, draw, data, y_positions["trait_builds"], traits_h, phase="fill"
            ))
        if has_items:
            futures_fill.append(_DRAWING_POOL.submit(
                self._draw_char_item_stats, img, draw, data, y_positions["item_stats"], items_h, phase="fill"
            ))

        for f in futures_fill:
            f.result()

        self._add_cat_paw_watermark(img)

        futures_top = []
        futures_top.append(_DRAWING_POOL.submit(
            self._draw_char_header, img, draw, data, y_positions["char_header"], phase="top"
        ))
        futures_top.append(_DRAWING_POOL.submit(
            self._draw_char_stats, img, draw, data, y_positions["char_stats"], phase="top"
        ))

        if has_skills or has_tactical:
            futures_top.append(_DRAWING_POOL.submit(
                self._draw_char_skills_tactical_row, img, draw, data, y_positions["skills_and_tactical"], skills_h, phase="top"
            ))
        if has_traits:
            futures_top.append(_DRAWING_POOL.submit(
                self._draw_char_trait_builds, img, draw, data, y_positions["trait_builds"], traits_h, phase="top"
            ))
        if has_items:
            futures_top.append(_DRAWING_POOL.submit(
                self._draw_char_item_stats, img, draw, data, y_positions["item_stats"], items_h, phase="top"
            ))

        for f in futures_top:
            f.result()

        footer_y = cur_y + 10
        footer_text = "Powered by 椛椿&小休比"
        footer_font = self._load_named_font("MiSans-Regular", 16)
        bbox = draw.textbbox((0, 0), footer_text, font=footer_font)
        tw = bbox[2] - bbox[0]
        footer_x = (self.W - tw) / 2
        draw.text(
            (footer_x, footer_y),
            footer_text,
            font=footer_font,
            fill=self.sub_text,
        )

        help_text = "输入 /指令 查询所有指令使用方法"
        help_font = self._load_named_font("MiSans-Regular", 14)
        help_bbox = draw.textbbox((0, 0), help_text, font=help_font)
        help_tw = help_bbox[2] - help_bbox[0]
        help_x = (self.W - help_tw) / 2
        help_y = footer_y + 22
        draw.text(
            (help_x, help_y),
            help_text,
            font=help_font,
            fill=self.sub_text,
        )

        output_path = self.output_dir / f"er_char_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.png"
        img = self._finish_canvas(img)
        img.convert("RGB").save(output_path, "PNG", optimize=False)
        
        return str(output_path)

    def _draw_char_header(self, img: Image.Image, draw: ImageDraw.ImageDraw, data: dict, y: int, phase: str = "all") -> int:
        x1 = self.PADDING
        x2 = self.W - self.PADDING
        HEADER_H = 200
        y2 = y + HEADER_H

        if phase in ("all", "fill"):
            self._draw_card(draw, x1, y, x2, y2, only_fill=True)
        if phase in ("all", "top"):
            self._draw_card(draw, x1, y, x2, y2, only_outline=True)
        if phase == "fill":
            return y + HEADER_H

        AVATAR_SZ = 130
        AVATAR_INSET = 5
        AVATAR_PAD = 30
        INFO_GAP = 30
        BADGE_SZ = 80
        BADGE_PAD = 40

        avatar_x = x1 + AVATAR_PAD
        avatar_y = y + 35
        icon_inner = AVATAR_SZ - 2 * AVATAR_INSET

        char_icon = self._load_character_icon(data.get("character_icon", ""), (icon_inner, icon_inner))
        if char_icon:
            self._paste_icon_rounded(img, char_icon, (avatar_x + AVATAR_INSET, avatar_y + AVATAR_INSET),
                                     self._avatar_radius(AVATAR_SZ))
        self._draw_char_border(img, draw, avatar_x, avatar_y, AVATAR_SZ)

        info_x = avatar_x + AVATAR_SZ + INFO_GAP
        info_y = y + 30

        char_name = data.get("character_name", "")
        weapon_name = data.get("weapon_name", "")
        tier_label = data.get("tier", "")
        tier_count = data.get("tier_count", 0)
        tier_name_map = {
            "diamond_plus": "灭钻+",
            "meteorite_plus": "星陨+",
            "mithril_plus": "无瑕+",
            "in1000": "in 1000",
            "asura_plus": "修罗+",
            "asura": "修罗",
            "gold": "金魄",
            "silver": "银烙",
            "bronze": "铜魂",
            "iron": "铁阎",
        }
        tier_display = tier_name_map.get(tier_label, tier_label)

        draw.text((info_x, info_y), char_name, font=self.font_title, fill=self.text)
        draw.text((info_x, info_y + 55), f"{weapon_name} | {tier_display}", font=self.font, fill=self.sub_text)
        draw.text((info_x, info_y + 90), f"选择数: {data.get('count', 0):,}", font=self.font_small, fill=self.sub_text)

        if tier_count > 0:
            draw.text((info_x, info_y + 120), f"总样本场次数: {tier_count:,}", font=self.font_small, fill=self.sub_text)

        badge_x = x2 - BADGE_SZ - BADGE_PAD
        badge_y = y + 60

        badge_path = data.get("tier_badge_path", "")
        badge_icon = self._load_icon(badge_path, (BADGE_SZ, BADGE_SZ))
        if badge_icon:
            self._paste_icon(img, badge_icon, (badge_x, badge_y))

        return y2 + 12

    def _draw_char_stats(self, img: Image.Image, draw: ImageDraw.ImageDraw, data: dict, y: int, phase: str = "all") -> int:
        x1 = self.PADDING
        x2 = self.W - self.PADDING
        block_h = 440
        y2 = y + block_h

        if phase in ("all", "fill"):
            self._draw_card(draw, x1, y, x2, y2, only_fill=True)
        if phase in ("all", "top"):
            self._draw_card(draw, x1, y, x2, y2, only_outline=True)
        if phase == "fill":
            return y + block_h

        INNER_PAD = 18
        TOP_PAD = 12
        TITLE_H = 36
        CELL_GAP = 10

        draw.text(
            (x1 + INNER_PAD, y + TOP_PAD),
            "数据统计",
            font=self.font,
            fill=self.text,
        )

        rank_size = data.get("rank_size", 113)

        stats = [
            ("胜率", f"{data.get('win_rate', 0):.1f}%", data.get("win_rank", 0)),
            ("TOP 3", f"{data.get('top3_rate', 0):.1f}%", data.get("top3_rank", 0)),
            ("平均排名", f"#{data.get('avg_rank', 0):.2f}", data.get("place_rank", 0)),
            ("平均击杀", f"{data.get('avg_kill', 0):.2f}", data.get("player_kill_rank", 0)),
            ("RP", f"{data.get('avg_rp', 0):.1f}", data.get("mmr_gain_rank", 0)),
            ("选择率", f"{data.get('pick_rate', 0):.2f}%", data.get("pick_rank", 0)),
            ("平均伤害", f"{data.get('avg_damage', 0):,.0f}", 0),
            ("平均TK", f"{data.get('avg_tk', 0):.2f}", 0),
        ]

        CELL_TOP = y + TOP_PAD + TITLE_H
        CELL_ROW_H = 96
        CELL_W = (x2 - x1 - INNER_PAD * 2 - CELL_GAP) // 2

        LABEL_OFF_Y = 8
        VALUE_OFF_Y = 34
        RANK_OFF_Y = 68

        for i, (label, value, rank) in enumerate(stats):
            col = i % 2
            row = i // 2

            cell_x = x1 + INNER_PAD + col * (CELL_W + CELL_GAP)
            cell_y = CELL_TOP + row * CELL_ROW_H

            self._draw_inner_card(draw, cell_x, cell_y, cell_x + CELL_W, cell_y + CELL_ROW_H - CELL_GAP)

            draw.text(
                (cell_x + INNER_PAD, cell_y + LABEL_OFF_Y),
                label,
                font=self.font_small,
                fill=self.sub_text,
            )

            draw.text(
                (cell_x + INNER_PAD, cell_y + VALUE_OFF_Y),
                value,
                font=self.font_big,
                fill=self.text,
            )

            if rank > 0:
                rank_text = f"#{rank} / {rank_size}"
                rank_color = self.sub_text
                if rank <= 3:
                    rank_color = self.gold
                elif rank <= 10:
                    rank_color = self.style.gold_dim

                draw.text(
                    (cell_x + INNER_PAD, cell_y + RANK_OFF_Y),
                    rank_text,
                    font=self.font_tiny,
                    fill=rank_color,
                )

        return y2

    def _draw_char_skills_tactical_row(self, img: Image.Image, draw: ImageDraw.ImageDraw, data: dict, y: int, block_h: int, phase: str = "all") -> int:
        x1 = self.PADDING
        x2 = self.W - self.PADDING
        y2 = y + block_h

        gap = 12
        total_w = x2 - x1
        card_w = (total_w - gap) // 2
        left_x1 = x1
        left_x2 = x1 + card_w
        right_x1 = x1 + card_w + gap
        right_x2 = x2

        if phase in ("all", "fill"):
            self._draw_card(draw, left_x1, y, left_x2, y2, only_fill=True)
            self._draw_card(draw, right_x1, y, right_x2, y2, only_fill=True)
        if phase in ("all", "top"):
            self._draw_card(draw, left_x1, y, left_x2, y2, only_outline=True)
            self._draw_card(draw, right_x1, y, right_x2, y2, only_outline=True)
        if phase == "fill":
            return y + block_h

        self._draw_skill_builds_card(img, draw, data, left_x1, y, left_x2, y2)
        self._draw_tactical_skills_card(img, draw, data, right_x1, y, right_x2, y2)

        return y2

    def _draw_skill_builds_card(self, img: Image.Image, draw: ImageDraw.ImageDraw, data: dict, x1: int, y1: int, x2: int, y2: int) -> None:
        builds = data.get("skill_builds", [])

        INNER_PAD = 14
        TOP_PAD = 12
        HEADER_Y = y1 + TOP_PAD + 28
        CONTENT_Y = HEADER_Y + 24
        ROW_GAP = 8

        LEFT = x1 + INNER_PAD
        RIGHT = x2 - INNER_PAD
        CONTENT_LEFT = LEFT
        METRIC_W = 130
        METRIC_RIGHT = RIGHT
        METRIC_LEFT = METRIC_RIGHT - METRIC_W
        METRIC_DIV = METRIC_LEFT + METRIC_W // 2
        COL_SEL_CENTER = METRIC_LEFT + METRIC_W // 4
        COL_WIN_CENTER = METRIC_LEFT + METRIC_W * 3 // 4

        draw.text((LEFT, y1 + TOP_PAD), "推荐技能", font=self.font, fill=self.text)

        if not builds:
            draw.text((LEFT, y1 + TOP_PAD + 36), "暂无数据", font=self.font_small, fill=self.sub_text)
            return

        skill_icon_size = 36
        badge_size = 14
        icon_gap = 2
        row_h = skill_icon_size + 14
        max_rows = min(len(builds), 5)

        hdr_font = self.font_tiny
        hdr2_bbox = draw.textbbox((0, 0), "胜率", font=hdr_font)
        hdr2_w = hdr2_bbox[2] - hdr2_bbox[0]
        hdr3_bbox = draw.textbbox((0, 0), "选择率", font=hdr_font)
        hdr3_w = hdr3_bbox[2] - hdr3_bbox[0]

        draw.text((CONTENT_LEFT, HEADER_Y), "技能构建", font=hdr_font, fill=self.text)
        draw.text((COL_SEL_CENTER - hdr3_w // 2, HEADER_Y), "选择率", font=hdr_font, fill=self.text)
        draw.text((COL_WIN_CENTER - hdr2_w // 2, HEADER_Y), "胜率", font=hdr_font, fill=self.text)

        draw.line([(LEFT - 2, CONTENT_Y), (RIGHT, CONTENT_Y)], fill=self.style.char_divider, width=1)
        draw.line([(METRIC_DIV, CONTENT_Y), (METRIC_DIV, CONTENT_Y + max_rows * row_h + ROW_GAP)], fill=self.style.char_col_divider, width=1)

        for i in range(max_rows):
            row_y = CONTENT_Y + ROW_GAP + i * row_h
            build = builds[i]
            slots = build.get("slots", [])
            selection_rate = build.get("selection_rate", 0)
            win_rate = build.get("win_rate", 0)

            icon_x = CONTENT_LEFT
            for s_idx, slot in enumerate(slots):
                skill_icon = slot.get("skill_icon", "")
                skill_image_url = slot.get("skill_image_url", "")
                slot_char = slot.get("slot_char", "?")

                if s_idx > 0:
                    sep_bbox = draw.textbbox((0, 0), ">", font=self.font_tiny)
                    sep_w = sep_bbox[2] - sep_bbox[0]
                    sep_h = sep_bbox[3] - sep_bbox[1]
                    draw.text(
                        (icon_x, row_y + (skill_icon_size - sep_h) / 2),
                        ">",
                        font=self.font_tiny,
                        fill=self.style.skill_sep,
                    )
                    icon_x += sep_w + 3

                icon = self._load_icon_fixed(skill_icon, (skill_icon_size, skill_icon_size)) if skill_icon else None
                if icon is None and skill_image_url:
                    icon = self._load_icon_from_url(skill_image_url, (skill_icon_size, skill_icon_size))
                if icon:
                    self._paste_icon(img, icon, (icon_x, row_y))
                else:
                    draw.rounded_rectangle(
                        (icon_x, row_y, icon_x + skill_icon_size, row_y + skill_icon_size),
                        radius=4,
                        fill=self.style.skill_icon_bg,
                    )

                badge_x = icon_x + skill_icon_size - badge_size
                badge_y = row_y + skill_icon_size - badge_size
                draw.rounded_rectangle(
                    (badge_x, badge_y, badge_x + badge_size, badge_y + badge_size),
                    radius=3,
                    fill=self.style.skill_badge_fill,
                    outline=self.style.skill_badge_outline,
                    width=1,
                )
                ch_font = self.font_mini
                ch_bbox = draw.textbbox((0, 0), slot_char, font=ch_font)
                ch_w = ch_bbox[2] - ch_bbox[0]
                ch_h = ch_bbox[3] - ch_bbox[1]
                draw.text(
                    (badge_x + (badge_size - ch_w) / 2, badge_y + (badge_size - ch_h) / 2 - 1),
                    slot_char,
                    font=ch_font,
                    fill=self.style.skill_badge_text,
                )

                icon_x += skill_icon_size + icon_gap

            sel_text = f"{selection_rate:.1f}%"
            sel_color = self.text
            if selection_rate >= 30:
                sel_color = self.style.sel_hi
            elif selection_rate >= 15:
                sel_color = self.style.sel_mid
            sel_bbox = draw.textbbox((0, 0), sel_text, font=self.font_tiny)
            sel_w = sel_bbox[2] - sel_bbox[0]
            draw.text((COL_SEL_CENTER - sel_w // 2, row_y + (skill_icon_size - 12) / 2), sel_text, font=self.font_tiny, fill=sel_color)

            win_text = f"{win_rate:.1f}%"
            win_color = self.text
            if win_rate >= 30:
                win_color = self.style.win_hi
            elif win_rate >= 20:
                win_color = self.style.win_mid
            elif win_rate >= 15:
                win_color = self.style.win_lo
            win_bbox = draw.textbbox((0, 0), win_text, font=self.font_tiny)
            win_w = win_bbox[2] - win_bbox[0]
            draw.text((COL_WIN_CENTER - win_w // 2, row_y + (skill_icon_size - 12) / 2), win_text, font=self.font_tiny, fill=win_color)

    def _draw_tactical_skills_card(self, img: Image.Image, draw: ImageDraw.ImageDraw, data: dict, x1: int, y1: int, x2: int, y2: int) -> None:
        skills = data.get("tactical_skills", [])

        INNER_PAD = 14
        TOP_PAD = 12
        HEADER_Y = y1 + TOP_PAD + 28
        CONTENT_Y = HEADER_Y + 24
        ROW_GAP = 8

        LEFT = x1 + INNER_PAD
        RIGHT = x2 - INNER_PAD
        ICON_X = LEFT
        METRIC_W = 130
        METRIC_RIGHT = RIGHT
        METRIC_LEFT = METRIC_RIGHT - METRIC_W
        METRIC_DIV = METRIC_LEFT + METRIC_W // 2
        COL_SEL_CENTER = METRIC_LEFT + METRIC_W // 4
        COL_WIN_CENTER = METRIC_LEFT + METRIC_W * 3 // 4

        draw.text((LEFT, y1 + TOP_PAD), "推荐战术技能", font=self.font, fill=self.text)

        if not skills:
            draw.text((LEFT, y1 + TOP_PAD + 36), "暂无数据", font=self.font_small, fill=self.sub_text)
            return

        header_font = self.font_tiny
        hdr2_bbox = draw.textbbox((0, 0), "胜率", font=header_font)
        hdr2_w = hdr2_bbox[2] - hdr2_bbox[0]
        hdr3_bbox = draw.textbbox((0, 0), "选择率", font=header_font)
        hdr3_w = hdr3_bbox[2] - hdr3_bbox[0]

        draw.text((LEFT, HEADER_Y), "战术技能", font=header_font, fill=self.text)
        draw.text((COL_SEL_CENTER - hdr3_w // 2, HEADER_Y), "选择率", font=header_font, fill=self.text)
        draw.text((COL_WIN_CENTER - hdr2_w // 2, HEADER_Y), "胜率", font=header_font, fill=self.text)

        draw.line([(LEFT - 2, CONTENT_Y), (RIGHT, CONTENT_Y)], fill=self.style.char_divider, width=1)

        row_h = 44
        max_rows = min(len(skills), 6)
        draw.line([(METRIC_DIV, CONTENT_Y), (METRIC_DIV, CONTENT_Y + max_rows * row_h + ROW_GAP)], fill=self.style.char_col_divider, width=1)

        for i in range(max_rows):
            row_y = CONTENT_Y + ROW_GAP + i * row_h
            skill = skills[i]
            name = skill.get("name", "")
            selection_rate = skill.get("selection_rate", 0)
            win_rate = skill.get("win_rate", 0)
            icon_path = skill.get("icon", "")

            icon = self._load_icon_fixed(icon_path, (30, 30)) if icon_path else None
            if icon:
                self._paste_icon(img, icon, (ICON_X, row_y + 6))
            else:
                draw.rounded_rectangle((ICON_X, row_y + 6, ICON_X + 30, row_y + 36), radius=6, fill=self.style.char_slot_empty)

            name_font = self.font_tiny if len(name) > 5 else self.font_small
            draw.text((ICON_X + 38, row_y + 8), name, font=name_font, fill=self.text)

            sel_text = f"{selection_rate:.1f}%"
            sel_color = self.text
            if selection_rate >= 50:
                sel_color = self.style.sel_hi
            elif selection_rate >= 20:
                sel_color = self.style.sel_mid
            sel_bbox = draw.textbbox((0, 0), sel_text, font=self.font_tiny)
            sel_w = sel_bbox[2] - sel_bbox[0]
            draw.text((COL_SEL_CENTER - sel_w // 2, row_y + 8), sel_text, font=self.font_tiny, fill=sel_color)

            win_text = f"{win_rate:.1f}%"
            win_color = self.text
            if win_rate >= 30:
                win_color = self.style.win_hi
            elif win_rate >= 20:
                win_color = self.style.win_mid
            elif win_rate >= 15:
                win_color = self.style.win_lo
            win_bbox = draw.textbbox((0, 0), win_text, font=self.font_tiny)
            win_w = win_bbox[2] - win_bbox[0]
            draw.text((COL_WIN_CENTER - win_w // 2, row_y + 8), win_text, font=self.font_tiny, fill=win_color)

    def _draw_char_trait_builds(self, img: Image.Image, draw: ImageDraw.ImageDraw, data: dict, y: int, block_h: int, phase: str = "all") -> int:
        x1 = self.PADDING
        x2 = self.W - self.PADDING
        y2 = y + block_h

        if phase in ("all", "fill"):
            self._draw_card(draw, x1, y, x2, y2, only_fill=True)
        if phase in ("all", "top"):
            self._draw_card(draw, x1, y, x2, y2, only_outline=True)
        if phase == "fill":
            return y + block_h

        INNER_PAD = 18
        TOP_PAD = 10
        CARD_GAP = 16
        CARD_H = 288

        draw.text((x1 + INNER_PAD, y + TOP_PAD), "推荐潜能", font=self.font, fill=self.text)

        builds = data.get("trait_builds", [])
        if not builds:
            draw.text((x1 + INNER_PAD, y + TOP_PAD + 38), "暂无数据", font=self.font_small, fill=self.sub_text)
            return y2

        avail_w = x2 - x1 - INNER_PAD * 2 - CARD_GAP * 2
        card_w = avail_w // 3
        card_top = y + TOP_PAD + 34

        ICON_SZ = 44
        CARD_INNER = 14

        for i in range(min(len(builds), 3)):
            bx = x1 + INNER_PAD + i * (card_w + CARD_GAP)
            by = card_top
            build = builds[i]

            draw.rounded_rectangle(
                (bx, by, bx + card_w, by + CARD_H),
                radius=12,
                fill=self.style.char_row_bg,
                outline=self.style.char_row_border,
                width=1,
            )

            core = build.get("core", {})
            core_name = core.get("name", "")
            core_icon = core.get("icon", "")
            selection_rate = build.get("selection_rate", 0)
            win_rate = build.get("win_rate", 0)

            icon_x = bx + CARD_INNER
            icon_y = by + CARD_INNER
            icon = self._load_icon_fixed(core_icon, (ICON_SZ, ICON_SZ)) if core_icon else None
            if icon:
                self._paste_icon(img, icon, (icon_x, icon_y))
            else:
                draw.rounded_rectangle((icon_x, icon_y, icon_x + ICON_SZ, icon_y + ICON_SZ), radius=6, fill=self.style.char_slot_empty)

            name_x = icon_x + ICON_SZ + 12
            name_y = by + CARD_INNER
            draw.text((name_x, name_y), core_name, font=self.font, fill=self.text)
            sub_info_y = by + CARD_INNER + ICON_SZ + 4
            draw.text((bx + CARD_INNER, sub_info_y), f"选择率 {selection_rate:.1f}%  |  胜率 {win_rate:.1f}%", font=self.font_small, fill=self.sub_text)

            line_y = sub_info_y + 28
            draw.line([(bx + CARD_INNER, line_y), (bx + card_w - CARD_INNER, line_y)], fill=self.style.char_divider, width=1)

            combinations = build.get("combinations", [])
            combo_y = line_y + 10
            max_combos = min(len(combinations), 4)
            SLOT_SZ = 28
            SLOT_GAP = 6

            for j in range(max_combos):
                combo = combinations[j]
                slots = [
                    combo.get("sub1", {}),
                    combo.get("sub1_alt", {}),
                    combo.get("sub2", {}),
                    combo.get("sub2_alt", {}),
                ]

                sx = bx + CARD_INNER + 2
                for slot_data in slots:
                    if slot_data and slot_data.get("id", 0) != 0:
                        s_icon_path = slot_data.get("icon", "")
                        s_icon = self._load_icon_fixed(s_icon_path, (SLOT_SZ, SLOT_SZ)) if s_icon_path else None
                        if s_icon:
                            self._paste_icon(img, s_icon, (sx, combo_y))
                        else:
                            draw.rounded_rectangle((sx, combo_y, sx + SLOT_SZ, combo_y + SLOT_SZ), radius=4, fill=self.style.char_slot_empty)
                    else:
                        draw.rounded_rectangle((sx, combo_y, sx + SLOT_SZ, combo_y + SLOT_SZ), radius=4, fill=self.style.char_slot_empty2)
                    sx += SLOT_SZ + SLOT_GAP

                combo_sel = combo.get("win_rate", 0)
                draw.text((sx + 8, combo_y + 6), f"{combo_sel:.1f}%", font=self.font_small, fill=self.sub_text)

                combo_y += SLOT_SZ + 10

        return y2

    # ---------- 物品统计（分组 + 芯片版） ----------
    _ITEM_GROUP_HDR = 30
    _ITEM_GROUP_PAD = 10
    _ITEM_GROUP_GAP = 14
    _ITEM_CHIP_H = 60
    _ITEM_CHIP_GAP = 10
    _ITEM_CHIP_MAX = 4

    def _item_group_h(self, slot_group: dict) -> int:
        n = min(len(slot_group.get("items") or []), ERRenderer._ITEM_CHIP_MAX)
        rows = max(1, (n + 1) // 2)
        return (ERRenderer._ITEM_GROUP_HDR + ERRenderer._ITEM_GROUP_PAD
                + rows * ERRenderer._ITEM_CHIP_H
                + (rows - 1) * ERRenderer._ITEM_CHIP_GAP
                + ERRenderer._ITEM_GROUP_PAD)

    def _char_items_h(self, item_stats) -> int:
        """物品统计块高度（与绘制共用一套常量）。"""
        HEADER_H = 44
        CONTENT_PAD = 14
        groups = list(item_stats or [])
        if not getattr(self.style, "item_stat_chips", False):
            n_slots = len(groups)
            item_row_h = 52
            slot_header_h = 26
            inner_pad = 6
            slot_card_h = slot_header_h + inner_pad * 2 + 3 * item_row_h + 4
            col_h = min(n_slots, 3) * (slot_card_h + 6)
            return HEADER_H + col_h + CONTENT_PAD
        total = 0
        rows = 0
        for i in range(0, len(groups), 2):
            pair = groups[i:i + 2]
            total += max(self._item_group_h(g) for g in pair)
            rows += 1
        if rows > 1:
            total += (rows - 1) * ERRenderer._ITEM_GROUP_GAP
        return HEADER_H + total + CONTENT_PAD

    def _draw_item_chip(self, img: Image.Image, draw: ImageDraw.ImageDraw,
                        item: dict, x: int, y: int, w: int, h: int) -> None:
        """一颗物品芯片：品质发光边框（与玩家信息里的装备一致）+ 图标 + 名称 + 选择率/胜率。"""
        color = self._get_item_bg_color(item.get("grade", ""))
        self._draw_item_slot_bg(img, x, y, w, h)
        self._draw_item_glow_frame(img, x, y, w, h, color)

        icon_sz = h - 18
        icon = self._load_icon_fixed(item.get("icon", ""), (icon_sz, icon_sz)) if item.get("icon") else None
        if icon is not None:
            self._paste_icon(img, icon, (x + 8, y + (h - icon.size[1]) // 2))
        else:
            draw.rounded_rectangle((x + 8, y + (h - icon_sz) // 2,
                                    x + 8 + icon_sz, y + (h + icon_sz) // 2),
                                   radius=6, fill=self.style.char_slot_icon)

        tx = x + 8 + icon_sz + 8
        name = str(item.get("name", "") or "")
        if len(name) > 8:
            name = name[:7] + ".."
        draw.text((tx, y + 9), name, font=self.font_small, fill=self.text)

        sel = float(item.get("selection_rate", 0) or 0)
        if sel >= 30:
            sel_color = self.style.sel_hi
        elif sel >= 15:
            sel_color = self.style.sel_mid
        else:
            sel_color = self.sub_text
        draw.text((tx, y + 34), f"选择率 {sel:.1f}%", font=self.font_mini, fill=sel_color)
        win = item.get("win_rate")
        if win:
            draw.text((x + w - 10, y + 40), f"胜率 {float(win):.1f}%",
                      font=self.font_mini, fill=self.sub_text, anchor="rm")

    def _draw_item_group(self, img: Image.Image, draw: ImageDraw.ImageDraw,
                         slot_group: dict, x: int, y: int, w: int, h: int) -> None:
        draw.rounded_rectangle((x, y, x + w, y + h), radius=14,
                               fill=self.style.char_row_bg,
                               outline=self.style.char_row_border, width=1)
        draw.text((x + 14, y + 4), str(slot_group.get("slot_name", "") or ""),
                  font=self.font_small, fill=self.text)

        items = list(slot_group.get("items") or [])[:ERRenderer._ITEM_CHIP_MAX]
        pad = ERRenderer._ITEM_GROUP_PAD
        chip_gap = ERRenderer._ITEM_CHIP_GAP
        chip_w = (w - 2 * pad - chip_gap) // 2
        chip_h = ERRenderer._ITEM_CHIP_H
        top = y + ERRenderer._ITEM_GROUP_HDR + pad
        for i, item in enumerate(items):
            cx = x + pad + (i % 2) * (chip_w + chip_gap)
            cy = top + (i // 2) * (chip_h + chip_gap)
            self._draw_item_chip(img, draw, item, cx, cy, chip_w, chip_h)

    def _draw_char_item_stats_themed(self, img: Image.Image, draw: ImageDraw.ImageDraw,
                                     data: dict, y: int, block_h: int,
                                     phase: str = "all") -> int:
        x1 = self.PADDING
        x2 = self.W - self.PADDING
        y2 = y + block_h

        if phase in ("all", "fill"):
            self._draw_card(draw, x1, y, x2, y2, only_fill=True)
        if phase in ("all", "top"):
            self._draw_card(draw, x1, y, x2, y2, only_outline=True)
        if phase == "fill":
            return y + block_h

        draw.text((x1 + 18, y + 10), "物品统计", font=self.font, fill=self.text)

        groups = list(data.get("item_stats") or [])
        if not groups:
            draw.text((x1 + 18, y + 48), "暂无数据", font=self.font_small, fill=self.sub_text)
            return y2

        inner_x1 = x1 + 18
        inner_w = (x2 - x1) - 36
        gap = 16
        group_w = (inner_w - gap) // 2
        cursor = y + 44
        for i in range(0, len(groups), 2):
            pair = groups[i:i + 2]
            row_h = max(self._item_group_h(g) for g in pair)
            for j, g in enumerate(pair):
                gx = inner_x1 + j * (group_w + gap)
                self._draw_item_group(img, draw, g, gx, cursor, group_w, row_h)
            cursor += row_h + ERRenderer._ITEM_GROUP_GAP
        return y2

    def _draw_char_item_stats(self, img: Image.Image, draw: ImageDraw.ImageDraw, data: dict, y: int, block_h: int, phase: str = "all") -> int:
        if getattr(self.style, "item_stat_chips", False):
            return self._draw_char_item_stats_themed(img, draw, data, y, block_h, phase)
        x1 = self.PADDING
        x2 = self.W - self.PADDING
        y2 = y + block_h

        if phase in ("all", "fill"):
            self._draw_card(draw, x1, y, x2, y2, only_fill=True)
        if phase in ("all", "top"):
            self._draw_card(draw, x1, y, x2, y2, only_outline=True)
        if phase == "fill":
            return y + block_h

        INNER_PAD = 18
        TOP_PAD = 10
        COL_GAP = 16
        SLOT_GAP = 6
        SLOT_HDR_H = 26
        ITEM_ICON_SZ = 44
        ITEM_ROW_H = 52
        ITEM_PAD = 4
        INNER_PAD2 = 6
        SLOT_CARD_H = SLOT_HDR_H + INNER_PAD2 * 2 + 3 * ITEM_ROW_H + 4

        draw.text((x1 + INNER_PAD, y + TOP_PAD), "物品统计", font=self.font, fill=self.text)

        slot_groups = data.get("item_stats", [])
        if not slot_groups:
            draw.text((x1 + INNER_PAD, y + TOP_PAD + 38), "暂无数据", font=self.font_small, fill=self.sub_text)
            return y2

        avail_w = x2 - x1 - INNER_PAD * 2 - COL_GAP
        col_w = avail_w // 2
        slots_per_col = 3
        col_top = y + TOP_PAD + 34

        for col in range(2):
            col_x = x1 + INNER_PAD + col * (col_w + COL_GAP)
            slots_in_col = slot_groups[col * slots_per_col:(col + 1) * slots_per_col]
            if not slots_in_col:
                break

            for s_idx, slot_data in enumerate(slots_in_col):
                sy = col_top + s_idx * (SLOT_CARD_H + SLOT_GAP)
                slot_name = slot_data.get("slot_name", "")
                items = slot_data.get("items", [])

                draw.rounded_rectangle(
                    (col_x, sy, col_x + col_w, sy + SLOT_CARD_H),
                    radius=12,
                    fill=self.style.char_row_bg,
                    outline=self.style.char_row_border,
                    width=1,
                )

                draw.text((col_x + 12, sy + 4), slot_name, font=self.font_small, fill=self.text)

                inner_x1 = col_x + 10
                inner_y1 = sy + SLOT_HDR_H
                inner_x2 = col_x + col_w - 10
                inner_y2 = sy + SLOT_CARD_H - 6
                draw.rounded_rectangle(
                    (inner_x1, inner_y1, inner_x2, inner_y2),
                    radius=10,
                    fill=self.style.char_row_bg2,
                )

                item_start_y = inner_y1 + INNER_PAD2
                for item_idx, item in enumerate(items[:3]):
                    item_icon = item.get("icon", "")
                    item_name = item.get("name", "")
                    item_sel = item.get("selection_rate", 0)
                    item_grade = item.get("grade", "")

                    row_y = item_start_y + item_idx * ITEM_ROW_H
                    frame_x = inner_x1 + INNER_PAD2
                    frame_y = row_y + 2

                    frame_color = self._get_item_bg_color(item_grade)

                    inner_sz = ITEM_ICON_SZ - ITEM_PAD * 2
                    icon = self._load_icon_fixed(item_icon, (inner_sz, inner_sz)) if item_icon else None
                    if icon:
                        icon_w, icon_h = icon.size
                        ix = frame_x + (ITEM_ICON_SZ - icon_w) // 2
                        iy = frame_y + (ITEM_ICON_SZ - icon_h) // 2
                        self._paste_icon(img, icon, (ix, iy))
                    else:
                        draw.rounded_rectangle(
                            (frame_x + ITEM_PAD, frame_y + ITEM_PAD,
                             frame_x + ITEM_ICON_SZ - ITEM_PAD, frame_y + ITEM_ICON_SZ - ITEM_PAD),
                            radius=5,
                            fill=self.style.char_slot_icon,
                        )

                    self._draw_item_glow_frame(img, frame_x, frame_y, ITEM_ICON_SZ, ITEM_ICON_SZ, frame_color)

                    text_x = frame_x + ITEM_ICON_SZ + 10
                    text_max_w = (inner_x2 - INNER_PAD2) - text_x - 4

                    display_name = item_name
                    if len(item_name) > 7:
                        display_name = item_name[:6] + ".."

                    draw.text((text_x, row_y + 2), display_name, font=self.font_tiny, fill=self.text)

                    sel_text = f"{item_sel:.1f}%"
                    sel_color = self.sub_text
                    if item_sel >= 30:
                        sel_color = self.style.sel_hi
                    elif item_sel >= 15:
                        sel_color = self.style.sel_mid
                    draw.text((text_x, row_y + 24), sel_text, font=self.font_tiny, fill=sel_color)

        return y2