import asyncio
import os
import shutil
import time
from typing import Dict, Optional, Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.event import filter
from astrbot.api.message_components import Plain as CompPlain, Image as CompImage, Record as CompRecord
from astrbot.api.star import Context, Star, register
# 导入aiocqhttp的异常类和事件类
from aiocqhttp.exceptions import ActionFailed
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

# 导入外部模块
from . import cqhttp_forwarder

# 缓存池不再包含 'forward'
MESSAGE_CACHE: Dict[str, Dict[str, dict]] = {
    "text": {}, "image": {}, "audio": {}
}


@register(
    "RecallGuard",
    "和泉智宏",
    "功能更全的防撤回插件，支持自定义转发格式、黑名单、缓存体积管理和群聊信息提示。",
    "2.0", # 版本号更新
    "https://github.com/0d00-Ciallo-0721/astrbot_plugin_RecallGuard"
)
class RecallGuardPlugin(Star):
    def __init__(self, context: Context, config=None):
        """插件初始化"""
        super().__init__(context)
        self.config = config
        self.running = True
        self.cache_dir = "/shared/recall_guard_cache"
        os.makedirs(self.cache_dir, exist_ok=True)
        self.cleanup_task = asyncio.create_task(self._periodic_cleanup())
        self._update_monitored_groups_set()
        logger.info("防撤回插件 v8.2 (群名修复版) 加载成功！")


    def _update_monitored_groups_set(self):
        conf_group_list = self.config.get('group_monitoring', {}).get('monitored_groups', [])
        self.monitored_groups_set = {g.split(':')[-1] for g in conf_group_list if 'group' in g.lower()}


    async def terminate(self):
        self.running = False
        if self.cleanup_task:
            self.cleanup_task.cancel()
        logger.info("防撤回插件 v8.2 (群名修复版) 已停用。")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息，根据优先级和黑名单判断是否缓存。"""
        self._update_monitored_groups_set()
        sender_id = str(event.get_sender_id())
        group_id = str(event.get_group_id())
        
        conf_user = self.config.get('user_monitoring', {})
        blacklist_users = {str(u) for u in conf_user.get('blacklist_users', [])}
        monitored_users = {str(u) for u in conf_user.get('monitored_users', [])}

        if sender_id in blacklist_users:
            return

        should_monitor = False
        if sender_id in monitored_users:
            should_monitor = True
        elif self.config.get('group_monitoring', {}).get('enable_group_monitoring'):
            if group_id and group_id in self.monitored_groups_set:
                 should_monitor = True
        
        if not should_monitor:
            return
        
        # 新增逻辑：在缓存前，先获取群聊名称
        group_name = ""
        if group_id and isinstance(event, AiocqhttpMessageEvent):
            try:
                # 直接调用 get_group_info API
                group_info = await event.bot.api.call_action('get_group_info', group_id=int(group_id))
                if group_info and isinstance(group_info, dict):
                    group_name = group_info.get('group_name', '')
            except ActionFailed as e:
                logger.warning(f"获取群聊 {group_id} 名称失败: {e}")
            except Exception as e:
                logger.error(f"获取群聊 {group_id} 名称时发生未知错误: {e}", exc_info=True)

        conf_options = self.config.get('monitoring_options', {})
        message_id = str(event.message_obj.message_id)

        for component in event.message_obj.message:
            try:
                if isinstance(component, CompPlain) and conf_options.get('monitor_plain_text'):
                    self._cache_to_memory(message_id, event, 'text', group_name, content=component.text)
                elif isinstance(component, CompImage) and conf_options.get('monitor_images'):
                    if file_path := await self._cache_file_from_api(event, component.file, 'get_image'):
                        self._cache_to_memory(message_id, event, 'image', group_name, file_path=file_path)
                elif isinstance(component, CompRecord) and conf_options.get('monitor_audio'):
                    if file_path := await self._cache_file_from_api(event, component.file, 'get_record'):
                        self._cache_to_memory(message_id, event, 'audio', group_name, file_path=file_path)
            except Exception as e:
                logger.error(f"缓存消息组件时发生错误: {e}", exc_info=True)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_recall_notice(self, event: AstrMessageEvent):
        raw_event = event.message_obj.raw_message
        if not isinstance(raw_event, dict): return
        if raw_event.get("post_type") == "notice" and raw_event.get("notice_type") in ["group_recall", "friend_recall"]:
            if not isinstance(event, AiocqhttpMessageEvent): return
            recalled_message_id = str(raw_event.get("message_id"))
            
            cached_info = None
            for cache_pool in MESSAGE_CACHE.values():
                if recalled_message_id in cache_pool:
                    cached_info = cache_pool.pop(recalled_message_id)
                    break
            
            if cached_info:
                logger.info(f"检测到受监控的 {cached_info['message_type']} 消息被撤回: {recalled_message_id}")
                bot_client = event.bot
                bot_self_id = event.get_self_id()
                await self._forward_recalled_content(cached_info, bot_client, bot_self_id)
            
    def _cache_to_memory(self, message_id: str, event: AstrMessageEvent, msg_type: str, group_name: str, content: Optional[Any] = None, file_path: Optional[str] = None):
        if (cache_pool := MESSAGE_CACHE.get(msg_type)) is None: return
        
        # 直接使用传入的 group_name
        cache_pool[message_id] = {
            "sender_id": str(event.get_sender_id()), "sender_name": event.get_sender_name(),
            "group_id": str(event.get_group_id() or ""),
            "group_name": group_name, # 使用从API获取的名称
            "timestamp": time.time(), "message_type": msg_type,
            "content": content, "file_path": file_path
        }
        logger.info(f"已缓存 {msg_type} 消息到内存, 消息ID: {message_id}")

    async def _cache_file_from_api(self, event: AstrMessageEvent, file_id: str, api_action: str) -> Optional[str]:
        if not isinstance(event, AiocqhttpMessageEvent): return None
        try:
            client = event.bot
            api_response = await client.api.call_action(api_action, file=file_id)
            if api_response and (source_path := api_response.get('file')):
                _, file_ext = os.path.splitext(source_path)
                dest_path = os.path.join(self.cache_dir, f"{event.message_obj.message_id}{file_ext or '.cache'}")
                shutil.copy2(source_path, dest_path)
                return dest_path
            logger.error(f"调用 {api_action} API 失败或未返回文件路径: {api_response}")
        except ActionFailed as e:
            if "timeout" in str(getattr(e, 'message', '')).lower():
                logger.warning(f"缓存文件失败：协议端下载超时。将忽略此次缓存。消息ID: {event.message_obj.message_id}")
            else:
                logger.error(f"调用协议端API时发生未知的ActionFailed错误: {e}")
            return None
        except Exception as e:
            logger.error(f"缓存文件时发生严重错误: {e}", exc_info=True)
        return None

    async def _forward_recalled_content(self, cached_info: dict, bot_client: Any, bot_self_id: str):
        conf_fwd = self.config.get('forwarding_options', {})
        forward_format = conf_fwd.get("forwarding_format", "sequential")
        target_sessions = conf_fwd.get("target_sessions", [])
        file_path = cached_info.get("file_path")
        if not target_sessions:
            if file_path and os.path.exists(file_path):
                try: os.remove(file_path)
                except Exception as e: logger.error(f"删除缓存文件 {file_path} 失败: {e}")
            return
        if forward_format == "merged":
            await self._send_as_merged(cached_info, bot_client, bot_self_id)
        else:
            await self._send_as_sequential(cached_info)

    def _format_prompt_text(self, cached_info: dict) -> str:
        """统一格式化提示文本"""
        conf_fwd = self.config.get('forwarding_options', {})
        template = conf_fwd.get("forward_message_text", "用户 {user_name}({user_id}) 撤回了一条消息：")
        return template.format(
            user_name=cached_info.get("sender_name", ""),
            user_id=cached_info.get("sender_id", ""),
            group_name=cached_info.get("group_name", "私聊/未知群聊"), # 提供一个默认值
            group_id=cached_info.get("group_id", "")
        )

    async def _send_as_merged(self, cached_info: dict, bot_client: Any, bot_self_id: str):

        conf_fwd = self.config.get('forwarding_options', {})
        target_sessions = conf_fwd.get("target_sessions", [])
        file_path = cached_info.get("file_path")
        sender_id, sender_name = cached_info["sender_id"], cached_info["sender_name"]
        msg_type, content = cached_info.get("message_type"), cached_info.get("content")
        prompt_text = self._format_prompt_text(cached_info)
        prompt_segment = [cqhttp_forwarder.text_to_segment(prompt_text)]
        prompt_node = cqhttp_forwarder.create_forward_node(bot_self_id, "撤回守卫", prompt_segment)
        recalled_segment = []
        if msg_type == 'text' and content:
            recalled_segment.append(cqhttp_forwarder.text_to_segment(content))
        elif msg_type == 'image' and file_path and os.path.exists(file_path):
            recalled_segment.append(cqhttp_forwarder.local_image_to_segment(file_path))
        elif msg_type == 'audio' and file_path and os.path.exists(file_path):
            recalled_segment.append(cqhttp_forwarder.local_audio_to_segment(file_path))
        if not recalled_segment:
            logger.warning(f"无法构造转发内容: 消息类型 {msg_type} 或文件不存在 {file_path}")
            return
        recalled_node = cqhttp_forwarder.create_forward_node(sender_id, sender_name, recalled_segment)
        nodes_payload = [prompt_node, recalled_node]
        try:
            for session_id in target_sessions:
                if 'group' not in session_id.lower(): continue
                group_id = int(session_id.split(':')[-1])
                await cqhttp_forwarder.send_group_forward_message_by_api(
                    bot_client=bot_client, group_id=group_id, nodes=nodes_payload
                )
        finally:
            if file_path and os.path.exists(file_path):
                try: os.remove(file_path)
                except Exception as e: logger.error(f"删除缓存文件 {file_path} 失败: {e}")
    
    async def _send_as_sequential(self, cached_info: dict):

        conf_fwd = self.config.get('forwarding_options', {})
        target_sessions = conf_fwd.get("target_sessions", [])
        file_path = cached_info.get("file_path")
        msg_type, content = cached_info.get("message_type"), cached_info.get("content")
        prompt_text = self._format_prompt_text(cached_info)
        prompt_message = MessageChain([CompPlain(text=prompt_text)])
        content_message = None
        if msg_type == 'text' and content:
            content_message = MessageChain([CompPlain(text=content)])
        elif msg_type == 'image' and file_path and os.path.exists(file_path):
            content_message = MessageChain([CompImage.fromFileSystem(file_path)])
        elif msg_type == 'audio' and file_path and os.path.exists(file_path):
            content_message = MessageChain([CompRecord(file=file_path)])
        if not content_message:
            logger.warning(f"无法构造转发内容: 消息类型 {msg_type} 或文件不存在 {file_path}")
            return
        try:
            for session_id in target_sessions:
                try:
                    await self.context.send_message(session_id, prompt_message)
                    await self.context.send_message(session_id, content_message)
                except Exception as e:
                    logger.error(f"逐条转发到 {session_id} 时发生错误: {e}")
        finally:
            if file_path and os.path.exists(file_path):
                try: os.remove(file_path)
                except Exception as e: logger.error(f"删除缓存文件 {file_path} 失败: {e}")

    async def _periodic_cleanup(self):

        conf_cleanup = self.config.get('cleanup_options', {})
        interval = conf_cleanup.get("cleanup_interval_seconds", 600)
        while self.running:
            await asyncio.sleep(interval)
            try:
                lifetime = conf_cleanup.get("cache_lifetime_seconds", 86400)
                expiration_time = time.time() - lifetime
                time_cleaned_count = 0
                for cache_pool in MESSAGE_CACHE.values():
                    keys_to_delete = [msg_id for msg_id, data in cache_pool.items() if data.get("timestamp", 0) < expiration_time]
                    if not keys_to_delete: continue
                    time_cleaned_count += len(keys_to_delete)
                    for msg_id in keys_to_delete:
                        if (cached_info := cache_pool.pop(msg_id, None)) and (file_path := cached_info.get("file_path")):
                            if os.path.exists(file_path):
                                try: os.remove(file_path)
                                except Exception as e: logger.error(f"按时间清理缓存文件失败: {file_path}, {e}")
                if time_cleaned_count > 0:
                    logger.info(f"过期缓存清理完毕，共清理 {time_cleaned_count} 条记录。")
                max_size_mb = conf_cleanup.get("max_cache_size_mb", 1024)
                if max_size_mb <= 0: continue
                max_size_bytes = max_size_mb * 1024 * 1024
                total_size = sum(os.path.getsize(os.path.join(self.cache_dir, f)) for f in os.listdir(self.cache_dir) if os.path.isfile(os.path.join(self.cache_dir, f)))
                if total_size > max_size_bytes:
                    logger.info(f"缓存目录体积 {total_size / 1024 / 1024:.2f} MB 已超过阈值 {max_size_mb} MB，开始清理...")
                    files = [os.path.join(self.cache_dir, f) for f in os.listdir(self.cache_dir) if os.path.isfile(os.path.join(self.cache_dir, f))]
                    files.sort(key=lambda x: os.path.getmtime(x))
                    size_cleaned_count = 0
                    while total_size > max_size_bytes and files:
                        file_to_delete = files.pop(0)
                        try:
                            file_size = os.path.getsize(file_to_delete)
                            os.remove(file_to_delete)
                            total_size -= file_size
                            size_cleaned_count += 1
                            file_name_without_ext = os.path.splitext(os.path.basename(file_to_delete))[0]
                            for cache_pool in MESSAGE_CACHE.values():
                                if file_name_without_ext in cache_pool:
                                    cache_pool.pop(file_name_without_ext, None)
                                    break
                        except Exception as e:
                            logger.error(f"按体积清理缓存文件失败: {file_to_delete}, {e}")
                    logger.info(f"缓存体积清理完毕，共清理 {size_cleaned_count} 个文件。")
            except Exception as e:
                logger.error(f"后台清理任务发生错误: {e}", exc_info=True)
