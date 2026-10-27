"""QQ 官方 Markdown 图片素材上传（附加模块）。

用于把本地渲染的战绩图变成 ``qqbot-media://`` 引用，从而和 keyboard 一起塞进
同一条 markdown 消息（图文 + 按钮单条发送），不再需要「图一条 + 按钮一条」。

官方接口（素材管理）：
- POST /v2/media-images/prepare   申请上传，返回 presigned_url
- PUT  <presigned_url>            直传图片二进制（Content-Type 必须与 prepare 一致）
- POST /v2/media-images/complete  完成上传，返回 qqbot-media:// 引用
- GET  /v2/media-images           查询当前机器人有效图片（配额清理用）
- DELETE /v2/media-images/{id}/delete

限制：单张 ≤10MB、png/jpeg/gif/webp、单机器人 100 张、90 天滑动过期（被引用即顺延）。
鉴权复用 qq_panel_manager 的 appId/clientSecret 换取 access_token 逻辑。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any, Optional

from .qq_panel_manager import PanelAPIError, _build_headers, _get_appid_secret

from astrbot.api import logger

try:
    import aiohttp

    _HAS_AIOHTTP = True
except Exception:
    _HAS_AIOHTTP = False

# 素材接口域名（与指令面板的 api.sgroup.qq.com 不同，官方文档以 api.bot.qq.com 为例）
BASE_URL = "https://api.bot.qq.com"

TIMEOUT_SECONDS = 60

# 单机器人素材数量上限（官方默认 100 张）
MEDIA_QUOTA = 100

# 本地缓存：md5 -> (qqbot-media 引用, image_id, 时间戳)
# 90 天滑动过期，缓存 30 天足够安全，避免每次查询都重复上传
_MEDIA_CACHE: dict[str, tuple[str, str, float]] = {}
_CACHE_TTL = 30 * 24 * 3600

_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# 错误码：素材数量上限（清理旧图后重试）
_ERR_QUOTA = 40035007


def available() -> bool:
    """当前环境是否具备素材上传能力。"""
    return _HAS_AIOHTTP


async def _request(
    client: Any,
    method: str,
    path: str,
    query: Optional[dict] = None,
    body: Optional[dict] = None,
) -> dict:
    """带 QQBot 鉴权的素材接口请求；非 code=0 抛 PanelAPIError。"""
    if not _HAS_AIOHTTP:
        raise PanelAPIError(message="环境缺少 aiohttp，无法上传图片素材")
    headers = await _build_headers(client)
    url = BASE_URL + path
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method,
                url,
                params=query,
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS),
            ) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    text = await resp.text()
                    data = {"code": -1, "message": f"HTTP {resp.status} {text[:200]}"}
    except PanelAPIError:
        raise
    except Exception as e:
        logger.warning(f"[ER图文] 素材接口请求失败 {method} {path}: {e}")
        raise PanelAPIError(message=str(e)) from e
    if isinstance(data, dict) and data.get("code") not in (None, 0):
        raise PanelAPIError(
            code=data.get("code"),
            message=str(data.get("message", "")) or f"HTTP {path}",
        )
    return data


def _read_image(path: str) -> tuple[bytes, str, str, str]:
    """读取图片，返回 (bytes, content_type, md5, sha1)。"""
    suffix = ""
    lower = path.lower()
    for ext in _CONTENT_TYPES:
        if lower.endswith(ext):
            suffix = ext
            break
    if not suffix:
        raise PanelAPIError(message=f"不支持的图片格式（仅 png/jpg/gif/webp）: {path}")
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise PanelAPIError(message=f"读取图片失败: {e}") from e
    if len(raw) > 10 * 1024 * 1024:
        raise PanelAPIError(message="图片超过 10MB，无法作为 markdown 素材上传")
    return raw, _CONTENT_TYPES[suffix], hashlib.md5(raw).hexdigest(), hashlib.sha1(raw).hexdigest()


async def _put_binary(presigned_url: str, raw: bytes, content_type: str) -> str:
    """按预签名 URL 直传二进制，返回 ETag（即文件 MD5）。"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.put(
                presigned_url,
                data=raw,
                headers={"Content-Type": content_type},
                timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS),
            ) as resp:
                if resp.status not in (200, 201, 204):
                    text = await resp.text()
                    raise PanelAPIError(
                        message=f"素材直传失败（HTTP {resp.status}）: {text[:200]}"
                    )
                etag = (resp.headers.get("ETag") or "").strip('"')
    except PanelAPIError:
        raise
    except Exception as e:
        raise PanelAPIError(message=f"素材直传异常: {e}") from e
    return etag


