import logging
import asyncio
from pyrogram.types import Message, ReplyParameters, LinkPreviewOptions
from plugins.base_plugin import BasePlugin, AppContext
from core.context import get_global_context # 导入 get_global_context

class Plugin(BasePlugin):
    """
    处理 ,插件 指令。
    """
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        # --- (修改: 移除多余的属性) ---
        self.plugin_statuses = {} # 初始化为空，在命令处理时重新获取
        self.telegram_client_instance = getattr(context, 'telegram_client', None)
        self.admin_id = self.config.get("telegram.admin_id")
        self.control_chat_id = self.config.get("telegram.control_chat_id")
        # --- (修改结束) ---
        self.info("插件已加载。")

    def register(self):
        """注册系统指令的事件监听"""
        # --- (修改: 只监听 system_plugins_command) ---
        self.event_bus.on("system_plugins_command", self.handle_plugins_command)
        self.info("已注册 plugins command 事件监听器。")
        # --- (修改结束) ---

    async def handle_plugins_command(self, message: Message, args: str | None, edit_target_id: int | None):
        """处理 ,插件 指令"""
        self.info(f"处理 ,插件 指令 (args: {args})")
        # --- (修改: 每次都从 context 重新获取最新状态) ---
        self.plugin_statuses = getattr(self.context, 'plugin_statuses', {})
        # --- (修改结束) ---

        if not args:
            reply = "⚙️ **当前插件状态** ⚙️\n\n"
            if not self.plugin_statuses:
                reply += "无法获取插件状态或没有插件加载。"
            else:
                plugin_name_map = self.context.plugin_name_map
                for name, status in sorted(self.plugin_statuses.items()):
                    emoji = "✅" if status == "enabled" else ("⚠️" if status == "disabled" else "❌")
                    cn_name = plugin_name_map.get(name, name)
                    reply += f"{emoji} {cn_name} (`{name}`)\n"
            reply += "\n💡 使用 `,插件 <英文名> 开/关` 可管理插件状态 (此功能正在开发中)。"
            await self._edit_or_reply(message.chat.id, edit_target_id, reply, original_message=message)
        else:
            self.warning("收到插件启停指令，但功能尚未实现。")
            await self._edit_or_reply(message.chat.id, edit_target_id, "ℹ️ 动态插件启停功能正在开发中...", original_message=message)

    # --- (移除 handle_config_command, handle_log_command, handle_loglevel_command, _save_config, _toggle_cultivation_internal) ---

    # --- (辅助函数 _edit_or_reply 和 _send_status_message 保持不变) ---
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

