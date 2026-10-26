from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .asset_manager import AssetManager
from .config import QUERY, MAPPER


GRADE_ROMAN_MAP = {
    1: "I",
    2: "II",
    3: "III",
    4: "IV",
}

TIERS_WITH_GRADE_BASE = {1, 2, 3, 4, 5, 6}

METEORITE_TIER_ID = 63
METEORITE_GRADE_START_SEASON = 19

AREA_NAME_MAP_ZH = {
    10: "港口",
    20: "仓库",
    30: "池塘",
    40: "小溪",
    50: "沙滩",
    60: "高级住宅区",
    70: "小巷",
    80: "加油站",
    90: "酒店",
    100: "警察局",
    110: "消防局",
    120: "医院",
    130: "寺庙",
    140: "射箭场",
    150: "墓地",
    160: "森林",
    170: "工厂",
    180: "教堂",
    190: "学校",
}


class DataMapper:
    SEASON_ID_MAP: dict[str, int] = {
        **{f"SEASON_{i + 9}": 2 * (i + 9) - 1 for i in range(1, 21)},
        **{f"PRE_SEASON_{i + 9}": 2 * (i + 9) - 2 for i in range(1, 21)},
    }

    def __init__(self, assets: AssetManager, seasons_data: list[dict[str, Any]] | None = None):
        self.assets = assets
        self.seasons = seasons_data or []

    def build_render_data(
        self,
        profile_data: dict[str, Any],
        matches_data: dict[str, Any],
        match_count: int = QUERY.match_count,
        cutoff_mmr: dict[str, int] | None = None,
        season_key: str | None = None,
        matching_mode_id: int | None = None,
        mode_label: str = "",
        sync_updated: bool = True,
    ) -> dict[str, Any]:
        # 确定有效模式：默认(排位)若无对局则回退到匹配模式
        effective_mode_id = matching_mode_id
        effective_mode_label = mode_label

        if matching_mode_id == 3 or matching_mode_id is None:
            rank_overview = self._get_mode_overview(profile_data, season_key, matching_mode_id=3)
            rank_play = int(rank_overview.get("play", 0) or 0)
            if rank_play == 0:
                effective_mode_id = 2
                effective_mode_label = "匹配"

        player_data = self._build_player(profile_data, cutoff_mmr, season_key, matching_mode_id=effective_mode_id)
        player_data["mode_label"] = effective_mode_label
        player_data["sync_updated"] = sync_updated

        return {
            "player": player_data,
            "summary": self._build_summary(profile_data, season_key, matching_mode_id=effective_mode_id),
            "common_heroes": self._build_common_heroes(profile_data, season_key, limit=MAPPER.common_heroes_limit, matching_mode_id=effective_mode_id),
            "matches": self._build_matches(matches_data, match_count),
            "mode_label": effective_mode_label,
            "sync_updated": sync_updated,
        }

    # ============================================================
    # 排名 / 赛季
    # ============================================================

    def _season_type_to_id(self, season_type: str) -> int:
        for season in self.seasons:
            if season.get("type") == season_type:
                return int(season.get("id", -1))
        return -1

    def _get_mode_overview(self, profile_data: dict[str, Any], season_key: str | None = None, matching_mode_id: int | None = None) -> dict[str, Any]:
        overviews = profile_data.get("playerSeasonOverviews", [])

        if season_key and season_key in self.SEASON_ID_MAP:
            target_id = self.SEASON_ID_MAP[season_key]
            filtered = [item for item in overviews if int(item.get("seasonId", -1)) == target_id]
        else:
            filtered = overviews

        mode_id = matching_mode_id if matching_mode_id is not None else 3

        for item in filtered:
            if int(item.get("matchingModeId", -1)) == mode_id and int(item.get("teamModeId", -1)) == 3:
                return item

        for item in filtered:
            if int(item.get("matchingModeId", -1)) == mode_id:
                return item

        return {}

    def _get_rank_overview(self, profile_data: dict[str, Any], season_key: str | None = None, matching_mode_id: int | None = None) -> dict[str, Any]:
        overview = self._get_mode_overview(profile_data, season_key, matching_mode_id=matching_mode_id)
        return overview if "rank" in overview else {}

    def _get_rank_value(self, profile_data: dict[str, Any], scope: str, season_key: str | None = None, matching_mode_id: int | None = None) -> int:
        overview = self._get_rank_overview(profile_data, season_key, matching_mode_id=matching_mode_id)
        return int(overview.get("rank", {}).get(scope, {}).get("rank", 0) or 0)

    def _get_rank_size(self, profile_data: dict[str, Any], scope: str, season_key: str | None = None, matching_mode_id: int | None = None) -> int:
        overview = self._get_rank_overview(profile_data, season_key, matching_mode_id=matching_mode_id)
        return int(overview.get("rank", {}).get(scope, {}).get("rankSize", 0) or 0)

    def _get_rank_percent(self, profile_data: dict[str, Any], scope: str, season_key: str | None = None, matching_mode_id: int | None = None) -> float:
        rank = self._get_rank_value(profile_data, scope, season_key, matching_mode_id=matching_mode_id)
        rank_size = self._get_rank_size(profile_data, scope, season_key, matching_mode_id=matching_mode_id)
        if rank <= 0 or rank_size <= 0:
            return 0.0
        return rank / rank_size * 100

    def _get_season_data(self, profile_data: dict[str, Any], season_key: str | None = None) -> dict[str, Any] | None:
        player_seasons = profile_data.get("playerSeasons", [])

        if season_key and season_key in self.SEASON_ID_MAP:
            target_id = self.SEASON_ID_MAP[season_key]
            for item in player_seasons:
                if int(item.get("seasonId", -1)) == target_id:
                    return item
            return None

        for item in player_seasons:
            if "tierId" in item or "mmr" in item:
                return item

        return {}

        
    def _get_cutoff_mmr_values(self, leaderbord_data: dict [str, Any]) -> dict[str, int]:
        cutoffs = leaderbord_data.get("cutoffs", [])
        mmr_values = []
        for item in cutoffs:
            if isinstance(item, dict) and "mmr" in item:
                mmr = item.get("mmr", 0)
                if isinstance(mmr, int):
                    mmr_values.append(mmr)
        mmr_values.sort(reverse=True)
        
        return {
            "Immortal_mmr": mmr_values[0] if len(mmr_values) > 0 else 0,
            "Titan_mmr": mmr_values[1] if len(mmr_values) > 1 else 0
        }

    def _get_cutoff_player_rank(self, leaderboard_data: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
        """
        获取前N名玩家数据
        返回格式化的玩家信息列表
        """
        players = leaderboard_data.get("leaderboards", [])
        result = []
        
        for i, player in enumerate(players[:limit]):
            player_info = {
                "rank": i + 1,
                "player_name": player.get("nickname", player.get("playerName", "未知")),
                "mmr": player.get("mmr", 0),
                "win_rate": float(player.get("winRate", 0)) * 100,  # 转换为百分比
                "games": player.get("playCount", player.get("games", 0)),
                "avg_rank": player.get("avgPlacement", player.get("averageRank", 0)),
                "top3_rate": float(player.get("top3Rate", 0)) * 100,  # 转换为百分比
                "avg_kills": player.get("avgPlayerKill", 0),
                "character_ids": player.get("characterIds", []),
                "most_characters": player.get("mostCharacters", [])
            }
            result.append(player_info)
        
        return result
    

    # ============================================================
    # 玩家头部
    # ============================================================

    def _build_player(self, profile_data: dict[str, Any], cutoff_mmr: dict[str, int] | None = None, season_key: str | None = None, matching_mode_id: int | None = None) -> dict[str, Any]:
        player = profile_data.get("player", {})
        season_info = self._get_season_data(profile_data, season_key)
        rank_overview = self._get_rank_overview(profile_data, season_key, matching_mode_id=matching_mode_id)

        has_ranked_data = bool(season_info and ("mmr" in season_info or "tierId" in season_info))

        if has_ranked_data:
            tier_id = int(season_info.get("tierId", 0) or rank_overview.get("tierId", 0) or 0)
            mmr = int(season_info.get("mmr", 0) or rank_overview.get("mmr", 0) or 0)
            tier_mmr = int(season_info.get("tierMmr", 0) or rank_overview.get("tierMmr", 0) or 0)
            tier_grade_id = int(season_info.get("tierGradeId", 0) or rank_overview.get("tierGradeId", 0) or 0)
        else:
            tier_id = 0
            mmr = 0
            tier_mmr = 0
            tier_grade_id = 0

        has_grade = False
        if has_ranked_data and tier_grade_id in GRADE_ROMAN_MAP:
            if tier_id in TIERS_WITH_GRADE_BASE:
                has_grade = True
            elif tier_id == METEORITE_TIER_ID:
                season_num = 0
                if season_key and season_key.startswith("SEASON_"):
                    try:
                        season_num = int(season_key.split("_")[1])
                    except (ValueError, IndexError):
                        season_num = 0
                if season_num >= METEORITE_GRADE_START_SEASON:
                    has_grade = True

        if has_grade:
            tier_name = f"{self.assets.get_tier_name(tier_id)} {GRADE_ROMAN_MAP[tier_grade_id]}"
        else:
            tier_name = self.assets.get_tier_name(tier_id) if has_ranked_data else ""

        return {
            "name": player.get("name", ""),
            "level": int(player.get("accountLevel", 0) or 0),
            "mmr": mmr,
            "has_ranked_data": has_ranked_data,

            "tier_id": tier_id,
            "tier_name": tier_name,
            "tier_grade_id": tier_grade_id,
            "tier_icon": self.assets.get_tier_icon_path(tier_id) if has_ranked_data else "",
            "tier_full": self.assets.get_tier_full_path(tier_id) if has_ranked_data else "",
            "tier_mmr": tier_mmr,

            "mmr_chart": self._build_mmr_chart(profile_data, season_key, matching_mode_id=matching_mode_id) if has_ranked_data else [],

            "rank_global": self._get_rank_value(profile_data, "global", season_key, matching_mode_id=matching_mode_id) if has_ranked_data else 0,
            "rank_global_size": self._get_rank_size(profile_data, "global", season_key, matching_mode_id=matching_mode_id) if has_ranked_data else 0,
            "rank_global_percent": self._get_rank_percent(profile_data, "global", season_key, matching_mode_id=matching_mode_id) if has_ranked_data else 0.0,

            "rank_local": self._get_rank_value(profile_data, "local", season_key, matching_mode_id=matching_mode_id) if has_ranked_data else 0,
            "rank_local_size": self._get_rank_size(profile_data, "local", season_key, matching_mode_id=matching_mode_id) if has_ranked_data else 0,
            "rank_local_percent": self._get_rank_percent(profile_data, "local", season_key, matching_mode_id=matching_mode_id) if has_ranked_data else 0.0,

            "cutoff_mmr": cutoff_mmr,
            "Immortal_mmr": cutoff_mmr.get("Immortal_mmr", 0) if cutoff_mmr else 0,
            "Titan_mmr": cutoff_mmr.get("Titan_mmr", 0) if cutoff_mmr else 0,
        }

    # ============================================================
    # overview 查找
    # ============================================================

    def _find_overview(
        self,
        profile_data: dict[str, Any],
        matching_mode_id: int | None = None,
        team_mode_id: int | None = None,
        require_rank: bool = False,
        season_key: str | None = None,
    ) -> dict[str, Any] | None:
        overviews = profile_data.get("playerSeasonOverviews", [])

        if season_key and season_key in self.SEASON_ID_MAP:
            target_id = self.SEASON_ID_MAP[season_key]
            overviews = [item for item in overviews if int(item.get("seasonId", -1)) == target_id]

        for item in overviews:
            if matching_mode_id is not None and int(item.get("matchingModeId", -1)) != int(matching_mode_id):
                continue
            if team_mode_id is not None and int(item.get("teamModeId", -1)) != int(team_mode_id):
                continue
            if require_rank and "rank" not in item:
                continue
            return item

        return overviews[0] if overviews else None

    def _find_all_overview(self, profile_data: dict[str, Any], season_key: str | None = None) -> dict[str, Any] | None:
        overviews = profile_data.get("playerSeasonOverviews", [])

        if season_key and season_key in self.SEASON_ID_MAP:
            target_id = self.SEASON_ID_MAP[season_key]
            overviews = [item for item in overviews if int(item.get("seasonId", -1)) == target_id]

        for item in overviews:
            if int(item.get("matchingModeId", -1)) == 0:
                return item

        return self._find_overview(profile_data, None, season_key=season_key)

    def _find_rank_overview(self, profile_data: dict[str, Any], season_key: str | None = None, matching_mode_id: int | None = None) -> dict[str, Any] | None:
        overview = self._get_rank_overview(profile_data, season_key, matching_mode_id=matching_mode_id)
        return overview or None

    def _find_mode_overview(self, profile_data: dict[str, Any], season_key: str | None = None, matching_mode_id: int | None = None) -> dict[str, Any] | None:
        overview = self._get_mode_overview(profile_data, season_key, matching_mode_id=matching_mode_id)
        return overview or None

    # ============================================================
    # 特性辅助
    # ============================================================

    def _get_trait_sub1_icon(self, match: dict[str, Any]) -> str:
        subs = match.get("traitFirstSub", [])
        if not subs:
            return ""

        trait = self.assets.build_trait_render_data(int(subs[0]))
        return trait.get("icon", "")

    def _get_trait_group_key(self, match: dict[str, Any]) -> str:
        core = match.get("traitFirstCore")
        if not core:
            return ""

        trait = self.assets.get_trait_skill(int(core))
        return trait.get("group", "")

    def _get_trait_group_name(self, match: dict[str, Any]) -> str:
        group_key = self._get_trait_group_key(match)
        if not group_key:
            return ""

        group = self.assets.get_trait_group(group_key)
        return group.get("name", "")

    def _get_trait_names(self, match: dict[str, Any]) -> list[str]:
        result: list[str] = []

        core = match.get("traitFirstCore")
        if core:
            trait = self.assets.get_trait_skill(int(core))
            name = trait.get("name", "")
            if name:
                result.append(name)

        for trait_id in match.get("traitFirstSub", []):
            trait = self.assets.get_trait_skill(int(trait_id))
            name = trait.get("name", "")
            if name:
                result.append(name)

        for trait_id in match.get("traitSecondSub", []):
            trait = self.assets.get_trait_skill(int(trait_id))
            name = trait.get("name", "")
            if name:
                result.append(name)

        return result

    def _get_trait_group_icon(self, match: dict[str, Any]) -> str:
        group_key = self._get_trait_group_key(match)
        if not group_key:
            return ""
        return self.assets.get_trait_group_icon_path(group_key)

    def _get_trait_core_icon(self, match: dict[str, Any]) -> str:
        core = match.get("traitFirstCore")
        if not core:
            return ""
        trait = self.assets.build_trait_render_data(int(core))
        return trait.get("icon", "")

    def _get_trait_second_group_key(self, match: dict[str, Any]) -> str:
        second_subs = match.get("traitSecondSub", [])
        if not second_subs:
            return ""

        trait = self.assets.get_trait_skill(int(second_subs[0]))
        return trait.get("group", "")

    def _get_trait_second_group_icon(self, match: dict[str, Any]) -> str:
        group_key = self._get_trait_second_group_key(match)
        if not group_key:
            return ""
        return self.assets.get_trait_group_icon_path(group_key)

    # ============================================================
    # MMR 折线图
    # ============================================================

    def _build_mmr_chart(self, profile_data: dict[str, Any], season_key: str | None = None, matching_mode_id: int | None = None) -> list[dict[str, Any]]:
        overview = self._get_rank_overview(profile_data, season_key, matching_mode_id=matching_mode_id)

        if not overview:
            overviews = profile_data.get("playerSeasonOverviews", [])
            if overviews:
                overview = overviews[0]

        if not overview:
            return []

        mmr_stats = overview.get("mmrStats", [])
        temp = []

        for row in mmr_stats:
            if not isinstance(row, list) or len(row) < 2:
                continue

            raw_date = str(row[0])
            value = row[1]

            if len(raw_date) == 8:
                label = f"{raw_date[4:6]}/{raw_date[6:8]}"
            else:
                label = raw_date

            temp.append({
                "label": label,
                "value": value,
                "raw_date": raw_date,
            })

        if not temp:
            return []

        latest_points = temp[:6]
        latest_points.reverse()

        return [{"label": p["label"], "value": p["value"]} for p in latest_points]

    # ============================================================
    # 汇总统计（12字段）
    # ============================================================

    def _build_summary(self, profile_data: dict[str, Any], season_key: str | None = None, matching_mode_id: int | None = None) -> dict[str, Any]:
        overview = self._find_mode_overview(profile_data, season_key, matching_mode_id=matching_mode_id)

        if overview is None:
            return {
                "avg_tk": 0.0,
                "win_rate": 0.0,
                "play": 0,
                "avg_kill": 0.0,
                "top2_rate": 0.0,
                "avg_damage": 0.0,
                "avg_assist": 0.0,
                "top3_rate": 0.0,
                "avg_rank": 0.0,
                "avg_monster_kill": 0.0,
                "avg_vf_credit": 0.0,
                "avg_view_contribution": 0.0,
            }

        play_raw = int(overview.get("play", 0) or 0)
        play = max(play_raw, 1)

        win = float(overview.get("win", 0) or 0)
        top2 = float(overview.get("top2", 0) or 0)
        top3 = float(overview.get("top3", 0) or 0)

        team_kill = float(overview.get("teamKill", 0) or 0)
        player_kill = float(overview.get("playerKill", 0) or 0)
        player_assistant = float(overview.get("playerAssistant", 0) or 0)
        damage_to_player = float(overview.get("damageToPlayer", 0) or 0)
        place = float(overview.get("place", 0) or 0)
        monster_kill = float(overview.get("monsterKill", 0) or 0)
        total_gain_vf_credit = float(overview.get("totalGainVFCredit", 0) or 0)
        view_contribution = float(overview.get("viewContribution", 0) or 0)

        return {
            "avg_tk": round(team_kill / play, 2),
            "win_rate": round(win / play * 100, 1),
            "play": play_raw,
            "avg_kill": round(player_kill / play, 2),
            "top2_rate": round(top2 / play * 100, 1),
            "avg_damage": round(damage_to_player / play, 1),
            "avg_assist": round(player_assistant / play, 2),
            "top3_rate": round(top3 / play * 100, 1),
            "avg_rank": round(place / play, 2),
            "avg_monster_kill": round(monster_kill / play, 1),
            "avg_vf_credit": round(total_gain_vf_credit / play, 0),
            "avg_view_contribution": round(view_contribution / play, 2),
        }

    # ============================================================
    # 常用英雄（rank）
    # ============================================================

    def _build_common_heroes(self, profile_data: dict[str, Any], season_key: str | None = None, limit: int = 0, matching_mode_id: int | None = None) -> list[dict[str, Any]]:
        overview = self._find_mode_overview(profile_data, season_key, matching_mode_id=matching_mode_id)

        if overview is None:
            return []

        result = []
        stats = overview.get("characterStats", [])
        if limit > 0:
            stats = stats[:limit]
        for stat in stats:
            char_id = int(stat.get("key", 0) or 0)
            play = int(stat.get("play", 0) or 0)
            if play <= 0:
                continue

            avg_kill = (stat.get("playerKill", 0) or 0) / play
            avg_assist = (stat.get("playerAssistant", 0) or 0) / play
            avg_damage = (stat.get("damageToPlayer", 0) or 0) / play
            avg_rank = (stat.get("place", 0) or 0) / play
            mmr_gain = (stat.get("mmrGain", 0) or 0)

            result.append({
                "character_id": char_id,
                "character_name": self.assets.get_character_name(char_id),
                "character_icon": self.assets.get_character_icon_path(char_id),
                "play": play,
                "win": int(stat.get("win", 0) or 0),
                "win_rate": round((stat.get("win", 0) or 0) / play * 100, 1),
                "avg_rank": round(avg_rank, 2),
                "avg_kill": round(avg_kill, 2),
                "avg_assist": round(avg_assist, 2),
                "avg_damage": round(avg_damage, 1),
                "mmr_gain": mmr_gain
            })

        return result

    # ============================================================
    # 对局
    # ============================================================

    def _build_matches(self, matches_data: dict[str, Any], match_count: int) -> list[dict[str, Any]]:
        result = []
        for match in matches_data.get("matches", [])[:match_count]:
            result.append(self._build_single_match(match))
        return result

    def _build_single_match(self, match: dict[str, Any]) -> dict[str, Any]:
        char_id = int(match.get("characterNum", 0) or 0)
        weapon_id = int(match.get("bestWeapon", 0) or 0)
        tactical_id = int(match.get("tacticalSkillGroup", 0) or 0)

        skin_id = int(match.get("skinCode", 0) or 0)
        skin_icon = self.assets.get_character_skin_icon_path(skin_id) if skin_id else ""

        matching_mode_raw = match.get("matchingMode", "")
        matching_mode = str(matching_mode_raw or "").upper()
        
        # 模式映射：数值 -&gt; (模式英文, 模式中文)
        mode_map = {
            2: ("NORMAL", "匹配"),
            3: ("RANK", "排位"),
            6: ("COBALT", "钴协议"),
            8: ("SQUAD_RUMBLE", "联盟"),
            9: ("LONE_WOLF", "孤狼"),
            11: ("SQUAD_RUMBLE_COBALT", "钴联盟"),
        }
        
        # 尝试将 matching_mode_raw 转为整数并查找对应模式
        mode_enum = "UNKNOWN"
        mode_label = "未知"
        is_rank_match = False
        
        try:
            mode_num = int(matching_mode_raw)
            if mode_num in mode_map:
                mode_enum, mode_label = mode_map[mode_num]
                is_rank_match = mode_num == 3
        except (ValueError, TypeError):
            # 如果转换失败，尝试用字符串匹配
            if matching_mode == "RANK":
                mode_enum = "RANK"
                mode_label = "排位"
                is_rank_match = True
            else:
                mode_enum = "NORMAL"
                mode_label = "匹配"

        path_id = (
            match.get("routeIdOfStart")
            or match.get("pathId")
            or match.get("routeId")
            or match.get("routeID")
            or 0
        )

        place_of_start = int(match.get("placeOfStart", 0) or 0)
        start_area = AREA_NAME_MAP_ZH.get(place_of_start, "")

        return {
            "game_id": match.get("gameId"),
            "rank": int(match.get("gameRank", 0) or 0),
            # escapeState: 3 表示逃生成功
            "escaped": int(match.get("escapeState", 0) or 0) == 3,

            "character_id": char_id,
            "character_name": self.assets.get_character_name(char_id),
            "character_icon": skin_icon or self.assets.get_character_icon_path(char_id),
            "skin_id": skin_id,

            "weapon_id": weapon_id,
            "weapon_name": self.assets.get_mastery_name(weapon_id),
            "weapon_icon": self.assets.get_mastery_icon_path(weapon_id),

            "kda": f"{int(match.get('playerKill', 0) or 0)}/{int(match.get('playerDeaths', 0) or 0)}/{int(match.get('playerAssistant', 0) or 0)}",
            "kills": int(match.get("playerKill", 0) or 0),
            "deaths": int(match.get("playerDeaths", 0) or 0),
            "assists": int(match.get("playerAssistant", 0) or 0),
            "team_kill": int(match.get("teamKill", 0) or 0),

            "damage": int(match.get("damageToPlayer", 0) or 0),

            "mmr_before": int(match.get("mmrBefore", 0) or 0),
            "mmr_after": int(match.get("mmrAfter", 0) or 0),
            "mmr_change": int(match.get("mmrGain", 0) or 0),

            "matching_mode": matching_mode,
            "matching_mode_enum": mode_enum,
            "is_rank_match": is_rank_match,
            "mode_label": mode_label,

            "path_id": int(path_id or 0),
            "start_area": str(start_area or ""),

            "start_time": self._format_time(match.get("startDtm", "")),
            "server_name": str(match.get("serverName", "") or ""),

            "tactical_skill": self.assets.build_tactical_skill_render_data(tactical_id) if tactical_id else {},
            "tactical_skill_level": int(match.get("tacticalSkillLevel", 0) or 0),

            "traits": self._build_traits(match),
            "trait_core_icon": self._get_trait_core_icon(match),
            "trait_second_group_icon": self._get_trait_second_group_icon(match),

            "items": self._build_items(match),
        }

    # ============================================================
    # 装备 / traits / 时间
    # ============================================================

    def _build_items(self, match: dict[str, Any]) -> list[dict[str, Any]]:
        equipment = match.get("equipment", [])
        # 装备完整时为列表（按部位顺序），只装备部分部位时为 {部位下标: 物品ID} 字典
        if isinstance(equipment, dict):
            try:
                raw_items = sorted(equipment.items(), key=lambda kv: int(kv[0]))
            except (TypeError, ValueError):
                raw_items = list(equipment.items())
        elif isinstance(equipment, list):
            raw_items = list(enumerate(equipment))
        else:
            raw_items = []

        items = []
        for slot, item_id in raw_items:
            try:
                slot = int(slot)
                item_id = int(item_id)
            except (TypeError, ValueError):
                continue
            if item_id <= 0:
                continue
            item = self.assets.build_item_render_data(item_id)
            item["slot"] = slot
            items.append(item)
        return items

    def _build_traits(self, match: dict[str, Any]) -> list[dict[str, Any]]:
        result = []

        core = match.get("traitFirstCore")
        if core:
            result.append(self.assets.build_trait_render_data(int(core)))

        for trait_id in match.get("traitFirstSub", []):
            result.append(self.assets.build_trait_render_data(int(trait_id)))

        for trait_id in match.get("traitSecondSub", []):
            result.append(self.assets.build_trait_render_data(int(trait_id)))

        return result

    def _format_time(self, value: str) -> str:
        if not value:
            return ""

        try:
            dt = datetime.fromisoformat(value)
            dt = dt - timedelta(hours=1)
            return dt.strftime("%m-%d %H:%M")
        except Exception:
            return value

    def _resolve_mastery_render(self, assets: AssetManager, weapon_key: Any) -> tuple[str, str]:
        """根据 weaponStats 的 key 解析武器名称与图标路径。

        weaponStats 的 key 可能是 mastery 的 id（数字）或 key（字符串），
        优先按 id 匹配，失败后按 key 匹配，兜底返回占位文本。
        """
        name = ""
        icon = ""
        try:
            mid = int(weapon_key)
            data = assets.get_mastery(mid)
            if data:
                name = data.get("name", "")
                icon = assets.get_mastery_icon_path(mid)
        except (ValueError, TypeError):
            mid = None
            name = ""
        if not name:
            for mid, mdata in assets.masteries.items():
                if str(mdata.get("key", "")) == str(weapon_key):
                    name = mdata.get("name", "")
                    icon = assets.get_mastery_icon_path(int(mid))
                    break
        if not name:
            name = f"武器{weapon_key}"
        return name, icon

    def build_character_ranking_data(
        self,
        stats_data: dict[str, Any],
        assets: AssetManager,
        tier: str,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """将 character-stats 快照展开为「角色+武器」维度并按综合评分(tierScore)降序排列。

        同一角色的不同武器作为独立条目保留，避免混淆。
        limit 为 None 时返回全部条目（默认）。
        """
        snapshot = stats_data.get("characterStatSnapshot", {})
        character_stats = snapshot.get("characterStats", [])
        tier_count = snapshot.get("tierCount", 0)

        entries: list[dict[str, Any]] = []
        for char in character_stats:
            char_key = char.get("key")
            try:
                char_id = int(char_key)
            except (ValueError, TypeError):
                char_id = 0
            char_name = assets.get_character_name(char_id)
            char_icon = assets.get_character_icon_path(char_id)

            for ws in char.get("weaponStats", []):
                count = int(ws.get("count", 0) or 0)
                if count <= 0:
                    continue

                weapon_key = ws.get("key")
                weapon_name, weapon_icon = self._resolve_mastery_render(assets, weapon_key)

                tier_badge = str(ws.get("tier", "") or "")
                tier_badge_path = assets.get_character_tier_badge_path(tier_badge) if tier_badge else ""
                tier_score = ws.get("tierScore", 0) or 0

                avg_damage = round((ws.get("damageToPlayer", 0) or 0) / count)
                avg_mmr_gain = round((ws.get("mmrGain", 0) or 0) / count, 1)

                entries.append({
                    "character_id": char_id,
                    "character_name": char_name,
                    "character_icon": char_icon,
                    "weapon_key": weapon_key,
                    "weapon_name": weapon_name,
                    "weapon_icon": weapon_icon,
                    "tier_badge": tier_badge,
                    "tier_badge_path": tier_badge_path,
                    "tier_score": round(float(tier_score), 1),
                    "count": count,
                    "avg_damage": avg_damage,
                    "avg_mmr_gain": avg_mmr_gain,
                })

        entries.sort(key=lambda e: e["tier_score"], reverse=True)

        ranking = entries[:limit] if limit is not None else entries
        for i, entry in enumerate(ranking):
            entry["rank"] = i + 1

        return {
            "tier": tier,
            "tier_count": tier_count,
            "ranking": ranking,
        }

    def build_character_stats_render_data(
        self,
        stats_data: dict[str, Any],
        character_name: str,
        weapon_name: str | None,
        tier: str,
        assets: AssetManager,
        recommendations: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        char_matches = assets.find_character_by_name(character_name)
        if not char_matches:
            return None

        char_data = char_matches[0]
        char_id = char_data.get("id")
        char_key = char_data.get("key", "")
        char_display_name = char_data.get("name", character_name)

        mastery_key = None
        mastery_id = None
        mastery_display_name = ""
        if weapon_name:
            mastery_matches = assets.find_mastery_by_name(weapon_name)
            if mastery_matches:
                mastery_key = mastery_matches[0].get("key")
                mastery_id = mastery_matches[0].get("id")
                mastery_display_name = mastery_matches[0].get("name", weapon_name)

        snapshot = stats_data.get("characterStatSnapshot", {})
        character_stats = snapshot.get("characterStats", [])
        meta = stats_data.get("meta", {})
        tier_count = snapshot.get("tierCount", 0)

        char_entry = None
        for entry in character_stats:
            entry_key = entry.get("key")
            try:
                if int(entry_key) == int(char_id):
                    char_entry = entry
                    break
            except (ValueError, TypeError):
                if str(entry_key).lower() == str(char_key).lower():
                    char_entry = entry
                    break

        if not char_entry:
            return None

        weapon_stats_list = char_entry.get("weaponStats", [])

        char_stats = None
        if mastery_key is not None or mastery_id is not None:
            for ws in weapon_stats_list:
                ws_key = ws.get("key")
                try:
                    if mastery_id is not None and int(ws_key) == int(mastery_id):
                        char_stats = ws
                        break
                except (ValueError, TypeError):
                    pass
                if mastery_key is not None and str(ws_key).lower() == str(mastery_key).lower():
                    char_stats = ws
                    break
        else:
            if weapon_stats_list:
                best = max(weapon_stats_list, key=lambda m: m.get("count", 0))
                char_stats = best
                best_key = best.get("key")
                for mid, mdata in assets.masteries.items():
                    if mdata.get("key") == str(best_key) or mdata.get("id") == best_key:
                        mastery_key = mdata.get("key")
                        mastery_display_name = mdata.get("name", str(best_key))
                        break

        if not char_stats:
            return None

        count = char_stats.get("count", 0)
        win = char_stats.get("win", 0)
        top3 = char_stats.get("top3", 0)
        place_total = char_stats.get("place", 0)
        player_kill_total = char_stats.get("playerKill", 0)
        mmr_gain_total = char_stats.get("mmrGain", 0)
        damage_total = char_stats.get("damageToPlayer", 0)
        team_kill_total = char_stats.get("teamKill", 0)
        player_deaths = char_stats.get("playerDeaths", 0)

        win_rate = (win / count * 100) if count > 0 else 0
        top3_rate = (top3 / count * 100) if count > 0 else 0
        avg_rank = (place_total / count) if count > 0 else 0
        avg_kill = (player_kill_total / count) if count > 0 else 0
        avg_rp = (mmr_gain_total / count) if count > 0 else 0
        avg_damage = (damage_total / count) if count > 0 else 0
        avg_tk = (team_kill_total / count) if count > 0 else 0

        rank = char_stats.get("rank", {})
        rank_size = rank.get("size", 0)
        win_rank = rank.get("win", 0)
        top3_rank = rank.get("top3", 0)
        place_rank = rank.get("place", 0)
        player_kill_rank = rank.get("playerKill", 0)
        mmr_gain_rank = rank.get("mmrGain", 0)
        pick_rank = rank.get("count", 0)

        pick_rate = (count / tier_count * 100) if tier_count > 0 else 0

        tier_badge = char_stats.get("tier", "") or ""
        tier_badge_path = assets.get_character_tier_badge_path(tier_badge) if tier_badge else ""
        char_icon_path = assets.get_character_icon_path(char_id) if char_id else ""

        result = {
            "character_name": char_display_name,
            "character_icon": char_icon_path,
            "weapon_name": mastery_display_name,
            "tier": tier,
            "tier_badge": tier_badge,
            "tier_badge_path": tier_badge_path,
            "count": count,
            "tier_count": tier_count,
            "win_rate": win_rate,
            "top3_rate": top3_rate,
            "avg_rank": avg_rank,
            "avg_kill": avg_kill,
            "avg_rp": avg_rp,
            "pick_rate": pick_rate,
            "avg_damage": avg_damage,
            "avg_tk": avg_tk,
            "win_rank": win_rank,
            "top3_rank": top3_rank,
            "place_rank": place_rank,
            "player_kill_rank": player_kill_rank,
            "mmr_gain_rank": mmr_gain_rank,
            "pick_rank": pick_rank,
            "rank_size": rank_size,
        }

        if recommendations:
            skill_slots = assets.get_character_skill_slots(char_id) if char_id else {}
            result["skill_slots"] = skill_slots
            result["skill_builds"] = self._map_skill_builds(recommendations.get("skillBuildStats", []), skill_slots)
            result["tactical_skills"] = self._map_tactical_skills(recommendations.get("tacticalSkillStats", []), assets)
            result["trait_builds"] = self._map_trait_builds(recommendations.get("traitStats", []), assets)
            result["item_stats"] = self._map_item_stats(recommendations.get("itemStats", []), assets)
            if not tier_badge:
                tier_badge = recommendations.get("tier", "")
                result["tier_badge"] = tier_badge
                result["tier_badge_path"] = assets.get_character_tier_badge_path(tier_badge) if tier_badge else ""
        else:
            result["skill_slots"] = {}
            result["skill_builds"] = []
            result["tactical_skills"] = []
            result["trait_builds"] = []
            result["item_stats"] = []

        return result

    def _map_skill_builds(
        self, skill_build_stats: list[dict], skill_slots: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        result = []
        total_count = sum(entry.get("count", 0) for entry in skill_build_stats) or 1
        for entry in skill_build_stats[:5]:
            key = entry.get("key", "")
            count = entry.get("count", 0)
            win = entry.get("win", 0)
            slots = []
            for ch in key:
                ch_lower = ch.lower()
                skill_info = skill_slots.get(ch_lower, {})
                slots.append({
                    "slot_char": ch.upper(),
                    "skill_id": skill_info.get("id", 0),
                    "skill_name": skill_info.get("name", ch),
                    "skill_icon": skill_info.get("icon", ""),
                    "skill_image_url": skill_info.get("image_url", ""),
                })
            result.append({
                "key": key,
                "slots": slots,
                "count": count,
                "selection_rate": round((count / total_count * 100), 1),
                "win_rate": round((win / count * 100), 1) if count > 0 else 0,
            })
        return result

    def _map_tactical_skills(
        self, tactical_stats: list[dict], assets: AssetManager
    ) -> list[dict[str, Any]]:
        result = []
        total_count = sum(entry.get("count", 0) for entry in tactical_stats) or 1
        for entry in tactical_stats[:6]:
            skill_id = int(entry.get("key", 0))
            count = entry.get("count", 0)
            win = entry.get("win", 0)
            name = assets.get_tactical_skill_name(skill_id)
            icon = assets.get_tactical_skill_icon_path(skill_id)
            level_stats = entry.get("levelStats", [])
            levels = []
            for lv in level_stats:
                lv_count = lv.get("count", 0)
                levels.append({
                    "level": int(lv.get("key", 0)),
                    "count": lv_count,
                    "win_rate": round((lv.get("win", 0) / lv_count * 100), 1) if lv_count > 0 else 0,
                })
            result.append({
                "skill_id": skill_id,
                "name": name,
                "icon": icon,
                "count": count,
                "selection_rate": round((count / total_count * 100), 1),
                "win_rate": round((win / count * 100), 1) if count > 0 else 0,
                "levels": levels,
            })
        return result

    def _map_trait_builds(
        self, trait_stats: list[dict], assets: AssetManager
    ) -> list[dict[str, Any]]:
        core_groups: dict[int, dict[str, Any]] = {}
        for entry in trait_stats:
            key_data = entry.get("key", [])
            count = entry.get("count", 0)
            win = entry.get("win", 0)

            if not isinstance(key_data, list) or len(key_data) < 1:
                continue

            core_id = int(key_data[0])
            if core_id == 0:
                continue

            if core_id not in core_groups:
                core_groups[core_id] = {
                    "core_id": core_id,
                    "core": assets.build_trait_render_data(core_id),
                    "total_count": 0,
                    "total_win": 0,
                    "combinations": [],
                }

            sub1_id = 0
            sub2_id = 0
            sub1_alt_id = 0
            sub2_alt_id = 0

            if isinstance(key_data, list) and len(key_data) >= 2:
                sub1_data = key_data[1]
                if isinstance(sub1_data, list) and len(sub1_data) >= 1:
                    sub1_id = int(sub1_data[0])
                    sub1_alt_id = int(sub1_data[1]) if len(sub1_data) >= 2 else 0
            if isinstance(key_data, list) and len(key_data) >= 3:
                sub2_data = key_data[2]
                if isinstance(sub2_data, list) and len(sub2_data) >= 1:
                    sub2_id = int(sub2_data[0])
                    sub2_alt_id = int(sub2_data[1]) if len(sub2_data) >= 2 else 0

            core_groups[core_id]["total_count"] += count
            core_groups[core_id]["total_win"] += win

            core_groups[core_id]["combinations"].append({
                "count": count,
                "win": win,
                "win_rate": round((win / count * 100), 1) if count > 0 else 0,
                "sub1": assets.build_trait_render_data(sub1_id) if sub1_id else {},
                "sub1_alt": assets.build_trait_render_data(sub1_alt_id) if sub1_alt_id else {},
                "sub2": assets.build_trait_render_data(sub2_id) if sub2_id else {},
                "sub2_alt": assets.build_trait_render_data(sub2_alt_id) if sub2_alt_id else {},
            })

        result = []
        # 选择率基准：所有核心潜能组合的总选择次数（与技能/战术技能口径一致）
        total_all = sum(g["total_count"] for g in core_groups.values())
        for core_id, group in sorted(core_groups.items(), key=lambda x: x[1]["total_count"], reverse=True):
            group["selection_rate"] = round((group["total_count"] / total_all * 100), 1) if total_all > 0 else 0
            group["win_rate"] = round((group["total_win"] / group["total_count"] * 100), 1) if group["total_count"] > 0 else 0
            group["combinations"].sort(key=lambda c: c["count"], reverse=True)
            result.append(group)

        return result[:3]

    def _map_item_stats(
        self, item_stats_list: list[dict], assets: AssetManager
    ) -> list[dict[str, Any]]:
        slot_names = {0: "武器", 1: "胸部", 2: "头部", 3: "手臂", 4: "腿部"}
        result = []
        for slot_entry in item_stats_list:
            slot_key = slot_entry.get("key", 0)
            slot_name = slot_names.get(slot_key, f"部位{slot_key}")
            stats = slot_entry.get("stats", [])
            slot_total = sum(s.get("count", 0) for s in stats) or 1
            items = []
            for item_entry in stats[:5]:
                item_id = int(item_entry.get("key", 0))
                count = item_entry.get("count", 0)
                win = item_entry.get("win", 0)
                item_data = assets.build_item_render_data(item_id)
                items.append({
                    "item_id": item_id,
                    "name": item_data.get("name", ""),
                    "icon": item_data.get("icon", ""),
                    "grade": item_data.get("grade", ""),
                    "count": count,
                    "selection_rate": round((count / slot_total * 100), 1),
                    "win_rate": round((win / count * 100), 1) if count > 0 else 0,
                })
            if items:
                result.append({
                    "slot": slot_key,
                    "slot_name": slot_name,
                    "items": items,
                })
        return result