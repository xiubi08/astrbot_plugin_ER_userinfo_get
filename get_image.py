from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests

try:
    from .config import NETWORK, RESOURCE
except ImportError:
    class _FallbackNetwork:
        default_hl = "zh_CN"
        user_agent = "Mozilla/5.0"
    class _FallbackResource:
        download_timeout = 20
        download_sleep = 1.0
        download_concurrency = 6
        chunk_size = 8192
    NETWORK = _FallbackNetwork()
    RESOURCE = _FallbackResource()

BASE = "https://er.dakgg.io/api/v1/data"
HL = NETWORK.default_hl

ROOT = Path(__file__).resolve().parent / "data"
API_DIR = ROOT / "cache" / "api"
ASSET_DIR = ROOT / "assets"


def set_paths(root: Path):
    global ROOT, API_DIR, ASSET_DIR
    ROOT = root
    API_DIR = ROOT / "cache" / "api"
    ASSET_DIR = ROOT / "assets"

TIMEOUT = RESOURCE.download_timeout
SLEEP_SECONDS = RESOURCE.download_sleep

session = requests.Session()
session.headers.update({
    "User-Agent": NETWORK.user_agent,
    "Accept": "application/json,text/plain,*/*",
})


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def normalize_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    return url


def get_ext_from_url(url: str) -> str:
    path = urlparse(url).path
    suffix = Path(path).suffix.lower()
    return suffix if suffix else ".png"


def safe_name(value: str) -> str:
    value = value.strip()
    value = re.sub(r'[\\/:*?"<>|]+', "_", value)
    return value or "unknown"


def fetch_json(endpoint: str) -> dict:
    url = f"{BASE}/{endpoint}?hl={HL}"
    resp = session.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def save_json(data: dict, path: Path) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _download_with_meta(url: str, path: Path, force: bool = False) -> tuple[bool, dict]:
    """下载文件并返回 (是否成功, 响应头元数据{etag,last_modified,size})。"""
    url = normalize_url(url)
    if not url:
        return False, {}

    ensure_dir(path.parent)

    if not force and path.exists() and path.stat().st_size > 0:
        return True, {}

    try:
        with session.get(url, timeout=TIMEOUT, stream=True) as resp:
            resp.raise_for_status()
            meta = {
                "etag": resp.headers.get("ETag", ""),
                "last_modified": resp.headers.get("Last-Modified", ""),
                "size": int(resp.headers.get("Content-Length") or 0) or 0,
            }
            with open(path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=RESOURCE.chunk_size):
                    if chunk:
                        f.write(chunk)
        time.sleep(SLEEP_SECONDS)
        return True, meta
    except Exception as e:
        print(f"[FAIL] {url} -> {path} | {e}")
        time.sleep(max(SLEEP_SECONDS, 1.0))
        return False, {}


def _head_meta(url: str) -> dict:
    """HEAD 请求远端，返回 {etag,last_modified,size}；失败/不支持时返回空。"""
    try:
        r = session.head(normalize_url(url), timeout=TIMEOUT, allow_redirects=True)
        if r.status_code != 200:
            return {}
        return {
            "etag": r.headers.get("ETag", ""),
            "last_modified": r.headers.get("Last-Modified", ""),
            "size": int(r.headers.get("Content-Length") or 0) or 0,
        }
    except Exception:
        return {}


def download_file(url: str, path: Path, force: bool = False) -> bool:
    ok, _ = _download_with_meta(url, path, force)
    return ok


def write_index(name: str, data: dict) -> None:
    path = API_DIR / f"{name}.json"
    save_json(data, path)


