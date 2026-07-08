"""
QQ 社交信息采集模块

通过 QQ REST API 获取用户昵称、群名称等社交信息，并缓存到 memory_store。
"""

import json
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from log_config import get_logger
logger = get_logger("social")

# 缓存过期时间（秒）
CACHE_USER_PROFILE = 24 * 3600   # 24 小时
CACHE_GROUP_INFO = 1 * 3600      # 1 小时


async def _get_access_token() -> str:
    """获取 QQ Bot access_token（从 qq_protocol 委托）。"""
    try:
        from qq_protocol import get_access_token as qq_get_token
        return await qq_get_token()
    except Exception:
        logger.exception("获取 access_token 失败")
        return ""


async def fetch_user_profile(openid: str) -> Optional[dict]:
    """通过 QQ REST API 获取用户资料，并缓存到 memory_store。

    如果缓存存在且未过期，直接从缓存返回。

    Returns:
        dict with nickname, avatar_url, or None on failure
    """
    from memory_store import get as mem_get, set as mem_set

    # 检查缓存
    cached = await mem_get(f"user/{openid}/profile", "profile")
    if cached:
        try:
            pv = json.loads(cached["value"])
            cache_time = cached.get("updated_at", "")
            if cache_time:
                # 检查是否在有效期内
                age = time.time() - datetime.fromisoformat(cache_time).timestamp()
                if age < CACHE_USER_PROFILE:
                    return pv
        except (json.JSONDecodeError, ValueError, KeyError):
            pass

    # 调用 QQ API
    token = await _get_access_token()
    if not token:
        logger.warning("无 access_token，跳过用户资料获取")
        return None

    url = f"https://api.sgroup.qq.com/v2/users/{openid}"
    headers = {
        "Authorization": f"QQBot {token}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    profile = {
                        "nickname": data.get("nickname", ""),
                        "avatar_url": data.get("avatar", ""),
                        "first_seen": datetime.now(timezone.utc).isoformat(),
                        "last_seen": datetime.now(timezone.utc).isoformat(),
                        "message_count": 0,
                        "notes": "",
                    }
                    await mem_set(
                        f"user/{openid}/profile", "profile",
                        json.dumps(profile, ensure_ascii=False)
                    )
                    return profile
                else:
                    logger.warning(
                        "获取用户资料失败: openid=%s status=%d", openid, resp.status
                    )
    except Exception:
        logger.exception("获取用户资料异常: openid=%s", openid)

    return None


async def fetch_group_info(group_openid: str) -> Optional[dict]:
    """通过 QQ REST API 获取群信息，并缓存到 memory_store。

    如果缓存存在且未过期，直接从缓存返回。

    Returns:
        dict with name, member_count, or None on failure
    """
    from memory_store import get as mem_get, set as mem_set

    # 检查缓存
    cached = await mem_get(f"group/{group_openid}/info", "info")
    if cached:
        try:
            pv = json.loads(cached["value"])
            cache_time = cached.get("updated_at", "")
            if cache_time:
                age = time.time() - datetime.fromisoformat(cache_time).timestamp()
                if age < CACHE_GROUP_INFO:
                    return pv
        except (json.JSONDecodeError, ValueError, KeyError):
            pass

    # 调用 QQ API
    token = await _get_access_token()
    if not token:
        logger.warning("无 access_token，跳过群信息获取")
        return None

    url = f"https://api.sgroup.qq.com/v2/groups/{group_openid}"
    headers = {
        "Authorization": f"QQBot {token}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    group_info = {
                        "name": data.get("name", ""),
                        "member_count": data.get("member_count", 0),
                        "first_seen": datetime.now(timezone.utc).isoformat(),
                        "last_seen": datetime.now(timezone.utc).isoformat(),
                    }
                    await mem_set(
                        f"group/{group_openid}/info", "info",
                        json.dumps(group_info, ensure_ascii=False)
                    )
                    return group_info
                else:
                    logger.warning(
                        "获取群信息失败: group_openid=%s status=%d",
                        group_openid, resp.status
                    )
    except Exception:
        logger.exception("获取群信息异常: group_openid=%s", group_openid)

    return None


async def update_user_seen(user_id: str) -> None:
    """更新用户最后活跃时间。"""
    from memory_store import get as mem_get, set as mem_set

    cached = await mem_get(f"user/{user_id}/profile", "profile")
    if cached:
        try:
            profile = json.loads(cached["value"])
            profile["last_seen"] = datetime.now(timezone.utc).isoformat()
            profile["message_count"] = profile.get("message_count", 0) + 1
            await mem_set(
                f"user/{user_id}/profile", "profile",
                json.dumps(profile, ensure_ascii=False)
            )
        except (json.JSONDecodeError, KeyError):
            pass
