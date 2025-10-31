import logging
import asyncio
import os
import yaml
import pytz
import re
from datetime import datetime
from pyrogram.types import Message, ReplyParameters, LinkPreviewOptions
from plugins.base_plugin import BasePlugin, AppContext
from core.context import get_global_context
# --- (修改: 导入 format_local_time 和 format_job_details) ---
try:
    from plugins.character_sync_plugin import format_local_time
except ImportError: format_local_time = None
from plugins.utils import format_job_details # 导入新的辅助函数
# --- (修改结束) ---
from apscheduler.jobstores.base import JobLookupError
from core.config import CONFIG_PATH
from ast import literal_eval
from datetime import timezone

try:
    from plugins.cultivation_plugin import JOB_ID as CULTIVATION_JOB_ID
    CULTIVATION_PLUGIN_LOADED = True
except ImportError:
    CULTIVATION_JOB_ID = 'auto_cultivation_job'
    CULTIVATION_PLUGIN_LOADED = False
# --- 新增: 导入自动斗法任务 ID ---
try:
    from plugins.auto_duel_plugin import AUTO_DUEL_JOB_ID
    AUTO_DUEL_PLUGIN_LOADED = True
except ImportError:
    AUTO_DUEL_JOB_ID = 'auto_duel_job'
    AUTO_DUEL_PLUGIN_LOADED = False
# --- 新增结束 ---

VALID_LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

CONFIGURABLE_ITEMS = {
    # 自动闭关相关
    "自动闭关": ("cultivation.auto_enabled", "布尔值 (开/关)", "自动闭关开关"),
    "闭关延迟": ("cultivation.random_delay_range", "列表 (如 [1, 5])", "自动闭关随机延迟范围(秒)"),
    "闭关超时": ("cultivation.response_timeout", "整数 (秒)", "等待闭关响应的超时时间"),
    "闭关重试延迟": ("cultivation.retry_delay_on_fail", "整数 (秒)", "闭关失败或API错误后的重试延迟"),

    # 同步相关
    "同步间隔角色": ("sync_intervals.character", "整数 (分钟)", "角色自动同步间隔"),
    "同步间隔背包": ("sync_intervals.inventory", "整数 (分钟)", "背包自动同步间隔"),
    "启动同步角色": ("sync_on_startup.character", "布尔值 (开/关)", "启动时自动同步角色"),
    "启动同步背包": ("sync_on_startup.inventory", "布尔值 (开/关)", "启动时自动同步背包"),
    "启动同步商店": ("sync_on_startup.shop", "布尔值 (开/关)", "启动时自动同步商店"),
    "启动同步物品": ("sync_on_startup.item", "布尔值 (开/关)", "启动时自动同步物品"),

    # 玄骨考校相关
    "考校启用": ("xuangu_exam.enabled", "布尔值 (开/关)", "玄骨考校功能开关"),
    "考校自动答题": ("xuangu_exam.auto_answer", "布尔值 (开/关)", "玄骨考校自动答题开关"),
    "考校AI": ("xuangu_exam.use_ai_fallback", "布尔值 (开/关)", "玄骨考校 AI 备选答案开关"),
    "考校答题延迟": ("xuangu_exam.answer_delay_seconds", "整数 (秒)", "自动答题前的延迟时间"),
    "考校未知通知": ("xuangu_exam.notify_on_unknown_question", "布尔值 (开/关)", "遇到未知题目时通知管理员"),

    # 药园配置
    "药园启用": ("herb_garden.enabled", "布尔值 (开/关)", "自动药园功能开关"),
    "药园检查间隔": ("herb_garden.check_interval_minutes", "整数 (分钟)", "自动药园检查间隔"),
    "药园种植目标": ("herb_garden.target_seed_name", "字符串", "自动播种的目标种子名称"),
    "药园种子保留": ("herb_garden.min_seed_reserve", "整数", "背包中最低保留的种子数量"),
    "药园购买数量": ("herb_garden.buy_seed_quantity", "整数", "每次自动购买种子的数量"),

    # --- 新增: 自动斗法 ---
    "斗法启用": ("auto_duel.enabled", "布尔值 (开/关)", "自动斗法开关"),
    "斗法目标": ("auto_duel.targets", "列表 (e.g., [\"@user1\"])", "自动斗法目标 (请直接修改 config.yaml)"),
    "斗法间隔": ("auto_duel.interval_seconds", "整数 (秒)", "自动斗法间隔 (默认 305)"),
    # --- 新增结束 ---
    
    # 系统与其他
    "日志级别": ("logging.level", f"字符串 ({'/'.join(VALID_LOG_LEVELS)})", "主日志级别"),
    "目标用户": ("game_api.target_username", "字符串", "API 请求的目标游戏用户名 (留空则自动获取)"),
    "Cookie": ("api_services.shared_cookie", "字符串", "API 请求使用的 Cookie"),
}