def download_collection(
    collection: list,
    folder: Path,
    id_key: str = "id",
    url_key: str = "imageUrl",
    extra_fields: tuple[str, ...] = ("name", "key"),
    index_name: str | None = None,
    skip_existing: bool = False,
) -> dict:
    ensure_dir(folder)
    index = {}
    old_index = _read_local_index(f"{index_name}_index.json") if index_name else {}

    total = len(collection)
    ok_count = 0
    skipped_count = 0

    rows: list[dict] = []
    for item in collection:
        raw_id = item.get(id_key)
        if raw_id is None:
            continue
        url = item.get(url_key, "")
        url = normalize_url(url)
        if not url:
            continue
        ext = get_ext_from_url(url)
        file_path = folder / f"{raw_id}{ext}"
        exists = file_path.exists() and file_path.stat().st_size > 0
        rows.append({
            "item": item,
            "raw_id": raw_id,
            "url": url,
            "ext": ext,
            "file_path": file_path,
            "skip": skip_existing and exists,
        })

    # 并发下载缺失文件，提升整体下载速度
    todo = [r for r in rows if not r["skip"]]
    result_meta: dict[str, dict] = {}
    if todo:
        workers = max(1, int(getattr(RESOURCE, "download_concurrency", 6)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_download_with_meta, r["url"], r["file_path"]): r["raw_id"]
                for r in todo
            }
            for fut in as_completed(futures):
                ok, meta = fut.result()
                raw_id = futures[fut]
                if ok:
                    result_meta[str(raw_id)] = meta

    for i, r in enumerate(rows, 1):
        raw_id = r["raw_id"]
        if r["skip"]:
            ok = True
            skipped_count += 1
            ok_count += 1
            meta = None
        else:
            meta = result_meta.get(str(raw_id))
            ok = meta is not None
            if ok:
                ok_count += 1

        entry = {
            "id": raw_id,
            "file": str(r["file_path"].as_posix()),
            "url": r["url"],
        }
        if meta:
            entry.update({k: v for k, v in meta.items() if v})
        else:
            # 文件已存在/下载失败：继承旧索引内容指纹，避免版本换代后被误判为内容变更
            om = old_index.get(str(raw_id), {})
            for k in ("etag", "last_modified", "size"):
                if om.get(k):
                    entry[k] = om[k]
        index[str(raw_id)] = entry

        for field in extra_fields:
            if field in r["item"] and field not in entry:
                entry[field] = r["item"][field]

        if not r["skip"]:
            print(f"[{i}/{total}] {'OK' if ok else 'FAIL'} {folder.name}/{raw_id}{r['ext']}")

    if index_name:
        save_json(index, API_DIR / f"{index_name}_index.json")

    if skip_existing and skipped_count > 0:
        print(f"[DONE] {folder.name}: {ok_count}/{total} (跳过 {skipped_count} 个已存在的文件)")
    else:
        print(f"[DONE] {folder.name}: {ok_count}/{total}")
    return index


