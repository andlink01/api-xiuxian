import logging
import os
from logging.handlers import RotatingFileHandler
from datetime import datetime
import pytz # 保留 pytz 用于可能的时区感知处理
from plugins.base_plugin import BasePlugin, AppContext
from pyrogram.types import Message, User
from pyrogram import enums
from typing import Optional

LOG_FILENAME = "logs/game_chat_log.txt"
MAX_BYTES = 3 * 1024 * 1024
BACKUP_COUNT = 10
# --- (修改: 调整分隔符长度) ---
SEPARATOR = "\n==========================================\n\n"
# --- (修改结束) ---

# --- (修改: 不再强制使用北京时间) ---
# beijing_tz = pytz.timezone('Asia/Shanghai')
# --- (修改结束) ---

class ReopenableRotatingFileHandler(RotatingFileHandler):
    """
    一个 RotatingFileHandler 子类，在 emit 前检查文件是否存在，
    如果不存在则尝试重新打开。
    """
    def emit(self, record):
        if self.stream is None or (hasattr(self.stream, 'name') and not os.path.exists(self.stream.name)):
            try:
                if self.stream:
                    self.stream.close()
                    self.stream = None
                self.stream = self._open()
            except Exception:
                self.handleError(record)
                return
        super().emit(record)


def setup_message_logger():
    """配置用于记录游戏群消息的独立 logger"""
    log_formatter = logging.Formatter('%(message)s')
    setup_logger = logging.getLogger("MessageLoggerPlugin.Setup")

    try:
        os.makedirs(os.path.dirname(LOG_FILENAME), exist_ok=True)
    except OSError as e:
        setup_logger.error(f"【消息记录】创建日志目录 {os.path.dirname(LOG_FILENAME)} 失败: {e}")
        return None

    try:
        log_handler = ReopenableRotatingFileHandler(
            LOG_FILENAME,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding='utf-8'
        )
        log_handler.setFormatter(log_formatter)

        message_logger = logging.getLogger("GameChatLogger")
        message_logger.setLevel(logging.INFO)
        if message_logger.hasHandlers():
            message_logger.handlers.clear()
        message_logger.addHandler(log_handler)
        message_logger.propagate = False

        # --- (修改: 使用本地时间记录启动信息) ---
        # now_beijing = datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")
        # message_logger.info(f"{SEPARATOR}--- 【消息记录】Logger 启动于 {now_beijing} (北京时间) ---{SEPARATOR}")
        now_local = datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z%z") # 获取系统本地时间及区域信息
        message_logger.info(f"{SEPARATOR}--- 【消息记录】Logger 启动于 {now_local} (系统本地时间) ---{SEPARATOR}")
        # --- (修改结束) ---

        return message_logger
    except Exception as e:
        setup_logger.error(f"【消息记录】设置 RotatingFileHandler 失败: {e}", exc_info=True)
        return None

