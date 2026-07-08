"""
图片处理器 — 接收图片 → 模型理解 → 分类存储 → 检索引用

Spec 003: 多模态自主 AI — Layer 1

支持四大类图片:
- meme: 纯表情包（以文字为主、表达情绪）
- meme_pic: 梗图（有梗、有上下文、有笑点）
- scenery: 风景图/生活照
- favorite: 用户明确标记为喜欢的图
"""

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import aiohttp

from json_utils import parse_json_block

from config import (
    LLM_API_KEY, LLM_BASE_URL, LLM_FAST_MODEL,
    IMAGE_MAX_SIZE_MB, IMAGE_STORAGE_DIR,
)

from log_config import get_logger
logger = get_logger("image")


# ==================== Enums & Dataclasses ====================

class ImageCategory(str, Enum):
    MEME = "meme"           # 纯表情包
    MEME_PIC = "meme_pic"   # 梗图
    SCENERY = "scenery"     # 风景/生活照
    FAVORITE = "favorite"   # 收藏
    UNKNOWN = "unknown"     # 分类失败


@dataclass
class ImageRecord:
    """图片记忆记录"""
    file_path: str
    category: str
    description: str
    opinion: str
    tags: list[str]
    source_user: str
    source_msg_id: str
    metadata: dict = field(default_factory=dict)
    created_at: str = ""
    width: int = 0
    height: int = 0


# ==================== Classification ====================

_CLASSIFICATION_PROMPT = """你是一个图片理解助手。分析图片并输出 JSON:

{"category": "meme|meme_pic|scenery|favorite",
 "description": "图片内容的客观描述",
 "opinion": "你个人对这张图的真实看法（像朋友评论，带点语气）",
 "tags": ["标签1","标签2",...]}

分类标准:
- meme: 纯表情包（以文字为主、表达情绪）
- meme_pic: 梗图（有梗、有上下文、有笑点）
- scenery: 风景图/生活照
- favorite: 人物自拍/有纪念意义的照片"""


async def classify_and_describe(image_data: bytes, content_type: str = "image/jpeg", fallback_url: str = "") -> dict:
    """使用 step-3.7-flash 理解 + 分类图片。

    优先使用远程 URL（模型服务器可直接访问），
    失败时降级为 base64 data URI。

    Args:
        image_data: 图片字节数据（用于降级 base64）
        content_type: MIME 类型
        fallback_url: 远程 URL（优先使用）

    Returns:
        {category, description, opinion, tags}
    """
    from openai import AsyncOpenAI
    import base64

    client = AsyncOpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

    # 优先使用远程 URL（模型服务器在国内，可访问 QQ CDN）
    image_input = fallback_url if fallback_url else f"data:{content_type};base64,{base64.b64encode(image_data).decode('utf-8')}"

    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=LLM_FAST_MODEL,
                messages=[
                    {"role": "system", "content": _CLASSIFICATION_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "请分析这张图片"},
                            {"type": "image_url", "image_url": {"url": image_input}},
                        ],
                    },
                ],
                max_tokens=1000,
                temperature=0.3,
            ),
            timeout=10.0,
        )
        raw = response.choices[0].message.content or ""
        finish = response.choices[0].finish_reason
        logger.info("图片分类 LLM 响应: finish=%s, len=%d, raw=%s",
                     finish, len(raw), raw[:200])
        # 解析 JSON
        data = parse_json_block(raw)
        if data:
            return {
                "category": data.get("category", "meme"),
                "description": data.get("description", ""),
                "opinion": data.get("opinion", ""),
                "tags": data.get("tags", []),
            }
        logger.warning("图片分类 JSON 解析失败, raw=%s", raw[:200])
        return {"category": "unknown", "description": raw[:200], "opinion": "", "tags": []}

    except asyncio.TimeoutError:
        logger.warning("图片理解超时 10s")
        return {"category": "unknown", "description": None, "opinion": None, "tags": []}
    except Exception as e:
        logger.exception("图片理解失败: %s", e)
        return {"category": "unknown", "description": None, "opinion": None, "tags": []}


# ==================== Download ====================

async def download_image(url: str) -> bytes:
    """HTTP GET 下载图片，带大小限制和超时。

    Args:
        url: 图片 URL

    Returns:
        图片字节数据

    Raises:
        ValueError: 图片超过大小限制或不是图片
        asyncio.TimeoutError: 下载超时
    """
    max_bytes = IMAGE_MAX_SIZE_MB * 1024 * 1024

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise ValueError(f"下载失败 HTTP {resp.status}")

            content_type = resp.headers.get("Content-Type", "")
            if "image" not in content_type and not url.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")):
                raise ValueError(f"not_an_image: {content_type}")

            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise ValueError(f"image_too_large: {int(content_length) / 1024 / 1024:.1f}MB > {IMAGE_MAX_SIZE_MB}MB")

            data = b""
            async for chunk in resp.content.iter_chunked(8192):
                data += chunk
                if len(data) > max_bytes:
                    raise ValueError(f"image_too_large: > {IMAGE_MAX_SIZE_MB}MB")

            return data