class Plugin(BasePlugin):
    """
    处理 ,配置, ,日志级别, ,任务列表 指令。
    """
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        self.telegram_client_instance = getattr(context, 'telegram_client', None)
        self.admin_id = self.config.get("telegram.admin_id")
        self.control_chat_id = self.config.get("telegram.control_chat_id")
        self.info("插件已加载。")

    # --- (修改: 注册新事件) ---
    def register(self):
        """注册系统指令的事件监听"""
        self.event_bus.on("system_config_command", self.handle_config_command)
        self.event_bus.on("system_loglevel_command", self.handle_loglevel_command)
        self.event_bus.on("system_show_tasks_command", self.handle_show_tasks_command) # 新增
        self.info("已注册 config, loglevel, show_tasks 命令事件监听器。")
    # --- (修改结束) ---

    async def handle_config_command(self, message: Message, args: str | None, edit_target_id: int | None):
        """处理 ,配置 指令。"""
        self.info(f"处理 ,配置 指令 (args: {args})")

        if args:
            parts = args.split(maxsplit=1)
            config_key_name = parts[0]
            new_value_str = parts[1] if len(parts) > 1 else None

            if config_key_name in CONFIGURABLE_ITEMS:
                config_path, expected_type, _ = CONFIGURABLE_ITEMS[config_key_name]

                # --- 新增: 阻止通过指令修改列表 ---
                if "列表" in expected_type and new_value_str is not None and config_key_name != "闭关延迟": # 允许修改闭关延迟
                    reply = f"❌ 出于安全和格式考虑，列表类型 (如 **{config_key_name}**) 无法通过指令修改。\n请直接编辑 `config.yaml` 文件并重启助手。"
                    await self._edit_or_reply(message.chat.id, edit_target_id, reply, original_message=message)
                    return
                # --- 新增结束 ---
                    
                if new_value_str is None:
                    current_value = self.config.get(config_path)
                    display_value = current_value
                    if config_key_name == "Cookie": display_value = "[已配置]" if current_value else "[未配置]"
                    elif config_key_name == "目标用户": display_value = current_value if current_value else "[自动获取]"
                    elif config_key_name == "日志级别": display_value = logging.getLevelName(logging.getLogger("GameAssistant").level)
                    elif config_key_name == "斗法目标": display_value = f"已配置 {len(current_value)} 个" if isinstance(current_value, list) and current_value else "[未配置]"


                    reply = f"ℹ️ **配置项:** {config_key_name}\n"
                    reply += f"   **当前值:** `{display_value}`\n"
                    reply += f"   **路径:** `{config_path}`\n"
                    reply += f"   **类型:** {expected_type}\n\n"
                    reply += f"**用法:** `,配置 {config_key_name} <新值>`"
                    await self._edit_or_reply(message.chat.id, edit_target_id, reply, original_message=message)
                    return

                new_value = None; valid = False; error_msg = ""

                try:
                    if "布尔值" in expected_type:
                        lower_val = new_value_str.lower()
                        if lower_val in ['开', 'on', 'true', 'yes', '启用', 'enable', 'start', '1']: new_value = True; valid = True
                        elif lower_val in ['关', 'off', 'false', 'no', '禁用', 'disable', 'stop', '0']: new_value = False; valid = True
                        else: error_msg = f"无法识别的布尔值 '{new_value_str}'。请使用 开/关, on/off, true/false 等。"
                    elif "整数" in expected_type:
                        new_value = int(new_value_str)
                        valid = True
                        if (("间隔" in config_key_name or "延迟" in config_key_name or "超时" in config_key_name or "数量" in config_key_name or "保留" in config_key_name or "斗法间隔" in config_key_name) and new_value < 0): valid = False; error_msg = "时间或数量相关的值不能为负数。"
                        elif "间隔" in config_key_name and new_value == 0: valid = False; error_msg = "同步间隔不能为 0。"
                        elif "斗法间隔" in config_key_name and new_value < 60: valid = False; error_msg = "斗法间隔过短，至少应为 60 秒。"
                    elif "列表" in expected_type:
                        parsed_list = literal_eval(new_value_str)
                        if isinstance(parsed_list, list) and len(parsed_list) == 2 and all(isinstance(x, (int, float)) for x in parsed_list) and parsed_list[0] <= parsed_list[1] and parsed_list[0] >= 0:
                            new_value = [int(x) if isinstance(x, int) else float(x) for x in parsed_list]; valid = True # 确保是数字
                        else: error_msg = "无效的列表格式或值。期望格式如 `[1, 5]` 或 `[2.0, 5.0]`，且第一个值小于等于第二个值，且都为非负数。"
                    elif "字符串" in expected_type:
                        new_value = new_value_str; valid = True
                        if config_key_name == "Cookie":
                             if new_value and len(new_value) < 10: self.warning(f"设置的 Cookie 值过短，可能无效。")
                             elif not new_value: self.info("Cookie 值被设置为空。")
                        elif config_key_name == "目标用户":
                             if new_value and not re.match(r"^[a-zA-Z0-9_]{5,}$", new_value): self.warning(f"设置的目标用户名 '{new_value}' 格式可能不正确。")
                             elif not new_value: self.info("目标用户设置为空，将自动获取。")
                    elif f"字符串 ({'/'.join(VALID_LOG_LEVELS)})" in expected_type: # 日志级别
                         upper_val = new_value_str.upper()
                         if upper_val in VALID_LOG_LEVELS: new_value = upper_val; valid = True
                         else: error_msg = f"无效的日志级别 '{new_value_str}'。有效级别: {', '.join(VALID_LOG_LEVELS)}"

                except ValueError: error_msg = f"无法将 '{new_value_str}' 转换为期望的 {expected_type} 类型。"
                except Exception as e: error_msg = f"解析值时发生错误: {e}"

                if valid:
                    save_success, save_msg = await self._save_config(config_path, new_value)
                    if save_success:
                        display_new_value = new_value
                        if config_key_name == "Cookie": display_new_value = "[新值已设置]" if new_value else "[已清空]"
                        elif config_key_name == "目标用户": display_new_value = new_value if new_value else "[自动获取]"
                        reply = f"✅ 配置项 **{config_key_name}** 已更新为:\n`{display_new_value}`"
                        self.info(f"配置项 {config_key_name} ({config_path}) 已通过指令更新为 {'[隐藏]' if config_key_name == 'Cookie' else new_value}")
                        # 特殊处理
                        if config_path == "api_services.shared_cookie":
                            if self.context.http: self.context.http.cookie_str = new_value; self.info("HTTPClient 的 Cookie 已实时更新。"); reply += "\n(HTTPClient Cookie 已实时更新)"
                        elif config_path == "logging.level":
                            try:
                                level_int = logging.getLevelName(new_value); target_logger = logging.getLogger("GameAssistant"); target_logger.setLevel(level_int)
                                for handler in target_logger.handlers:
                                    if isinstance(handler, logging.StreamHandler): handler.setLevel(level_int)
                                self.info(f"运行时日志级别已更新为 {new_value}"); reply += "\n(运行时日志级别已更新)"
                            except Exception as e_level: self.error(f"运行时更新日志级别失败: {e_level}"); reply += "\n(警告: 运行时级别更新失败)"
                        elif config_path == "cultivation.auto_enabled":
                             event_name = "start_auto_cultivation" if new_value else "stop_auto_cultivation"; self.info(f"触发事件: {event_name}"); await self.event_bus.emit(event_name); reply += f"\n(已{'请求启动' if new_value else '请求停止'}自动闭关)"
                        # --- 新增: 自动斗法 ---
                        elif config_path == "auto_duel.enabled" or config_path == "auto_duel.interval_seconds":
                             reply += "\n(将在下次调度器重启时生效，请重启助手或等待)" # 简单处理，重启任务比较麻烦
                        # --- 新增结束 ---
                        elif config_path.startswith("sync_intervals.") or config_path.startswith("herb_garden.") or config_path.startswith("auto_learn_recipe.") or config_path.startswith("yindao.") or config_path.startswith("sect_teach."):
                             reply += "\n(将在下次定时任务触发或重新调度时生效)"
                        elif config_path.startswith("cultivation."): reply += "\n(将在下次调度计算或任务执行时生效)"
                        elif config_path.startswith("xuangu_exam."): reply += "\n(将在下次考校触发时生效)"
                        elif config_path == "game_api.target_username": reply += "\n(将在下次需要用户名时生效)"
                    else:
                        reply = f"❌ 更新配置项 **{config_key_name}** 失败: {save_msg}"
                        self.error(f"更新配置项 {config_key_name} 失败: {save_msg}")
                else: reply = f"❌ 设置失败: {error_msg}\n\n**配置项:** {config_key_name}\n**期望类型:** {expected_type}"

            elif config_key_name == "修炼": # 保留旧指令兼容
                 sub_args = new_value_str if new_value_str else ""
                 if sub_args in ['开', 'on', '启用', 'enable', 'start', '1']: await self._toggle_cultivation_internal(message, True, edit_target_id)
                 elif sub_args in ['关', 'off', '禁用', 'disable', 'stop', '0']: await self._toggle_cultivation_internal(message, False, edit_target_id)
                 else: reply = "❌ 参数错误。\n用法: `,配置 修炼 开/关`"; await self._edit_or_reply(message.chat.id, edit_target_id, reply, original_message=message)
                 return
            else:
                reply = f"❓ 未知的配置项: `{config_key_name}`\n请使用 `,配置` 查看可配置项列表。"
                await self._edit_or_reply(message.chat.id, edit_target_id, reply, original_message=message)
                return

            await self._edit_or_reply(message.chat.id, edit_target_id, reply, original_message=message)
            return
        else:
            # 显示状态 (无参数)
            reply = "🔧 **配置状态** 🔧\n\n"
            for name, (path, type_info, desc) in sorted(CONFIGURABLE_ITEMS.items()):
                current_value = self.config.get(path)
                display_value = current_value; emoji = "⚙️"
                if "布尔值" in type_info: emoji = "✅" if current_value else "❌"; display_value = "开启" if current_value else "关闭"
                elif name == "Cookie": emoji = "🔑"; display_value = "[已配置]" if current_value else "[未配置]"
                elif name == "目标用户": emoji = "👤"; display_value = current_value if current_value else "[自动获取]"
                elif name == "日志级别": emoji = "📊"; display_value = logging.getLevelName(logging.getLogger("GameAssistant").level)
                elif name == "闭关延迟": emoji = "⏳"; display_value = str(current_value)
                elif "间隔" in name: emoji = "⏱️"; display_value = f"{current_value} {'分钟' if '分钟' in desc else '秒'}"
                elif name == "闭关超时" or name == "闭关重试延迟" or name == "考校答题延迟" or name == "药园种子保留" or name == "药园购买数量":
                     emoji = "⏱️" if "延迟" in name or "超时" in name else ("🌱" if "种子" in name else "🔢")
                     display_value = f"{current_value} {'秒' if '秒' in desc else ('颗' if '种子' in name else '')}".strip()
                elif name == "药园种植目标": emoji = "🎯"; display_value = current_value
                # --- 新增: 自动斗法显示 ---
                elif name == "斗法目标":
                    emoji = "⚔️"
                    if isinstance(current_value, list) and current_value:
                        display_value = f"已配置 {len(current_value)} 个"
                    else:
                        display_value = "[未配置]"
                # --- 新增结束 ---
                reply += f"{emoji} **{name}**: `{display_value}`\n"
                
                # --- 修改: 显示闭关和斗法任务状态 ---
                job_id_to_check = None
                if path == "cultivation.auto_enabled" and CULTIVATION_PLUGIN_LOADED and current_value:
                    job_id_to_check = CULTIVATION_JOB_ID
                elif path == "auto_duel.enabled" and AUTO_DUEL_PLUGIN_LOADED and current_value:
                    job_id_to_check = AUTO_DUEL_JOB_ID

                if job_id_to_check and format_local_time: # 检查函数是否导入成功
                    try:
                        context = get_global_context(); job = None
                        if context and context.scheduler and context.scheduler.running:
                            try: job = context.scheduler.get_job(job_id_to_check)
                            except JobLookupError: job = None
                            except Exception as get_job_err: self.warning(f"获取任务 ({job_id_to_check}) 时出错: {get_job_err}"); job = None
                        if job and job.next_run_time:
                            next_run_local_str = format_local_time(job.next_run_time) or str(job.next_run_time)
                            reply += f"    └─ 下次运行: {next_run_local_str}\n"
                        elif job: reply += f"    └─ 状态: 等待执行\n"
                        else: reply += f"    └─ 状态: 等待调度\n"
                    except Exception as e: self.debug(f"检查任务 {job_id_to_check} 状态时出错: {e}"); reply += "    └─ 状态: 检查任务出错\n"
                elif job_id_to_check: reply += "    └─ 状态: (无法格式化时间)\n"
                # --- 修改结束 ---

            # 显示固定信息
            api_keys_count = len(self.config.get('gemini.api_keys', [])); gemini_emoji = "✨"
            reply += f"{gemini_emoji} **Gemini API**: 已配置 {api_keys_count} 个密钥\n"
            target_chat_id = self.config.get('telegram.target_chat_id'); control_chat_id = self.config.get('telegram.control_chat_id')
            reply += f"🎮 **游戏群 ID**: `{target_chat_id}`\n"; reply += f"⚙️ **控制群 ID**: `{control_chat_id}`\n"
            reply += "\n使用 `,配置 <配置项名称> <新值>` 进行设置。"
            await self._edit_or_reply(message.chat.id, edit_target_id, reply, original_message=message)

    # --- (修改: handle_log_command 已移至 log_plugin.py) ---
    # async def handle_log_command(self, message: Message, args: str | None, edit_target_id: int | None):
    #     pass # 逻辑已移动

    async def handle_loglevel_command(self, message: Message, args: str | None, edit_target_id: int | None):
        self.info(f"收到旧的 ,日志级别 指令，转发给 handle_config_command 处理...")
        new_args = f"日志级别 {args}" if args else "日志级别"
        await self.handle_config_command(message, new_args, edit_target_id)

    # --- (新增: 处理任务列表指令) ---
    async def handle_show_tasks_command(self, message: Message, edit_target_id: int | None):
        """处理 ,任务列表 指令"""
        self.info("处理 ,任务列表 指令...")
        scheduler = self.context.scheduler
        plugin_name_map = self.context.plugin_name_map

        if not scheduler or not scheduler.running:
            await self._edit_or_reply(message.chat.id, edit_target_id, "❌ 错误：调度器未运行或不可用。", original_message=message)
            return

        reply = "🕒 **当前定时任务列表** 🕒\n"
        jobs = []
        try:
             jobs = scheduler.get_jobs()
        except Exception as e:
             self.error(f"获取任务列表时出错: {e}", exc_info=True)
             await self._edit_or_reply(message.chat.id, edit_target_id, "❌ 获取任务列表时发生错误。", original_message=message)
             return

        if not jobs:
            reply += "\n_(当前没有正在运行的定时任务)_"
        else:
            job_details_list = []
            for job in sorted(jobs, key=lambda j: j.id):
                 job_details_list.append(await format_job_details(job, plugin_name_map))
            reply += "\n" + "\n\n".join(job_details_list)

        await self._edit_or_reply(message.chat.id, edit_target_id, reply, original_message=message)
    # --- (新增结束) ---

    async def _save_config(self, config_path_key: str, new_value) -> tuple[bool, str]:
        self.debug(f"尝试保存配置: {config_path_key} = {'[隐藏]' if 'cookie' in config_path_key.lower() else new_value}")
        keys = config_path_key.split('.')
        if not keys: return False, "无效的配置路径"
        try:
            current_level = self.config.config_data
            for i, key in enumerate(keys[:-1]):
                if key not in current_level or not isinstance(current_level.get(key), dict): current_level[key] = {}; self.debug(f"在内存配置中创建路径: {'.'.join(keys[:i+1])}")
                current_level = current_level[key]
            last_key = keys[-1]; current_level[last_key] = new_value
            self.info(f"内存配置已更新: {config_path_key} = {'[隐藏]' if 'cookie' in config_path_key.lower() else new_value}")
            current_yaml_data = {}
            if os.path.exists(CONFIG_PATH):
                try:
                    with open(CONFIG_PATH, 'r', encoding='utf-8') as f_read: current_yaml_data = yaml.safe_load(f_read) or {}
                    if not isinstance(current_yaml_data, dict): self.warning(f"配置文件 {CONFIG_PATH} 格式无效，将覆盖为空字典。"); current_yaml_data = {}
                except Exception as e_read: self.error(f"读取配置文件 {CONFIG_PATH} 失败: {e_read}", exc_info=True); return False, f"读取配置文件失败: {e_read}"
            else: self.warning(f"配置文件 {CONFIG_PATH} 不存在，将创建新文件。"); current_yaml_data = {}
            current_level_yaml = current_yaml_data
            for i, key in enumerate(keys[:-1]):
                if key not in current_level_yaml or not isinstance(current_level_yaml.get(key), dict): current_level_yaml[key] = {}
                current_level_yaml = current_level_yaml[key]
            current_level_yaml[last_key] = new_value
            try:
                with open(CONFIG_PATH, 'w', encoding='utf-8') as f_write: yaml.dump(current_yaml_data, f_write, allow_unicode=True, sort_keys=False, indent=2)
                self.info(f"配置文件 {CONFIG_PATH} 已成功写入。"); return True, "配置已成功保存。"
            except Exception as e_write: self.error(f"写入配置文件 {CONFIG_PATH} 失败: {e_write}", exc_info=True); return False, f"写入配置文件失败: {e_write}"
        except Exception as e: self.error(f"更新配置时发生意外错误 ({config_path_key}): {e}", exc_info=True); return False, f"发生意外错误: {e}"

    async def _toggle_cultivation_internal(self, message: Message, new_state: bool, edit_target_id: int | None):
        action_text = "开启" if new_state else "关闭"; self.info(f"尝试 {action_text} 自动闭关...")
        if not CULTIVATION_PLUGIN_LOADED:
             self.error(f"无法{action_text}：自动闭关插件 (cultivation_plugin) 未加载。")
             await self._edit_or_reply(message.chat.id, edit_target_id, f"❌ 无法{action_text}：自动闭关插件 (cultivation_plugin) 未加载。", original_message=message); return
        save_success, save_msg = await self._save_config('cultivation.auto_enabled', new_state)
        if save_success:
            event_name = "start_auto_cultivation" if new_state else "stop_auto_cultivation"; self.info(f"触发事件: {event_name}"); await self.event_bus.emit(event_name); reply = f"✅ 自动闭关已 **{action_text}**。"
        else: reply = f"❌ 切换自动闭关状态失败: {save_msg}"; self.error(f"切换自动闭关状态失败: {save_msg}")
        await self._edit_or_reply(message.chat.id, edit_target_id, reply, original_message=message)

    async def _edit_or_reply(self, chat_id: int, message_id: int | None, text: str, original_message: Message):
        tg_client = self.telegram_client_instance
        if not tg_client or not tg_client.app.is_connected: self.error("无法编辑/回复：TG 客户端不可用。"); return
        edited = False; link_preview_options = LinkPreviewOptions(is_disabled=True); MAX_LEN = 4096
        if len(text) > MAX_LEN: self.warning(f"即将发送/编辑的消息过长 ({len(text)} > {MAX_LEN})，将被截断。"); text = text[:MAX_LEN - 15] + "\n...(消息过长截断)"
        if message_id:
            try: await tg_client.app.edit_message_text(chat_id, message_id, text, link_preview_options=link_preview_options); edited = True
            except Exception as e:
                if "MESSAGE_NOT_MODIFIED" not in str(e): self.warning(f"编辑消息 {message_id} 失败 ({e})，尝试回复..."); edited = False
                else: self.debug(f"消息 {message_id} 未修改。"); edited = True
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
        if not tg_client or not tg_client.app.is_connected: self.warning("无法发送状态消息：TG 客户端不可用。"); return None
        reply_params = ReplyParameters(message_id=original_message.id); link_preview_options = LinkPreviewOptions(is_disabled=True)
        try: return await tg_client.app.send_message(original_message.chat.id, status_text, reply_parameters=reply_params, link_preview_options=link_preview_options)
        except Exception as e:
            self.warning(f"回复状态消息失败 ({e})，尝试直接发送...")
            try: return await tg_client.app.send_message(original_message.chat.id, status_text, link_preview_options=link_preview_options)
            except Exception as e2: self.error(f"直接发送状态消息也失败: {e2}"); return None