def main() -> None:
    ensure_dir(API_DIR)
    ensure_dir(ASSET_DIR)

    # 1. 拉取字典 JSON
    print("=== Fetching JSON data ===")
    characters_data = fetch_json("characters")
    items_data = fetch_json("items")
    tiers_data = fetch_json("tiers")
    tactical_skills_data = fetch_json("tactical-skills")
    trait_skills_data = fetch_json("trait-skills")
    masteries_data = fetch_json("masteries")
    skills_data = fetch_json("skills")

    write_index("characters", characters_data)
    write_index("items", items_data)
    write_index("tiers", tiers_data)
    write_index("tactical_skills", tactical_skills_data)
    write_index("trait_skills", trait_skills_data)
    write_index("masteries", masteries_data)
    write_index("skills", skills_data)

    # 2. 下载角色头像
    print("\n=== Downloading character images ===")
    download_collection(
        collection=characters_data.get("characters", []),
        folder=ASSET_DIR / "characters",
        id_key="id",
        url_key="imageUrl",
        index_name="characters",
    )

    # 角色皮肤图也一起下，可选
    print("\n=== Downloading character skins ===")
    skins = []
    for char in characters_data.get("characters", []):
        for skin in char.get("skins", []):
            skin_copy = dict(skin)
            skin_copy["characterId"] = char.get("id")
            skins.append(skin_copy)

    download_collection(
        collection=skins,
        folder=ASSET_DIR / "character_skins",
        id_key="id",
        url_key="imageUrl",
        extra_fields=("name", "imageName", "characterId"),
        index_name="character_skins",
    )

    # 3. 下载物品图标
    print("\n=== Downloading item icons ===")
    download_collection(
        collection=items_data.get("items", []),
        folder=ASSET_DIR / "items",
        id_key="id",
        url_key="imageUrl",
        index_name="items",
    )

    # 4. 下载段位图标
    print("\n=== Downloading tier icons ===")
    tier_items = []
    for t in tiers_data.get("tiers", []):
        icon_item = dict(t)
        icon_item["__url"] = normalize_url(t.get("iconUrl", ""))
        tier_items.append(icon_item)

    download_collection(
        collection=tier_items,
        folder=ASSET_DIR / "tiers",
        id_key="id",
        url_key="__url",
        index_name="tiers",
    )

    # 同时下载 full 图，可选
    print("\n=== Downloading tier full images ===")
    tier_full_items = []
    for t in tiers_data.get("tiers", []):
        full_item = dict(t)
        full_item["__url"] = normalize_url(t.get("imageUrl", ""))
        tier_full_items.append(full_item)

    download_collection(
        collection=tier_full_items,
        folder=ASSET_DIR / "tiers_full",
        id_key="id",
        url_key="__url",
        index_name="tiers_full",
    )

    # 5. 下载战术技能图标
    print("\n=== Downloading tactical skill icons ===")
    download_collection(
        collection=tactical_skills_data.get("tacticalSkills", []),
        folder=ASSET_DIR / "tactical_skills",
        id_key="id",
        url_key="imageUrl",
        index_name="tactical_skills",
    )

    # 6. 下载特性图标
    print("\n=== Downloading trait skill icons ===")
    download_collection(
        collection=trait_skills_data.get("traitSkills", []),
        folder=ASSET_DIR / "trait_skills",
        id_key="id",
        url_key="imageUrl",
        index_name="trait_skills",
    )

    # 7. 下载特性分组图标
    print("\n=== Downloading trait skill group icons ===")
    group_folder = ASSET_DIR / "trait_skill_groups"
    ensure_dir(group_folder)
    group_index = {}

    groups = trait_skills_data.get("traitSkillGroups", [])
    for i, g in enumerate(groups, 1):
        key = safe_name(g.get("key", "unknown"))
        url = normalize_url(g.get("imageUrl", ""))
        ext = get_ext_from_url(url)
        path = group_folder / f"{key}{ext}"

        ok = download_file(url, path)
        group_index[key] = {
            "key": g.get("key"),
            "name": g.get("name"),
            "file": str(path.as_posix()),
            "url": url,
        }
        print(f"[{i}/{len(groups)}] {'OK' if ok else 'FAIL'} trait_skill_groups/{key}{ext}")

    save_json(group_index, API_DIR / "trait_skill_groups_index.json")

    # 8. 下载武器/熟练度图标
    print("\n=== Downloading mastery icons ===")
    mastery_items = []
    for m in masteries_data.get("masteries", []):
        item = dict(m)
        item["__url"] = normalize_url(m.get("iconUrl", ""))
        mastery_items.append(item)

    download_collection(
        collection=mastery_items,
        folder=ASSET_DIR / "masteries",
        id_key="id",
        url_key="__url",
        index_name="masteries",
    )

    # 9. 下载角色技能图标
    print("\n=== Downloading skill icons ===")
    download_collection(
        collection=skills_data.get("skills", []),
        folder=ASSET_DIR / "skills",
        id_key="id",
        url_key="imageUrl",
        extra_fields=("name", "slot", "characterId"),
        index_name="skills",
    )

    # 10. 下载角色段位评级图标（S/A/B/C/D）
    print("\n=== Downloading character tier badges ===")
    char_tier_badges = [
        {"id": "S", "key": "S", "name": "S", "url": "https://cdn.dak.gg/er/images/character/tier/character-tier-S.svg"},
        {"id": "A", "key": "A", "name": "A", "url": "https://cdn.dak.gg/er/images/character/tier/character-tier-A.svg"},
        {"id": "B", "key": "B", "name": "B", "url": "https://cdn.dak.gg/er/images/character/tier/character-tier-B.svg"},
        {"id": "C", "key": "C", "name": "C", "url": "https://cdn.dak.gg/er/images/character/tier/character-tier-C.svg"},
        {"id": "D", "key": "D", "name": "D", "url": "https://cdn.dak.gg/er/images/character/tier/character-tier-D.svg"},
    ]
    char_tier_badge_dir = ASSET_DIR / "character_tier_badges"
    ensure_dir(char_tier_badge_dir)
    char_tier_badge_index = {}
    for i, badge in enumerate(char_tier_badges, 1):
        badge_id = badge["id"]
        url = normalize_url(badge["url"])
        ext = get_ext_from_url(url)
        file_path = char_tier_badge_dir / f"{badge_id}{ext}"
        ok = download_file(url, file_path)
        char_tier_badge_index[badge_id] = {
            "id": badge_id,
            "key": badge["key"],
            "name": badge["name"],
            "file": str(file_path.as_posix()),
            "url": url,
        }
        print(f"[{i}/{len(char_tier_badges)}] {'OK' if ok else 'FAIL'} character_tier_badges/{badge_id}{ext}")
    save_json(char_tier_badge_index, API_DIR / "character_tier_badges_index.json")

    print("\nAll done.")