class Plugin(BasePlugin):
    """
    一个独立的插件，用于记录指定游戏群组的所有消息（不含服务消息）。
    """
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        self.message_logger = setup_message_logger()
        self.target_chat_id = self.config.get("telegram.target_chat_id", 0)

        if self.message_logger:
            self.info("插件已加载，聊天记录器设置成功。")
        else:
             self.error("插件已加载，但聊天记录器设置失败！将无法记录聊天。")

        if not self.target_chat_id:
             self.warning("未配置 target_chat_id，插件将不会记录任何内容。")

    def register(self):
        """注册事件监听器"""
        if self.target_chat_id and self.message_logger:
            self.event_bus.on("raw_message_received", self.handle_raw_message)
            self.event_bus.on("raw_message_edited", self.handle_raw_edited_message)
            self.info(f"已注册 raw_message_received 和 raw_message_edited 监听器，将记录群组 {self.target_chat_id} 的新消息和编辑消息 (不含服务消息)。")

    async def _log_message_common(self, message: Message, message_type_override: Optional[str] = None):
        """通用的消息记录逻辑 (不含服务消息)"""
        if not self.message_logger:
            return
        # 确保过滤掉服务消息 (虽然事件发送端已过滤)
        if message.service or message.chat.id != self.target_chat_id:
            return

        try:
            timestamp_dt = message.edit_date or message.date
            # --- (修改: 获取系统本地时间) ---
            # if timestamp_dt and (timestamp_dt.tzinfo is None or timestamp_dt.tzinfo.utcoffset(timestamp_dt) is None):
            #      timestamp_dt = pytz.utc.localize(timestamp_dt)
            # formatted_time = timestamp_dt.astimezone(beijing_tz).strftime("%Y-%m-%d %H:%M:%S") if timestamp_dt else "未知时间"
            formatted_time = "未知时间"
            if timestamp_dt:
                # Pyrogram 的时间通常是 UTC 的 naive datetime，转换为带 UTC 时区信息，然后转本地时区
                if timestamp_dt.tzinfo is None:
                    timestamp_dt = pytz.utc.localize(timestamp_dt)
                local_dt = timestamp_dt.astimezone() # 转换为系统默认本地时区
                formatted_time = local_dt.strftime("%Y-%m-%d %H:%M:%S %Z%z") # 包含时区信息
            # --- (修改结束) ---

            chat_name = message.chat.title or f"ChatID:{message.chat.id}"
            chat_id = message.chat.id

            sender_info = "未知发送者"
            sender_id = "N/A"
            sender_type = "Unknown"
            if message.from_user:
                sender_type = "Bot" if message.from_user.is_bot else "User"
                sender_name = f"{message.from_user.first_name or ''} {message.from_user.last_name or ''}".strip() or f"{sender_type}:{message.from_user.id}"
                sender_id = message.from_user.id
                sender_info = f"{sender_name} ({sender_type} ID:{sender_id})"
            elif message.sender_chat:
                sender_type = "Channel"
                sender_name = message.sender_chat.title or f"{sender_type}:{message.sender_chat.id}"
                sender_id = message.sender_chat.id
                sender_info = f"{sender_name} ({sender_type} ID:{sender_id})"

            message_type = message_type_override or ("发出" if message.outgoing else "收到")

            if message.reply_to_message_id:
                message_type += f" (回复 -> {message.reply_to_message_id})"

            content = "[空消息]"
            if message.text: content = message.text
            elif message.caption: content = message.caption
            elif message.sticker: content = f"[表情: {message.sticker.emoji or '未知'}] (ID:{message.sticker.file_unique_id})"
            elif message.photo: content = f"[图片] (ID:{message.photo.file_unique_id})"
            elif message.video: content = f"[视频] (ID:{message.video.file_unique_id})"
            elif message.document: content = f"[文件: {message.document.file_name or '无名'}] (ID:{message.document.file_unique_id})"
            elif message.audio: content = f"[音频: {message.audio.title or '无名'}] (ID:{message.audio.file_unique_id})"
            elif message.voice: content = f"[语音] (ID:{message.voice.file_unique_id})"
            elif message.animation: content = f"[动画/GIF] (ID:{message.animation.file_unique_id})"
            elif message.empty: content = "[消息已删除/空]"
            else: content = "[未知类型媒体/无内容]"

            log_entry = (
                f"时间: {formatted_time}\n"
                f"群组: {chat_name} ({chat_id})\n"
                f"来源: {sender_info}\n"
                f"类型: {message_type}\n"
                f"MsgID: {message.id}\n"
                f"内容:\n{content}\n"
            )

            self.message_logger.info(log_entry + SEPARATOR)

        except Exception as e:
            self.error(f"记录消息时出错 (MsgID: {message.id}): {e}", exc_info=True)

    async def handle_raw_message(self, message: Message):
        """Handle raw new messages (excluding service) and log"""
        await self._log_message_common(message)

    async def handle_raw_edited_message(self, message: Message):
        """Handle raw edited messages (excluding service) and log"""
        await self._log_message_common(message, message_type_override="编辑")

