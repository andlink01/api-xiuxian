# sphinx_doc_embed_ignore_end
import logging
import asyncio
import json
import pytz
from datetime import datetime
from pyrogram.types import Message, ReplyParameters, LinkPreviewOptions
from plugins.base_plugin import AppContext # 导入 AppContext
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.job import Job
from typing import TYPE_CHECKING, Tuple, Optional, Any, Dict # 引入 Dict

# --- (修改: 导入 format_local_time) ---
try:
    from plugins.character_sync_plugin import format_local_time
except ImportError:
    # 提供一个备用实现或记录错误
    logger = logging.getLogger("PluginUtils")
    logger.error("无法从 character_sync_plugin 导入 format_local_time！")
    def format_local_time(dt: datetime | None) -> str | None:
         if dt is None: return None
         try: # 尝试基本的本地化
             if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
                 dt = pytz.utc.localize(dt)
             local_dt = dt.astimezone()
             return local_dt.strftime("%Y-%m-%d %H:%M:%S %Z%z")
         except Exception: return str(dt) # 失败则返回原始字符串
# --- (修改结束) ---


if TYPE_CHECKING:
    from plugins.base_plugin import BasePlugin

# 获取一个独立的 logger
logger = logging.getLogger("PluginUtils")

async def get_my_id(plugin: 'BasePlugin', message: Message, edit_target_id: int | None) -> int | None:
    my_id = None
    tg_client = plugin.context.telegram_client
    if tg_client and tg_client.app.is_connected:
         my_id = tg_client._my_id
         if not my_id:
              try: my_id = await tg_client.get_my_id()
              except Exception as e: plugin.warning(f"获取助手 User ID 时出错: {e}"); my_id = None
    if not my_id:
         plugin.error("无法获取助手 User ID (TG客户端未连接或获取失败/缓存为空?)")
         await edit_or_reply(plugin, message.chat.id, edit_target_id, "❌ 错误：无法获取助手 User ID，无法执行操作。", original_message=message)
    return my_id


async def get_redis_ttl_and_value(plugin: 'BasePlugin', redis_key: str) -> tuple[int | None, str | None]:
    redis_client = plugin.context.redis.get_client()
    value = None
    ttl = None
    if redis_client:
        try:
            async with redis_client.pipeline(transaction=False) as pipe:
                pipe.get(redis_key)
                pipe.ttl(redis_key)
                results = await pipe.execute()
            value = results[0]
            ttl = results[1] if results[1] >= 0 else None
        except Exception as e: plugin.error(f"访问 Redis (Key: {redis_key}) 出错: {e}", exc_info=True)
    else: plugin.error(f"无法访问 Redis (Key: {redis_key})：Redis 客户端不可用。")
    return ttl, value


async def edit_or_reply(plugin: 'BasePlugin', chat_id: int, message_id: int | None, text: str, original_message: Message):
    tg_client = plugin.context.telegram_client
    if not tg_client or not tg_client.app.is_connected:
         plugin.error("无法编辑/回复：TG 客户端不可用。")
         return
    edited = False
    link_preview_options = LinkPreviewOptions(is_disabled=True)
    MAX_LEN = 4096
    if len(text) > MAX_LEN:
        plugin.warning(f"即将发送/编辑的消息过长 ({len(text)} > {MAX_LEN})，将被截断。")
        text = text[:MAX_LEN - 15] + "\n...(消息过长截断)"
    if message_id:
        try:
            await tg_client.app.edit_message_text(chat_id, message_id, text, link_preview_options=link_preview_options)
            edited = True
        except Exception as e:
            if "MESSAGE_NOT_MODIFIED" not in str(e):
                plugin.warning(f"编辑消息 {message_id} 失败 ({e})，尝试回复...")
                edited = False
            else: plugin.debug(f"消息 {message_id} 未修改。"); edited = True
    if not edited:
        if not original_message:
             plugin.error("编辑失败且无法回复：缺少原始消息对象。")
             fallback_chat_id = plugin.context.config.get("telegram.control_chat_id") or plugin.context.config.get("telegram.admin_id")
             if fallback_chat_id:
                 try: await tg_client.app.send_message(fallback_chat_id, f"(Edit/Reply Failed)\n{text[:1000]}...", link_preview_options=link_preview_options)
                 except Exception as final_err: plugin.critical(f"最终 fallback 发送失败: {final_err}")
             return
        try:
            reply_params = ReplyParameters(message_id=original_message.id)
            await tg_client.app.send_message(chat_id, text, reply_parameters=reply_params, link_preview_options=link_preview_options)
        except Exception as e2:
            plugin.error(f"编辑和回复均失败: {e2}")
            fallback_chat_id = plugin.context.config.get("telegram.control_chat_id") or plugin.context.config.get("telegram.admin_id")
            if fallback_chat_id:
                try: await tg_client.app.send_message(fallback_chat_id, f"(Edit/Reply Failed)\n{text[:1000]}...", link_preview_options=link_preview_options)
                except Exception as final_err: plugin.critical(f"最终 fallback 发送失败: {final_err}")


