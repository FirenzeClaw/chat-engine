"""
消息 Schema 定义

用 TypedDict 约定 server ↔ bridge 之间的消息格式，
IDE 可自动补全字段名，重构时编译器安全。
"""

from typing import TypedDict


class UnifiedMessage(TypedDict, total=False):
    """server → bridge 的统一消息格式"""
    type: str          # 事件类型: AT_MESSAGE_CREATE / C2C_MESSAGE_CREATE / ...
    id: str            # 事件唯一 ID
    user_id: str       # 发送者 QQ openid
    username: str      # 发送者昵称
    content: str       # 消息文本内容
    group_id: str      # 群 openid（群聊事件）
    channel_id: str    # 频道 ID（频道事件）
    guild_id: str      # 频道服务器 ID（频道私信事件）
    ref_msg_id: str    # 原消息 ID（回复时回传）
    timestamp: float   # 接收时间戳


class ReplyMessage(TypedDict, total=False):
    """bridge → server 的 AI 回复格式"""
    type: str          # 固定 "reply"
    user_id: str       # 目标用户 openid
    content: str       # AI 回复文本
    group_id: str      # 群 openid
    channel_id: str    # 频道 ID
    guild_id: str      # 频道服务器 ID
    ref_msg_id: str    # 原消息 ID（用于被动回复）
    msg_type: str      # 原始事件类型（用于路由回复端点）
