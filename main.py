import asyncio
import os
import shutil
import time
from typing import Dict, Optional, List, Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import (
    Image as CompImage,
    Plain as CompPlain,
    Record as CompRecord,
)
from astrbot.api.star import Context, Star, register
# 导入aiocqhttp的异常类以进行捕获
from aiocqhttp.exceptions import ActionFailed

# --- 缓存池现在只包含支持的类型 ---
MESSAGE_CACHE: Dict[str, Dict[str, dict]] = {
    "text": {}, "image": {}, "audio": {}
}


@register(
    name="RecallGuard_Final",
    author="和泉智宏 & Gemini",
    desc="监听用户或群聊撤回的消息(文本/图片/语音)并转发。",
    version="4.1-hotfix", # 版本号更新，体现修复
    repo="https://github.com/0d00-Ciallo-0721/astrbot_plugin_RecallGuard"
)
class RecallGuardPlugin(Star):
    def __init__(self, context: Context, config=None):
        """插件初始化"""
        super().__init__(context)
        self.config = config
        self.running = True

        self.cache_dir = os.path.join(os.path.dirname(__file__), "files_cache")
        os.makedirs(self.cache_dir, exist_ok=True)

        self.cleanup_task = asyncio.create_task(self._periodic_cleanup())
        self._update_monitored_groups_set()
        logger.info("防撤回插件 v4.1 (修复版) 加载成功！")


    def _update_monitored_groups_set(self):
        """将配置文件中的群聊列表转换为小写集合以便快速、不区分大小写地查找"""
        conf_group_list = self.config.get('group_monitoring', {}).get('monitored_groups', [])
        self.monitored_groups_set = {g.lower() for g in conf_group_list}


    async def terminate(self):
        """插件卸载或停用时调用的清理函数"""
        self.running = False
        if self.cleanup_task:
            self.cleanup_task.cancel()
        logger.info("防撤回插件 v4.1 (修复版) 已停用。")


    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息，缓存需要监控的内容"""
        self._update_monitored_groups_set()

        sender_id = str(event.get_sender_id())
        
        should_monitor = False
        if sender_id in self.config.get('user_monitoring', {}).get('monitored_users', []):
            should_monitor = True
        elif self.config.get('group_monitoring', {}).get('enable_group_monitoring'):
            if event.unified_msg_origin and event.unified_msg_origin.lower() in self.monitored_groups_set:
                should_monitor = True

        if not should_monitor:
            return

        conf_options = self.config.get('monitoring_options', {})
        message_id = str(event.message_obj.message_id)

        for component in event.message_obj.message:
            try:
                if isinstance(component, CompPlain) and conf_options.get('monitor_plain_text'):
                    self._cache_to_memory(message_id, event, 'text', content=component.text)
                elif isinstance(component, CompImage) and conf_options.get('monitor_images'):
                    if file_path := await self._cache_file_from_api(event, component.file, 'get_image'):
                        self._cache_to_memory(message_id, event, 'image', file_path=file_path)
                elif isinstance(component, CompRecord) and conf_options.get('monitor_audio'):
                    if file_path := await self._cache_file_from_api(event, component.file, 'get_record'):
                        self._cache_to_memory(message_id, event, 'audio', file_path=file_path)
            except Exception as e:
                logger.error(f"缓存消息组件时发生错误: {e}", exc_info=True)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_recall_notice(self, event: AstrMessageEvent):
        """使用高优先级监听所有事件，用于捕获撤回通知"""
        raw_event = event.message_obj.raw_message
        if not isinstance(raw_event, dict): return

        if raw_event.get("post_type") == "notice" and raw_event.get("notice_type") in ["group_recall", "friend_recall"]:
            recalled_message_id = str(raw_event.get("message_id"))
            for cache_pool in MESSAGE_CACHE.values():
                if recalled_message_id in cache_pool:
                    cached_info = cache_pool.pop(recalled_message_id)
                    logger.info(f"检测到受监控的 {cached_info['message_type']} 消息被撤回: {recalled_message_id}")
                    # [BUG修复 2] 在此处获取机器人ID，并传递给转发函数
                    bot_self_id = event.get_self_id()
                    await self._forward_recalled_content(cached_info, bot_self_id)
                    return

    def _cache_to_memory(self, message_id: str, event: AstrMessageEvent, msg_type: str, content: Optional[str] = None, file_path: Optional[str] = None):
        """将消息元数据存入对应类型的内存缓存池"""
        if (cache_pool := MESSAGE_CACHE.get(msg_type)) is None: return
        cache_pool[message_id] = {
            "sender_id": str(event.get_sender_id()), "sender_name": event.get_sender_name(),
            "timestamp": time.time(), "message_type": msg_type,
            "content": content, "file_path": file_path
        }
        logger.info(f"已缓存 {msg_type} 消息到内存, 消息ID: {message_id}")

    async def _cache_file_from_api(self, event: AstrMessageEvent, file_id: str, api_action: str) -> Optional[str]:
        """通过API获取文件并复制到本地缓存"""
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
            if not isinstance(event, AiocqhttpMessageEvent): return None
            client = event.bot
            api_response = await client.api.call_action(api_action, file=file_id)
            if api_response and (source_path := api_response.get('file')):
                _, file_ext = os.path.splitext(source_path)
                dest_path = os.path.join(self.cache_dir, f"{event.message_obj.message_id}{file_ext or '.cache'}")
                shutil.copy2(source_path, dest_path)
                return dest_path
            logger.error(f"调用 {api_action} API 失败或未返回文件路径: {api_response}")
        
        # [BUG修复 1] 捕获ActionFailed异常，优雅处理超时
        except ActionFailed as e:
            if "timeout" in str(e.wording).lower():
                logger.warning(f"缓存文件失败：协议端下载超时。将忽略此次缓存。消息ID: {event.message_obj.message_id}")
            else:
                logger.error(f"调用协议端API时发生未知的ActionFailed错误: {e}")
            return None # 无论哪种ActionFailed，都返回None表示失败

        except Exception as e:
            logger.error(f"缓存文件时发生严重错误: {e}", exc_info=True)
        return None

    # [BUG修复 2] 增加 bot_self_id 参数
    async def _forward_recalled_content(self, cached_info: dict, bot_self_id: str):
        """将撤回的内容以简单消息链的形式发送到目标会话"""
        conf_fwd = self.config.get('forwarding_options', {})
        target_sessions = conf_fwd.get("target_sessions", [])
        file_path = cached_info.get("file_path")

        if not target_sessions:
            if file_path and os.path.exists(file_path):
                try: os.remove(file_path)
                except Exception as e: logger.error(f"删除缓存文件 {file_path} 失败: {e}")
            return

        sender_id, sender_name = cached_info["sender_id"], cached_info["sender_name"]
        msg_type, content = cached_info.get("message_type"), cached_info.get("content")
        
        prompt_text = conf_fwd.get("forward_message_text", "...").format(user_id=sender_id, user_name=sender_name)
        # 注意：由于合并转发的问题，我们依然采用发送两条消息的稳定策略
        # 第一条：提示消息
        prompt_message = MessageChain([CompPlain(text=prompt_text)])
        
        # 第二条：内容消息
        content_message = None
        if msg_type == 'text' and content:
            content_message = MessageChain([CompPlain(text=content)])
        elif msg_type == 'image' and file_path and os.path.exists(file_path):
            content_message = MessageChain([CompImage.fromFileSystem(file_path)])
        elif msg_type == 'audio' and file_path and os.path.exists(file_path):
            content_message = MessageChain([CompRecord(file=file_path)])
        else:
            logger.warning(f"无法构造转发消息: 消息类型 {msg_type} 或文件不存在 {file_path}"); return
        
        try:
            for session_id in target_sessions:
                try:
                    await self.context.send_message(session_id, prompt_message)
                    if content_message:
                        await self.context.send_message(session_id, content_message)
                except Exception as e:
                    logger.error(f"转发到 {session_id} 时发生错误: {e}")
        finally:
            if file_path and os.path.exists(file_path):
                try: os.remove(file_path)
                except Exception as e: logger.error(f"删除缓存文件 {file_path} 失败: {e}")

    async def _periodic_cleanup(self):
        """后台任务，定期清理所有缓存池中过期的内存记录和文件缓存"""
        conf_cleanup = self.config.get('cleanup_options', {})
        interval = conf_cleanup.get("cleanup_interval_seconds", 600)
        lifetime = conf_cleanup.get("cache_lifetime_seconds", 86400)

        while self.running:
            await asyncio.sleep(interval)
            try:
                expiration_time = time.time() - lifetime
                total_cleaned = 0
                for cache_pool in MESSAGE_CACHE.values():
                    keys_to_delete = [msg_id for msg_id, data in cache_pool.items() if data.get("timestamp", 0) < expiration_time]
                    if not keys_to_delete: continue
                    total_cleaned += len(keys_to_delete)
                    for msg_id in keys_to_delete:
                        if (cached_info := cache_pool.pop(msg_id, None)) and (file_path := cached_info.get("file_path")):
                            if os.path.exists(file_path):
                                try: os.remove(file_path)
                                except Exception as e: logger.error(f"清理缓存文件失败: {file_path}, {e}")
                if total_cleaned > 0:
                    logger.info(f"过期内存缓存清理完毕，共清理 {total_cleaned} 条记录。")
            except Exception as e:
                logger.error(f"后台清理任务发生错误: {e}", exc_info=True)