# ==================== Storage ====================

async def store_image(file_data: bytes, metadata: dict) -> str:
    """存储图片到本地文件系统 + 写入 memory_store 索引。

    Args:
        file_data: 图片字节数据
        metadata: {uid, category, description, opinion, tags, source_user, source_msg_id}

    Returns:
        本地文件路径
    """
    uid = metadata.get("uid", "unknown")
    now = datetime.now(timezone.utc)

    # 目录: botuser/images/{uid}/
    img_dir = Path(IMAGE_STORAGE_DIR) / uid
    img_dir.mkdir(parents=True, exist_ok=True)

    # 文件名: YYYYMMDD-HHMMSS.jpg
    timestamp = now.strftime("%Y%m%d-%H%M%S")
    ext = ".jpg"
    file_path = img_dir / f"{timestamp}{ext}"

    file_path.write_bytes(file_data)
    logger.info("图片已存储: %s (%d bytes)", file_path, len(file_data))

    # 写入 memory_store 索引
    try:
        from memory_store import set_image_entry

        record = {
            "file_path": str(file_path.relative_to(Path(IMAGE_STORAGE_DIR).parent)),
            "category": metadata.get("category", "meme"),
            "description": metadata.get("description", ""),
            "opinion": metadata.get("opinion", ""),
            "tags": metadata.get("tags", []),
            "source_user": metadata.get("source_user", ""),
            "source_msg_id": metadata.get("source_msg_id", ""),
            "created_at": now.isoformat(),
            "width": metadata.get("width", 0),
            "height": metadata.get("height", 0),
            "entity_type": "image",
        }

        namespace = f"user/{uid}/images/{metadata.get('category', 'meme')}"
        key = f"{timestamp}"
        await set_image_entry(
            namespace=namespace,
            key=key,
            value=json.dumps(record, ensure_ascii=False),
        )
    except Exception:
        logger.exception("图片索引写入失败")

    return str(file_path)


# ==================== Main Pipeline ====================

async def handle_image(
    image_url: str,
    source_user: str,
    source_msg_id: str,
    metadata: dict,
) -> dict:
    """图片处理全管线：下载 → 理解 → 分类 → 存储。

    Args:
        image_url: 图片 URL（QQ 附件 URL）
        source_user: 发送者 QQ openid
        source_msg_id: 来源消息 ID
        metadata: QQ 消息元数据 {msg_type, group_id, ...}

    Returns:
        {file_path, category, description, opinion, tags}
        或 {error: "..."}
    """
    # 1. 下载图片
    try:
        file_data = await download_image(image_url)
    except ValueError as e:
        logger.warning("图片下载被拒绝: %s", e)
        return {"error": str(e)}
    except asyncio.TimeoutError:
        logger.warning("图片下载超时: %s", image_url[:60])
        return {"error": "download_timeout"}
    except Exception:
        logger.exception("图片下载失败")
        return {"error": "download_failed"}

    # 2. 理解 + 分类（优先 QQ CDN URL，StepFun 服务器在国内可直连）
    classify_result = await classify_and_describe(file_data, fallback_url=image_url)

    # 3. 存储
    store_meta = {
        "uid": source_user,
        "category": classify_result.get("category", "meme"),
        "description": classify_result.get("description", ""),
        "opinion": classify_result.get("opinion", ""),
        "tags": classify_result.get("tags", []),
        "source_user": source_user,
        "source_msg_id": source_msg_id,
        "source": "group" if "GROUP" in metadata.get("msg_type", "") else "private",
        "group_id": metadata.get("group_id", ""),
    }

    file_path = await store_image(file_data, store_meta)

    return {
        "file_path": file_path,
        "category": classify_result.get("category", "meme"),
        "description": classify_result.get("description", ""),
        "opinion": classify_result.get("opinion", ""),
        "tags": classify_result.get("tags", []),
    }


# ==================== Retrieval ====================

async def retrieve_relevant_images(
    query: str,
    user_id: str,
    limit: int = 5,
) -> list[dict]:
    """检索与查询相关的图片记忆。

    Args:
        query: 搜索关键词
        user_id: 用户 ID
        limit: 最大返回条数

    Returns:
        图片记录列表 [{file_path, description, opinion, tags, ...}]
    """
    from memory_store import search_images

    try:
        results = await search_images(query, user_id, limit)
        output = []
        for r in results:
            try:
                record = json.loads(r["value"])
                output.append({
                    "file_path": record.get("file_path", ""),
                    "category": record.get("category", ""),
                    "description": record.get("description", ""),
                    "opinion": record.get("opinion", ""),
                    "tags": record.get("tags", []),
                    "created_at": record.get("created_at", ""),
                })
            except (json.JSONDecodeError, TypeError):
                pass
        return output
    except Exception:
        logger.exception("图片检索失败")
        return []