def _read_local_index(index_file: str) -> dict:
    path = API_DIR / index_file
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _check_image_category(
    diff_info: dict, cat_key: str, entries, quiet: bool = False
) -> None:
    """逐条比对远端 url/扩展名 与本地 *_index.json 记录，区分 正常/修改/丢失。"""
    try:
        index = _read_local_index(f"{cat_key}_index.json")
        stats = diff_info["category_stats"][cat_key]
        for key, url in entries:
            if not url:
                continue
            ext = get_ext_from_url(url)
            rel = f"{cat_key}/{key}{ext}"
            file_path = ASSET_DIR / cat_key / f"{key}{ext}"
            rec = index.get(str(key))
            exists = file_path.exists() and file_path.stat().st_size > 0
            if not exists:
                stats["missing"] += 1
                diff_info["image_files"]["missing"].append({"cat": cat_key, "id": key, "url": url})
                if not quiet:
                    print(f"[MISSING] {rel}")
            elif rec is None:
                # 有文件但索引无记录：无法比对 url，视为正常，避免误触发全量重下
                stats["ok"] += 1
            elif normalize_url(str(rec.get("url") or "")) == url:
                stats["ok"] += 1
            else:
                stats["changed"] += 1
                diff_info["image_files"]["changed"].append({"cat": cat_key, "id": key, "url": url})
                if not quiet:
                    print(f"[CHANGED] {rel}")
    except Exception as e:
        if not quiet:
            print(f"[ERROR] 检查 {cat_key} 图片差异失败: {e}")


