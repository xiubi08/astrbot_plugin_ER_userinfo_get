"""对局战报卡 v6（蓝色主题 · 行式布局）

- 单栏行参照参考图：头像+等级角标 / 技能×2 / 名字+标签 / K-A-D / 伤害条 / 承伤条 / 第三指标 / 装备（长方形 3+2）
- 伤害与承伤均为百分比条，各以自己的全场最高值为 100%，蓝色柔光渐变
- 卡片、名次、分隔线统一蓝色主色；装备为固定长方形槽位 + 图标按原比例 fit 居中
- 两栏模式（三人/孤狼）不显示胜负标识；不显示人数/队数，不展示起始地点与技能加点
"""
from __future__ import annotations

import threading
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from .match_report import CARD_W, PAD, W, MatchReportRenderer, _fmt
from .render_theme import border_layer, rounded_gradient

BLUE = (59, 130, 246)








_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()


def _cached_sprite(key, build, limit: int = 400):
    """按 key 缓存可复用精灵（渐变色块、发光、分隔线等）。"""
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return hit
    val = build()
    with _CACHE_LOCK:
        if len(_CACHE) > limit:
            _CACHE.clear()
        _CACHE[key] = val
    return val


class _ScaledDraw:
    """把逻辑坐标绘制到 scale 倍画布上。"""

    def __init__(self, draw, scale):
        self._d = draw
        self._s = float(scale)

    @staticmethod
    def _sc(v, s):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(round(v * s))
        if isinstance(v, (tuple, list)):
            return type(v)(_ScaledDraw._sc(x, s) for x in v)
        return v

    def __getattr__(self, name):
        fn = getattr(self._d, name)
        s = self._s
        if name == "textlength":
            def _tl(*a, **k):
                if a:
                    a = (self._sc(a[0], s),) + tuple(a[1:])
                return fn(*a, **k) / s
            return _tl
        if name == "textbbox":
            def _tb(*a, **k):
                if a:
                    a = (self._sc(a[0], s),) + tuple(a[1:])
                return tuple(v / s for v in fn(*a, **k))
            return _tb

        coord_first = {"line", "rectangle", "rounded_rectangle", "ellipse", "arc", "chord",
                       "pieslice", "polygon", "text", "textbbox", "textlength", "multiline_text"}

        def wrapped(*a, **k):
            if name in coord_first and a:
                a = (self._sc(a[0], s),) + tuple(a[1:])
            for key in ("width", "radius"):
                if key in k and isinstance(k[key], (int, float)):
                    k[key] = int(round(k[key] * s))
            return fn(*a, **k)

        return wrapped


