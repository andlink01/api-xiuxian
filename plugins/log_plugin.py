import logging
import asyncio
import os
from pyrogram.types import Message, ReplyParameters, LinkPreviewOptions
from plugins.base_plugin import BasePlugin, AppContext

class Plugin(BasePlugin):
    """
    处理 ,日志 指令。
    """
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        self.telegram_client_instance = getattr(context, 'telegram_client', None)
        self.admin_id = self.config.get("telegram.admin_id")
        self.control_chat_id = self.config.get("telegram.control_chat_id")
        self.info("插件已加载。")

    def register(self):
        """注册系统指令的事件监听"""
        self.event_bus.on("system_log_command", self.handle_log_command)
        self.info("已注册 log command 事件监听器。")

    async def handle_log_command(self, message: Message, args: str | None, edit_target_id: int | None):
        """处理 ,日志 指令"""
        self.info(f"处理 ,日志 指令 (args: {args})")
        log_type = 'main'
        lines_to_fetch = 50

        if args:
            parts = args.split()
            for part in parts:
                if part.isdigit():
                    lines_to_fetch = int(part)
                elif part.lower() in ['main', 'chat']:
                    log_type = part.lower()

            if lines_to_fetch < 1: lines_to_fetch = 1
            if lines_to_fetch > 200: lines_to_fetch = 200

        log_file_path = f"logs/game_assistant.log" if log_type == 'main' else f"logs/game_chat_log.txt"
        self.debug(f"准备读取日志文件: {log_file_path}, 行数: {lines_to_fetch}")

        reply_header = f"📜 **{log_type.capitalize()} 日志 (最后 {lines_to_fetch} 行)** 📜\n\n"

        if not os.path.exists(log_file_path):
            reply_body = f"❌ 错误: 日志文件 {log_file_path} 未找到。"
            self.error(f"日志文件未找到: {log_file_path}")
            await self._edit_or_reply(message.chat.id, edit_target_id, reply_header + reply_body, original_message=message)
            return

        try:
            log_lines = []
            try:
                with open(log_file_path, 'rb') as f:
                    f.seek(0, os.SEEK_END)
                    file_size = f.tell()
                    estimated_bytes_per_line = 150
                    target_bytes = (lines_to_fetch + 5) * estimated_bytes_per_line
                    seek_pos = max(0, file_size - target_bytes)
                    f.seek(seek_pos)
                    content_bytes = f.read()
                    log_lines = content_bytes.decode('utf-8', errors='ignore').splitlines()[-lines_to_fetch:]
            except Exception as read_err:
                 self.error(f"读取日志文件 {log_file_path} 时出错: {read_err}")
                 reply_body = f"❌ 读取日志时发生错误。"
                 await self._edit_or_reply(message.chat.id, edit_target_id, reply_header + reply_body, original_message=message)
                 return

            log_content = "\n".join(log_lines)
            reply_body = ""

            if not log_content:
                reply_body = "(日志为空或读取的部分为空)"
            else:
                max_len = 4096
                code_block_overhead = 10
                header_len = len(reply_header)
                available_len = max_len - header_len - code_block_overhead
                truncated = False
                if len(log_content) > available_len:
                    log_content = log_content[-available_len:]
                    first_newline = log_content.find('\n')
                    if first_newline != -1: log_content = log_content[first_newline+1:]
                    log_content = "...(日志过长，已截断)...\n" + log_content
                    truncated = True
                reply_body = f"```{log_content}```"

        except Exception as e:
            reply_body = f"❌ 读取或处理日志文件时出错: {e}"
            self.error(f"处理日志文件 {log_file_path} 时出错: {e}", exc_info=True)

        await self._edit_or_reply(message.chat.id, edit_target_id, reply_header + reply_body, original_message=message)

    # --- (辅助函数 _edit_or_reply 和 _send_status_message) ---
    async def _edit_or_reply(self, chat_id: int, message_id: int | None, text: str, original_message: Message):
        tg_client = self.telegram_client_instance
        if not tg_client or not tg_client.app.is_connected:
             self.error("无法编辑/回复：TG 客户端不可用。")
             return
        edited = False
        link_preview_options = LinkPreviewOptions(is_disabled=True)
        MAX_LEN = 4096
        if len(text) > MAX_LEN:
            self.warning(f"即将发送/编辑的消息过长 ({len(text)} > {MAX_LEN})，将被截断。")
            text = text[:MAX_LEN - 15] + "\n...(消息过长截断)"
        if message_id:
            try:
                await tg_client.app.edit_message_text(chat_id, message_id, text, link_preview_options=link_preview_options)
                edited = True
            except Exception as e:
                if "MESSAGE_NOT_MODIFIED" not in str(e):
                    self.warning(f"编辑消息 {message_id} 失败 ({e})，尝试回复...")
                    edited = False
                else:
                    self.debug(f"消息 {message_id} 未修改。")
                    edited = True
        if not edited:
            if not original_message:
                 self.error("编辑失败且无法回复：缺少原始消息对象。")
                 fallback_chat_id = self.control_chat_id or self.config.get("telegram.admin_id")
                 if fallback_chat_id:
                     try: await tg_client.app.send_message(fallback_chat_id, f"(Edit/Reply Failed)\n{text[:1000]}...", link_preview_options=link_preview_options)
                     except Exception as final_err: self.critical(f"最终 fallback 发送失败: {final_err}")
                 return
            try:
                reply_params = ReplyParameters(message_id=original_message.id)
                await tg_client.app.send_message(chat_id, text, reply_parameters=reply_params, link_preview_options=link_preview_options)
            except Exception as e2:
                self.error(f"编辑和回复均失败: {e2}")
                fallback_chat_id = self.control_chat_id or self.config.get("telegram.admin_id")
                if fallback_chat_id:
                    try: await tg_client.app.send_message(fallback_chat_id, f"(Edit/Reply Failed)\n{text[:1000]}...", link_preview_options=link_preview_options)
                    except Exception as final_err: self.critical(f"最终 fallback 发送失败: {final_err}")

    async def _send_status_message(self, original_message: Message, status_text: str) -> Message | None:
        # (这个函数在 log_plugin 中没有被 handle_log_command 调用，但保留以备将来使用)
        tg_client = self.telegram_client_instance
        if not tg_client or not tg_client.app.is_connected:
            self.warning("无法发送状态消息：TG 客户端不可用。")
            return None
        reply_params = ReplyParameters(message_id=original_message.id)
        link_preview_options = LinkPreviewOptions(is_disabled=True)
        try:
            return await tg_client.app.send_message(
                original_message.chat.id, status_text,
                reply_parameters=reply_params, link_preview_options=link_preview_options
            )
        except Exception as e:
            self.warning(f"回复状态消息失败 ({e})，尝试直接发送...")
            try:
                 return await tg_client.app.send_message(original_message.chat.id, status_text, link_preview_options=link_preview_options)
            except Exception as e2:
                 self.error(f"直接发送状态消息也失败: {e2}")
                 return None

