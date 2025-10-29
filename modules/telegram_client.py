from pyrogram import Client, filters, idle
from pyrogram.types import Message, User, ReplyParameters, MessageEntity, LinkPreviewOptions
from pyrogram.enums import ChatType, MessageEntityType
from core.config import Config
from core.event_bus import EventBus
from core.logger import logger
import os
import asyncio
import re
# --- (修改: 导入 Tuple, Optional, Any) ---
from typing import Tuple, Optional, Any
# --- (修改结束) ---

from asyncio import Queue

class TelegramClient:
    def __init__(self, event_bus: EventBus, config: Config):
        self.config = config
        self.event_bus = event_bus
        self._me: User | None = None
        self._my_id: int | None = None
        self._my_username: str | None = None
        # --- 新增: 引用 RedisClient ---
        self.redis_client = None # 将在 AppContext 中设置
        self.task_channel = self.config.get("communication.task_channel", "assistant_tasks")
        # --- 新增结束 ---

        session_path = "data/my_game_assistant"
        try:
            os.makedirs(os.path.dirname(session_path), exist_ok=True)
            logger.debug(f"确保 session 目录 '{os.path.dirname(session_path)}' 存在。")
        except OSError as e:
             logger.critical(f"无法创建 session 目录 '{os.path.dirname(session_path)}': {e}。程序可能无法保存会话。")

        self.app = Client(
            name=session_path,
            api_id=self.config.get("telegram.api_id"),
            api_hash=self.config.get("telegram.api_hash")
        )

        self.admin_id = self.config.get("telegram.admin_id")
        self.target_chat_id = self.config.get("telegram.target_chat_id", 0)
        self.control_chat_id = self.config.get("telegram.control_chat_id", 0)
        self.game_bot_ids = self.config.get("telegram.game_bot_ids", [])

        self.command_queue = Queue()
        self.queue_task: asyncio.Task | None = None
        self.COMMAND_DELAY_SECONDS = self.config.get("telegram.command_delay", 10.5)
        logger.info(f"【消息队列】游戏指令发送延迟设置为: {self.COMMAND_DELAY_SECONDS} 秒。")

    # --- 新增: 设置 RedisClient 引用 ---
    def set_redis_client(self, redis_client):
        self.redis_client = redis_client
        logger.debug("RedisClient 引用已设置到 TelegramClient。")
    # --- 新增结束 ---

    async def _ensure_me(self):
        if self._me:
            return
        if self.app.is_connected:
            try:
                self._me = await self.app.get_me()
                self._my_id = self._me.id if self._me else None
                self._my_username = self._me.username if self._me else None
                logger.info(f"成功获取自身用户信息: ID={self._my_id}, Username={self._my_username}")
            except Exception as e:
                logger.error(f"运行时获取自身用户信息失败: {e}")
                self._me = None; self._my_id = None; self._my_username = None
        else:
             logger.warning("尝试获取自身用户信息，但 TG 客户端未连接。")

    async def get_my_username(self) -> str | None:
        if self._my_username: return self._my_username
        await self._ensure_me(); return self._my_username

    async def get_my_id(self) -> int | None:
        if self._my_id: return self._my_id
        await self._ensure_me(); return self._my_id

    async def _command_queue_processor(self):
        logger.info("【消息队列】游戏指令队列处理器已启动。")
        while True:
            try:
                command_to_send_raw = await self.command_queue.get()
                original_command = command_to_send_raw
                command_to_send = command_to_send_raw
                reply_to_id = None; reply_params = None

                reply_match = re.search(r" --reply_to (\d+)$", command_to_send_raw)
                if reply_match:
                    try:
                        reply_to_id = int(reply_match.group(1))
                        command_to_send = command_to_send_raw[:reply_match.start()].strip()
                        reply_params = ReplyParameters(message_id=reply_to_id)
                        logger.debug(f"【消息队列】解析到回复标记，将回复 MsgID: {reply_to_id}，实际发送指令: '{command_to_send}'")
                    except ValueError:
                         logger.error(f"【消息队列】解析回复标记 MsgID 失败: '{reply_match.group(1)}'。按普通指令发送。")
                         reply_to_id = None; reply_params = None; command_to_send = command_to_send_raw

                if not self.target_chat_id:
                    logger.error(f"【消息队列】无法发送指令 '{original_command}'：未配置 target_chat_id。")
                    self.command_queue.task_done(); continue
                if not self.app.is_connected:
                     logger.error(f"【消息队列】无法发送指令 '{original_command}'：Telegram 客户端未连接。放回队列重试。")
                     # 简单的重试逻辑，避免无限循环
                     retry_count = getattr(command_to_send_raw, '_retry_count', 0)
                     if retry_count < 3:
                        setattr(command_to_send_raw, '_retry_count', retry_count + 1)
                        await self.command_queue.put(command_to_send_raw)
                        logger.info(f"指令 '{original_command[:30]}...' 放回队列重试 ({retry_count + 1}/3)")
                     else:
                        logger.error(f"指令 '{original_command[:30]}...' 重试次数过多，丢弃。")
                     await asyncio.sleep(self.COMMAND_DELAY_SECONDS * (retry_count + 1)) # 增加重试延迟
                     self.command_queue.task_done(); continue

                try:
                    sent_message = await self.app.send_message(self.target_chat_id, command_to_send, reply_parameters=reply_params)
                    # --- 修改: 添加 try-except ---
                    try:
                        safe_log_preview = command_to_send[:30].encode('utf-8', errors='replace').decode('utf-8', errors='replace')
                    except Exception:
                        safe_log_preview = "[预览创建失败]"
                    # --- 修改结束 ---
                    logger.info(f"【消息队列】已发送游戏指令: {safe_log_preview}... (MsgID: {sent_message.id}){' (回复 '+str(reply_to_id)+')' if reply_to_id else ''}")
                    await self.event_bus.emit("game_command_sent", sent_message, original_command)
                except Exception as e:
                    # --- 修改: 添加 try-except ---
                    try:
                        safe_log_preview_err = original_command[:30].encode('utf-8', errors='replace').decode('utf-8', errors='replace')
                    except Exception:
                         safe_log_preview_err = "[预览创建失败]"
                    # --- 修改结束 ---
                    logger.error(f"【消息队列】发送游戏指令 '{safe_log_preview_err}...' 时失败: {e}")
                    await self.event_bus.emit("game_command_failed", original_command, str(e))
                finally:
                    self.command_queue.task_done()
                    await asyncio.sleep(self.COMMAND_DELAY_SECONDS)
            except asyncio.CancelledError:
                 logger.info("【消息队列】队列处理器任务被取消。"); break
            except Exception as e:
                logger.critical(f"【消息队列】队列处理器发生严重错误: {e}", exc_info=True)
                await asyncio.sleep(60)
                try:
                    # 尝试清空可能的坏消息
                    while not self.command_queue.empty():
                        bad_item = self.command_queue.get_nowait()
                        logger.warning(f"从队列中丢弃潜在的错误项: {str(bad_item)[:50]}")
                        self.command_queue.task_done()
                except asyncio.QueueEmpty:
                    pass
                except Exception as td_err:
                    logger.error(f"【消息队列】严重错误处理中尝试清空队列时发生未知错误: {td_err}")

    def _is_from_game_bot(self, message: Message) -> bool:
        """检查消息是否来自配置的游戏机器人ID之一"""
        if not self.game_bot_ids: return False
        # 修正：game_bot_ids 可能是列表的列表 [[id1], [id2]]
        flat_bot_ids = [item for sublist in self.game_bot_ids for item in (sublist if isinstance(sublist, list) else [sublist])]
        return (message.sender_chat and message.sender_chat.id in flat_bot_ids) or \
               (message.from_user and message.from_user.id in flat_bot_ids)

    async def _calculate_target_flags(self, message: Message) -> Tuple[bool, bool]:
        """计算 is_reply_to_me 和 is_mentioning_me"""
        is_reply_to_me = False
        is_mentioning_me = False
        my_current_id = await self.get_my_id()
        my_current_username = await self.get_my_username()

        if message.reply_to_message and message.reply_to_message.from_user and my_current_id:
            if message.reply_to_message.from_user.id == my_current_id:
                is_reply_to_me = True

        raw_text = message.text or message.caption or ""
        if my_current_username and raw_text and message.entities:
             text_content = raw_text
             for entity in message.entities:
                  try:
                      if entity.type == MessageEntityType.MENTION:
                          mention_offset = entity.offset + 1; mention_length = entity.length - 1
                          if 0 <= mention_offset < len(text_content) and mention_length > 0:
                               mentioned_username = text_content[mention_offset : mention_offset + mention_length]
                               if mentioned_username.lower() == my_current_username.lower():
                                   is_mentioning_me = True; break
                      elif entity.type == MessageEntityType.TEXT_MENTION:
                          if entity.user and entity.user.id == my_current_id:
                              is_mentioning_me = True; break
                  except Exception as e:
                       logger.warning(f"【TG交互】解析 @ 提及 flags 时出错: {e}. Entity: {entity}, Text: {text_content[:100]}...")

        return is_reply_to_me, is_mentioning_me

    # --- 新增: 处理来自 Redis 的定向任务 ---
    async def _handle_assistant_task(self, channel: str, data: Any):
        """处理来自 assistant_tasks 频道的任务消息"""
        if not isinstance(data, dict):
            logger.warning(f"收到格式无效的任务消息: {data}")
            return

        target_user_id = data.get("target_user_id")
        task_type = data.get("task_type")
        payload = data.get("payload")
        task_id = data.get("task_id", "UnknownTaskID")

        my_current_id = await self.get_my_id()

        # 检查是否是发给自己的任务
        if target_user_id is None or target_user_id != my_current_id:
            # logger.debug(f"忽略非本实例的任务 (Target: {target_user_id}, MyID: {my_current_id})")
            return

        logger.info(f"收到指派给本实例的任务 (ID: {task_id}, Type: {task_type})")

        if task_type == "send_command":
            command_to_send = payload.get("command")
            if isinstance(command_to_send, str) and command_to_send:
                logger.info(f"准备执行定向指令: '{command_to_send[:50]}...' (TaskID: {task_id})")
                success = await self.send_game_command(command_to_send)
                # 可选：向结果频道反馈执行状态
                # if self.redis_client and self.result_channel:
                #     result_payload = {"status": "queued" if success else "failed_to_queue", "details": ""}
                #     await self.redis_client.publish(self.result_channel, {
                #         "task_id": f"result_{task_id}", "original_task_id": task_id, "task_type": "task_result",
                #         "source_user_id": my_current_id, "payload": result_payload
                #     })
            else:
                logger.error(f"无效的 send_command 任务 payload (TaskID: {task_id}): {payload}")

        # --- 可以扩展处理其他 task_type ---
        # elif task_type == "recipe_share_post":
        #     # 处理学生上架逻辑（如果需要插件交互）
        #     pass
        # elif task_type == "recipe_share_learn":
        #     # 处理学生学习逻辑（如果需要插件交互）
        #     pass
        else:
            logger.warning(f"收到未知的任务类型 '{task_type}' (TaskID: {task_id})")

    # --- 新增结束 ---

    def register_handlers(self):
        """注册 Pyrogram 的消息处理器"""
        # --- 游戏 Bot 回复处理器 (监听新消息) ---
        if self.game_bot_ids and self.target_chat_id:
             async def game_bot_filter(flt, _, message: Message):
                  return self._is_from_game_bot(message)
             custom_game_bot_filter = filters.create(game_bot_filter, name="game_bot_sender_filter", game_bot_ids=self.game_bot_ids)
             game_filter = custom_game_bot_filter & filters.chat(self.target_chat_id)
             # 修正：确保 game_bot_ids 是扁平列表
             flat_bot_ids_log = [item for sublist in self.game_bot_ids for item in (sublist if isinstance(sublist, list) else [sublist])]
             logger.info(f"游戏消息监听器: 监听 {len(flat_bot_ids_log)} 频道/用户在群组 {self.target_chat_id}。")

             @self.app.on_message(game_filter, group=1)
             async def on_game_response(client: Client, message: Message):
                 is_reply_to_me, is_mentioning_me = await self._calculate_target_flags(message)

                 raw_text = message.text or message.caption or ""
                 log_content = "[无标题媒体]"
                 if raw_text:
                     # --- 修改: 添加 try-except ---
                     try:
                         # 尝试使用更安全的方式获取预览，避免直接 UTF-16 编解码
                         safe_text = raw_text.encode('utf-16', errors='surrogatepass').decode('utf-16', errors='ignore')
                         log_content = safe_text[:50].replace('\n', ' ')
                     except Exception:
                         log_content = "[预览创建失败]"
                     # --- 修改结束 ---

                 sender_id = message.sender_chat.id if message.sender_chat else (message.from_user.id if message.from_user else "未知")
                 sender_type = "Channel" if message.sender_chat else ("User" if message.from_user else "未知")
                 logger.debug(f"收到游戏回复 (新) (From {sender_type}:{sender_id}, RTM:{is_reply_to_me}, MM:{is_mentioning_me}): {log_content}...")
                 if is_reply_to_me: logger.info(f"【TG交互】游戏 Bot ({sender_type}:{sender_id}) 回复了我: {log_content}...")
                 if is_mentioning_me: logger.info(f"【TG交互】游戏 Bot ({sender_type}:{sender_id}) @提及了我: {log_content}...")

                 await self.event_bus.emit("game_response_received", message, is_reply_to_me, is_mentioning_me)
        else:
             logger.warning("游戏消息监听器未启动: 配置缺失 game_bot_ids 或 target_chat_id。")

        # --- 管理员指令处理器 ---
        if self.admin_id:
             admin_filter = filters.user(self.admin_id) & (filters.private | filters.chat(self.control_chat_id))
             logger.info(f"管理员指令监听器: 监听用户 {self.admin_id} 在私聊及群组 {self.control_chat_id}。")
             @self.app.on_message(admin_filter, group=1)
             async def on_admin_command(client: Client, message: Message):
                 logger.info(f"【TG交互】检测到管理员消息 (Chat: {message.chat.id})，转发给插件...")
                 my_current_username = await self.get_my_username()
                 await self.event_bus.emit("admin_command_received", message, my_current_username)
        else:
             logger.error("管理员指令监听器未启动: 未配置 admin_id。")

        # --- 通用消息/编辑日志处理器 ---
        if self.target_chat_id:
            logger.info(f"通用消息日志监听器: 监听群组 {self.target_chat_id} (排除服务消息)。")
            @self.app.on_message(filters.chat(self.target_chat_id) & ~filters.service, group=10)
            async def on_raw_message_in_target_chat(client: Client, message: Message):
                await self.event_bus.emit("raw_message_received", message)

            logger.info(f"编辑消息日志监听器: 监听群组 {self.target_chat_id} (排除服务消息)。")
            @self.app.on_edited_message(filters.chat(self.target_chat_id) & ~filters.service, group=11)
            async def on_raw_edited_message_in_target_chat(client: Client, message: Message):
                 await self.event_bus.emit("raw_message_edited", message)

            # --- 游戏机器人编辑消息处理器 ---
            if self.game_bot_ids:
                 logger.info(f"游戏机器人编辑消息监听器: 监听群组 {self.target_chat_id}。")
                 @self.app.on_edited_message(game_filter, group=2) # 使用与 on_game_response 相同的过滤器
                 async def on_game_edited_response(client: Client, message: Message):
                     is_reply_to_me, is_mentioning_me = await self._calculate_target_flags(message)

                     raw_text = message.text or message.caption or ""
                     log_content = "[无标题媒体]"
                     if raw_text:
                         # --- 修改: 添加 try-except ---
                         try:
                             safe_text = raw_text.encode('utf-16', errors='surrogatepass').decode('utf-16', errors='ignore')
                             log_content = safe_text[:50].replace('\n', ' ')
                         except Exception:
                             log_content = "[预览创建失败]"
                         # --- 修改结束 ---
                     sender_id = message.sender_chat.id if message.sender_chat else (message.from_user.id if message.from_user else "未知")
                     sender_type = "Channel" if message.sender_chat else ("User" if message.from_user else "未知")
                     logger.debug(f"收到游戏回复 (编辑) (From {sender_type}:{sender_id}, RTM:{is_reply_to_me}, MM:{is_mentioning_me}): {log_content}...")

                     # 触发 game_response_received 事件
                     await self.event_bus.emit("game_response_received", message, is_reply_to_me, is_mentioning_me)
        else:
             logger.warning("通用/编辑消息日志监听器未启动: 未配置 target_chat_id。")

        logger.info("Telegram 消息处理器已注册。")

    def register_listeners(self):
        """注册事件总线的监听器 (用于发送消息)"""
        self.event_bus.on("send_admin_reply", self.send_admin_reply)
        self.event_bus.on("send_system_notification", self.send_system_notification)
        self.event_bus.on("send_admin_private_notification", self.send_admin_private_message) # 新增
        logger.info("Telegram 事件监听器已注册。")

    async def send_game_command(self, command: str) -> bool:
        """将指令放入当前实例的发送队列""" #<-- 修改注释
        if not command:
            logger.warning("【消息队列】尝试发送空指令，已忽略。")
            return False
        try:
            await self.command_queue.put(command)
            # --- 修改: 添加 try-except ---
            try:
                safe_log_preview_q = command[:30].encode('utf-8', errors='replace').decode('utf-8', errors='replace')
            except Exception:
                 safe_log_preview_q = "[预览创建失败]"
            # --- 修改结束 ---
            logger.info(f"【消息队列】指令 '{safe_log_preview_q}...' 已加入本实例队列 (当前队列大小: {self.command_queue.qsize()})。")
            return True
        except Exception as e:
            # --- 修改: 添加 try-except ---
            try:
                safe_log_preview_q_err = command[:30].encode('utf-8', errors='replace').decode('utf-8', errors='replace')
            except Exception:
                 safe_log_preview_q_err = "[预览创建失败]"
            # --- 修改结束 ---
            logger.error(f"【消息队列】将指令 '{safe_log_preview_q_err}...' 加入队列时失败: {e}", exc_info=True)
            return False


    async def send_admin_reply(self, text: str, original_message: Message):
        if not original_message:
             logger.error("无法回复管理员：缺少原始消息对象。")
             return
        chat_to_reply_id = original_message.chat.id
        if not chat_to_reply_id:
             logger.error(f"无法回复管理员：原始消息 ({original_message.id}) 缺少 chat id。")
             return
        if not self.app.is_connected:
             logger.error("无法回复管理员：Telegram 客户端未连接。")
             return
        try:
            MAX_LEN = 4096
            if len(text) > MAX_LEN:
                 logger.warning(f"回复消息过长 ({len(text)} > {MAX_LEN})，将被截断。")
                 text = text[:MAX_LEN - 15] + "\n...(消息过长截断)"

            reply_params = ReplyParameters(message_id=original_message.id)
            await self.app.send_message(
                chat_to_reply_id, text,
                reply_parameters=reply_params,
                link_preview_options=LinkPreviewOptions(is_disabled=True)
            )
            # --- 修改: 添加 try-except ---
            try:
                log_preview_admin = text.replace('\n', ' ')[:80].encode('utf-8', errors='replace').decode('utf-8', errors='replace')
            except Exception:
                 log_preview_admin = "[预览创建失败]"
            # --- 修改结束 ---
            logger.info(f"【TG交互】已回复管理员到 {chat_to_reply_id}: {log_preview_admin}...")
        except Exception as e:
            logger.error(f"回复管理员到 {chat_to_reply_id} 失败: {e}")

    async def send_system_notification(self, text: str):
        notify_chat_id = self.control_chat_id or self.admin_id
        if not notify_chat_id:
            # --- 修改: 添加 try-except ---
            try:
                safe_log_preview_sys_err = text[:30].encode('utf-8', errors='replace').decode('utf-8', errors='replace')
            except Exception:
                 safe_log_preview_sys_err = "[预览创建失败]"
            # --- 修改结束 ---
            logger.error(f"无法发送系统通知 (\"{safe_log_preview_sys_err}...\")：未配置 control_chat_id 或 admin_id。")
            return
        if not self.app.is_connected:
             # --- 修改: 添加 try-except ---
             try:
                 safe_log_preview_sys_err2 = text[:30].encode('utf-8', errors='replace').decode('utf-8', errors='replace')
             except Exception:
                  safe_log_preview_sys_err2 = "[预览创建失败]"
             # --- 修改结束 ---
             logger.error(f"无法发送系统通知 (\"{safe_log_preview_sys_err2}...\")：Telegram 客户端未连接。")
             return
        try:
            MAX_LEN = 4096
            if len(text) > MAX_LEN:
                 logger.warning(f"系统通知过长 ({len(text)} > {MAX_LEN})，将被截断。")
                 text = text[:MAX_LEN - 15] + "\n...(消息过长截断)"
            await self.app.send_message(
                 notify_chat_id, text,
                 link_preview_options=LinkPreviewOptions(is_disabled=True)
            )
            # --- 修改: 添加 try-except ---
            try:
                log_preview_sys = text[:50].encode('utf-8', errors='replace').decode('utf-8', errors='replace')
            except Exception:
                 log_preview_sys = "[预览创建失败]"
            # --- 修改结束 ---
            logger.info(f"【TG交互】已发送系统通知到 {notify_chat_id}: {log_preview_sys}...")
        except Exception as e:
            logger.error(f"发送系统通知到 {notify_chat_id} 失败: {e}")

    async def send_admin_private_message(self, text: str):
        """发送私聊消息给管理员 (不经过队列)"""
        if not self.admin_id:
            # --- 修改: 添加 try-except ---
            try:
                safe_log_preview_priv_err = text[:30].encode('utf-8', errors='replace').decode('utf-8', errors='replace')
            except Exception:
                 safe_log_preview_priv_err = "[预览创建失败]"
            # --- 修改结束 ---
            logger.error(f"无法发送管理员私聊 (\"{safe_log_preview_priv_err}...\")：未配置 admin_id。")
            return
        if not self.app.is_connected:
             # --- 修改: 添加 try-except ---
             try:
                 safe_log_preview_priv_err2 = text[:30].encode('utf-8', errors='replace').decode('utf-8', errors='replace')
             except Exception:
                  safe_log_preview_priv_err2 = "[预览创建失败]"
             # --- 修改结束 ---
             logger.error(f"无法发送管理员私聊 (\"{safe_log_preview_priv_err2}...\")：Telegram 客户端未连接。")
             return
        try:
            MAX_LEN = 4096
            if len(text) > MAX_LEN:
                 logger.warning(f"管理员私聊消息过长 ({len(text)} > {MAX_LEN})，将被截断。")
                 text = text[:MAX_LEN - 15] + "\n...(消息过长截断)"
            await self.app.send_message(
                 self.admin_id, text,
                 link_preview_options=LinkPreviewOptions(is_disabled=True)
            )
            # --- 修改: 添加 try-except ---
            try:
                log_preview_priv = text[:50].encode('utf-8', errors='replace').decode('utf-8', errors='replace')
            except Exception:
                 log_preview_priv = "[预览创建失败]"
            # --- 修改结束 ---
            logger.info(f"【TG交互】已发送私聊通知给管理员 {self.admin_id}: {log_preview_priv}...")
        except Exception as e:
            logger.error(f"发送私聊通知给管理员 {self.admin_id} 失败: {e}")

    async def run(self):
        """启动并运行客户端"""
        logger.info("Telegram 客户端模块已初始化。")
        try:
            self.register_handlers()
            self.register_listeners()
            # --- 新增: 注册 Redis 订阅 ---
            if self.redis_client:
                 await self.redis_client.subscribe(self.task_channel, self._handle_assistant_task)
                 logger.info(f"已注册 Redis 任务频道 '{self.task_channel}' 的处理器。")
            else:
                 logger.error("无法注册 Redis 任务频道处理器：RedisClient 未设置。")
            # --- 新增结束 ---
            logger.info("Telegram 客户端正在启动并连接...")
            await self.app.start()
            await self._ensure_me() # 确保获取到 _my_id
            user_id_str = str(self._my_id) if self._my_id else "N/A"
            username_str = self._my_username or 'Unknown'
            logger.info(f"Telegram 客户端已启动，用户: {username_str} (ID: {user_id_str})")

            if not self.queue_task or self.queue_task.done():
                logger.info("准备启动游戏指令队列处理器...")
                self.queue_task = asyncio.create_task(self._command_queue_processor())

            logger.info("触发 telegram_client_started 事件...")
            await self.event_bus.emit("telegram_client_started")

            logger.info("系统现在完全运行中...")
            await idle()
        except (KeyboardInterrupt, SystemExit):
            logger.info("收到退出信号 (KeyboardInterrupt/SystemExit)...")
        except Exception as e:
            logger.critical(f"TG 客户端运行时发生严重错误: {e}", exc_info=True)
        finally:
            logger.info("Telegram 客户端正在停止...")
            if self.queue_task and not self.queue_task.done():
                logger.info("正在取消游戏指令队列处理器任务...")
                self.queue_task.cancel()
                try: await self.queue_task
                except asyncio.CancelledError: logger.info("【消息队列】队列处理器任务已成功取消。")
                except Exception as q_stop_e: logger.error(f"等待队列处理器任务取消时出错: {q_stop_e}")
            me_username = self._my_username or "Unknown"
            if hasattr(self, 'app') and self.app.is_initialized:
                 try:
                     if self.app.is_connected:
                        logger.info("正在停止 Pyrogram 客户端...")
                        await self.app.stop()
                     logger.info(f"Telegram 客户端已停止 (用户: {me_username})。")
                 except Exception as stop_e:
                     logger.error(f"停止 TG 客户端时出错: {stop_e}")
            else:
                logger.info("Telegram 客户端未初始化或已停止，无需再次停止。")