def check_resource_diff(quiet: bool = False) -> dict:
    ensure_dir(API_DIR)
    ensure_dir(ASSET_DIR)

    diff_info = {
        "json_files": {"missing": [], "changed": [], "ok": []},
        "image_files": {"missing": [], "changed": [], "ok": []},
        "category_stats": {
            "json": {"ok": 0, "changed": 0, "missing": 0},
            "characters": {"ok": 0, "changed": 0, "missing": 0},
            "character_skins": {"ok": 0, "changed": 0, "missing": 0},
            "items": {"ok": 0, "changed": 0, "missing": 0},
            "tiers": {"ok": 0, "changed": 0, "missing": 0},
            "tiers_full": {"ok": 0, "changed": 0, "missing": 0},
            "tactical_skills": {"ok": 0, "changed": 0, "missing": 0},
            "trait_skills": {"ok": 0, "changed": 0, "missing": 0},
            "trait_skill_groups": {"ok": 0, "changed": 0, "missing": 0},
            "masteries": {"ok": 0, "changed": 0, "missing": 0},
            "skills": {"ok": 0, "changed": 0, "missing": 0},
            "character_tier_badges": {"ok": 0, "changed": 0, "missing": 0}
        }
    }

    if not quiet:
        print("=== 检查JSON数据差异 ===")
    json_endpoints = [
        ("characters", "characters"),
        ("items", "items"),
        ("tiers", "tiers"),
        ("tactical-skills", "tactical_skills"),
        ("trait-skills", "trait_skills"),
        ("masteries", "masteries"),
        ("skills", "skills")
    ]

    remote: dict = {}
    for endpoint, filename in json_endpoints:
        try:
            remote[filename] = fetch_json(endpoint)
        except Exception as e:
            remote[filename] = None
            if not quiet:
                print(f"[ERROR] 获取 {filename}.json 失败: {e}")

    for endpoint, filename in json_endpoints:
        local_path = API_DIR / f"{filename}.json"
        if not local_path.exists():
            diff_info["json_files"]["missing"].append(filename)
            diff_info["category_stats"]["json"]["missing"] += 1
            if not quiet:
                print(f"[MISSING] {filename}.json")
            continue
        remote_data = remote.get(filename)
        if remote_data is None:
            if not quiet:
                print(f"[ERROR] 检查 {filename}.json 失败: 远端数据获取失败")
            continue
        try:
            local_data = json.loads(local_path.read_text(encoding="utf-8"))
            if remote_data != local_data:
                diff_info["json_files"]["changed"].append(filename)
                diff_info["category_stats"]["json"]["changed"] += 1
                if not quiet:
                    print(f"[CHANGED] {filename}.json")
            else:
                diff_info["json_files"]["ok"].append(filename)
                diff_info["category_stats"]["json"]["ok"] += 1
                if not quiet:
                    print(f"[OK] {filename}.json")
        except Exception as e:
            if not quiet:
                print(f"[ERROR] 检查 {filename}.json 失败: {e}")

    if not quiet:
        print("\n=== 检查图片资源差异 ===")

    characters_data = remote.get("characters")
    items_data = remote.get("items")
    tiers_data = remote.get("tiers")
    tactical_skills_data = remote.get("tactical_skills")
    trait_skills_data = remote.get("trait_skills")
    masteries_data = remote.get("masteries")
    skills_data = remote.get("skills")

    if characters_data is not None:
        _check_image_category(
            diff_info, "characters",
            ((str(c["id"]), normalize_url(c.get("imageUrl", ""))) for c in characters_data.get("characters", []) if isinstance(c, dict) and c.get("id") is not None),
            quiet,
        )
        _check_image_category(
            diff_info, "character_skins",
            ((str(s["id"]), normalize_url(s.get("imageUrl", ""))) for c in characters_data.get("characters", []) if isinstance(c, dict) and isinstance(c.get("skins"), list) for s in c["skins"] if isinstance(s, dict) and s.get("id") is not None),
            quiet,
        )

    if items_data is not None:
        _check_image_category(
            diff_info, "items",
            ((str(i["id"]), normalize_url(i.get("imageUrl", ""))) for i in items_data.get("items", []) if isinstance(i, dict) and i.get("id") is not None),
            quiet,
        )

    if tiers_data is not None:
        _check_image_category(
            diff_info, "tiers",
            ((str(t["id"]), normalize_url(t.get("iconUrl", ""))) for t in tiers_data.get("tiers", []) if isinstance(t, dict) and t.get("id") is not None),
            quiet,
        )
        _check_image_category(
            diff_info, "tiers_full",
            ((str(t["id"]), normalize_url(t.get("imageUrl", ""))) for t in tiers_data.get("tiers", []) if isinstance(t, dict) and t.get("id") is not None),
            quiet,
        )

    if tactical_skills_data is not None:
        _check_image_category(
            diff_info, "tactical_skills",
            ((str(s["id"]), normalize_url(s.get("imageUrl", ""))) for s in tactical_skills_data.get("tacticalSkills", []) if isinstance(s, dict) and s.get("id") is not None),
            quiet,
        )

    if trait_skills_data is not None:
        _check_image_category(
            diff_info, "trait_skills",
            ((str(s["id"]), normalize_url(s.get("imageUrl", ""))) for s in trait_skills_data.get("traitSkills", []) if isinstance(s, dict) and s.get("id") is not None),
            quiet,
        )
        _check_image_category(
            diff_info, "trait_skill_groups",
            ((str(safe_name(g.get("key", "unknown"))), normalize_url(g.get("imageUrl", ""))) for g in trait_skills_data.get("traitSkillGroups", []) if isinstance(g, dict)),
            quiet,
        )

    if masteries_data is not None:
        _check_image_category(
            diff_info, "masteries",
            ((str(m["id"]), normalize_url(m.get("iconUrl", ""))) for m in masteries_data.get("masteries", []) if isinstance(m, dict) and m.get("id") is not None),
            quiet,
        )

    if skills_data is not None:
        _check_image_category(
            diff_info, "skills",
            ((str(s["id"]), normalize_url(s.get("imageUrl", ""))) for s in skills_data.get("skills", []) if isinstance(s, dict) and s.get("id") is not None),
            quiet,
        )

    char_tier_badges = [
        {"id": "S", "url": "https://cdn.dak.gg/er/images/character/tier/character-tier-S.svg"},
        {"id": "A", "url": "https://cdn.dak.gg/er/images/character/tier/character-tier-A.svg"},
        {"id": "B", "url": "https://cdn.dak.gg/er/images/character/tier/character-tier-B.svg"},
        {"id": "C", "url": "https://cdn.dak.gg/er/images/character/tier/character-tier-C.svg"},
        {"id": "D", "url": "https://cdn.dak.gg/er/images/character/tier/character-tier-D.svg"},
    ]
    _check_image_category(
        diff_info, "character_tier_badges",
        ((str(b["id"]), normalize_url(b["url"])) for b in char_tier_badges),
        quiet,
    )

    if not quiet:
        print("\n=== 差异检查完成 ===")
        print(f"JSON文件 - 缺失: {len(diff_info['json_files']['missing'])} 个, 变更: {len(diff_info['json_files']['changed'])} 个, 正常: {len(diff_info['json_files']['ok'])} 个")
        print(f"图片文件 - 缺失: {len(diff_info['image_files']['missing'])} 个, 变更: {len(diff_info['image_files']['changed'])} 个")

    return diff_info


