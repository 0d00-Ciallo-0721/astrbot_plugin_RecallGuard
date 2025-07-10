import asyncio
import os
import shutil
import time
from typing import Dict

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image as CompImage
from astrbot.api.message_components import Plain as CompPlain
from astrbot.api.star import Context, Star, register

IMAGE_CACHE: Dict[str, dict] = {}


@register(
    name="RecallGuard",
    author="和泉智宏",
    desc="监听指定用户撤回图片并将其转发到指定群聊。",
    version="1.0", # 版本号更新
    repo="https://github.com/0d00-Ciallo-0721/astrbot_plugin_RecallGuard"
)
class AntiRecallPlugin(Star):
    def __init__(self, context: Context, config=None):
        """
        插件初始化
        """
        super().__init__(context)
        self.config = config
        self.running = True

        # 修改后的路径：创建用于存储缓存图片的目录，路径为与main.py同级的images文件夹
        self.cache_dir = os.path.join(os.path.dirname(__file__), "images")
        os.makedirs(self.cache_dir, exist_ok=True)

        # 创建并启动后台清理任务
        self.cleanup_task = asyncio.create_task(self._periodic_cleanup())

        logger.info("图片防撤回插件加载成功！(v1.0.4)")
        logger.info(f"监听用户: {self.config.get('monitored_users', [])}")
        logger.info(f"转发目标: {self.config.get('target_sessions', [])}")

    async def terminate(self):
        """
        插件卸载或停用时调用的清理函数。
        """
        self.running = False
        if self.cleanup_task:
            self.cleanup_task.cancel()
        logger.info("图片防撤回插件已停用。")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_image_message(self, event: AstrMessageEvent):
        """
        监听所有消息，通过调用API来获取并缓存目标用户发送的图片。
        """
        sender_id = event.get_sender_id()
        monitored_users = self.config.get("monitored_users", [])

        # 检查发送者是否在监听列表中
        if not sender_id or str(sender_id) not in monitored_users:
            return

        # 仅处理 aiocqhttp 平台
        if event.get_platform_name() != "aiocqhttp":
            return
            
        # 遍历消息链，查找图片组件
        for component in event.message_obj.message:
            if isinstance(component, CompImage):
                # 图片的内部标识符
                image_identifier = component.file
                message_id = event.message_obj.message_id

                try:
                    # 动态导入 aiocqhttp 的事件类以获取 client
                    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
                    if not isinstance(event, AiocqhttpMessageEvent):
                        logger.warning("事件类型不是 AiocqhttpMessageEvent，无法获取 client。")
                        continue

                    client = event.bot
                    # 调用 onebot 的 'get_image' API
                    api_response = await client.api.call_action('get_image', file=image_identifier)

                    if api_response and api_response.get('file'):
                        # API返回图片在服务器上的绝对路径
                        source_image_path = api_response['file']
                        
                        # 复制图片到插件缓存目录
                        file_path = await self._copy_and_cache_image(source_image_path, message_id)
                        
                        if file_path:
                            IMAGE_CACHE[str(message_id)] = {
                                "path": file_path,
                                "sender_id": sender_id,
                                "sender_name": event.get_sender_name(),
                                "timestamp": time.time()
                            }
                            logger.info(f"已通过API缓存用户 {sender_id} 的图片，消息ID: {message_id}")
                    else:
                        logger.error(f"调用 get_image API 失败或未返回文件路径: {api_response}")

                except Exception as e:
                    logger.error(f"处理图片或调用API时发生严重错误: {e}", exc_info=True)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_recall_notice(self, event: AstrMessageEvent):
        """
        使用高优先级监听所有事件，用于捕获撤回通知。
        """
        raw_event = event.message_obj.raw_message
        if not isinstance(raw_event, dict):
            return

        post_type = raw_event.get("post_type")
        notice_type = raw_event.get("notice_type")

        if post_type == "notice" and notice_type in ["group_recall", "friend_recall"]:
            recalled_message_id = str(raw_event.get("message_id"))
            operator_id = str(raw_event.get("operator_id") or raw_event.get("user_id"))

            if recalled_message_id in IMAGE_CACHE:
                # 先从缓存中获取信息，确认是目标用户后再删除，防止误删
                cached_info = IMAGE_CACHE[recalled_message_id]
                
                if str(operator_id) in self.config.get("monitored_users", []):
                    logger.info(f"检测到用户 {operator_id} 撤回了图片消息: {recalled_message_id}")
                    # 取出并删除
                    del IMAGE_CACHE[recalled_message_id]
                    await self._forward_recalled_image(cached_info)


    async def _copy_and_cache_image(self, source_path: str, message_id: str) -> str or None:
        """
        从源路径复制图片到插件缓存目录。
        """
        try:
            # 从源路径推断文件后缀名
            _, file_ext = os.path.splitext(source_path)
            if not file_ext: file_ext = ".png" # 如果没有后缀，默认.png
            
            file_name = f"{message_id}{file_ext}"
            dest_path = os.path.join(self.cache_dir, file_name)

            shutil.copy2(source_path, dest_path)
            return dest_path
        except Exception as e:
            logger.error(f"复制图片文件时出错: 从 {source_path} 到 {dest_path}。错误: {e}")
            return None

    async def _forward_recalled_image(self, cached_info: dict):
        """
        将撤回的图片转发到所有目标会话。
        """
        target_sessions = self.config.get("target_sessions", [])
        if not target_sessions:
            logger.warning("没有配置转发目标，无法转发撤回的图片。")
            # 即使不转发，也要删除本地缓存文件
            if os.path.exists(cached_info["path"]):
                try:
                    os.remove(cached_info["path"])
                except Exception as e:
                     logger.error(f"删除缓存图片 {cached_info['path']} 失败: {e}")
            return

        sender_id = cached_info["sender_id"]
        sender_name = cached_info["sender_name"]
        image_path = cached_info["path"]
        
        text_template = self.config.get("forward_message_text", "检测到用户 {user_name}({user_id}) 撤回了一张图片：")
        text_content = text_template.format(user_id=sender_id, user_name=sender_name)

        # 1. 先创建一个消息组件的列表
        chain_list = [
            CompPlain(text=text_content),
            CompImage.fromFileSystem(image_path)
        ]
        # 2. 将列表传入 MessageChain() 来构造一个符合API要求的对象
        message_to_send = MessageChain(chain_list)

        for session_id in target_sessions:
            try:
                # 使用构造好的 MessageChain 对象进行发送
                success = await self.context.send_message(session_id, message_to_send)
                if success:
                    logger.info(f"已将撤回的图片转发到 {session_id}")
                else:
                    logger.error(f"发送到 {session_id} 失败，可能是不支持的平台或会话ID无效。")
            except Exception as e:
                logger.error(f"转发到 {session_id} 时发生错误: {e}")

        # 删除已转发的本地图片文件，释放空间
        try:
            if os.path.exists(image_path):
                os.remove(image_path)
        except Exception as e:
            logger.error(f"删除缓存图片 {image_path} 失败: {e}")

    async def _periodic_cleanup(self):
        """
        后台任务，每10分钟清理一次全部的图片缓存。
        """
        while self.running:
            # 修改等待时间为10分钟 (600秒)
            await asyncio.sleep(600)
            
            try:
                # 获取当前所有缓存项的键
                all_cached_keys = list(IMAGE_CACHE.keys())

                if not all_cached_keys:
                    continue

                logger.info(f"开始清理全部 {len(all_cached_keys)} 个图片缓存...")
                for msg_id in all_cached_keys:
                    # 从字典中移除记录，并删除对应的文件
                    cached_info = IMAGE_CACHE.pop(msg_id, None)
                    if cached_info and os.path.exists(cached_info["path"]):
                        try:
                            os.remove(cached_info["path"])
                        except Exception as e:
                            logger.error(f"清理缓存图片文件失败: {cached_info['path']}, {e}")
                logger.info("所有图片缓存清理完毕。")
            except Exception as e:
                logger.error(f"后台清理任务发生错误: {e}")
