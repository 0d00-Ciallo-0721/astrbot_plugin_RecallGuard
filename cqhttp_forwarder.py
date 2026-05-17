"""Low-level OneBot/NapCat sending helpers for RecallGuard."""

import os
from typing import Any, Dict, List, Optional

from astrbot.api import logger


def _file_uri(file_path: str) -> str:
    return f"file:///{os.path.abspath(file_path).replace(os.sep, '/')}"


def parse_session_id(session_id: str) -> tuple[Optional[str], Optional[int]]:
    lowered = session_id.lower()
    target_id = session_id.split(":")[-1]
    if not target_id.isdigit():
        return None, None
    if "group" in lowered:
        return "group", int(target_id)
    if "private" in lowered or "friend" in lowered or "user" in lowered:
        return "private", int(target_id)
    return None, int(target_id)


async def call_action(bot_client: Any, action: str, **params) -> Any:
    if not bot_client or not hasattr(bot_client, "api"):
        raise RuntimeError("invalid bot client")
    return await bot_client.api.call_action(action, **params)


async def send_message_by_api(bot_client: Any, session_id: str, segments: List[Dict]) -> bool:
    target_type, target_id = parse_session_id(session_id)
    if not target_type or target_id is None:
        logger.error(f"[Forwarder] invalid target session: {session_id}")
        return False

    try:
        if target_type == "group":
            await call_action(bot_client, "send_group_msg", group_id=target_id, message=segments)
        else:
            await call_action(bot_client, "send_private_msg", user_id=target_id, message=segments)
        logger.info(f"[Forwarder] sent native message to {session_id}")
        return True
    except Exception as e:
        logger.error(f"[Forwarder] native send failed: target={session_id}, error={e}", exc_info=True)
        return False


async def send_forward_message_by_api(bot_client: Any, session_id: str, nodes: List[Dict]) -> bool:
    target_type, target_id = parse_session_id(session_id)
    if not target_type or target_id is None:
        logger.error(f"[Forwarder] invalid forward target session: {session_id}")
        return False

    actions: List[tuple[str, Dict[str, Any]]] = []
    if target_type == "group":
        actions.append(("send_group_forward_msg", {"group_id": target_id, "messages": nodes}))
    else:
        actions.append(("send_private_forward_msg", {"user_id": target_id, "messages": nodes}))
    actions.append(("send_forward_msg", {"messages": nodes}))

    for action, params in actions:
        try:
            await call_action(bot_client, action, **params)
            logger.info(f"[Forwarder] sent forward message via {action} to {session_id}")
            return True
        except Exception as e:
            logger.warning(f"[Forwarder] {action} failed for {session_id}: {e}")
    return False


async def send_group_forward_message_by_api(bot_client: Any, group_id: int, nodes: List[Dict]) -> bool:
    return await send_forward_message_by_api(bot_client, f"aiocqhttp:group:{group_id}", nodes)


def create_forward_node(user_id: str, nickname: str, content_segments: List[Dict]) -> Dict:
    return {
        "type": "node",
        "data": {
            "user_id": str(user_id),
            "nickname": nickname or "RecallGuard",
            "content": content_segments,
        },
    }


def text_to_segment(text: str) -> Dict:
    return {"type": "text", "data": {"text": text}}


def local_image_to_segment(file_path: str) -> Dict:
    return {"type": "image", "data": {"file": _file_uri(file_path)}}


def local_audio_to_segment(file_path: str) -> Dict:
    return {"type": "record", "data": {"file": _file_uri(file_path)}}


def local_video_to_segment(file_path: str) -> Dict:
    return {"type": "video", "data": {"file": _file_uri(file_path)}}