def update_diff_resources(diff_info: dict, debug: bool = True) -> dict:
    ensure_dir(API_DIR)
    ensure_dir(ASSET_DIR)

    summary = {"download": 0, "adopt": 0, "fail": 0}
    print("=== 开始更新差异资源 ===")

    if diff_info["json_files"]["missing"] or diff_info["json_files"]["changed"]:
        print("\n=== 更新JSON数据 ===")
        json_endpoints = [
            ("characters", "characters"),
            ("items", "items"),
            ("tiers", "tiers"),
            ("tactical-skills", "tactical_skills"),
            ("trait-skills", "trait_skills"),
            ("masteries", "masteries"),
            ("skills", "skills")
        ]

        for endpoint, filename in json_endpoints:
            if filename in diff_info["json_files"]["missing"] or filename in diff_info["json_files"]["changed"]:
                if debug:
                    print(f"[DEBUG] 更新 {filename}.json...")
                try:
                    data = fetch_json(endpoint)
                    write_index(filename, data)
                    if debug:
                        print(f"[DEBUG] {filename}.json 更新成功")
                except Exception as e:
                    print(f"[ERROR] 更新 {filename}.json 失败: {e}")

    if diff_info["image_files"]["missing"] or diff_info["image_files"]["changed"]:
        if diff_info["image_files"]["changed"]:
            print("\n=== 更新变更图片资源（HEAD 比对，仅内容变化才重下）===")
            idx_by_cat: dict[str, dict] = {}
            jobs = []
            for rec in diff_info["image_files"]["changed"]:
                ext = get_ext_from_url(rec["url"])
                target = ASSET_DIR / rec["cat"] / f'{rec["id"]}{ext}'
                cat_index = idx_by_cat.get(rec["cat"])
                if cat_index is None:
                    cat_index = _read_local_index(f'{rec["cat"]}_index.json')
                    idx_by_cat[rec["cat"]] = cat_index
                old = cat_index.get(str(rec["id"]), {})
                local_size = target.stat().st_size if target.exists() else 0
                jobs.append((rec, target, ext, old, local_size))

            def _refresh_one(job):
                rec, target, ext, old, local_size = job
                remote_meta = _head_meta(rec["url"])
                if remote_meta:
                    old_etag = str(old.get("etag") or "")
                    new_etag = str(remote_meta.get("etag") or "")
                    if old_etag and new_etag:
                        same = old_etag == new_etag
                    else:
                        # 有一侧无 etag（旧数据）：退化为内容长度比对
                        same = bool(remote_meta.get("size")) and bool(local_size) and remote_meta["size"] == local_size
                    if same:
                        # 内容未变化（仅 URL/版本目录变化）：只更新索引，不下载
                        return ("adopt", rec, target, ext, remote_meta)
                ok, meta = _download_with_meta(rec["url"], target, force=True)
                if ok:
                    return ("download", rec, target, ext, meta)
                return ("none", rec, target, ext, {})

            workers = max(1, int(getattr(RESOURCE, "download_concurrency", 6)))
            results: list = []
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_refresh_one, j) for j in jobs]
                for fut in as_completed(futures):
                    action, rec, target, ext, meta = fut.result()
                    summary[{"download": "download", "adopt": "adopt", "none": "fail"}[action]] += 1
                    if debug:
                        print(f"[DEBUG] {action.upper():<8s} {rec['cat']}/{rec['id']}{ext}")
                    if action in ("adopt", "download"):
                        results.append((rec["cat"], str(rec["id"]), rec["url"], str(target.as_posix()), meta))
            by_cat: dict[str, list] = {}
            for cat, key, url, fpath, meta in results:
                by_cat.setdefault(cat, []).append((key, url, fpath, meta))
            for cat, updates in by_cat.items():
                idx = idx_by_cat[cat]
                for key, url, file_path, meta in updates:
                    entry = idx.get(key)
                    if entry is not None:
                        entry["url"] = url
                        entry["file"] = file_path
                        for k in ("etag", "last_modified", "size"):
                            if meta.get(k):
                                entry[k] = meta[k]
                            else:
                                entry.pop(k, None)
                save_json(idx, API_DIR / f"{cat}_index.json")

        print("\n=== 更新图片资源 ===")
        try:
            characters_data = fetch_json("characters")
            items_data = fetch_json("items")
            tiers_data = fetch_json("tiers")
            tactical_skills_data = fetch_json("tactical-skills")
            trait_skills_data = fetch_json("trait-skills")
            masteries_data = fetch_json("masteries")
            skills_data = fetch_json("skills")

            print("\n更新角色头像...")
            download_collection(
                collection=characters_data.get("characters", []),
                folder=ASSET_DIR / "characters",
                id_key="id",
                url_key="imageUrl",
                index_name="characters",
                skip_existing=True,
            )

            print("\n更新角色皮肤...")
            skins = []
            for char in characters_data.get("characters", []):
                for skin in char.get("skins", []):
                    skin_copy = dict(skin)
                    skin_copy["characterId"] = char.get("id")
                    skins.append(skin_copy)

            download_collection(
                collection=skins,
                folder=ASSET_DIR / "character_skins",
                id_key="id",
                url_key="imageUrl",
                extra_fields=("name", "imageName", "characterId"),
                index_name="character_skins",
                skip_existing=True,
            )

            print("\n更新物品图标...")
            download_collection(
                collection=items_data.get("items", []),
                folder=ASSET_DIR / "items",
                id_key="id",
                url_key="imageUrl",
                index_name="items",
                skip_existing=True,
            )

            print("\n更新段位图标...")
            tier_items = []
            for t in tiers_data.get("tiers", []):
                icon_item = dict(t)
                icon_item["__url"] = normalize_url(t.get("iconUrl", ""))
                tier_items.append(icon_item)

            download_collection(
                collection=tier_items,
                folder=ASSET_DIR / "tiers",
                id_key="id",
                url_key="__url",
                index_name="tiers",
                skip_existing=True,
            )

            print("\n更新段位大图...")
            tier_full_items = []
            for t in tiers_data.get("tiers", []):
                full_item = dict(t)
                full_item["__url"] = normalize_url(t.get("imageUrl", ""))
                tier_full_items.append(full_item)

            download_collection(
                collection=tier_full_items,
                folder=ASSET_DIR / "tiers_full",
                id_key="id",
                url_key="__url",
                index_name="tiers_full",
                skip_existing=True,
            )

            print("\n更新战术技能图标...")
            download_collection(
                collection=tactical_skills_data.get("tacticalSkills", []),
                folder=ASSET_DIR / "tactical_skills",
                id_key="id",
                url_key="imageUrl",
                index_name="tactical_skills",
                skip_existing=True,
            )

            print("\n更新特性技能图标...")
            download_collection(
                collection=trait_skills_data.get("traitSkills", []),
                folder=ASSET_DIR / "trait_skills",
                id_key="id",
                url_key="imageUrl",
                index_name="trait_skills",
                skip_existing=True,
            )

            print("\n更新特性分组图标...")
            group_folder = ASSET_DIR / "trait_skill_groups"
            ensure_dir(group_folder)
            group_index = {}

            groups = trait_skills_data.get("traitSkillGroups", [])
            for i, g in enumerate(groups, 1):
                key = safe_name(g.get("key", "unknown"))
                url = normalize_url(g.get("imageUrl", ""))
                ext = get_ext_from_url(url)
                path = group_folder / f"{key}{ext}"

                if path.exists() and path.stat().st_size > 0:
                    ok = True
                else:
                    ok = download_file(url, path)

                group_index[key] = {
                    "key": g.get("key"),
                    "name": g.get("name"),
                    "file": str(path.as_posix()),
                    "url": url,
                }

                if not (path.exists() and path.stat().st_size > 0):
                    if debug:
                        print(f"[DEBUG] [{i}/{len(groups)}] {'OK' if ok else 'FAIL'} trait_skill_groups/{key}{ext}")

            save_json(group_index, API_DIR / "trait_skill_groups_index.json")

            print("\n更新武器熟练度图标...")
            mastery_items = []
            for m in masteries_data.get("masteries", []):
                item = dict(m)
                item["__url"] = normalize_url(m.get("iconUrl", ""))
                mastery_items.append(item)

            download_collection(
                collection=mastery_items,
                folder=ASSET_DIR / "masteries",
                id_key="id",
                url_key="__url",
                index_name="masteries",
                skip_existing=True,
            )

            print("\n更新角色技能图标...")
            download_collection(
                collection=skills_data.get("skills", []),
                folder=ASSET_DIR / "skills",
                id_key="id",
                url_key="imageUrl",
                extra_fields=("name", "slot", "characterId"),
                index_name="skills",
                skip_existing=True,
            )

            print("\n更新角色段位评级图标...")
            char_tier_badges = [
                {"id": "S", "url": "https://cdn.dak.gg/er/images/character/tier/character-tier-S.svg"},
                {"id": "A", "url": "https://cdn.dak.gg/er/images/character/tier/character-tier-A.svg"},
                {"id": "B", "url": "https://cdn.dak.gg/er/images/character/tier/character-tier-B.svg"},
                {"id": "C", "url": "https://cdn.dak.gg/er/images/character/tier/character-tier-C.svg"},
                {"id": "D", "url": "https://cdn.dak.gg/er/images/character/tier/character-tier-D.svg"},
            ]
            char_tier_badge_dir = ASSET_DIR / "character_tier_badges"
            ensure_dir(char_tier_badge_dir)
            char_tier_badge_index = {}
            for badge in char_tier_badges:
                badge_id = badge["id"]
                url = normalize_url(badge["url"])
                ext = get_ext_from_url(url)
                file_path = char_tier_badge_dir / f"{badge_id}{ext}"
                if file_path.exists() and file_path.stat().st_size > 0:
                    ok = True
                else:
                    ok = download_file(url, file_path)
                char_tier_badge_index[badge_id] = {
                    "id": badge_id,
                    "key": badge_id,
                    "name": badge_id,
                    "file": str(file_path.as_posix()),
                    "url": url,
                }
                if debug:
                    print(f"[DEBUG] {'OK' if ok else 'FAIL'} character_tier_badges/{badge_id}{ext}")
            save_json(char_tier_badge_index, API_DIR / "character_tier_badges_index.json")

        except Exception as e:
            print(f"[ERROR] 更新图片资源失败: {e}")

    print("\n=== 差异资源更新完成 ===")
    if summary["download"] or summary["adopt"] or summary["fail"]:
        print(
            f"[变更图片处理] 实际重下: {summary['download']} | "
            f"内容未变仅更新URL: {summary['adopt']} | 失败: {summary['fail']}"
        )
    return summary


def check_and_update_resources(debug: bool = True) -> dict:
    diff_info = check_resource_diff()

    if diff_info["json_files"]["missing"] or diff_info["json_files"]["changed"] or diff_info["image_files"]["missing"] or diff_info["image_files"]["changed"]:
        update_diff_resources(diff_info, debug=debug)
    else:
        print("\n所有资源都是最新的，无需更新。")

    return diff_info


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        if sys.argv[1] == "check":
            check_resource_diff()
        elif sys.argv[1] == "update":
            diff_info = check_resource_diff()
            update_diff_resources(diff_info, debug=True)
        else:
            print("用法:")
            print("  python get_image.py          # 完整更新所有资源")
            print("  python get_image.py check    # 只检查资源差异")
            print("  python get_image.py update   # 检查并更新差异资源")
    else:
        main()