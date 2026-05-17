import asyncio
import copy
import json
import os
import shutil
import time
from typing import Any, Dict, List, Optional

from aiocqhttp.exceptions import ActionFailed
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.event import filter
from astrbot.api.message_components import (
    File as CompFile,
    Image as CompImage,
    Plain as CompPlain,
    Record as CompRecord,
    Video as CompVideo,
)
from astrbot.api.star import Context, Star, register
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

from . import cqhttp_forwarder


MESSAGE_CACHE: Dict[str, Dict[str, Any]] = {}
DIRECT_SEGMENT_TYPES = {"text", "image", "record", "video", "file", "face", "at", "reply", "json", "xml"}
MEDIA_ACTIONS = {"image": ("get_image",), "record": ("get_record",), "video": ("get_file",), "file": ("get_file",)}
SUPPORTED_SUMMARY_TYPES = {"forward", "node", "share", "location", "music", "markdown", "light_app", "shake", "poke"}
VIDEO_SIZE_LIMIT = 100 * 1024 * 1024


@register(
    "RecallGuard",
    "和泉智宏",
    "面向 NapCat/OneBot 的防撤回插件，支持全消息段缓存、原配置转发模式和原生 API 保底。",
    "2.1.0",
    "https://github.com/0d00-Ciallo-0721/astrbot_plugin_RecallGuard",
)
class RecallGuardPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.running = True
        self.cache_dir = self.config.get("cleanup_options", {}).get("cache_dir", "/shared/recall_guard_cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.cleanup_task = asyncio.create_task(self._periodic_cleanup())
        self._update_monitored_groups_set()
        logger.info("RecallGuard v2.1.0 NapCat adapter loaded.")

    def _update_monitored_groups_set(self):
        conf_group_list = self.config.get("group_monitoring", {}).get("monitored_groups", [])
        self.monitored_groups_set = {str(g).split(":")[-1] for g in conf_group_list if str(g).strip()}

    def _get_cache_scope(self, group_id: str, user_id: str) -> str:
        return f"group:{group_id}" if group_id else f"private:{user_id}"

    def _get_cache_key(self, message_id: str, group_id: str, user_id: str) -> str:
        return f"{self._get_cache_scope(group_id, user_id)}:{message_id}"

    def _get_cache_key_from_event(self, message_id: str, event: AstrMessageEvent) -> str:
        return self._get_cache_key(
            message_id,
            str(event.get_group_id() or ""),
            str(event.get_sender_id() or ""),
        )

    def _safe_cache_name(self, cache_key: str, suffix: str = "") -> str:
        safe = cache_key.replace(":", "_").replace("/", "_").replace("\\", "_")
        return f"{safe}{suffix}"

    def _get_recall_cache_keys(self, raw_event: dict, event: AstrMessageEvent, message_id: str) -> List[str]:
        group_id = str(raw_event.get("group_id") or event.get_group_id() or "")
        user_id = str(raw_event.get("user_id") or event.get_sender_id() or "")
        keys = [self._get_cache_key(message_id, group_id, user_id)]
        keys.extend(key for key in MESSAGE_CACHE if key.endswith(f":{message_id}") and key not in keys)
        if message_id not in keys:
            keys.append(message_id)
        return keys

    async def terminate(self):
        self.running = False
        if self.cleanup_task:
            self.cleanup_task.cancel()
        logger.info("RecallGuard v2.1.0 stopped.")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        raw_event = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw_event, dict) and raw_event.get("post_type") == "notice":
            return

        self._update_monitored_groups_set()
        sender_id = str(event.get_sender_id())
        group_id = str(event.get_group_id() or "")

        if not self._should_monitor(sender_id, group_id):
            return

        message_id = str(event.message_obj.message_id)
        cache_key = self._get_cache_key(message_id, group_id, sender_id)
        segments = self._extract_raw_segments(event)
        segments = self._filter_segments_by_config(segments)
        if not segments:
            logger.info(f"RecallGuard ignored message without monitored segments: message_id={message_id}, cache_key={cache_key}")
            return

        MESSAGE_CACHE[cache_key] = {
            "message_id": message_id,
            "cache_key": cache_key,
            "sender_id": sender_id,
            "sender_name": event.get_sender_name(),
            "group_id": group_id,
            "group_name": "",
            "timestamp": time.time(),
            "message_type": self._describe_segment_types(segments),
            "segments": segments,
            "raw_event": self._safe_copy(raw_event),
            "preparing": True,
        }
        cached_segments = await self._prepare_cache_segments(event, cache_key, segments)
        if not cached_segments:
            MESSAGE_CACHE.pop(cache_key, None)
            logger.warning(f"RecallGuard failed to cache any segment: message_id={message_id}, cache_key={cache_key}")
            return

        if cache_key not in MESSAGE_CACHE:
            self._remove_cached_files({"segments": cached_segments})
            logger.info(f"RecallGuard media prepared after recall handled: message_id={message_id}, cache_key={cache_key}")
            return

        group_name = await self._get_group_name(event, group_id)
        MESSAGE_CACHE[cache_key] = {
            "message_id": message_id,
            "cache_key": cache_key,
            "sender_id": sender_id,
            "sender_name": event.get_sender_name(),
            "group_id": group_id,
            "group_name": group_name,
            "timestamp": time.time(),
            "message_type": self._describe_segment_types(cached_segments),
            "segments": cached_segments,
            "raw_event": self._safe_copy(raw_event),
            "preparing": False,
        }
        logger.info(
            f"RecallGuard cached message: message_id={message_id}, cache_key={cache_key}, "
            f"segments={self._describe_segment_types(cached_segments)}"
        )

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_recall_notice(self, event: AstrMessageEvent):
        raw_event = event.message_obj.raw_message
        if not isinstance(raw_event, dict):
            return
        recall_notice_types = {"group_recall", "friend_recall", "group_msg_recall", "friend_msg_recall", "message_recall"}
        if raw_event.get("post_type") != "notice" or raw_event.get("notice_type") not in recall_notice_types:
            return
        if not isinstance(event, AiocqhttpMessageEvent):
            logger.warning(f"RecallGuard received recall notice from unsupported event: {type(event)}")
            return

        message_id = str(raw_event.get("message_id") or "")
        cache_keys = self._get_recall_cache_keys(raw_event, event, message_id)
        cached_info = await self._wait_and_pop_cached_info(cache_keys, message_id)
        if not cached_info:
            logger.warning(
                f"RecallGuard recall miss: message_id={message_id}, keys={cache_keys}, "
                f"notice_type={raw_event.get('notice_type')}, cache_size={len(MESSAGE_CACHE)}"
            )
            return

        logger.info(
            f"RecallGuard recall hit: message_id={message_id}, cache_key={cached_info.get('cache_key')}, "
            f"segments={cached_info.get('message_type')}"
        )
        await self._forward_recalled_content(cached_info, event.bot, event.get_self_id())

    def _should_monitor(self, sender_id: str, group_id: str) -> bool:
        conf_user = self.config.get("user_monitoring", {})
        blacklist_users = {str(u) for u in conf_user.get("blacklist_users", [])}
        monitored_users = {str(u) for u in conf_user.get("monitored_users", [])}
        if sender_id in blacklist_users:
            return False
        if sender_id in monitored_users:
            return True
        group_conf = self.config.get("group_monitoring", {})
        return bool(group_conf.get("enable_group_monitoring") and group_id and group_id in self.monitored_groups_set)

    async def _get_group_name(self, event: AstrMessageEvent, group_id: str) -> str:
        if not group_id or not isinstance(event, AiocqhttpMessageEvent):
            return ""
        try:
            group_info = await event.bot.api.call_action("get_group_info", group_id=int(group_id))
            if isinstance(group_info, dict):
                return group_info.get("group_name", "") or ""
        except ActionFailed as e:
            logger.warning(f"RecallGuard failed to fetch group name: group_id={group_id}, error={e}")
        except Exception as e:
            logger.error(f"RecallGuard unexpected group name error: group_id={group_id}, error={e}", exc_info=True)
        return ""

    def _extract_raw_segments(self, event: AstrMessageEvent) -> List[Dict[str, Any]]:
        raw_event = getattr(event.message_obj, "raw_message", None)
        raw_message = raw_event.get("message") if isinstance(raw_event, dict) else None
        if isinstance(raw_message, list):
            return [self._normalize_segment(segment) for segment in raw_message if isinstance(segment, dict)]
        if isinstance(raw_message, str) and raw_message:
            return [cqhttp_forwarder.text_to_segment(raw_message)]
        return self._segments_from_components(event.message_obj.message)

    def _segments_from_components(self, components: List[Any]) -> List[Dict[str, Any]]:
        segments: List[Dict[str, Any]] = []
        for component in components:
            if isinstance(component, CompPlain):
                segments.append(cqhttp_forwarder.text_to_segment(component.text))
            elif isinstance(component, CompImage):
                data = {"file": component.file or component.path or component.url or ""}
                if component.url:
                    data["url"] = component.url
                if component.path:
                    data["path"] = component.path
                segments.append({"type": "image", "data": data})
            elif isinstance(component, CompRecord):
                data = {"file": component.file or component.path or component.url or ""}
                if component.url:
                    data["url"] = component.url
                if component.path:
                    data["path"] = component.path
                segments.append({"type": "record", "data": data})
            elif isinstance(component, CompVideo):
                segments.append({"type": "video", "data": {"file": component.file or component.path or "", "path": component.path or ""}})
            elif isinstance(component, CompFile):
                segments.append({"type": "file", "data": {"file": component.file, "url": component.url, "name": component.name}})
            else:
                segments.append(self._summary_segment("component", self._safe_component_repr(component)))
        return segments

    def _normalize_segment(self, segment: Dict[str, Any]) -> Dict[str, Any]:
        normalized = copy.deepcopy(segment)
        normalized["type"] = str(normalized.get("type", "")).lower()
        data = normalized.get("data")
        normalized["data"] = data if isinstance(data, dict) else {}
        return normalized

    def _filter_segments_by_config(self, segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        conf_options = self.config.get("monitoring_options", {})
        result: List[Dict[str, Any]] = []
        for segment in segments:
            segment_type = segment.get("type", "")
            if segment_type == "text" and not conf_options.get("monitor_plain_text", True):
                continue
            if segment_type == "image" and not conf_options.get("monitor_images", True):
                continue
            if segment_type == "record" and not conf_options.get("monitor_audio", True):
                continue
            if segment_type == "video" and not conf_options.get("monitor_video", True):
                continue
            if segment_type == "file" and not conf_options.get("monitor_files", True):
                continue
            if segment_type not in {"text", "image", "record", "video", "file"} and not conf_options.get("monitor_other_segments", True):
                continue
            result.append(segment)
        return result

    async def _prepare_cache_segments(self, event: AstrMessageEvent, cache_key: str, segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        cached_segments: List[Dict[str, Any]] = []
        for index, segment in enumerate(segments):
            prepared = await self._prepare_segment(event, cache_key, index, segment)
            cached_segments.extend(prepared)
        return cached_segments

    async def _prepare_segment(self, event: AstrMessageEvent, cache_key: str, index: int, segment: Dict[str, Any]) -> List[Dict[str, Any]]:
        segment_type = segment.get("type", "")
        if segment_type in MEDIA_ACTIONS:
            return [await self._prepare_media_segment(event, cache_key, index, segment)]
        if segment_type in DIRECT_SEGMENT_TYPES:
            return [segment]
        if segment_type in SUPPORTED_SUMMARY_TYPES or segment_type:
            return [self._summary_segment(segment_type or "unknown", json.dumps(segment, ensure_ascii=False))]
        return []

    async def _prepare_media_segment(self, event: AstrMessageEvent, cache_key: str, index: int, segment: Dict[str, Any]) -> Dict[str, Any]:
        prepared = copy.deepcopy(segment)
        data = prepared.setdefault("data", {})
        segment_type = prepared.get("type", "")
        file_ref = data.get("file") or data.get("file_id") or data.get("url") or data.get("path") or data.get("file_unique")
        local_path = self._existing_local_path(data)

        if isinstance(file_ref, str) and file_ref.startswith(("http://", "https://")):
            return prepared
        if not local_path and isinstance(event, AiocqhttpMessageEvent) and file_ref:
            local_path = await self._cache_file_from_api(event, cache_key, index, segment_type, str(file_ref))
        if local_path:
            data["local_path"] = local_path
            if segment_type == "video" and self._is_large_file(local_path, VIDEO_SIZE_LIMIT):
                return self._summary_segment("video", f"视频文件超过 100MB，已跳过直接重发: {os.path.basename(local_path)}")
            return prepared
        if segment_type == "record":
            return self._summary_segment(
                "record",
                f"语音文件获取失败，NapCat 未返回可发送的本地文件。原始引用: {file_ref}",
            )
        return prepared

    def _existing_local_path(self, data: Dict[str, Any]) -> Optional[str]:
        for key in ("local_path", "path", "file"):
            value = data.get(key)
            if isinstance(value, str):
                path = value.removeprefix("file:///").removeprefix("file://")
                if os.path.exists(path):
                    return os.path.abspath(path)
        return None

    async def _cache_file_from_api(self, event: AstrMessageEvent, cache_key: str, index: int, segment_type: str, file_ref: str) -> Optional[str]:
        actions = MEDIA_ACTIONS.get(segment_type, ())
        for action in actions:
            for params in self._media_api_params(segment_type, file_ref):
                try:
                    api_response = await event.bot.api.call_action(action, **params)
                    source_path = self._extract_source_path(api_response)
                    if not source_path:
                        continue
                    if source_path.startswith("http://") or source_path.startswith("https://"):
                        return None
                    if not os.path.exists(source_path):
                        logger.warning(
                            f"RecallGuard media path not found: action={action}, file_ref={file_ref}, path={source_path}, cache_key={cache_key}"
                        )
                        continue
                    _, file_ext = os.path.splitext(source_path)
                    dest_path = os.path.join(self.cache_dir, self._safe_cache_name(cache_key, f"_{index}{file_ext or '.cache'}"))
                    os.makedirs(self.cache_dir, exist_ok=True)
                    shutil.copy2(source_path, dest_path)
                    logger.info(f"RecallGuard cached media: action={action}, cache_key={cache_key}, path={dest_path}")
                    return dest_path
                except ActionFailed as e:
                        logger.warning(f"RecallGuard media API failed: action={action}, params={params}, error={e}, cache_key={cache_key}")
                except Exception as e:
                    logger.error(f"RecallGuard media cache error: action={action}, params={params}, error={e}", exc_info=True)
        return None

    def _media_api_params(self, segment_type: str, file_ref: str) -> List[Dict[str, Any]]:
        if segment_type == "record":
            return [
                {"file": file_ref, "out_format": "mp3"},
                {"file_id": file_ref, "out_format": "mp3"},
                {"file": file_ref},
                {"file_id": file_ref},
            ]
        return [{"file": file_ref}, {"file_id": file_ref}]

    def _extract_source_path(self, api_response: Any) -> Optional[str]:
        if isinstance(api_response, dict):
            for key in ("file", "path", "url"):
                value = api_response.get(key)
                if value:
                    return str(value)
        return None

    def _pop_cached_info(self, cache_keys: List[str]) -> Optional[Dict[str, Any]]:
        for cache_key in cache_keys:
            cached_info = MESSAGE_CACHE.pop(cache_key, None)
            if cached_info:
                return cached_info
        return None

    async def _wait_and_pop_cached_info(self, cache_keys: List[str], message_id: str) -> Optional[Dict[str, Any]]:
        wait_logged = False
        for _ in range(60):
            cached_info = None
            for cache_key in cache_keys:
                cached_info = MESSAGE_CACHE.get(cache_key)
                if cached_info:
                    break
            if cached_info and not cached_info.get("preparing"):
                return self._pop_cached_info(cache_keys)
            if cached_info and not wait_logged:
                logger.info(
                    f"RecallGuard waiting for media cache: message_id={message_id}, "
                    f"cache_key={cached_info.get('cache_key')}, segments={cached_info.get('message_type')}"
                )
                wait_logged = True
            await asyncio.sleep(0.25)
        cached_info = self._pop_cached_info(cache_keys)
        if cached_info and cached_info.get("preparing"):
            cached_info["preparing"] = False
            cached_info["segments"] = [
                self._summary_segment(
                    cached_info.get("message_type", "media"),
                    "媒体文件仍在缓存或已无法从 NapCat 获取，已跳过原始媒体重发。",
                )
            ]
            cached_info["message_type"] = "summary"
        return cached_info

    async def _forward_recalled_content(self, cached_info: Dict[str, Any], bot_client: Any, bot_self_id: str):
        conf_fwd = self.config.get("forwarding_options", {})
        forward_format = conf_fwd.get("forwarding_format", "sequential")
        target_sessions = conf_fwd.get("target_sessions", [])
        if not target_sessions:
            logger.warning(f"RecallGuard has no forwarding targets: cache_key={cached_info.get('cache_key')}")
            self._remove_cached_files(cached_info)
            return

        try:
            if forward_format == "merged":
                await self._send_as_merged(cached_info, bot_client, bot_self_id, target_sessions)
            else:
                await self._send_as_sequential(cached_info, bot_client, target_sessions)
        finally:
            self._remove_cached_files(cached_info)

    def _format_prompt_text(self, cached_info: Dict[str, Any]) -> str:
        conf_fwd = self.config.get("forwarding_options", {})
        template = conf_fwd.get("forward_message_text", "用户 {user_name}({user_id}) 撤回了一条消息：")
        group_name = cached_info.get("group_name")
        if not group_name:
            group_id = cached_info.get("group_id")
            group_name = f"群聊 {group_id}" if group_id else "私聊/未知群聊"
        try:
            return template.format(
                user_name=cached_info.get("sender_name", ""),
                user_id=cached_info.get("sender_id", ""),
                group_name=group_name,
                group_id=cached_info.get("group_id", ""),
            )
        except Exception as e:
            logger.warning(f"RecallGuard prompt template failed: {e}")
            return f"用户 {cached_info.get('sender_name', '')}({cached_info.get('sender_id', '')}) 撤回了一条消息："

    async def _send_as_sequential(self, cached_info: Dict[str, Any], bot_client: Any, target_sessions: List[str]):
        if self._has_segment_type(cached_info, {"record"}):
            await self._send_native_normal(cached_info, bot_client, target_sessions, "record segment requires native normal send")
            return

        prompt_message = MessageChain([CompPlain(text=self._format_prompt_text(cached_info))])
        content_message = self._build_message_chain(cached_info)
        native_segments = [cqhttp_forwarder.text_to_segment(self._format_prompt_text(cached_info))]
        native_segments.extend(self._build_native_segments(cached_info))

        for session_id in target_sessions:
            astr_ok = False
            try:
                await self.context.send_message(session_id, prompt_message)
                if content_message:
                    await self.context.send_message(session_id, content_message)
                astr_ok = True
                logger.info(f"RecallGuard sent sequential message by AstrBot: target={session_id}, cache_key={cached_info.get('cache_key')}")
            except Exception as e:
                logger.error(f"RecallGuard AstrBot sequential send failed: target={session_id}, cache_key={cached_info.get('cache_key')}, error={e}", exc_info=True)

            if not astr_ok:
                ok = await cqhttp_forwarder.send_message_by_api(bot_client, session_id, native_segments)
                if not ok:
                    logger.error(f"RecallGuard native sequential fallback failed: target={session_id}, cache_key={cached_info.get('cache_key')}")

    async def _send_as_merged(self, cached_info: Dict[str, Any], bot_client: Any, bot_self_id: str, target_sessions: List[str]):
        if self._has_segment_type(cached_info, {"record"}):
            await self._send_native_normal(cached_info, bot_client, target_sessions, "record segment is not reliable in merged forward")
            return

        prompt_node = cqhttp_forwarder.create_forward_node(
            bot_self_id,
            "RecallGuard",
            [cqhttp_forwarder.text_to_segment(self._format_prompt_text(cached_info))],
        )
        content_node = cqhttp_forwarder.create_forward_node(
            cached_info.get("sender_id", ""),
            cached_info.get("sender_name", ""),
            self._build_native_segments(cached_info),
        )
        nodes_payload = [prompt_node, content_node]
        for session_id in target_sessions:
            ok = await cqhttp_forwarder.send_forward_message_by_api(bot_client, session_id, nodes_payload)
            if not ok:
                logger.warning(f"RecallGuard merged send failed, fallback to native normal message: target={session_id}, cache_key={cached_info.get('cache_key')}")
                segments = [cqhttp_forwarder.text_to_segment(self._format_prompt_text(cached_info))]
                segments.extend(self._build_native_segments(cached_info))
                if not await cqhttp_forwarder.send_message_by_api(bot_client, session_id, segments):
                    logger.error(f"RecallGuard merged fallback failed: target={session_id}, cache_key={cached_info.get('cache_key')}")

    async def _send_native_normal(self, cached_info: Dict[str, Any], bot_client: Any, target_sessions: List[str], reason: str):
        content_segments = self._build_native_segments(cached_info)
        for session_id in target_sessions:
            logger.info(
                f"RecallGuard sending native normal message: target={session_id}, "
                f"cache_key={cached_info.get('cache_key')}, reason={reason}"
            )
            prompt_ok = await cqhttp_forwarder.send_message_by_api(
                bot_client,
                session_id,
                [cqhttp_forwarder.text_to_segment(self._format_prompt_text(cached_info))],
            )
            content_ok = True
            for segment in content_segments:
                content_ok = await cqhttp_forwarder.send_message_by_api(bot_client, session_id, [segment]) and content_ok
            if not prompt_ok or not content_ok:
                logger.error(f"RecallGuard native normal send failed: target={session_id}, cache_key={cached_info.get('cache_key')}")

    def _has_segment_type(self, cached_info: Dict[str, Any], segment_types: set[str]) -> bool:
        return any(segment.get("type") in segment_types for segment in cached_info.get("segments", []))

    def _build_message_chain(self, cached_info: Dict[str, Any]) -> Optional[MessageChain]:
        components: List[Any] = []
        for segment in cached_info.get("segments", []):
            component = self._segment_to_component(segment)
            if component:
                components.append(component)
        return MessageChain(components) if components else None

    def _segment_to_component(self, segment: Dict[str, Any]) -> Optional[Any]:
        segment_type = segment.get("type", "")
        data = segment.get("data", {})
        if segment_type == "text":
            return CompPlain(text=str(data.get("text", "")))
        if segment_type == "image":
            return self._media_component(CompImage, data)
        if segment_type == "record":
            return self._media_component(CompRecord, data)
        if segment_type == "video":
            return self._media_component(CompVideo, data)
        if segment_type == "file":
            return CompFile(name=str(data.get("name") or data.get("file") or "recall-file"), file=str(data.get("local_path") or data.get("file") or ""), url=str(data.get("url") or ""))
        if segment_type in {"face", "at", "reply"}:
            return CompPlain(text=self._segment_summary(segment))
        return CompPlain(text=self._segment_summary(segment))

    def _media_component(self, component_cls: Any, data: Dict[str, Any]) -> Optional[Any]:
        local_path = data.get("local_path")
        if local_path and os.path.exists(local_path):
            return component_cls.fromFileSystem(local_path)
        url = data.get("url") or data.get("file")
        if isinstance(url, str) and url.startswith(("http://", "https://")) and hasattr(component_cls, "fromURL"):
            return component_cls.fromURL(url)
        file_value = data.get("file")
        if file_value:
            return component_cls(file=str(file_value))
        return None

    def _build_native_segments(self, cached_info: Dict[str, Any]) -> List[Dict[str, Any]]:
        segments: List[Dict[str, Any]] = []
        for segment in cached_info.get("segments", []):
            native = self._segment_to_native(segment)
            if native:
                segments.append(native)
        return segments or [self._summary_segment("empty", "撤回消息内容为空或无法重发")]

    def _segment_to_native(self, segment: Dict[str, Any]) -> Dict[str, Any]:
        segment_type = segment.get("type", "")
        data = copy.deepcopy(segment.get("data", {}))
        local_path = data.get("local_path")
        if local_path and os.path.exists(local_path):
            if segment_type == "image":
                return cqhttp_forwarder.local_image_to_segment(local_path)
            if segment_type == "record":
                return cqhttp_forwarder.local_audio_to_segment(local_path)
            if segment_type == "video":
                return cqhttp_forwarder.local_video_to_segment(local_path)
            data["file"] = f"file:///{os.path.abspath(local_path).replace(os.sep, '/')}"
        if segment_type in DIRECT_SEGMENT_TYPES:
            data.pop("local_path", None)
            return {"type": segment_type, "data": data}
        return self._summary_segment(segment_type or "unknown", json.dumps(segment, ensure_ascii=False))

    def _summary_segment(self, segment_type: str, detail: str) -> Dict[str, Any]:
        return cqhttp_forwarder.text_to_segment(f"[撤回消息段: {segment_type}]\n{detail}")

    def _segment_summary(self, segment: Dict[str, Any]) -> str:
        return f"[撤回消息段: {segment.get('type', 'unknown')}]\n{json.dumps(segment, ensure_ascii=False)}"

    def _describe_segment_types(self, segments: List[Dict[str, Any]]) -> str:
        return ",".join(segment.get("type", "unknown") for segment in segments)

    def _remove_cached_files(self, cached_info: Dict[str, Any]):
        for segment in cached_info.get("segments", []):
            local_path = segment.get("data", {}).get("local_path")
            if local_path and os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except Exception as e:
                    logger.error(f"RecallGuard failed to delete cache file: path={local_path}, error={e}")

    async def _periodic_cleanup(self):
        conf_cleanup = self.config.get("cleanup_options", {})
        interval = conf_cleanup.get("cleanup_interval_seconds", 600)
        while self.running:
            await asyncio.sleep(interval)
            try:
                self._cleanup_expired(conf_cleanup.get("cache_lifetime_seconds", 86400))
                self._cleanup_cache_dir(conf_cleanup.get("max_cache_size_mb", 1024))
            except Exception as e:
                logger.error(f"RecallGuard cleanup task failed: {e}", exc_info=True)

    def _cleanup_expired(self, lifetime: int):
        expiration_time = time.time() - lifetime
        keys_to_delete = [cache_key for cache_key, data in MESSAGE_CACHE.items() if data.get("timestamp", 0) < expiration_time]
        for cache_key in keys_to_delete:
            cached_info = MESSAGE_CACHE.pop(cache_key, None)
            if cached_info:
                self._remove_cached_files(cached_info)
        if keys_to_delete:
            logger.info(f"RecallGuard expired cleanup removed {len(keys_to_delete)} records.")

    def _cleanup_cache_dir(self, max_size_mb: int):
        if max_size_mb <= 0 or not os.path.isdir(self.cache_dir):
            return
        max_size_bytes = max_size_mb * 1024 * 1024
        files = [
            os.path.join(self.cache_dir, f)
            for f in os.listdir(self.cache_dir)
            if os.path.isfile(os.path.join(self.cache_dir, f))
        ]
        total_size = sum(os.path.getsize(path) for path in files)
        if total_size <= max_size_bytes:
            return
        files.sort(key=lambda path: os.path.getmtime(path))
        removed = 0
        while total_size > max_size_bytes and files:
            file_to_delete = files.pop(0)
            try:
                file_size = os.path.getsize(file_to_delete)
                os.remove(file_to_delete)
                total_size -= file_size
                removed += 1
                self._drop_cache_entries_by_file(file_to_delete)
            except Exception as e:
                logger.error(f"RecallGuard size cleanup failed: path={file_to_delete}, error={e}")
        logger.info(f"RecallGuard size cleanup removed {removed} files.")

    def _drop_cache_entries_by_file(self, file_path: str):
        for cache_key, cached_info in list(MESSAGE_CACHE.items()):
            for segment in cached_info.get("segments", []):
                if segment.get("data", {}).get("local_path") == file_path:
                    MESSAGE_CACHE.pop(cache_key, None)
                    break

    def _is_large_file(self, file_path: str, limit: int) -> bool:
        try:
            return os.path.getsize(file_path) > limit
        except OSError:
            return False

    def _safe_copy(self, value: Any) -> Any:
        try:
            return copy.deepcopy(value)
        except Exception:
            return str(value)

    def _safe_component_repr(self, component: Any) -> str:
        try:
            return repr(component)
        except Exception:
            return f"<{type(component).__name__}>"