class MatchCardV6(MatchReportRenderer):
    def __init__(self, *a, **kw):
        self.S = float(kw.pop("scale", 2.5))
        self.char_names = kw.pop("char_names", None) or {}
        super().__init__(*a, **kw)
        st = self.style
        self.c = {
            "accent": tuple(st.accent),
            "deep": tuple(getattr(st, "stale", None) or st.accent),
            "light": tuple(getattr(st, "pink_border", None) or st.accent),
            "pale": (255, 242, 247),
            "ink": tuple(st.text),
            "sub": tuple(st.sub_text),
        }

    # 单栏
    AVATAR = 40
    SKILL = 24
    ROW_H = 64
    TEAM_HDR = 26
    TEAM_GAP = 8
    HEAD_H = 62
    SLOT_W, SLOT_H = 40, 26
    SLOT_GAP = 6
    SLOT_ROW_GAP = 2
    N_SLOT_GAP = 5
    N_SLOT_ROW_GAP = 2
    MINI = 20
    N_MINI = 14

    # 两栏
    N_AVATAR = 32
    N_SKILL = 18
    N_ROW_H = 56
    N_HDR = 22
    N_SLOT_W, N_SLOT_H = 34, 22
    N_SLOT_GAP = 6
    COL_GAP = 14


    # ---------- 缩放感知的基础方法 ----------
    def font(self, name: str, size: int):
        return super().font(name, max(1, int(round(size * self.S))))

    def icon(self, sub: str, ident, size, cover: bool = False):
        if not getattr(self, "asset_roots", None) or ident in (None, "", 0):
            return None
        if isinstance(size, (tuple, list)):
            max_w, max_h = size
        else:
            max_w = max_h = size
        pw = max(1, int(round(max_w * self.S)))
        ph = max(1, int(round(max_h * self.S)))
        key = (sub, str(ident), pw, ph, cover)
        if key not in self._icons:
            path = None
            for _root in self.asset_roots:
                _cand = _root / sub / f"{ident}.png"
                if _cand.exists():
                    path = _cand
                    break
            im = None
            if path.exists():
                try:
                    src = Image.open(path).convert("RGBA")
                    w0, h0 = src.size
                    if cover:
                        sc = pw / max(1, min(w0, h0))
                        im = src.resize((max(1, round(w0 * sc)), max(1, round(h0 * sc))), Image.LANCZOS)
                        left = (im.width - pw) // 2
                        top = (im.height - pw) // 2
                        im = im.crop((left, top, left + pw, top + pw))
                    else:
                        sc = min(pw / max(1, w0), ph / max(1, h0))
                        tw = max(1, round(w0 * sc))
                        th = max(1, round(h0 * sc))
                        # 4 倍超采样后再缩，边缘平滑
                        im = src.resize((tw * 4, th * 4), Image.LANCZOS).resize((tw, th), Image.LANCZOS)
                except Exception:
                    im = None
            self._icons[key] = im
        return self._icons[key]

    def paste_icon(self, img, ic, xy, radius: int = 6, frame=None, dark: bool = False, box=None) -> int:
        if ic is None:
            return 0
        S = self.S
        used = super().paste_icon(img, ic, (int(xy[0] * S), int(xy[1] * S)),
                                  radius=max(1, int(round(radius * S))), frame=frame, dark=dark,
                                  box=(int(round(box * S)) if box else None))
        return used / S

    # ---------------- 入口 ----------------
    def render_overview(self, data: dict, out_path, scale: float | None = None):
        if scale:
            self.S = float(scale)
        info = self.mode_of(data)
        ms = data.get("matches") or []
        self.max_dmg = max([int(m.get("damageToPlayer", 0) or 0) for m in ms] or [0]) or 1
        self.max_tak = max([int(m.get("damageFromPlayer", 0) or 0) for m in ms] or [0]) or 1
        two = self._two_col(info)
        h = self._height(info, two)
        S = self.S
        img = self._hgrad(int(W * S), int(h * S), (255, 255, 255), (255, 246, 250))
        draw = _ScaledDraw(ImageDraw.Draw(img), S)

        y = self._draw_head(img, draw, data, info)
        ordered = sorted(info["teams_map"].items(),
                         key=lambda kv: min(int(m.get("gameRank", 99) or 99) for m in kv[1]))
        if two:
            col_w = (CARD_W - self.COL_GAP) // 2
            left = ordered[0::2]
            right = ordered[1::2]
            y = self._draw_columns(img, draw, [left, right], info, y, col_w)
        else:
            for team_no, members in ordered:
                y = self._draw_team(img, draw, team_no, members, info, y)

        fb = self.font("MiSans-Regular", 12)
        brand = "Powered by 椛椿&小休比"
        bw = draw.textlength(brand, font=fb)
        draw.text(((W - bw) / 2, h - 26), brand, font=fb, fill=self.c["sub"])
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        img.convert("RGB").save(out, "PNG")
        return out

    @staticmethod
    def _two_col(info) -> bool:
        return info["teams"] >= 4 and info["per_team"] <= 3

    # ---------------- 渐变工具 ----------------
    @staticmethod
    def _hgrad(w, h, c1, c2):
        ramp = Image.linear_gradient("L").resize((w, 1), Image.BILINEAR)
        base = ImageOps.colorize(ramp, tuple(c1[:3]), tuple(c2[:3])).convert("RGBA")
        return base.resize((w, h), Image.BILINEAR)

    @staticmethod
    def _vgrad(w, h, c1, c2, a1=255, a2=255):
        ramp = Image.linear_gradient("L").resize((1, h), Image.BILINEAR)
        base = ImageOps.colorize(ramp, tuple(c1[:3]), tuple(c2[:3])).convert("RGBA")
        alpha = ramp.resize((w, h), Image.BILINEAR).point(
            lambda v: int(round(a1 + (a2 - a1) * v / 255)))
        base = base.resize((w, h), Image.BILINEAR)
        base.putalpha(alpha)
        return base

    # ---------------- 卡片 ----------------

    def _card_body(self, w, h, r, top, bottom):
        body = self._vgrad(w, h, top, bottom)
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=r, fill=255)
        body.putalpha(mask)
        return body

    def _card(self, img, x1, y1, x2, y2):
        S = self.S
        w, h = int(round((x2 - x1 + 1) * S)), int(round((y2 - y1 + 1) * S))
        r = int(round(16 * S))
        px1, py1 = int(round(x1 * S)), int(round(y1 * S))
        body = self._vgrad(w, h, tuple(self.style.card_fill), tuple(self.style.card_fill_bottom or self.style.card_fill))
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=r, fill=255)
        body.putalpha(mask)
        img.alpha_composite(body, (px1, py1))

        # 蓝色描边（上深下浅）
        ring = Image.new("L", (w, h), 0)
        ImageDraw.Draw(ring).rounded_rectangle((0, 0, w - 1, h - 1), radius=r, outline=255, width=3)
        line = self._vgrad(w, h, tuple(self.style.card_border), tuple(self.style.card_border_bottom or self.style.card_border))
        line.putalpha(Image.composite(line.getchannel("A"), Image.new("L", (w, h), 0), ring))
        img.alpha_composite(line, (px1, py1))

        # 柔和外发光
        gpad = int(round(12 * S))
        glow = Image.new("RGBA", (w + gpad * 2, h + gpad * 2), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gd.rounded_rectangle((gpad, gpad, gpad + w - 1, gpad + h - 1), radius=r,
                             outline=tuple(self.style.accent) + (70,), width=max(3, int(4 * S)))
        glow = glow.filter(__import__("PIL.ImageFilter", fromlist=["ImageFilter"]).GaussianBlur(6 * S))
        img.alpha_composite(glow, (px1 - gpad, py1 - gpad))

    # ---------------- 高度 ----------------
    def _col_height(self, teams):
        h = 0
        for i, (_, members) in enumerate(teams):
            h += self.N_HDR + len(members) * self.N_ROW_H + 8
            if i:
                h += 6
        return h

    def _height(self, info, two):
        h = PAD + self.HEAD_H + 8
        ordered = sorted(info["teams_map"].items(),
                         key=lambda kv: min(int(m.get("gameRank", 99) or 99) for m in kv[1]))
        if two:
            h += max(self._col_height(ordered[0::2]), self._col_height(ordered[1::2])) + 8
        else:
            for _, members in ordered:
                h += self.TEAM_HDR + len(members) * self.ROW_H + 10 + self.TEAM_GAP
        h += 46
        return h

    # ---------------- 头部 ----------------
    def _draw_head(self, img, draw, data, info):
        ms = data.get("matches") or []
        m0 = ms[0] if ms else {}
        x1, x2 = PAD, PAD + CARD_W
        y = PAD
        self._card(img, x1, y, x2, y + self.HEAD_H)
        draw = _ScaledDraw(ImageDraw.Draw(img), self.S)

        badge = "钴协议" if info["cobalt"] else ("孤狼" if info["solo"] else info["mode"])
        bw = draw.textlength(badge, font=self.font("MiSans-Bold", 15)) + 22
        draw.rounded_rectangle((x1 + 16, y + 10, x1 + 16 + bw, y + 34), radius=12,
                               fill=self.c["pale"], outline=self.c["accent"], width=1)
        draw.text((x1 + 28, y + 13), badge, font=self.font("MiSans-Bold", 15), fill=self.c["deep"])

        line = (f"对局 {m0.get('gameId','-')} · {str(m0.get('startDtm',''))[:16].replace('T', ' ')} · "
                f"{m0.get('serverName','-')} · 时长 {self._dur(m0.get('playTime'))} · "
                f"版本 S{m0.get('versionSeason','?')}.{m0.get('versionMajor','?')}.{m0.get('versionMinor','?')}")
        f1 = self.font("MiSans-Regular", 12)
        draw.text((x1 + 18, y + 40), self.fit(draw, line, f1, CARD_W - 280), font=f1, fill=self.c["sub"])

        return y + self.HEAD_H + 8

    # ---------------- 两栏 ----------------
    def _draw_columns(self, img, draw, cols, info, y, col_w):
        ends = []
        for ci, teams in enumerate(cols):
            cx1 = PAD + ci * (col_w + self.COL_GAP)
            cy = y
            for ti, (team_no, members) in enumerate(teams):
                if ti:
                    cy += 6
                cy = self._draw_team(img, draw, team_no, members, info, cy, cx1, cx1 + col_w, narrow=True)
            ends.append(cy)
        return max(ends) + 8

    # ---------------- 队伍块 ----------------
    def _draw_team(self, img, draw, team_no, members, info, y, x1=None, x2=None, narrow=False):
        if x1 is None:
            x1, x2 = PAD, PAD + CARD_W
        win = any(int(m.get("victory", 0) or 0) == 1 for m in members)
        best = min(int(m.get("gameRank", 99) or 99) for m in members)
        hdr = self.N_HDR if narrow else self.TEAM_HDR
        row_h = self.N_ROW_H if narrow else self.ROW_H
        box_h = hdr + len(members) * row_h + (8 if narrow else 10)
        self._card(img, x1, y, x2, y + box_h)
        draw = _ScaledDraw(ImageDraw.Draw(img), self.S)

        # 队头：面板底 + 名次 + Team（两栏不显示胜负）
        S = self.S
        hb = self._hgrad(int(round((x2 - x1 - 12) * S)), int(round(hdr * S)),
                         (255, 240, 246), (255, 252, 254))
        mask = Image.new("L", hb.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, hb.width - 1, hb.height - 1),
                                               radius=int(round(10 * S)), fill=255)
        hb.putalpha(mask)
        img.alpha_composite(hb, (int(round((x1 + 6) * S)), int(round((y + 3) * S))))

        if narrow:
            badge = 18
            bx0 = x1 + 9
            self._rank_badge(img, draw, bx0, y + 4, badge, best)
            bx = bx0 + badge + 9
        else:
            # 钴协议只有两队，不显示名次，仅保留 Team 与胜负
            bx = x1 + 14
        if narrow:
            draw.rectangle((bx, y + 6, bx + 2, y + hdr - 5), fill=self.c["accent"])
            draw.text((bx + 8, y + 3), f"Team {team_no}",
                      font=self.font("MiSans-Bold", 12), fill=self.c["ink"])
            # 三人/孤狼不显示胜负
        else:
            # 钴协议：只有两支队伍，不显示 Team 编号，仅突出胜负
            self._result_pill(img, draw, bx, y + 2, 22, win, "胜利" if win else "失败")

            k = sum(int(m.get("playerKill", 0) or 0) for m in members)
            a = sum(int(m.get("playerAssistant", 0) or 0) for m in members)
            d = sum(int(m.get("playerDeaths", 0) or 0) for m in members)
            dmg = sum(int(m.get("damageToPlayer", 0) or 0) for m in members)
            agg = f"K{k}/A{a}/D{d}"
            fa = self.font("MiSans-Regular", 11)
            draw.text((x2 - 14 - draw.textlength(agg, font=fa), y + 5), agg, font=fa, fill=self.c["sub"])
        ry = y + hdr
        for idx, m in enumerate(members):
            if narrow:
                self._row_narrow(img, draw, m, ry, x1, x2, idx)
            else:
                self._row_single(img, draw, m, ry, x1, x2, idx)
            ry += row_h
        return y + box_h

    # ---------------- 行共用组件 ----------------
    def _row_single(self, img, draw, m, ry, x1, x2, idx):
        if idx % 2 == 1:
            draw.rectangle((x1 + 2, ry, x2 - 2, ry + self.ROW_H - 1), fill=(255, 248, 251))
        if idx:
            draw.line((x1 + 10, ry, x2 - 10, ry), fill=(255, 226, 236), width=1)

        # 头像
        self._avatar(img, draw, m, x1 + 12, ry + (self.ROW_H - self.AVATAR) // 2 - 2, self.AVATAR)

        # 名字 + 角色标签（在 2×2 图标左侧）
        tx = x1 + 64
        nick, nf = self._autofit(draw, str(m.get("nickname", ""))[:16], 13, 112, 8)
        draw.text((tx, ry + 9), nick, font=nf, fill=self.c["ink"])
        tag = self._char_label(m)
        tf = self.font("MiSans-Regular", 10)
        tw = draw.textlength(tag, font=tf) + 12
        draw.rounded_rectangle((tx - 4, ry + 27, tx - 4 + tw, ry + 43), radius=8, fill=self.c["pale"])
        draw.text((tx, ry + 30), self.fit(draw, tag, tf, 150), font=tf, fill=self.c["deep"])

        # 武器 / 战术 / 主潜 / 副潜（2×2，无边框）
        self._icon_grid(img, draw, m, x1 + 186, ry + 6, 26, 4)

        self._vsep(img, x1 + 246, ry + 10, ry + self.ROW_H - 10)

        # K/A/D
        draw.text((x1 + 258, ry + 14),
                  f"{m.get('playerKill')} / {m.get('playerAssistant')} / {m.get('playerDeaths')}",
                  font=self.font("MiSans-Bold", 14), fill=self.c["ink"])
        draw.text((x1 + 258, ry + 36), "K / A / D", font=self.font("MiSans-Regular", 8), fill=self.c["sub"])

        # 输出 / 承伤
        dmg = int(m.get("damageToPlayer", 0) or 0)
        draw.text((x1 + 400, ry + 6), "输出", font=self.font("MiSans-Regular", 9), fill=self.c["sub"])
        self._bar(img, draw, x1 + 400, ry + 20, 150, 6, dmg / self.max_dmg, "dmg")
        draw.text((x1 + 400, ry + 30), f"{dmg:,}", font=self.font("MiSans-Bold", 11), fill=self.c["deep"])

        tak = int(m.get("damageFromPlayer", 0) or 0)
        draw.text((x1 + 572, ry + 6), "承伤", font=self.font("MiSans-Regular", 9), fill=(70, 150, 190))
        self._bar(img, draw, x1 + 572, ry + 20, 150, 6, tak / self.max_tak, "tak")
        draw.text((x1 + 572, ry + 30), f"{tak:,}", font=self.font("MiSans-Bold", 11), fill=(45, 130, 170))


        # 装备 3+2（先铺底与图标，再统一叠发光边框）
        equip = (m.get("equipment") or [])[:5]
        grades = m.get("equipmentGrade") or []
        cw = self.SLOT_W + self.SLOT_GAP
        ch = self.SLOT_H + self.SLOT_ROW_GAP
        ex = x2 - 18 - 3 * cw + self.SLOT_GAP
        second = max(0, len(equip) - 3)
        placed = []
        for i, item_id in enumerate(equip):
            row_i, col_i = divmod(i, 3)
            gx = ex + col_i * cw
            if row_i == 1 and second:
                gx += (3 - second) * cw // 2
            gy = ry + (7 if row_i == 0 else 7 + ch)
            grade = grades[i] if i < len(grades) else None
            placed.append((gx, gy, grade))
            self._slot(img, draw, gx, gy, item_id, grade, bg=True, frame=False)
        for gx, gy, grade in placed:
            self._slot(img, draw, gx, gy, None, grade, bg=False, frame=True)
    def _slot(self, img, draw, x, y, item_id, grade, narrow=False, bg=True, frame=True):
        """装备槽：同原战绩卡——固定长方形槽 + 图标等比 fit 居中（可放大）+ 品质色渐变发光边框。"""
        st = self.style
        S = self.S
        w = self.N_SLOT_W if narrow else self.SLOT_W
        h = self.N_SLOT_H if narrow else self.SLOT_H
        pad = 3
        pw, ph = int(round(w * S)), int(round(h * S))
        px, py = int(round(x * S)), int(round(y * S))
        gc = self.grade_color(grade)

        if bg:
            fill = getattr(st, "item_slot_fill", None) or (255, 250, 252, 235)
            plate = rounded_gradient(pw, ph, int(round(getattr(st, "item_radius", 11) * S)), fill, fill)
            if plate is not None:
                img.alpha_composite(plate, (px, py))
            if item_id is not None:
                ic = self.icon("items", item_id, (w - pad * 2, h - pad * 2))
                if ic is not None:
                    img.paste(ic, (px + (pw - ic.width) // 2, py + (ph - ic.height) // 2), ic)
        if not frame:
            return

        warm = tuple((getattr(st, "team_lead_bar2", None) or (255, 172, 120))[:3])
        top = tuple(int(gc[k] * 0.70 + 255 * 0.30) for k in range(3))
        bottom = tuple(int(gc[k] * 0.74 + warm[k] * 0.26) for k in range(3))
        blur = max(2, int(round(min(5.0, float(getattr(st, "item_glow_blur", 6))) * S)))
        gpad = max(8, int(round(min(5.0, float(getattr(st, "item_glow_blur", 6))) * S * 2)))
        sprite, p = border_layer(
            pw, ph, int(round(float(getattr(st, "item_radius", 11)) * S)),
            max(2, int(round(2.0 * S))),
            top + (255,), bottom + (255,),
            glow=gc + (int(min(110, getattr(st, "item_glow_alpha", 215))),),
            glow_blur=blur, glow_gain=float(min(1.9, getattr(st, "item_glow_gain", 2.6))), pad=gpad)
        if sprite is not None:
            img.alpha_composite(sprite, (px - p, py - p))
    def _row_narrow(self, img, draw, m, ry, x1, x2, idx):
        if idx % 2 == 1:
            draw.rectangle((x1 + 2, ry, x2 - 2, ry + self.N_ROW_H - 1), fill=(255, 248, 251))
        if idx:
            draw.line((x1 + 8, ry, x2 - 8, ry), fill=(255, 226, 236), width=1)

        self._avatar(img, draw, m, x1 + 8, ry + 11, self.N_AVATAR)

        # 名字 + 角色标签（在 2×2 左侧）
        tx = x1 + 48
        nick, nf = self._autofit(draw, str(m.get("nickname", ""))[:14], 11, 86, 8)
        draw.text((tx, ry + 7), nick, font=nf, fill=self.c["ink"])
        tag = self._char_label(m)
        tf = self.font("MiSans-Regular", 9)
        tw = draw.textlength(tag, font=tf) + 10
        draw.rounded_rectangle((tx - 3, ry + 23, tx - 3 + tw, ry + 35), radius=6, fill=self.c["pale"])
        draw.text((tx, ry + 25), self.fit(draw, tag, tf, 96), font=tf, fill=self.c["deep"])

        # 2×2 图标（无边框）
        self._icon_grid(img, draw, m, x1 + 132, ry + 5, 18, 3, with_level=False)

        self._vsep(img, x1 + 176, ry + 8, ry + self.N_ROW_H - 8)

        # K/A/D
        kx = x1 + 184
        draw.text((kx, ry + 10),
                  f"{m.get('playerKill')}/{m.get('playerAssistant')}/{m.get('playerDeaths')}",
                  font=self.font("MiSans-Bold", 11), fill=self.c["ink"])
        draw.text((kx, ry + 26), "K/A/D", font=self.font("MiSans-Regular", 8), fill=self.c["sub"])

        # 输出 / 承伤（同排，带标签）
        dmg = int(m.get("damageToPlayer", 0) or 0)
        draw.text((x1 + 244, ry + 6), "输出", font=self.font("MiSans-Regular", 8), fill=self.c["sub"])
        self._bar(img, draw, x1 + 244, ry + 18, 48, 5, dmg / self.max_dmg, "dmg")
        draw.text((x1 + 244, ry + 26), f"{dmg:,}", font=self.font("MiSans-Bold", 9), fill=self.c["deep"])

        tak = int(m.get("damageFromPlayer", 0) or 0)
        draw.text((x1 + 298, ry + 6), "承伤", font=self.font("MiSans-Regular", 8), fill=(70, 150, 190))
        self._bar(img, draw, x1 + 298, ry + 18, 48, 5, tak / self.max_tak, "tak")
        draw.text((x1 + 298, ry + 26), f"{tak:,}", font=self.font("MiSans-Bold", 9), fill=(45, 130, 170))

        # 装备 3+2（先铺底与图标，再统一叠发光边框）
        equip = (m.get("equipment") or [])[:5]
        grades = m.get("equipmentGrade") or []
        cw = self.N_SLOT_W + self.N_SLOT_GAP
        ex = x2 - 12 - 3 * cw + self.N_SLOT_GAP
        second = max(0, len(equip) - 3)
        placed = []
        for i, item_id in enumerate(equip):
            row_i, col_i = divmod(i, 3)
            gx = ex + col_i * cw
            if row_i == 1 and second:
                gx += (3 - second) * cw // 2
            gy = ry + (6 if row_i == 0 else 6 + self.N_SLOT_H + self.N_SLOT_ROW_GAP)
            grade = grades[i] if i < len(grades) else None
            placed.append((gx, gy, grade))
            self._slot(img, draw, gx, gy, item_id, grade, narrow=True, bg=True, frame=False)
        for gx, gy, grade in placed:
            self._slot(img, draw, gx, gy, None, grade, narrow=True, bg=False, frame=True)
    def _draw_cobalt(self, img, draw, data, info, y):
        ms = data.get("matches") or []
        x1, x2 = PAD, PAD + CARD_W
        box_h = self.TEAM_HDR + 4 * 18 + 12
        self._card(img, x1, y, x2, y + box_h)
        draw = _ScaledDraw(ImageDraw.Draw(img), self.S)
        draw.text((x1 + 14, y + 4), "钴协议数据", font=self.font("MiSans-Bold", 12), fill=self.c["deep"])
        f = self.font("MiSans-Regular", 10)
        ry = y + self.TEAM_HDR
        for m in ms[:4]:
            used = [v for v in (m.get("scoredPoint") or []) if v]
            line = (f"{str(m.get('nickname',''))[:10]}  阶段 {used} · 涡轮 {m.get('totalTurbineTakeOver')} · "
                    f"团灭 {m.get('teamElimination')} · 起始 {(m.get('startingItems') or [])} · "
                    f"灌注 {(m.get('finalInfusion') or [])}")
            draw.text((x1 + 14, ry), self.fit(draw, line, f, CARD_W - 28), font=f, fill=self.c["sub"])
            ry += 18
        return y + box_h + 8

    def _draw_meta(self, img, draw, data, y):
        mt = data.get("matchTier") or {}
        pt = data.get("playerTiers") or []
        rp = data.get("replayInfo") or {}
        line = f"matchTier {mt} · playerTiers {_fmt(pt, 36)} · replayInfo {_fmt(rp, 36)}"
        f = self.font("MiSans-Regular", 9)
        draw.text((PAD + 4, y + 2), self.fit(draw, line, f, CARD_W - 8), font=f, fill=self.c["sub"])
        return y + 20

    # ---------------- 基础绘制（缩放感知） ----------------

    def _autofit(self, draw, text, size, max_w, min_size=8, bold=True):
        """从 size 递减字号直到文本放得下；仍超宽才省略。返回 (text, font)。"""
        t = str(text)
        for sz in range(int(size), int(min_size) - 1, -1):
            f = self.font_for(t, sz, bold=bold)
            if draw.textlength(t, font=f) <= max_w:
                return t, f
        f = self.font_for(t, min_size, bold=bold)
        return self.fit(draw, t, f, max_w), f



    def _char_label(self, m) -> str:
        """角色名优先取持久化 characters.json，缺失时退回「角色<id>」。"""
        cid = m.get("characterNum")
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            return "角色"
        name = self.char_names.get(cid)
        return str(name) if name else f"角色{cid}"

    def _result_pill(self, img, draw, x, y, h, win, text):
        """胜负胶囊：渐变底 + 白字。"""
        S = self.S
        f = self.font("MiSans-Bold", int(round(h * 0.6)))
        tw = draw.textlength(text, font=f)
        w = int(tw + h * 1.1)
        if win:
            c1, c2 = (255, 104, 134), (255, 170, 116)
        else:
            c1, c2 = (150, 168, 196), (112, 132, 164)
        pill = rounded_gradient(int(round(w * S)), int(round(h * S)), int(round(h * S)) // 2,
                                tuple(c1) + (255,), tuple(c2) + (255,))
        if pill is not None:
            img.alpha_composite(pill, (int(round(x * S)), int(round(y * S))))
        draw.text((x + (w - tw) / 2, y + h * 0.13), text, font=f, fill=(255, 255, 255))

    def _rank_badge(self, img, draw, x, y, size, rank):
        """名次徽章：金银铜渐变的圆形 + 深色数字。"""
        S = self.S
        if rank == 1:
            c1, c2, tc = (255, 224, 150), (250, 172, 56), (122, 72, 16)
        elif rank == 2:
            c1, c2, tc = (240, 244, 250), (184, 198, 216), (78, 90, 106)
        elif rank == 3:
            c1, c2, tc = (248, 206, 172), (203, 136, 92), (108, 58, 32)
        else:
            c1, c2, tc = (255, 240, 246), (255, 206, 222), (162, 84, 106)
        dia = max(8, int(round(size * S)))
        spr = rounded_gradient(dia, dia, dia // 2, tuple(c1) + (255,), tuple(c2) + (255,))
        if spr is not None:
            img.alpha_composite(spr, (int(round(x * S)), int(round(y * S))))
        f = self.font("MiSans-Bold", int(round(size * 0.54)))
        t = str(rank)
        tw = draw.textlength(t, font=f)
        draw.text((x + size / 2 - tw / 2, y + size * 0.17), t, font=f, fill=tc)

    def _avatar(self, img, draw, m, x, y, size):
        av = self.icon("characters", m.get("characterNum"), size)
        box = size + 2
        if av is not None:
            self.paste_icon(img, av, (x, y), radius=8, frame=self.c["light"], box=box)
        else:
            draw.rounded_rectangle((x, y, x + box, y + box), radius=8, fill=self.c["pale"])
        lv = str(m.get("characterLevel", ""))
        f = self.font("MiSans-Bold", 9)
        bw = draw.textlength(lv, font=f) + 6
        bx, by = x + box - bw + 2, y + box - 12
        draw.rounded_rectangle((bx, by, bx + bw, by + 12), radius=4, fill=(255, 252, 254))
        draw.text((bx + 3, by + 1), lv, font=f, fill=self.c["deep"])

    def _circle_icon(self, img, draw, sub, ident, x, y, size):
        ic = self.icon(sub, ident, size)
        if ic is None:
            return False
        S = self.S
        mask = Image.new("L", ic.size, 0)
        ImageDraw.Draw(mask).ellipse((0, 0, ic.width - 1, ic.height - 1), fill=255)
        img.paste(ic, (int(round(x * S)), int(round(y * S))),
                  Image.composite(ic.getchannel("A"), Image.new("L", ic.size, 0), mask))
        return True


    def _load_path_icon(self, path: str, size: int):
        """按绝对路径加载图标（潜能组图标等），带缓存。"""
        if not path:
            return None
        S = self.S
        px = max(1, int(round(size * S)))
        key = ("path", str(path), px)
        if key not in self._icons:
            im = None
            try:
                src = Image.open(path).convert("RGBA")
                w0, h0 = src.size
                sc = min(px / max(1, w0), px / max(1, h0))
                im = src.resize((max(1, round(w0 * sc)), max(1, round(h0 * sc))), Image.LANCZOS)
            except Exception:
                im = None
            self._icons[key] = im
        return self._icons[key]

    def _circle_path(self, img, draw, path, x, y, size):
        ic = self._load_path_icon(path, size)
        if ic is None:
            return False
        S = self.S
        mask = Image.new("L", ic.size, 0)
        ImageDraw.Draw(mask).ellipse((0, 0, ic.width - 1, ic.height - 1), fill=255)
        img.paste(ic, (int(round(x * S)), int(round(y * S))),
                  Image.composite(ic.getchannel("A"), Image.new("L", ic.size, 0), mask))
        return True

    def _icon_grid(self, img, draw, m, x, y, cell, gap, with_level=True):
        """2×2：武器（方形深底）+ 战术（圆形带等级）/ 主潜能 + 副潜能（圆形）。"""
        S = self.S
        ic = self.icon("masteries", m.get("bestWeapon"), cell)
        if ic is not None:
            draw.rounded_rectangle((x, y, x + cell, y + cell), radius=5, fill=(48, 44, 54))
            cell_px = int(round(cell * S))
            img.paste(ic, (int(round(x * S)) + (cell_px - ic.width) // 2,
                           int(round(y * S)) + (cell_px - ic.height) // 2), ic)

        sub2 = (m.get("traitFirstSub") or [None])[0] if isinstance(m.get("traitFirstSub"), list) else None
        if sub2 is None and isinstance(m.get("traitSecondSub"), list) and m.get("traitSecondSub"):
            sub2 = m["traitSecondSub"][0]
        tx2 = x + cell + gap
        if self._circle_icon(img, draw, "tactical_skills", m.get("tacticalSkillGroup"), tx2, y, cell) and with_level:
            lv = m.get("tacticalSkillLevel")
            if lv:
                f = self.font("MiSans-Bold", 8)
                bw = draw.textlength(str(lv), font=f) + 6
                bx, by = tx2 + cell - bw + 1, y + cell - 9
                draw.rounded_rectangle((bx, by, bx + bw, by + 11), radius=3, fill=(255, 252, 254))
                draw.text((bx + 3, by + 1), str(lv), font=f, fill=self.c["deep"])
        core_path = m.get("_trait_core_icon")
        if not (core_path and self._circle_path(img, draw, core_path, x, y + cell + gap, cell)):
            self._circle_icon(img, draw, "trait_skills", m.get("traitFirstCore"), x, y + cell + gap, cell)
        grp_path = m.get("_trait_group_icon")
        if not (grp_path and self._circle_path(img, draw, grp_path, tx2, y + cell + gap, cell)):
            self._circle_icon(img, draw, "trait_skills", sub2, tx2, y + cell + gap, cell)

    def _bar(self, img, draw, x, y, w, h, ratio, kind="dmg"):
        """进度条：深色渐变 + 柔光。kind=dmg 粉 / tak 水蓝。"""
        if kind == "tak":
            c1, c2 = (56, 189, 248), (125, 211, 252)
            glow = (56, 189, 248, 90)
        else:
            c1, c2 = (240, 68, 107), (255, 143, 168)
            glow = (240, 68, 107, 90)
        S = self.S
        px, py = int(round(x * S)), int(round(y * S))
        pw, ph = max(2, int(round(w * S))), max(2, int(round(h * S)))
        draw.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=(250, 226, 234))
        fw = int(round(pw * max(0.0, min(1.0, ratio))))
        if fw >= ph:
            pad6 = max(2, int(round(6 * S)))
            halo = Image.new("RGBA", (pw + pad6 * 2, ph + pad6 * 2), (0, 0, 0, 0))
            hd = ImageDraw.Draw(halo)
            hd.rounded_rectangle((pad6, pad6, pad6 + fw, pad6 + ph), radius=ph // 2, fill=glow)
            halo = halo.filter(__import__("PIL.ImageFilter", fromlist=["ImageFilter"]).GaussianBlur(3 * S))
            img.alpha_composite(halo, (px - pad6, py - pad6))
            bar = self._hgrad(fw, ph, c1, c2)
            mask = Image.new("L", (fw, ph), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, fw - 1, ph - 1), radius=ph // 2, fill=255)
            bar.putalpha(mask)
            img.alpha_composite(bar, (px, py))

    def _vsep(self, img, x, y1, y2):
        """竖向渐变分隔线（上下淡、中间实）。"""
        S = self.S
        h = max(1, int(round((y2 - y1) * S)))
        lw = max(1, int(round(2 * S)))
        accent = tuple(self.style.accent)

        def _build():
            line = Image.new("RGBA", (lw, h), (0, 0, 0, 0))
            ld = ImageDraw.Draw(line)
            for i in range(h):
                t = i / max(1, h - 1)
                a = int(round(150 * (1 - abs(t - 0.5) * 2) ** 0.6))
                if a > 0:
                    ld.line((0, i, lw - 1, i), fill=accent + (a,))
            return line

        line = _cached_sprite(("vsep", lw, h, accent), _build)
        img.alpha_composite(line, (int(round(x * S)), int(round(y1 * S))))