async def _free_quota(client: Any, drop: int = 10) -> int:
    """配额满时删除最早的若干张素材，返回删除数量。

    只删除本机器人名下的旧图片（插件不持久化 image_id，重启后由这里兜底清理）。
    """
    try:
        data = await _request(client, "GET", "/v2/media-images")
    except PanelAPIError as e:
        logger.warning(f"[ER图文] 查询素材列表失败，无法清理配额: {e}")
        return 0
    images = list(data.get("images") or [])
    if not images:
        return 0
    deleted = 0
    deleted_ids: set[str] = set()
    for item in images[:drop]:
        image_id = item.get("image_id")
        if not image_id:
            continue
        try:
            await _request(client, "DELETE", f"/v2/media-images/{image_id}/delete")
            deleted += 1
            deleted_ids.add(str(image_id))
        except PanelAPIError as e:
            logger.warning(f"[ER图文] 删除素材 {image_id} 失败: {e}")
    if deleted:
        # 被删掉的素材引用已失效，同步清掉本地缓存，避免下次复用坏链接
        for key, (_, image_id, _ts) in list(_MEDIA_CACHE.items()):
            if image_id in deleted_ids:
                _MEDIA_CACHE.pop(key, None)
        logger.info(f"[ER图文] 素材配额已清理 {deleted} 张旧图片")
    return deleted


async def upload_image(client: Any, path: str) -> str:
    """把本地图片上传为 qqbot-media:// 引用。

    缓存按「appid + md5」区分：素材引用与机器人绑定，多机器人实例之间不能混用；
    同一 appid 下相同图片（md5）直接复用，避免重复占用 100 张配额。
    """
    raw, content_type, md5, sha1 = _read_image(path)

    appid, _secret = _get_appid_secret(client)
    cache_key = f"{appid}:{md5}"

    cached = _MEDIA_CACHE.get(cache_key)
    if cached and time.time() - cached[2] < _CACHE_TTL:
        return cached[0]

    async def _once() -> tuple[str, str]:
        prepare = await _request(
            client,
            "POST",
            "/v2/media-images/prepare",
            body={
                "file_name": f"er_profile_{md5[:8]}{_suffix_of(path)}",
                "file_size": len(raw),
                "content_type": content_type,
                "md5": md5,
                "sha1": sha1,
            },
        )
        upload_id = str(prepare.get("upload_id") or "")
        presigned_url = str(prepare.get("presigned_url") or "")
        if not (upload_id and presigned_url):
            raise PanelAPIError(message=f"素材申请上传返回异常: {prepare}")
        await _put_binary(presigned_url, raw, content_type)
        done = await _request(
            client,
            "POST",
            "/v2/media-images/complete",
            body={"upload_id": upload_id, "md5": md5},
        )
        url = str(done.get("url") or "")
        if not url:
            raise PanelAPIError(message=f"素材上传完成但未返回引用: {done}")
        return url, str(done.get("image_id") or "")

    try:
        url, image_id = await _once()
    except PanelAPIError as e:
        if e.code != _ERR_QUOTA:
            raise
        logger.warning("[ER图文] 素材数量已达上限，清理旧图片后重试")
        if not await _free_quota(client):
            raise
        url, image_id = await _once()

    _MEDIA_CACHE[cache_key] = (url, image_id, time.time())
    logger.info(f"[ER图文] 战绩图已上传为素材: {image_id or url[:40]}")
    return url


def _suffix_of(path: str) -> str:
    lower = path.lower()
    for ext in _CONTENT_TYPES:
        if lower.endswith(ext):
            return ext
    return ".png"


def image_size(path: str, max_width: int = 720) -> tuple[int, int]:
    """读取图片尺寸，并按 max_width 等比缩放（用于 markdown 的 #宽px #高px）。"""
    try:
        from PIL import Image

        with Image.open(path) as im:
            width, height = im.size
    except Exception as e:
        logger.debug(f"[ER图文] 读取图片尺寸失败: {e}")
        return (0, 0)
    if width <= 0 or height <= 0:
        return (0, 0)
    if width > max_width:
        height = max(1, round(height * max_width / width))
        width = max_width
    return (width, height)


def build_image_markdown(url: str, path: str, alt: str = "战绩", max_width: int = 720) -> str:
    """构造 markdown 图片行；拿不到尺寸时就省略宽高参数。"""
    width, height = image_size(path, max_width)
    size = f" #{width}px #{height}px" if width and height else ""
    return f"![{alt}{size}]({url})"


__all__ = [
    "available",
    "build_image_markdown",
    "image_size",
    "upload_image",
]
