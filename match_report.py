"""通用对局战报卡片（多模式）——纯 Pillow，复用现有 sakura 主题的绘制原语。

用法：
    from match_report import MatchReportRenderer
    r = MatchReportRenderer(fonts_dir)
    r.render_overview(data, out_png)      # 概要 + 队伍 + 队员 + 技能 + 专属
    r.render_appendix(data, out_dir)      # 全字段附录（分页）

模式：
    mode 6 / 11  → 钴协议 / 钴联盟：队伍名次 1/2、胜负判定、起始装备/灌注/阶段得分
    teamMode 3   → 三人：个人名次 1..N
    每队 1 人    → 孤狼：队伍表退化为单人榜
    其它         → 按实际队伍结构自适应
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .config import RENDER
from .render_theme import THEMES, blend_at, border_layer, resolve_theme, rounded_gradient

W = 1080
PAD = 20
CARD_W = W - PAD * 2

MODE_NAMES = {2: "三人", 6: "钴协议", 11: "钴联盟"}
TEAM_MODE_NAMES = {1: "孤狼", 2: "双人", 3: "三人", 4: "四人"}

# 概要里重点展示的字段（附录里是全部 331 个）
HERO_FIELDS = [
    ("characterNum", "角色"), ("characterLevel", "等级"), ("accountLevel", "账号等级"),
    ("gameRank", "名次"), ("victory", "胜负"),
    ("playerKill", "击杀"), ("playerAssistant", "助攻"), ("playerDeaths", "死亡"),
    ("teamKill", "团队击杀"), ("monsterKill", "击杀怪物"),
    ("damageToPlayer", "输出"), ("damageFromPlayer", "承受"),
    ("damageToMonster", "打怪"), ("healAmount", "治疗"),
    ("viewContribution", "视野"), ("survivableTime", "存活"),
    ("bestWeapon", "武器"), ("bestWeaponLevel", "武器等级"),
    ("tacticalSkillGroup", "战术"), ("tacticalSkillLevel", "战术等级"),
    ("escapeState", "撤离"), ("botAdded", "补位"), ("isMLBot", "机器人"),
    ("rewardCoin", "金币"), ("gainExp", "经验"), ("mmrGainGambit", "MMR波动"),
    ("routeIdOfStart", "起始路线"), ("placeOfStart", "出生点"),
]
COBALT_FIELDS = [
    ("totalTurbineTakeOver", "涡轮"), ("teamElimination", "团灭"),
    ("battleZonePlayerKill", "战斗区击杀"), ("squadRumbleRank", "乱斗名次"),
]


def _fmt(v: Any, limit: int = 46) -> str:
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, (int, float)):
        return f"{v:,}" if isinstance(v, int) and abs(v) >= 1000 else str(v)
    if isinstance(v, (list, dict)):
        s = str(v)
        return s if len(s) <= limit else s[: limit - 1] + "…"
    s = str(v)
    return s if len(s) <= limit else s[: limit - 1] + "…"


class MatchReportRenderer:
    def __init__(self, fonts_dir: Path | str, theme: str | None = None,
                 assets_dir: Path | str | None = None):
        self.fonts_dir = Path(fonts_dir)
        if assets_dir is None:
            for cand in (self.fonts_dir.parent / "data" / "assets",
                         self.fonts_dir.parent.parent / "data" / "assets",
                         self.fonts_dir.parent.parent.parent / "data" / "assets"):
                if cand.exists():
                    assets_dir = cand
                    break
        self.assets_dir = Path(assets_dir) if assets_dir else None
        self._icons: dict[tuple[str, int], Image.Image | None] = {}
        self.style = resolve_theme(theme or RENDER.theme or "sakura_glow")
        self._fonts: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
        self.text = self.style.text
        self.sub = self.style.sub_text

    # ---------------- 基础绘制 ----------------
    def font(self, name: str, size: int) -> ImageFont.FreeTypeFont:
        key = (name, size)
        if key not in self._fonts:
            path = self.fonts_dir / f"{name}.ttf"
            if not path.exists():
                path = self.fonts_dir / "MiSans-Semibold.ttf"
            self._fonts[key] = ImageFont.truetype(str(path), size)
        return self._fonts[key]

    @staticmethod
    def _has_hangul(t: str) -> bool:
        return any('\uac00' <= ch <= '\ud7a3' for ch in str(t))

    def font_for(self, text, size: int, bold: bool = False):
        if self._has_hangul(text):
            name = "NotoSansKR-Bold" if bold else "NotoSansKR-Bold"
            if not (self.fonts_dir / f"{name}.ttf").exists():
                name = "MiSans-Bold" if bold else "MiSans-Regular"
        else:
            name = "MiSans-Bold" if bold else "MiSans-Regular"
        return self.font(name, size)

    def fit(self, draw, text: str, font, max_w: int) -> str:
        t = str(text)
        if draw.textlength(t, font=font) <= max_w:
            return t
        while t and draw.textlength(t + "…", font=font) > max_w:
            t = t[:-1]
        return t + "…"

    def icon(self, sub: str, ident, size: int, cover: bool = False):
        """按资源目录取图标：assets/<sub>/<ident>.png，带缓存。

        cover=True 时等比缩放到最小边=size 后居中裁成 size×size（装备用，尺寸绝对统一）；
        cover=False 时按比例缩进 size 方框内（角色立绘用，避免切头）。
        """
        if not self.assets_dir or ident in (None, "", 0):
            return None
        key = (str(ident), size, cover)
        if key not in self._icons:
            path = self.assets_dir / sub / f"{ident}.png"
            im = None
            if path.exists():
                try:
                    im = Image.open(path).convert("RGBA")
                    if cover:
                        w, h = im.size
                        scale = size / max(1, min(w, h))
                        im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                                       Image.LANCZOS)
                        left = (im.width - size) // 2
                        top = (im.height - size) // 2
                        im = im.crop((left, top, left + size, top + size))
                    else:
                        im.thumbnail((size, size), Image.LANCZOS)
                except Exception:
                    im = None
            self._icons[key] = im
        return self._icons[key]

    def grade_color(self, grade) -> tuple:
        m = {1: (150, 150, 150), 2: (70, 160, 110), 3: (70, 130, 220),
             4: (160, 90, 210), 5: (200, 150, 60), 6: (200, 70, 80)}
        try:
            return m.get(int(grade), (180, 180, 180))
        except Exception:
            return (180, 180, 180)

    def paste_icon(self, img, ic, xy, radius: int = 6, frame=None, dark: bool = False,
                   box: int | None = None) -> int:
        """贴图标（可选品质色圆角框 / 深色底），返回占用宽度。box=固定方框边长。"""
        if ic is None:
            return 0
        x, y = xy
        w, h = ic.size
        pad = 2
        bw = box if box else max(w, h)
        if frame is not None:
            draw = ImageDraw.Draw(img)
            fill = (34, 32, 40, 240) if dark else (255, 252, 254, 235)
            draw.rounded_rectangle((x, y, x + bw, y + bw),
                                   radius=radius, fill=fill,
                                   outline=frame + (255,), width=2)
        mask = Image.new("L", ic.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1),
                                               radius=radius - 2 if radius > 3 else 2, fill=255)
        ox = x + (bw - w) // 2
        oy = y + (bw - h) // 2
        img.paste(ic, (ox, oy), Image.composite(ic.getchannel("A"), Image.new("L", ic.size, 0), mask))
        return bw

    def new_canvas(self, h: int) -> Image.Image:
        st = self.style
        img = Image.new("RGBA", (W, h), st.bg + (255,))
        if st.bg_top and st.bg_bottom:
            grad = rounded_gradient(W, h, 0, st.bg_top, st.bg_bottom)
            if grad is not None:
                img = grad
        return img

    def card(self, img: Image.Image, x1: int, y1: int, x2: int, y2: int,
             header_h: int = 34, header: str | None = None) -> None:
        st = self.style
        draw = ImageDraw.Draw(img)
        r = int(st.card_radius)
        w = x2 - x1 + 1
        h = y2 - y1 + 1
        if st.card_shadow is not None:
            from .render_theme import shadow_layer
            sprite, pad = shadow_layer(w, h, r, st.card_shadow, st.card_shadow_blur, st.card_shadow_dy)
            blend_at(img, sprite, x1 - pad, y1 - pad)
        blend_at(img, rounded_gradient(w, h, r, st.card_fill, st.card_fill_bottom or st.card_fill,
                                       st.card_rim), x1, y1)
        if st.card_glow is not None:
            from .render_theme import glow_layer
            sprite, pad = glow_layer(w, h, r, st.card_glow, st.card_glow_blur, st.card_glow_gain,
                                     int(st.card_glow_blur) + 4, width=max(1, int(st.card_border_w)),
                                     outside_only=True)
            blend_at(img, sprite, x1 - pad, y1 - pad)
        sprite, pad = border_layer(w, h, r, int(st.card_border_w), st.card_border,
                                   st.card_border_bottom or st.card_border, pad=0)
        blend_at(img, sprite, x1 - pad, y1 - pad)
        if header:
            band = self._section_band(header_h + 6, w - 2)
            blend_at(img, band, x1 + 1, y1 + 1)
            draw.text((x1 + 16, y1 + 8), header, font=self.font("MiSans-Bold", 20), fill=self.text)

    def _section_band(self, h: int, w: int) -> Image.Image:
        """横向柔光标题底（与主卡一致）。"""
        st = self.style
        left = getattr(st, "section_header", None)
        if left is None:
            return Image.new("RGBA", (w, h), (0, 0, 0, 0))
        right = getattr(st, "section_header2", None) or left
        ramp = Image.linear_gradient("L").resize((w, 1), Image.BILINEAR)
        base = ImageOps.colorize(ramp, tuple(left[:3]), tuple(right[:3])).convert("RGBA")
        base = base.resize((w, h), Image.BILINEAR)
        peak = float(left[3])

        def _a(v: float) -> int:
            t = v / 255.0
            k = 0.25 + 0.75 * (t / 0.35) if t < 0.35 else max(0.0, 1.0 - 0.75 * (t - 0.35) / 0.65)
            return int(round(peak * k))

        alpha = ramp.point(_a).resize((w, h), Image.BILINEAR)
        base.putalpha(alpha)
        return base

    def text(self_, draw, xy, s, size=16, bold=False, fill=None):  # noqa: N805
        draw.text(xy, s, font=self_.font("MiSans-Bold" if bold else "MiSans-Regular", size),
                  fill=fill or self_.text)

    # ---------------- 模式判定 ----------------
    @staticmethod
    def mode_of(data: dict) -> dict:
        ms = data.get("matches") or []
        if not ms:
            return {"mode": "空", "team_mode": "?", "size": 0, "teams": 0,
                    "cobalt": False, "solo": False, "per_team": 0}
        m0 = ms[0]
        mode_id = int(m0.get("matchingMode", 0) or 0)
        team_id = int(m0.get("matchingTeamMode", 0) or 0)
        teams: dict[int, list] = {}
        for m in ms:
            teams.setdefault(int(m.get("teamNumber", 0) or 0), []).append(m)
        per = max(len(v) for v in teams.values())
        return {
            "mode": MODE_NAMES.get(mode_id, f"模式{mode_id}"),
            "mode_id": mode_id,
            "team_mode": TEAM_MODE_NAMES.get(team_id, f"队{team_id}"),
            "team_id": team_id,
            "size": len(ms), "teams": len(teams), "per_team": per,
            "cobalt": mode_id in (6, 11),
            "solo": per == 1,
            "teams_map": teams,
        }

    # ---------------- 概要 ----------------
    def render_overview(self, data: dict, out_path: Path | str) -> Path:
        info = self.mode_of(data)
        ms = data.get("matches") or []
        h = self._overview_height(data, info)
        img = self.new_canvas(h)
        draw = ImageDraw.Draw(img)

        y = PAD
        y = self._draw_summary(img, draw, data, info, y)
        y = self._draw_teams(img, draw, data, info, y)
        y = self._draw_players(img, draw, data, info, y)
        y = self._draw_extras(img, draw, data, info, y)
        y = self._draw_tiers_replay(img, draw, data, info, y)
        draw.text((PAD, y + 6), "通用对局战报 · " + str(self.style.label),
                  font=self.font("MiSans-Regular", 14), fill=self.sub)
        img = img.convert("RGB")
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(out, "PNG")
        return out

    def _overview_height(self, data, info) -> int:
        ms = data.get("matches") or []
        h = PAD + 170                       # 概要
        h += 70 + info["teams"] * 52        # 队伍表
        h += 70 + len(ms) * 116             # 队员明细（头像+装备图标）
        if info["cobalt"]:
            h += 70 + 52 + min(len(ms), 4) * 22
        if data.get("playerTiers") or data.get("matchTier") or data.get("replayInfo"):
            h += 70 + 90
        return h + PAD + 40

    def _draw_summary(self, img, draw, data, info, y):
        ms = data.get("matches") or []
        m0 = ms[0] if ms else {}
        x1, x2 = PAD, PAD + CARD_W
        box_h = 150
        self.card(img, x1, y, x2, y + box_h, header_h=30, header="对局概要")
        draw = ImageDraw.Draw(img)

        badge = info["mode"] if not info["solo"] else "孤狼"
        bw = draw.textlength(badge, font=self.font("MiSans-Bold", 16)) + 24
        draw.rounded_rectangle((x1 + 16, y + 42, x1 + 16 + bw, y + 70), radius=14,
                               fill=(255, 236, 242), outline=self.style.accent, width=1)
        draw.text((x1 + 28, y + 46), badge, font=self.font("MiSans-Bold", 16), fill=self.style.accent)

        title = f"{info['size']} 人 · {info['teams']} 队 · 每队 {info['per_team']} 人"
        draw.text((x1 + 32 + bw, y + 44), title, font=self.font("MiSans-Regular", 16), fill=self.sub)

        line2 = (f"对局 {m0.get('gameId', '-')} · {str(m0.get('startDtm',''))[:16]} · "
                 f"{m0.get('serverName','-')} · 时长 {self._dur(m0.get('playTime'))} · "
                 f"版本 S{m0.get('versionSeason','?')}.{m0.get('versionMajor','?')}.{m0.get('versionMinor','?')}")
        draw.text((x1 + 16, y + 80), line2, font=self.font("MiSans-Regular", 15), fill=self.text)

        # 我的队伍（有 playerTiers/matchTier 时补充）
        mt = data.get("matchTier") or {}
        tier_txt = ""
        if mt:
            tier_txt = f" · 段位档 {mt.get('tierType','?')}-{mt.get('tierGrade','?')} · MMR {mt.get('mmr',0)}"
        draw.text((x1 + 16, y + 108),
                  f"模式ID {info.get('mode_id','?')} / 队伍ID {info.get('team_id','?')}{tier_txt}",
                  font=self.font("MiSans-Regular", 14), fill=self.sub)
        return y + box_h + PAD

    @staticmethod
    def _dur(sec) -> str:
        try:
            s = int(sec or 0)
        except Exception:
            return "-"
        return f"{s // 60}:{s % 60:02d}"

    def _draw_teams(self, img, draw, data, info, y):
        teams = info["teams_map"]
        x1, x2 = PAD, PAD + CARD_W
        rows = len(teams)
        box_h = 44 + rows * 50
        self.card(img, x1, y, x2, y + box_h, header_h=30, header="队伍汇总")
        draw = ImageDraw.Draw(img)

        ordered = sorted(teams.items(), key=lambda kv: min(int(m.get("gameRank", 99) or 99) for m in kv[1]))
        ty = y + 42
        for idx, (team_no, members) in enumerate(ordered):
            best = min(int(m.get("gameRank", 99) or 99) for m in members)
            win = any(int(m.get("victory", 0) or 0) == 1 for m in members)
            color = self.style.accent if win else self.style.blue
            if info["cobalt"]:
                rank_txt = f"#{best}"
                res = "胜利" if win else "失败"
            else:
                rank_txt = f"#{best}"
                res = "胜" if win else "负"
            draw.rounded_rectangle((x1 + 16, ty, x1 + 58, ty + 34), radius=8,
                                   fill=(255, 240, 246) if win else (240, 244, 255))
            draw.text((x1 + 26, ty + 6), rank_txt, font=self.font("MiSans-Bold", 15), fill=color)
            draw.rectangle((x1 + 66, ty + 4, x1 + 70, ty + 30), fill=color)
            draw.text((x1 + 78, ty + 6), f"Team {team_no}", font=self.font("MiSans-Bold", 15), fill=self.text)
            draw.text((x1 + 150, ty + 6), res, font=self.font("MiSans-Bold", 15), fill=color)

            k = sum(int(m.get("playerKill", 0) or 0) for m in members)
            d = sum(int(m.get("playerDeaths", 0) or 0) for m in members)
            dmg = sum(int(m.get("damageToPlayer", 0) or 0) for m in members)
            info_txt = f"{len(members)}人  K{k}/D{d}  伤 {dmg:,}  "
            fnt = self.font("MiSans-Regular", 14)
            draw.text((x1 + 200, ty + 6), info_txt, font=fnt, fill=self.sub)
            nx = x1 + 200 + draw.textlength(info_txt, font=fnt)
            for i, mem in enumerate(members[:4]):
                nm = str(mem.get("nickname", ""))[:8]
                sep = "、" if i else ""
                if sep:
                    draw.text((nx, ty + 6), sep, font=fnt, fill=self.sub)
                    nx += draw.textlength(sep, font=fnt)
                nf = self.font_for(nm, 14)
                if nx + draw.textlength(nm, font=nf) > x1 + CARD_W - 16:
                    break
                draw.text((nx, ty + 6), nm, font=nf, fill=self.sub)
                nx += draw.textlength(nm, font=nf)
            ty += 50
        return y + box_h + PAD - 6

    def _draw_players(self, img, draw, data, info, y):
        ms = sorted(data.get("matches") or [],
                    key=lambda m: (int(m.get("teamNumber", 99) or 99), int(m.get("gameRank", 99) or 99)))
        x1, x2 = PAD, PAD + CARD_W
        row_h = 116
        box_h = 44 + len(ms) * row_h
        self.card(img, x1, y, x2, y + box_h, header_h=30, header="队员明细")
        draw = ImageDraw.Draw(img)

        ry = y + 44
        for m in ms:
            win = int(m.get("victory", 0) or 0) == 1
            color = self.style.accent if win else self.sub

            # 队伍-名次 徽章
            draw.rounded_rectangle((x1 + 14, ry + 6, x1 + 56, ry + 34), radius=8,
                                   fill=(255, 240, 246) if win else (242, 240, 246))
            draw.text((x1 + 21, ry + 10), f"{m.get('teamNumber')}-{m.get('gameRank')}",
                      font=self.font("MiSans-Bold", 13), fill=color)

            # 角色头像
            av = self.icon("characters", m.get("characterNum"), 64)
            if av is not None:
                self.paste_icon(img, av, (x1 + 66, ry + 8), radius=10, frame=color)
            else:
                draw.rounded_rectangle((x1 + 66, ry + 8, x1 + 138, ry + 80), radius=10,
                                       fill=(255, 240, 246))

            # 名字 + 两行数据
            tx = x1 + 152
            nick = str(m.get("nickname", ""))[:14]
            nf = self.font_for(nick, 17, bold=True)
            draw.text((tx, ry + 6), self.fit(draw, nick, nf, 360), font=nf, fill=self.text)

            line1 = (f"角色{m.get('characterNum')} Lv{m.get('characterLevel')} · 账号{m.get('accountLevel')} · "
                     f"K{m.get('playerKill')}/A{m.get('playerAssistant')}/D{m.get('playerDeaths')} · "
                     f"TK{m.get('teamKill')} · 输出 {int(m.get('damageToPlayer', 0) or 0):,} · "
                     f"承 {int(m.get('damageFromPlayer', 0) or 0):,}")
            f1 = self.font("MiSans-Regular", 13)
            draw.text((tx, ry + 30), self.fit(draw, line1, f1, CARD_W - 180), font=f1, fill=self.sub)

            line2 = (f"打怪 {int(m.get('damageToMonster', 0) or 0):,} · 治疗 {int(m.get('healAmount', 0) or 0):,} · "
                     f"视野{m.get('viewContribution')} · 存活{m.get('survivableTime')}s · "
                     f"金币{m.get('rewardCoin')} · MMR{m.get('mmrGainGambit')} · "
                     f"机器人{'是' if (m.get('isMLBot') or m.get('mlbot')) else '否'}")
            f2 = self.font("MiSans-Regular", 12)
            draw.text((tx, ry + 50), self.fit(draw, line2, f2, CARD_W - 180), font=f2, fill=self.sub)

            # 装备 5 件（品质色框）
            ix = tx
            equip = m.get("equipment") or []
            grades = m.get("equipmentGrade") or []
            for i, item_id in enumerate(equip[:5]):
                grade = grades[i] if i < len(grades) else None
                ic = self.icon("items", item_id, 38)
                if ic is not None:
                    used = self.paste_icon(img, ic, (ix, ry + 70), radius=8,
                                           frame=self.grade_color(grade))
                    ix += used + 6
                else:
                    draw.rounded_rectangle((ix, ry + 70, ix + 42, ry + 112), radius=8,
                                           outline=self.grade_color(grade) + (255,), width=2)
                    ix += 48

            # 武器 / 战术 / 潜能
            ix += 10
            for sub, ident, sz, cap in (
                ("masteries", m.get("bestWeapon"), 36, None),
                ("tactical_skills", m.get("tacticalSkillGroup"), 36, None),
                ("trait_skills", m.get("traitFirstCore"), 32, "核"),
                ("trait_skills", (m.get("traitFirstSub") or [None])[0] if isinstance(m.get("traitFirstSub"), list) else None, 28, "1"),
                ("trait_skills", (m.get("traitSecondSub") or [None])[0] if isinstance(m.get("traitSecondSub"), list) else None, 28, "2"),
            ):
                ic = self.icon(sub, ident, sz)
                if ic is not None:
                    used = self.paste_icon(img, ic, (ix, ry + 72 + (36 - sz) // 2), radius=6,
                                           frame=self.sub, dark=True)
                    ix += used + 8
            ry += row_h
        return y + box_h + PAD - 6

    def _draw_extras(self, img, draw, data, info, y):
        if not info["cobalt"]:
            return y
        ms = data.get("matches") or []
        x1, x2 = PAD, PAD + CARD_W
        rows = min(len(ms), 4)
        box_h = 52 + rows * 22
        self.card(img, x1, y, x2, y + box_h, header_h=30, header="钴协议专属")
        draw = ImageDraw.Draw(img)
        ey = y + 44
        for m in ms[:4]:
            sp = m.get("scoredPoint") or []
            used = [v for v in sp if v]
            line = (f"#{m.get('userNum')} {str(m.get('nickname',''))[:10]}  阶段得分 {used} · 涡轮{m.get('totalTurbineTakeOver')} · "
                    f"团灭{m.get('teamElimination')} · 起始{(m.get('startingItems') or [])} · 灌注{(m.get('finalInfusion') or [])}")
            fnt = self.font("MiSans-Regular", 12)
            draw.text((x1 + 16, ey), self.fit(draw, line, fnt, CARD_W - 32), font=fnt, fill=self.sub)
            ey += 22
        return y + box_h + PAD - 6

    def _draw_tiers_replay(self, img, draw, data, info, y):
        mt = data.get("matchTier") or {}
        pt = data.get("playerTiers") or []
        rp = data.get("replayInfo") or {}
        if not (mt or pt or rp):
            return y
        x1, x2 = PAD, PAD + CARD_W
        box_h = 90
        self.card(img, x1, y, x2, y + box_h, header_h=30, header="matchTier / playerTiers / replayInfo")
        draw = ImageDraw.Draw(img)
        lines = [
            f"matchTier: {mt}",
            f"playerTiers: {_fmt(pt, 120)}",
            f"replayInfo: {_fmt(rp, 120)}",
        ]
        ly = y + 42
        for line in lines:
            draw.text((x1 + 16, ly), line, font=self.font("MiSans-Regular", 12), fill=self.sub)
            ly += 20
        return y + box_h + PAD - 6

    # ---------------- 全字段附录 ----------------
    def render_appendix(self, data: dict, out_dir: Path | str, per_page: int = 2,
                        columns: int = 3) -> list[Path]:
        ms = data.get("matches") or []
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        pages = []
        col_w = (CARD_W - 32) // columns
        for page_start in range(0, len(ms), per_page):
            chunk = ms[page_start: page_start + per_page]
            plan = []
            for m in chunk:
                keys = [k for k in sorted(m.keys())
                        if k not in ('routeIdOfStart', 'placeOfStart',
                                     'skillLevelInfo', 'skillOrderInfo')]
                rows_per_col = (len(keys) + columns - 1) // columns
                plan.append((m, keys, rows_per_col))
            h = 60 + sum(26 + rc * 16 + 16 for _, _, rc in plan) + 30
            img = self.new_canvas(h)
            draw = ImageDraw.Draw(img)
            draw.text((PAD, 14), f"全字段附录 · 第 {page_start // per_page + 1} 页 / "
                                 f"{(len(ms) + per_page - 1) // per_page}",
                      font=self.font("MiSans-Bold", 18), fill=self.text)
            y = 48
            for m, keys, rows_per_col in plan:
                head = (f"userNum {m.get('userNum')} · {m.get('nickname')} · "
                        f"team {m.get('teamNumber')} · rank {m.get('gameRank')} · 字段 {len(keys)}")
                hf = self.font_for(head, 15, bold=True)
                draw.text((PAD, y), head, font=hf, fill=self.style.accent)
                y += 26
                for c in range(columns):
                    col_keys = keys[c * rows_per_col:(c + 1) * rows_per_col]
                    cx = PAD + c * col_w
                    cy = y
                    for k in col_keys:
                        draw.text((cx, cy), f"{k}={_fmt(m.get(k), 30)}",
                                  font=self.font("MiSans-Regular", 11), fill=self.sub)
                        cy += 16
                y += rows_per_col * 16 + 16
            p = out / f"appendix_p{page_start // per_page + 1}.png"
            img.convert("RGB").save(p, "PNG")
            pages.append(p)
        return pages
