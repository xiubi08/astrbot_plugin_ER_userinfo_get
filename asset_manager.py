from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger


class AssetManager:
    def __init__(self, data_dir: Path, bundled_data_dir: Optional[Path] = None):
        self.data_dir = Path(data_dir)
        self.api_dir = self.data_dir / "cache" / "api"
        self.assets_dir = self.data_dir / "assets"
        self.bundled_assets_dir = Path(bundled_data_dir) / "assets" if bundled_data_dir else None

        self.characters: dict[int, dict[str, Any]] = {}
        self.character_skins: dict[int, dict[str, Any]] = {}
        self.items: dict[int, dict[str, Any]] = {}
        self.tiers: dict[int, dict[str, Any]] = {}
        self.tactical_skills: dict[int, dict[str, Any]] = {}
        self.trait_skills: dict[int, dict[str, Any]] = {}
        self.trait_skill_groups: dict[str, dict[str, Any]] = {}
        self.masteries: dict[int, dict[str, Any]] = {}
        self.skills: dict[int, dict[str, Any]] = {}
        self.skill_index: dict[str, dict[str, Any]] = {}
        self.character_tier_badges: dict[str, dict[str, Any]] = {}

        self.reload()

    def reload(self) -> None:
        self.characters = self._load_id_map("characters.json", "characters")
        self.character_skins = self._load_index_map("character_skins_index.json")
        self.items = self._load_id_map("items.json", "items")
        self.tiers = self._load_id_map("tiers.json", "tiers")
        self.tactical_skills = self._load_id_map("tactical_skills.json", "tacticalSkills")
        self.trait_skills = self._load_id_map("trait_skills.json", "traitSkills")
        self.masteries = self._load_id_map("masteries.json", "masteries")
        self.skills = self._load_id_map("skills.json", "skills")
        self.skill_index = self._load_str_key_index_map("skills_index.json")
        self.trait_skill_groups = self._load_key_map("trait_skills.json", "traitSkillGroups", "key")
        self.character_tier_badges = self._load_str_key_index_map("character_tier_badges_index.json")

        if not self.characters:
            logger.warning("[ER战绩] 资源数据为空，请发送 /更新资源 命令下载游戏资源")

        if not self.characters:
            logger.warning("[ER战绩] 资源数据为空，请发送 /更新资源 命令下载游戏资源")

    def _read_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _load_id_map(self, filename: str, root_key: str) -> dict[int, dict[str, Any]]:
        data = self._read_json(self.api_dir / filename)
        result: dict[int, dict[str, Any]] = {}

        for item in data.get(root_key, []):
            item_id = item.get("id")
            if item_id is None:
                continue
            result[int(item_id)] = item

        return result

    def _load_key_map(self, filename: str, root_key: str, key_name: str) -> dict[str, dict[str, Any]]:
        data = self._read_json(self.api_dir / filename)
        result: dict[str, dict[str, Any]] = {}

        for item in data.get(root_key, []):
            key = item.get(key_name)
            if not key:
                continue
            result[str(key)] = item

        return result

    def _load_index_map(self, filename: str) -> dict[int, dict[str, Any]]:
        path = self.api_dir / filename
        if not path.exists():
            return {}

        data = self._read_json(path)
        result: dict[int, dict[str, Any]] = {}

        for k, v in data.items():
            try:
                result[int(k)] = v
            except Exception:
                continue

        return result

    def _load_str_key_index_map(self, filename: str) -> dict[str, dict[str, Any]]:
        path = self.api_dir / filename
        if not path.exists():
            return {}

        data = self._read_json(path)
        result: dict[str, dict[str, Any]] = {}

        for k, v in data.items():
            result[str(k)] = v

        return result

    def _find_asset_path(self, folder: str, file_stem: str) -> Optional[Path]:
        for base_dir in (self.assets_dir, self.bundled_assets_dir):
            if base_dir is None:
                continue
            base = base_dir / folder
            if not base.exists():
                continue
            for ext in (".png", ".webp", ".jpg", ".jpeg"):
                path = base / f"{file_stem}{ext}"
                if path.exists():
                    return path
        return None

    def _path_str(self, path: Optional[Path]) -> str:
        return str(path) if path else ""

    # ---------- 角色 ----------
    def get_character(self, character_id: int) -> dict[str, Any]:
        return self.characters.get(int(character_id), {})

    def get_character_name(self, character_id: int) -> str:
        return self.get_character(character_id).get("name", f"角色{character_id}")

    def get_character_icon_path(self, character_id: int) -> str:
        return self._path_str(self._find_asset_path("characters", str(character_id)))

    def get_character_skin_icon_path(self, skin_id: int) -> str:
        skin = self.character_skins.get(int(skin_id), {})
        file_path = skin.get("file", "")
        if not file_path:
            return ""

        p = Path(file_path)
        if p.exists():
            return str(p)

        p = self.data_dir / file_path
        if p.exists():
            return str(p)

        return ""

    # ---------- 物品 ----------
    def get_item(self, item_id: int) -> dict[str, Any]:
        return self.items.get(int(item_id), {})

    def get_item_name(self, item_id: int) -> str:
        return self.get_item(item_id).get("name", f"物品{item_id}")

    def get_item_icon_path(self, item_id: int) -> str:
        return self._path_str(self._find_asset_path("items", str(item_id)))

    # ---------- 段位 ----------
    def get_tier(self, tier_id: int) -> dict[str, Any]:
        return self.tiers.get(int(tier_id), {})

    def get_tier_name(self, tier_id: int) -> str:
        return self.get_tier(tier_id).get("name", f"段位{tier_id}")

    def get_tier_icon_path(self, tier_id: int) -> str:
        return self._path_str(self._find_asset_path("tiers", str(tier_id)))

    def get_tier_full_path(self, tier_id: int) -> str:
        return self._path_str(self._find_asset_path("tiers_full", str(tier_id)))

    # ---------- 战术技能 ----------
    def get_tactical_skill(self, skill_id: int) -> dict[str, Any]:
        return self.tactical_skills.get(int(skill_id), {})

    def get_tactical_skill_name(self, skill_id: int) -> str:
        return self.get_tactical_skill(skill_id).get("name", f"战术技能{skill_id}")

    def get_tactical_skill_icon_path(self, skill_id: int) -> str:
        return self._path_str(self._find_asset_path("tactical_skills", str(skill_id)))

    # ---------- 特性 ----------
    def get_trait_skill(self, trait_id: int) -> dict[str, Any]:
        return self.trait_skills.get(int(trait_id), {})

    def get_trait_skill_name(self, trait_id: int) -> str:
        return self.get_trait_skill(trait_id).get("name", f"特性{trait_id}")

    def get_trait_skill_icon_path(self, trait_id: int) -> str:
        return self._path_str(self._find_asset_path("trait_skills", str(trait_id)))

    def get_trait_group(self, group_key: str) -> dict[str, Any]:
        return self.trait_skill_groups.get(str(group_key), {})

    def get_trait_group_name(self, group_key: str) -> str:
        return self.get_trait_group(group_key).get("name", str(group_key))

    def get_trait_group_icon_path(self, group_key: str) -> str:
        return self._path_str(self._find_asset_path("trait_skill_groups", str(group_key)))

    # ---------- 武器 / 熟练度 ----------
    def get_mastery(self, mastery_id: int) -> dict[str, Any]:
        return self.masteries.get(int(mastery_id), {})

    def get_mastery_name(self, mastery_id: int) -> str:
        return self.get_mastery(mastery_id).get("name", f"武器{mastery_id}")

    def get_mastery_icon_path(self, mastery_id: int) -> str:
        return self._path_str(self._find_asset_path("masteries", str(mastery_id)))

    # ---------- 角色技能 ----------
    def get_skill(self, skill_id: int) -> dict[str, Any]:
        return self.skills.get(int(skill_id), {})

    def get_skill_name(self, skill_id: int) -> str:
        return self.get_skill(skill_id).get("name", f"技能{skill_id}")

    def get_skill_icon_path(self, skill_id: int) -> str:
        path = self._path_str(self._find_asset_path("skills", str(skill_id)))
        if path:
            return path
        index_entry = self.skill_index.get(str(skill_id), {})
        file_path = index_entry.get("file", "")
        if file_path:
            p = Path(file_path)
            if p.exists():
                return str(p)
        return ""

    def get_character_skills(self, character_id: int) -> list[dict[str, Any]]:
        character_id = int(character_id)
        return [skill for skill in self.skills.values() if int(skill.get("characterId", -1)) == character_id]

    def get_character_skill_slots(self, character_id: int) -> dict[str, dict[str, Any]]:
        skills = self.get_character_skills(character_id)
        sorted_skills = sorted(skills, key=lambda s: s.get("order", s.get("slot", s.get("id", 0))))
        slot_chars = ["q", "w", "e", "t"]
        result: dict[str, dict[str, Any]] = {}
        for i, skill in enumerate(sorted_skills):
            if i < 4:
                slot_char = slot_chars[i]
                skill_id = int(skill.get("id", 0))
                icon_path = self.get_skill_icon_path(skill_id)
                image_url = skill.get("imageUrl", "")
                if not icon_path and image_url:
                    local_path = self._download_skill_icon(skill_id, image_url)
                    icon_path = str(local_path) if local_path else ""
                result[slot_char] = {
                    "id": skill_id,
                    "name": skill.get("name", f"技能{skill_id}"),
                    "icon": icon_path,
                    "image_url": image_url,
                    "order": skill.get("order", i),
                }
        return result

    def _download_skill_icon(self, skill_id: int, image_url: str) -> Optional[Path]:
        import requests
        try:
            from urllib.parse import urlparse
            parsed = urlparse(image_url)
            ext = Path(parsed.path).suffix or ".png"
            target_dir = self.assets_dir / "skills"
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"{skill_id}{ext}"
            if target.exists() and target.stat().st_size > 0:
                return target
            resp = requests.get(image_url, timeout=15)
            resp.raise_for_status()
            target.write_bytes(resp.content)
            logger.info(f"[ER资源] 下载技能图标: {skill_id}{ext}")
            return target
        except Exception as e:
            logger.warning(f"[ER资源] 下载技能图标失败 skill={skill_id}: {e}")
            return None

    # ---------- 渲染辅助 ----------
    def build_item_render_data(self, item_id: int) -> dict[str, Any]:
        raw = self.get_item(item_id)
        return {
            "id": item_id,
            "name": self.get_item_name(item_id),
            "icon": self.get_item_icon_path(item_id),
            "grade": raw.get("grade", ""),
            "raw": raw,
        }

    def build_character_render_data(self, character_id: int) -> dict[str, Any]:
        return {
            "id": character_id,
            "name": self.get_character_name(character_id),
            "icon": self.get_character_icon_path(character_id),
            "raw": self.get_character(character_id),
        }

    def build_tactical_skill_render_data(self, skill_id: int) -> dict[str, Any]:
        return {
            "id": skill_id,
            "name": self.get_tactical_skill_name(skill_id),
            "icon": self.get_tactical_skill_icon_path(skill_id),
            "raw": self.get_tactical_skill(skill_id),
        }

    def build_trait_render_data(self, trait_id: int) -> dict[str, Any]:
        trait = self.get_trait_skill(trait_id)
        group_key = trait.get("group", "")
        return {
            "id": trait_id,
            "name": trait.get("name", f"特性{trait_id}"),
            "icon": self.get_trait_skill_icon_path(trait_id),
            "group_key": group_key,
            "group_name": self.get_trait_group_name(group_key) if group_key else "",
            "group_icon": self.get_trait_group_icon_path(group_key) if group_key else "",
            "raw": trait,
        }

    def build_mastery_render_data(self, mastery_id: int) -> dict[str, Any]:
        return {
            "id": mastery_id,
            "name": self.get_mastery_name(mastery_id),
            "icon": self.get_mastery_icon_path(mastery_id),
            "raw": self.get_mastery(mastery_id),
        }

    def get_character_tier_badge_path(self, tier: str) -> str:
        # 只接受 Pillow 可加载的光栅格式（.svg 无法被 Pillow 解析，跳过避免加载失败）
        supported = (".png", ".webp", ".jpg", ".jpeg")
        badge = self.character_tier_badges.get(str(tier).upper(), {})
        file_path = badge.get("file", "")
        if file_path:
            p = Path(file_path)
            if p.suffix.lower() in supported and p.exists():
                return str(p)
        file_stem = f"character-tier-{str(tier).upper()}"
        # 优先插件自带资源目录，其次 AstrBot 数据目录（与其他资源加载方式一致）
        for base_dir in (self.bundled_assets_dir, self.assets_dir):
            if base_dir is None:
                continue
            base = base_dir / "character_tier_badges"
            if not base.exists():
                continue
            for ext in supported:
                path = base / f"{file_stem}{ext}"
                if path.exists():
                    return str(path)
        return ""

    @staticmethod
    def _to_pinyin_key(text: str) -> list[str]:
        """将文本转拼音键（全小写），用于同音字模糊匹配；pypinyin 不可用时返回空列表。"""
        try:
            from pypinyin import lazy_pinyin
            return [p.lower() for p in lazy_pinyin(text.strip())]
        except Exception:
            return []

    def find_character_by_name(self, name: str) -> list[dict[str, Any]]:
        name_lower = name.lower().strip()
        results = []
        for char_id, char_data in self.characters.items():
            char_name = char_data.get("name", "").lower()
            char_key = char_data.get("key", "").lower()
            if char_name == name_lower or char_key == name_lower:
                results.append(char_data)
        if not results:
            # 拼音完全匹配：整词全拼一致（优先级高于子串包含，避免同音字被子串误匹配）
            input_py = self._to_pinyin_key(name)
            if input_py:
                for char_id, char_data in self.characters.items():
                    char_name = char_data.get("name", "")
                    char_key = char_data.get("key", "")
                    if self._to_pinyin_key(char_name) == input_py or self._to_pinyin_key(char_key) == input_py:
                        results.append(char_data)
        if not results:
            for char_id, char_data in self.characters.items():
                char_name = char_data.get("name", "").lower()
                char_key = char_data.get("key", "").lower()
                if name_lower in char_name or name_lower in char_key:
                    results.append(char_data)
        return results

    def find_mastery_by_name(self, name: str) -> list[dict[str, Any]]:
        name_lower = name.lower().strip()
        results = []
        for mastery_id, mastery_data in self.masteries.items():
            mastery_name = mastery_data.get("name", "").lower()
            mastery_key = mastery_data.get("key", "").lower()
            if mastery_name == name_lower or mastery_key == name_lower:
                results.append(mastery_data)
        if not results:
            for mastery_id, mastery_data in self.masteries.items():
                mastery_name = mastery_data.get("name", "").lower()
                mastery_key = mastery_data.get("key", "").lower()
                if name_lower in mastery_name or name_lower in mastery_key:
                    results.append(mastery_data)
        return results