async def send_status_message(plugin: 'BasePlugin', original_message: Message, status_text: str) -> Message | None:
    tg_client = plugin.context.telegram_client
    if not tg_client or not tg_client.app.is_connected:
        plugin.warning("无法发送状态消息：TG 客户端不可用。")
        return None
    reply_params = ReplyParameters(message_id=original_message.id)
    link_preview_options = LinkPreviewOptions(is_disabled=True)
    try:
        return await tg_client.app.send_message(original_message.chat.id, status_text, reply_parameters=reply_params, link_preview_options=link_preview_options)
    except Exception as e:
        plugin.warning(f"回复状态消息失败 ({e})，尝试直接发送...")
        try: return await tg_client.app.send_message(original_message.chat.id, status_text, link_preview_options=link_preview_options)
        except Exception as e2: plugin.error(f"直接发送状态消息也失败: {e2}"); return None

# --- (新增: 格式化任务详情函数) ---
async def format_job_details(job: Job, plugin_name_map: Dict[str, str]) -> str:
    """格式化单个 APScheduler Job 的信息为中文描述"""
    job_id = job.id
    job_name = job.name or job_id # 如果没有名字，使用 ID
    next_run_str = "已暂停/已完成"
    if job.next_run_time:
         next_run_str = format_local_time(job.next_run_time) or str(job.next_run_time)

    trigger_info = "未知触发器"
    if isinstance(job.trigger, DateTrigger):
        trigger_info = "定时执行一次"
    elif isinstance(job.trigger, IntervalTrigger):
        interval_td: timedelta = job.trigger.interval
        total_seconds = interval_td.total_seconds()
        if total_seconds < 60: interval_str = f"{int(total_seconds)}秒"
        elif total_seconds < 3600: interval_str = f"{int(total_seconds / 60)}分钟"
        else: interval_str = f"{total_seconds / 3600:.1f}小时"
        trigger_info = f"每隔 {interval_str}"
        if hasattr(job.trigger, 'jitter') and job.trigger.jitter:
             trigger_info += f" (±{job.trigger.jitter}秒)"
    elif isinstance(job.trigger, CronTrigger):
        # 尝试简化 Cron 表达式的描述
        try:
             cron_str = str(job.trigger) # 获取原始字符串表示
             # 提取常见部分 (可能不完美)
             hour = job.trigger.fields[5] if len(job.trigger.fields) > 5 else None
             minute = job.trigger.fields[6] if len(job.trigger.fields) > 6 else None
             if hour is not None and minute is not None and str(hour) != '*' and str(minute) != '*':
                  hour_str = str(hour).replace('Range[', '').replace(']', '')
                  minute_str = str(minute).replace('Range[', '').replace(']', '')
                  trigger_info = f"每天 {hour_str}:{minute_str} 左右" # 简化描述
             else: trigger_info = f"Cron: {cron_str}"
        except Exception: trigger_info = f"Cron 触发器"

    # 尝试从 job_id 或 job.func_ref 获取插件信息
    plugin_guess = job_id.split('_job')[0] if '_job' in job_id else job_id
    cn_name_guess = plugin_name_map.get(f"{plugin_guess}_plugin", plugin_guess) # 尝试匹配中文名

    return (f"**任务ID:** `{job_id}`\n"
            f"  **名称/插件:** {job_name} ({cn_name_guess})\n"
            f"  **下次运行:** {next_run_str}\n"
            f"  **触发方式:** {trigger_info}")
# --- (新增结束) ---

# sphinx_doc_embed_ignore_start
# E O F marker, must be the last line
