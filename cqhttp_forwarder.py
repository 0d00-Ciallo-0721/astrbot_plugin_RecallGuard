"""
一个用于通过直连 aiocqhttp API 发送合并转发消息的实用工具模块。
该方法解决了 AstrBot 高层级 API 无法主动向任意群聊发送合并转发消息的问题。
"""
from typing import List, Dict, Any
import os
from astrbot.api import logger

async def send_group_forward_message_by_api(
    bot_client: Any,
    group_id: int,
    nodes: List[Dict]
) -> bool:
    """
    通过调用 aiocqhttp 底层 API 发送群聊合并转发消息。

    :param bot_client: AiocqhttpMessageEvent 中的 event.bot 对象。
    :param group_id: 目标群聊的ID。
    :param nodes: 符合 OneBot v11 "合并转发节点" 格式的字典列表。
    :return: 发送成功返回 True，否则返回 False。
    """
    if not bot_client or not hasattr(bot_client, 'api'):
        logger.error("[Forwarder] 传入的 bot_client 无效。")
        return False
    
    try:
        await bot_client.api.call_action(
            'send_group_forward_msg',
            group_id=group_id,
            messages=nodes
        )
        logger.info(f"[Forwarder] 已成功向群聊 {group_id} 发送合并转发消息。")
        return True
    except Exception as e:
        logger.error(f"[Forwarder] 向群聊 {group_id} 发送合并转发时发生错误: {e}", exc_info=True)
        return False

def create_forward_node(
    user_id: str,
    nickname: str,
    content_segments: List[Dict]
) -> Dict:
    """
    创建一个符合 OneBot v11 格式的合并转发节点。

    :param user_id: 该节点消息的发送者QQ号。
    :param nickname: 该节点消息的发送者昵称。
    :param content_segments: 符合 OneBot v11 "消息段" 格式的字典列表。
    :return: 一个标准的合并转发节点字典。
    """
    return {
        "type": "node",
        "data": {
            "user_id": user_id,
            "nickname": nickname,
            "content": content_segments
        }
    }

def text_to_segment(text: str) -> Dict:
    """将纯文本转换为 OneBot v11 消息段格式。"""
    return {"type": "text", "data": {"text": text}}

def local_image_to_segment(file_path: str) -> Dict:
    """将本地图片路径转换为 OneBot v11 图片消息段格式。"""
    absolute_path = os.path.abspath(file_path)
    # 构造标准的、有三个斜杠的 file URI (file:// + /path/to/file)
    correct_uri = f"file://{absolute_path}"
    return {"type": "image", "data": {"file": correct_uri}}

def local_audio_to_segment(file_path: str) -> Dict:
    """将本地语音路径转换为 OneBot v11 语音消息段格式。"""
    absolute_path = os.path.abspath(file_path)
    # 构造标准的、有三个斜杠的 file URI
    correct_uri = f"file://{absolute_path}"
    return {"type": "record", "data": {"file": correct_uri}}
