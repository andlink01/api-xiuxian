import logging
import asyncio
import random
import json
import pytz
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
from plugins.base_plugin import BasePlugin, AppContext
from core.context import get_global_context
from apscheduler.jobstores.base import JobLookupError
from plugins.character_sync_plugin import parse_iso_datetime, format_local_time # 导入时间处理
from pyrogram.types import Message # <--- 导入 Message

logger = logging.getLogger(__name__)

# --- 常量 ---
STAR_PLATFORM_JOB_ID = "star_platform_check_job"
ACTION_LOCK_KEY_FORMAT = "star_platform:action_lock:{}" # 操作锁
ACTION_LOCK_TTL = 300 # 锁 TTL (5分钟)

# Redis Keys for State Machine (similar to herb garden)
CMD_LIST_KEY_PREFIX = "star_platform:cmd_list:"
CMD_INDEX_KEY_PREFIX = "star_platform:cmd_index:"
PENDING_MSG_ID_KEY_PREFIX = "star_platform:pending_msgid:"
PENDING_COMMAND_KEY_PREFIX = "star_platform:pending_cmd:"
STATE_TTL = 360 # 指令列表和索引 TTL (6分钟)

RESPONSE_TIMEOUT = 120 # 等待响应超时 (秒)
TIMEOUT_JOB_ID_PREFIX = "star_platform_timeout:" # 超时任务ID前缀

TARGET_SECT_NAME = "星宫" # 目标宗门

# 星辰信息 (名称 -> 耗时(小时))
STAR_INFO = {
    "赤血星": 4,
    "庚金星": 6,
    "建木星": 8,
    "天雷星": 24,
    "帝魂星": 48,
}

# --- 辅助函数 ---
def calculate_remaining_time(start_time_str: Optional[str], duration_hours: int) -> Optional[timedelta]:
    """计算凝聚剩余时间"""
    if not start_time_str: return None
    start_dt = parse_iso_datetime(start_time_str)
    if not start_dt: return None
    end_dt = start_dt + timedelta(hours=duration_hours)
    now_utc = datetime.now(pytz.utc)
    remaining = end_dt - now_utc
    return remaining if remaining > timedelta(0) else timedelta(0)

async def _clear_star_platform_state(redis_client, user_id: int, scheduler, release_lock: bool = True):
    """清理观星台相关的 Redis 状态和超时任务"""
    cleanup_logger = logging.getLogger("StarPlatformPlugin.Cleanup")
    cleanup_logger.info(f"开始清理用户 {user_id} 的观星台状态...")
    list_key = f"{CMD_LIST_KEY_PREFIX}{user_id}"
    index_key = f"{CMD_INDEX_KEY_PREFIX}{user_id}"
    pending_msg_key = f"{PENDING_MSG_ID_KEY_PREFIX}{user_id}"
    pending_cmd_key = f"{PENDING_COMMAND_KEY_PREFIX}{user_id}"
    keys_to_delete = [list_key, index_key, pending_msg_key, pending_cmd_key]
    lock_key = ACTION_LOCK_KEY_FORMAT.format(user_id) if user_id else None

    try:
        if redis_client:
            deleted_count = await redis_client.delete(*keys_to_delete)
            cleanup_logger.info(f"已从 Redis 清理 {deleted_count} 个观星台状态键。")
        else:
            cleanup_logger.error("无法清理 Redis 状态：Redis 客户端无效。")

        if scheduler:
            jobs_removed = 0
            try:
                matching_jobs = [job.id for job in scheduler.get_jobs() if job.id.startswith(f"{TIMEOUT_JOB_ID_PREFIX}{user_id}:")]
                cleanup_logger.debug(f"找到 {len(matching_jobs)} 个匹配的超时任务: {matching_jobs}")
                for job_id in matching_jobs:
                    try:
                        await asyncio.to_thread(scheduler.remove_job, job_id)
                        jobs_removed += 1
                        cleanup_logger.debug(f"已移除超时任务: {job_id}")
                    except JobLookupError: pass
                    except Exception as e_rem_job: cleanup_logger.warning(f"移除超时任务 {job_id} 时出错: {e_rem_job}")
                if jobs_removed > 0: cleanup_logger.info(f"已移除 {jobs_removed} 个观星台超时任务。")
            except Exception as e_get_jobs: cleanup_logger.error(f"获取或移除超时任务时出错: {e_get_jobs}")
        else: cleanup_logger.warning("无法移除超时任务：Scheduler 无效。")

        if release_lock and redis_client and lock_key:
            try:
                deleted_lock = await redis_client.delete(lock_key)
                if deleted_lock > 0: cleanup_logger.info(f"已释放观星台操作锁 ({lock_key})。")
            except Exception as e_lock: cleanup_logger.error(f"释放观星台操作锁 ({lock_key}) 时出错: {e_lock}")

    except Exception as e_clean:
        cleanup_logger.error(f"清理观星台状态时发生意外错误: {e_clean}", exc_info=True)


async def _handle_star_platform_timeout(user_id: int, expected_msg_id: int):
    """处理等待观星台操作响应超时"""
    timeout_logger = logging.getLogger("StarPlatformPlugin.Timeout")
    timeout_logger.warning(f"【自动观星台】超时任务触发：检查 MsgID {expected_msg_id} 的响应是否超时...")
    context = get_global_context()
    if not context or not context.redis or not context.event_bus or not context.scheduler:
        timeout_logger.error("【自动观星台】无法处理超时：核心服务不可用。")
        return

    redis_client = context.redis.get_client()
    if not redis_client: timeout_logger.error("【自动观星台】无法处理超时：Redis 未连接。"); return

    pending_msg_key = f"{PENDING_MSG_ID_KEY_PREFIX}{user_id}"
    try:
        current_pending_msg_id_str = await redis_client.get(pending_msg_key)
        if current_pending_msg_id_str and current_pending_msg_id_str.isdigit() and int(current_pending_msg_id_str) == expected_msg_id:
            timeout_logger.warning(f"【自动观星台】确认超时！等待 MsgID {expected_msg_id} 的响应超时。")
            await _clear_star_platform_state(redis_client, user_id, context.scheduler, release_lock=True)
            timeout_logger.info("【自动观星台】超时后触发角色数据同步...")
            await context.event_bus.emit("trigger_character_sync_now")
        else:
            timeout_logger.info(f"【自动观星台】超时任务触发，但当前等待MsgID是 '{current_pending_msg_id_str}' (不是 {expected_msg_id}) 或无等待，忽略。")
    except ValueError:
        timeout_logger.error(f"【自动观星台】Redis 中的 pending MsgID '{current_pending_msg_id_str}' 无效，清理状态。")
        await _clear_star_platform_state(redis_client, user_id, context.scheduler, release_lock=True)
        await context.event_bus.emit("trigger_character_sync_now")
    except Exception as e:
        timeout_logger.error(f"【自动观星台】处理观星台响应超时状态时出错: {e}", exc_info=True)
        await _clear_star_platform_state(redis_client, user_id, context.scheduler, release_lock=True) # 强制清理
        await context.event_bus.emit("trigger_character_sync_now")

async def _check_star_platform_task():
    """由 APScheduler 调度的顶层函数，用于检查和管理观星台"""
    task_logger = logging.getLogger("StarPlatformPlugin.Task")
    task_logger.info("【自动观星台】任务启动，开始检查观星台状态...")

    context = get_global_context()
    if not context or not context.data_manager or not context.redis or not context.telegram_client:
        task_logger.error("【自动观星台】无法获取核心服务，任务终止。")
        return

    config = context.config
    redis_client = context.redis.get_client()
    data_manager = context.data_manager
    my_id = context.telegram_client._my_id

    if not config.get("star_platform.enabled", False):
        task_logger.info("【自动观星台】功能未启用，跳过检查。")
        return

    if not my_id:
        task_logger.warning("【自动观星台】无法获取助手 User ID，任务终止。")
        return

    lock_acquired = False
    lock_key = ACTION_LOCK_KEY_FORMAT.format(my_id)

    if redis_client:
        try:
            task_logger.info(f"【自动观星台】尝试获取 Redis 操作锁 ({lock_key})...")
            lock_acquired = await redis_client.set(lock_key, "1", ex=ACTION_LOCK_TTL, nx=True)
            if not lock_acquired:
                task_logger.info(f"【自动观星台】获取操作锁 ({lock_key}) 失败，上次操作序列可能仍在进行中，跳过本次检查。")
                return
            task_logger.info(f"【自动观星台】成功获取 Redis 操作锁 ({lock_key})。")

            list_key = f"{CMD_LIST_KEY_PREFIX}{my_id}"
            if await redis_client.exists(list_key):
                task_logger.info(f"【自动观星台】检测到用户 {my_id} 存在未完成的操作序列，跳过本次检查。")
                try: await redis_client.delete(lock_key)
                except Exception as del_err: task_logger.warning(f"释放锁 {lock_key} 失败: {del_err}")
                lock_acquired = False # 标记锁已释放
                return
        except Exception as e:
            task_logger.error(f"【自动观星台】检查或设置 Redis 锁/状态失败: {e}，为安全起见跳过本次检查。")
            if lock_acquired:
                 try: await redis_client.delete(lock_key)
                 except Exception as del_err: task_logger.warning(f"异常后释放锁 {lock_key} 失败: {del_err}")
            return
    else:
        task_logger.error("【自动观星台】Redis 未连接，无法检查锁/状态，任务终止。")
        return

    commands_to_send: List[str] = []

    try:
        task_logger.info("【自动观星台】正在通过 DataManager 强制获取宗门和观星台数据...")
        # 强制刷新 sect 和 star_platform 缓存
        sect_info = await data_manager.get_sect_info(my_id, use_cache=False)
        star_platform_data = await data_manager.get_star_platform(my_id, use_cache=False) # 使用新方法

        if not sect_info:
            task_logger.error("【自动观星台】无法从 API 获取宗门信息，任务终止。")
            return # finally 会释放锁
        if not star_platform_data:
            task_logger.error("【自动观星台】无法从 API 获取观星台数据，任务终止。")
            return # finally 会释放锁
        task_logger.info("【自动观星台】获取实时数据完成。")

        sect_name = sect_info.get("sect_name")
        if sect_name != TARGET_SECT_NAME:
            task_logger.info(f"检测到宗门为 '{sect_name}' (不是 {TARGET_SECT_NAME})，功能禁用。")
            try:
                if context.scheduler: await asyncio.to_thread(context.scheduler.remove_job, STAR_PLATFORM_JOB_ID)
                task_logger.info("已移除自动观星台定时任务。")
            except JobLookupError: task_logger.info("定时任务已不存在。")
            except Exception as e: task_logger.error(f"移除定时任务时出错: {e}")
            return # finally 会释放锁

        # 解析观星台数据
        size = star_platform_data.get("size", 0)
        plots = star_platform_data.get("plots")
        if not isinstance(plots, dict):
             task_logger.error("观星台地块数据格式不正确 (不是字典)，任务终止。")
             return # finally 会释放锁

        plot_statuses: Dict[str, Dict[str, Any]] = {} # plot_id -> {status, name, remaining_td, ...}
        empty_plot_ids: List[str] = []
        ready_plot_ids: List[str] = []
        abnormal_plot_ids: List[str] = [] # 星光黯淡 / 元磁紊乱

        # --- 优化: 状态集合 ---
        COLLECTIBLE_STATUSES = {"精华已成", "可收集"}
        ABNORMAL_STATUSES = {"星光黯淡", "元磁紊乱"}
        # --- 优化结束 ---

        for plot_id_str in range(1, size + 1):
            plot_id = str(plot_id_str)
            plot_info = plots.get(plot_id)
            if plot_info and isinstance(plot_info, dict):
                 status = plot_info.get("status")
                 if status in COLLECTIBLE_STATUSES:
                     ready_plot_ids.append(plot_id)
                 elif status in ABNORMAL_STATUSES:
                     abnormal_plot_ids.append(plot_id)
                 elif status == "凝聚中":
                     star_name = plot_info.get("star_name")
                     start_time = plot_info.get("start_time")
                     duration = STAR_INFO.get(star_name, 0) if star_name else 0
                     remaining_td = calculate_remaining_time(start_time, duration) if status == "凝聚中" else None
                     plot_statuses[plot_id] = {"status": status, "star_name": star_name, "remaining_td": remaining_td}
                 else: # 其他未知状态视为可牵引
                      task_logger.warning(f"地块 {plot_id} 状态未知: '{status}'，视为可牵引。")
                      empty_plot_ids.append(plot_id)
                      plot_statuses[plot_id] = {"status": "空闲"}
            else: # 空闲地块
                empty_plot_ids.append(plot_id)
                plot_statuses[plot_id] = {"status": "空闲"}

        task_logger.info(f"【自动观星台】状态分析: 共{size}盘 | {len(empty_plot_ids)}空闲 | {len(ready_plot_ids)}可收 | {len(abnormal_plot_ids)}异变")

        soothe_cmds = []; collect_cmds = []; attract_cmds = []
        soothe_priority = config.get("star_platform.soothe_priority", True)
        collect_priority = config.get("star_platform.collect_priority", True)

        # 1. 处理异变 (安抚)
        if abnormal_plot_ids:
            cmd = ".安抚星辰"
            if soothe_priority:
                if cmd not in commands_to_send: commands_to_send.append(cmd)
                task_logger.info(f"决策 (优先): 添加指令 '{cmd}' ({len(abnormal_plot_ids)} 块异变)")
            else:
                if cmd not in soothe_cmds: soothe_cmds.append(cmd)
                task_logger.info(f"待办: 添加 '{cmd}' ({len(abnormal_plot_ids)} 块异变)")

        # 2. 处理成熟 (收集)
        if ready_plot_ids:
            cmd = ".收集精华"
            if collect_priority:
                if cmd not in commands_to_send: commands_to_send.append(cmd)
                task_logger.info(f"决策 (优先): 添加指令 '{cmd}' ({len(ready_plot_ids)} 块可收)")
            else:
                 if cmd not in collect_cmds: collect_cmds.append(cmd)
                 task_logger.info(f"待办: 添加 '{cmd}' ({len(ready_plot_ids)} 块可收)")

        # 将非优先的指令按收集->安抚顺序加入 (确保不重复)
        for cmd in collect_cmds:
             if cmd not in commands_to_send: commands_to_send.append(cmd)
        for cmd in soothe_cmds:
             if cmd not in commands_to_send: commands_to_send.append(cmd)

        # --- 修复: 移除 'if not has_ready_or_abnormal' ---
        # 3. 处理空闲 (牵引) - 总是检查
        if empty_plot_ids:
        # --- 修复结束 ---
            attract_priority_list = config.get("star_platform.attract_priority", [])
            is_elder = sect_info.get("is_sect_elder", 0) or sect_info.get("is_grand_elder", 0)
            has_ztp = False
            inv_data = await data_manager.get_inventory(my_id, use_cache=True) # 读取缓存即可
            if inv_data and isinstance(inv_data.get("items_by_type"), dict):
                 treasure_list = inv_data.get("items_by_type", {}).get("treasure", [])
                 if isinstance(treasure_list, list):
                     for item in treasure_list:
                          if isinstance(item, dict) and item.get("item_id") == "treasure_xt_004": # 掌天瓶 ID
                              has_ztp = True; break
            if has_ztp: task_logger.info("检测到持有掌天瓶，无视牵引身份限制。")

            for star_name in attract_priority_list:
                can_attract = False
                if star_name == "天雷星": can_attract = is_elder or has_ztp
                elif star_name == "帝魂星": can_attract = (is_elder and has_ztp) # 简化
                elif star_name in STAR_INFO: can_attract = True

                if can_attract:
                    # 找到一个可牵引的星辰就执行并跳出
                    cmd = f".牵引星辰 {star_name}" # <-- 移除 plot_id
                    if cmd not in attract_cmds: attract_cmds.append(cmd)
                    task_logger.info(f"决策: 添加指令 '{cmd}' (目标 {len(empty_plot_ids)} 个空闲盘)")
                    break # 每次检查只尝试牵引一个优先级最高的
                else:
                    task_logger.debug(f"跳过牵引 '{star_name}'：权限不足 (长老:{is_elder}, 掌天瓶:{has_ztp})")
        
        # --- 修复: 总是将 attract_cmds 附加到最后 ---
        commands_to_send.extend(attract_cmds)
        # --- 修复结束 ---

        # --- 执行指令序列 (逻辑不变) ---
        if commands_to_send:
            task_logger.info(f"【自动观星台】本周期生成指令序列: {', '.join(commands_to_send)}")
            if redis_client and my_id:
                list_key = f"{CMD_LIST_KEY_PREFIX}{my_id}"
                index_key = f"{CMD_INDEX_KEY_PREFIX}{my_id}"
                async with redis_client.pipeline(transaction=True) as pipe:
                    pipe.delete(list_key); pipe.rpush(list_key, *commands_to_send)
                    pipe.set(index_key, "0", ex=STATE_TTL); pipe.expire(list_key, STATE_TTL)
                    await pipe.execute()
                task_logger.info(f"指令序列和初始索引已存入 Redis (TTL: {STATE_TTL}s)。")

                first_command = commands_to_send[0]
                task_logger.info(f"准备发送序列中的第一个指令: '{first_command}'")
                success = await context.telegram_client.send_game_command(first_command)
                if success:
                    task_logger.info(f"第一个指令 '{first_command}' 已成功加入队列。等待响应...")
                    lock_acquired = False # 锁不由 finally 块释放
                else:
                     task_logger.error(f"发送第一个指令 '{first_command}' 失败！清理状态并释放锁。")
                     await _clear_star_platform_state(redis_client, my_id, context.scheduler, release_lock=True)
                     lock_acquired = False
            else:
                task_logger.error("无法启动指令序列：Redis 或 User ID 不可用。")
                lock_acquired = True # 标记需要 finally 释放
        else:
            task_logger.info("【自动观星台】本周期无操作需要执行。")
            lock_acquired = True # 标记需要 finally 释放

    except Exception as outer_e:
         task_logger.error(f"【自动观星台】在 _check_star_platform_task 主逻辑中发生意外错误: {outer_e}", exc_info=True)
         lock_acquired = True # 标记需要 finally 释放

    finally:
        if lock_acquired and redis_client:
            try:
                task_logger.info(f"【自动观星台】检查结束或出错，释放 Redis 操作锁 ({lock_key})...")
                deleted = await redis_client.delete(lock_key)
                if deleted: task_logger.info("操作锁已释放。")
            except Exception as e_lock_final:
                task_logger.error(f"【自动观星台】释放 Redis 锁 ({lock_key}) 时出错: {e_lock_final}")
        elif not lock_acquired and redis_client: # 锁已被序列持有
             try:
                 list_key_final = f"{CMD_LIST_KEY_PREFIX}{my_id}"
                 if await redis_client.exists(list_key_final):
                      task_logger.info(f"【自动观星台】任务结束，操作锁 ({lock_key}) 由正在执行的指令序列持有。")
             except Exception as e_check:
                  task_logger.warning(f"检查指令列表是否存在时出错: {e_check}")


# --- 插件类 ---
class Plugin(BasePlugin):
    """
    自动观星台插件 (星宫专属)。
    """
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name or "自动观星台")
        self.load_config()
        self._my_id: Optional[int] = None

        if self.config_enabled:
            self.info(f"插件已加载并启用。检查间隔: {self.check_interval} 分钟。")
        else:
            self.info("插件已加载但未启用 (请在 config.yaml 中设置 star_platform.enabled: true)。")

    def load_config(self):
        self.config_enabled = self.config.get("star_platform.enabled", False)
        self.check_interval = self.config.get("star_platform.check_interval_minutes", 5)

    def register(self):
        """注册定时检查任务和事件监听器"""
        if not self.config_enabled:
            return

        if self.check_interval < 1:
            self.check_interval = 1
            self.warning("观星台检查间隔 'check_interval_minutes' 不能小于 1，已重置为 1 分钟。")

        try:
            if self.scheduler:
                self.scheduler.add_job(
                    _check_star_platform_task, trigger='interval', minutes=self.check_interval,
                    id=STAR_PLATFORM_JOB_ID, replace_existing=True, misfire_grace_time=60
                )
                self.info(f"已注册观星台定时检查任务 (每 {self.check_interval} 分钟)。")
                self.event_bus.on("telegram_client_started", self._initialize_id)
                self.event_bus.on("game_command_sent", self.handle_command_sent)
                self.event_bus.on("game_response_received", self.handle_game_response)
                self.info("已注册观星台相关的 game_command_sent 和 game_response_received 事件监听器。")
            else:
                 self.error("无法注册观星台定时任务或监听器：Scheduler 不可用。")
        except Exception as e:
            self.error(f"注册观星台定时任务或监听器时出错: {e}", exc_info=True)

    async def _initialize_id(self):
        if self.context.telegram_client:
             self._my_id = await self.context.telegram_client.get_my_id()
             self.info(f"已缓存当前 User ID: {self._my_id}")

    async def handle_command_sent(self, sent_message: Message, command_text: str):
        """监听观星台指令发送成功，设置等待状态和超时"""
        # 检查是否是观星台相关指令 (基础指令)
        cmd_base = command_text.split(" ")[0]
        if cmd_base not in [".观星台", ".牵引星辰", ".安抚星辰", ".收集精华", ".扩建星台"]:
             return

        if not self._my_id:
             self._my_id = await self.context.telegram_client.get_my_id()
             if not self._my_id: self.error("无法获取 User ID"); return
        redis_client = self.redis.get_client()
        if not redis_client: self.error("Redis 未连接"); return

        list_key = f"{CMD_LIST_KEY_PREFIX}{self._my_id}"
        index_key = f"{CMD_INDEX_KEY_PREFIX}{self._my_id}"
        pending_msg_key = f"{PENDING_MSG_ID_KEY_PREFIX}{self._my_id}"
        pending_cmd_key = f"{PENDING_COMMAND_KEY_PREFIX}{self._my_id}"

        try:
            current_index_str = await redis_client.get(index_key)
            if current_index_str is None: return # 不在序列中

            current_index = int(current_index_str)
            commands_in_list = await redis_client.lrange(list_key, 0, -1)
            if not commands_in_list or current_index >= len(commands_in_list):
                 self.warning(f"指令 '{command_text}' 发送，但 Redis 列表为空或索引 ({current_index}) 越界！清理状态。")
                 await _clear_star_platform_state(redis_client, self._my_id, self.scheduler, release_lock=True)
                 return

            expected_command = commands_in_list[current_index]
            # --- 修改: 比较基础指令部分 ---
            if command_text.split(" ")[0] != expected_command.split(" ")[0]:
                 self.debug(f"发送的指令 '{command_text}' 与期望的 '{expected_command}' (基础部分) 不符，忽略。")
                 return
            # --- 修改结束 ---

            self.info(f"【自动观星台】监听到序列指令 '{command_text}' 已发送 (MsgID: {sent_message.id})，设置等待状态和超时。")

            timeout_job_id = f"{TIMEOUT_JOB_ID_PREFIX}{self._my_id}:{sent_message.id}"
            timeout_seconds = RESPONSE_TIMEOUT

            async with redis_client.pipeline(transaction=True) as pipe:
                pipe.set(pending_msg_key, str(sent_message.id), ex=timeout_seconds + 60)
                pipe.set(pending_cmd_key, expected_command, ex=timeout_seconds + 60) # 存储期望的完整指令
                await pipe.execute()
            self.info(f"Redis 等待状态已设置 (MsgID: {sent_message.id}, Cmd: '{expected_command}')。")

            if self.scheduler:
                run_at = datetime.now(pytz.utc) + timedelta(seconds=timeout_seconds)
                self.scheduler.add_job(
                    _handle_star_platform_timeout, trigger='date', run_date=run_at,
                    args=[self._my_id, sent_message.id],
                    id=timeout_job_id, replace_existing=True, misfire_grace_time=10
                )
                self.info(f"已安排超时检查任务 '{timeout_job_id}'。")
            else:
                 self.error("无法安排超时任务：Scheduler 不可用。清理状态。")
                 await _clear_star_platform_state(redis_client, self._my_id, self.scheduler, release_lock=True)

        except ValueError:
             self.error(f"Redis 中的索引 '{current_index_str}' 无效！清理状态。")
             await _clear_star_platform_state(redis_client, self._my_id, self.scheduler, release_lock=True)
        except Exception as e:
            self.error(f"处理指令发送事件时出错: {e}", exc_info=True)
            await _clear_star_platform_state(redis_client, self._my_id, self.scheduler, release_lock=True)


    async def handle_game_response(self, message: Message, is_reply_to_me: bool, is_mentioning_me: bool):
        """处理游戏响应，推进观星台操作序列"""
        if not self.config_enabled or not is_reply_to_me: return
        text = message.text or message.caption
        if not text: return
        if not self._my_id: self.error("无法获取 User ID"); return
        redis_client = self.redis.get_client()
        if not redis_client: self.error("Redis 未连接"); return

        pending_msg_key = f"{PENDING_MSG_ID_KEY_PREFIX}{self._my_id}"
        pending_cmd_key = f"{PENDING_COMMAND_KEY_PREFIX}{self._my_id}"
        list_key = f"{CMD_LIST_KEY_PREFIX}{self._my_id}"
        index_key = f"{CMD_INDEX_KEY_PREFIX}{self._my_id}"
        expected_msg_id_str = None

        try:
            expected_msg_id_str = await redis_client.get(pending_msg_key)
            if not expected_msg_id_str or not expected_msg_id_str.isdigit(): return
            expected_msg_id = int(expected_msg_id_str)
            if message.reply_to_message_id != expected_msg_id: return

            pending_command = await redis_client.get(pending_cmd_key)
            if not pending_command:
                self.warning(f"匹配到 MsgID {expected_msg_id}，但无法获取等待的指令内容！清理状态。")
                await _clear_star_platform_state(redis_client, self._my_id, self.scheduler, release_lock=True)
                return

            self.info(f"【自动观星台】收到对指令 '{pending_command}' (MsgID: {expected_msg_id}) 的回复，检查结果...")

            # --- 结果判断 (根据用户提供的示例更新关键词) ---
            is_success = False
            is_no_need = False # 例如：没有需要安抚/收集的地块
            is_fail = False    # 例如：贡献不足，权限不足
            result_type = "unknown"

            cmd_base = pending_command.split(" ")[0]

            if cmd_base == ".收集精华":
                if "收集完成！" in text and "成功从" in text and "获得了" in text: # 示例 2, 3, 4
                    is_success = True; result_type = "成功"
                elif "没有已凝聚成形的星辰精华可供收集" in text: # 示例 1
                    is_no_need = True; result_type = "无需操作"
                else: # 其他情况（如失败）
                    is_fail = True; result_type = "失败 (未知原因)"
            elif cmd_base == ".安抚星辰":
                if "成功安抚了" in text and "引星盘的狂暴星力" in text: # 示例 2, 3
                    is_success = True; result_type = "成功"
                elif "没有需要安抚的星辰" in text: # 示例 1
                    is_no_need = True; result_type = "无需操作"
                elif "修为不足" in text: # 保留旧的失败判断
                    is_fail = True; result_type = "失败 (修为不足)"
                else: # 其他情况
                    is_fail = True; result_type = "失败 (未知原因)"
            elif cmd_base == ".牵引星辰":
                if "牵引成功！" in text and "成功在" in text and "牵引了" in text: # 示例 1, 2
                    is_success = True; result_type = "成功"
                elif "已被占用" in text or "无法牵引" in text or "没有空闲" in text: # 保留旧的无需操作判断
                    is_no_need = True; result_type = "无需操作/失败"
                elif "修为不足" in text or "权限不足" in text: # 保留旧的失败判断
                    is_fail = True; result_type = "失败 (修为/权限)"
                else: # 其他情况
                    is_fail = True; result_type = "失败 (未知原因)"
            elif cmd_base == ".扩建星台": # 保留示例
                 if "扩建成功" in text: is_success = True; result_type = "成功"
                 elif "贡献不足" in text: is_fail = True; result_type = "失败 (贡献不足)"
                 else: is_fail = True; result_type = "失败 (未知原因)"
            else: # 其他指令或未识别回复
                 self.warning(f"收到了对未知指令 '{cmd_base}' 或无法识别的回复，假定成功并尝试继续...")
                 is_success = True # 假定成功以尝试推进
                 result_type = "成功 (假定)"
            # --- 结果判断结束 ---


            log_method = self.info if is_success or is_no_need else self.warning
            log_method(f"指令 '{pending_command}' 执行结果: {result_type}")


            # 清理当前指令的等待状态和超时任务
            await redis_client.delete(pending_msg_key, pending_cmd_key)
            timeout_job_id = f"{TIMEOUT_JOB_ID_PREFIX}{self._my_id}:{expected_msg_id}"
            try:
                if self.scheduler: await asyncio.to_thread(self.scheduler.remove_job, timeout_job_id)
            except JobLookupError: pass
            except Exception as e_rem: self.warning(f"移除超时任务 '{timeout_job_id}' 失败: {e_rem}")

            if is_success or is_no_need:
                # 推进序列
                current_index_str = await redis_client.get(index_key)
                commands_in_list = await redis_client.lrange(list_key, 0, -1)
                if current_index_str is None or not commands_in_list:
                     self.warning("无法获取指令列表或索引，序列中断。清理状态。")
                     await _clear_star_platform_state(redis_client, self._my_id, self.scheduler, release_lock=True)
                     return

                current_index = int(current_index_str)
                next_index = current_index + 1

                if next_index < len(commands_in_list):
                    next_command = commands_in_list[next_index]
                    self.info(f"序列指令 {current_index + 1}/{len(commands_in_list)} 处理完成，准备发送下一条: '{next_command}'")
                    # 增加随机延迟
                    delay = random.uniform(1.5, 3.0)
                    self.info(f"增加 {delay:.1f} 秒延迟...")
                    await asyncio.sleep(delay)
                    # 更新索引并发送
                    await redis_client.set(index_key, str(next_index), ex=STATE_TTL)
                    success = await self.context.telegram_client.send_game_command(next_command)
                    if not success:
                         self.error(f"发送下一条指令 '{next_command}' 失败！清理状态。")
                         await _clear_star_platform_state(redis_client, self._my_id, self.scheduler, release_lock=True)
                else:
                    self.info(f"序列指令 {next_index}/{len(commands_in_list)} 全部处理完成！")
                    await _clear_star_platform_state(redis_client, self._my_id, self.scheduler, release_lock=True)
                    self.info("【自动观星台】序列完成后触发角色数据同步...")
                    await self.context.event_bus.emit("trigger_character_sync_now")
            elif is_fail:
                self.error(f"指令 '{pending_command}' 执行失败！序列中断。")
                await _clear_star_platform_state(redis_client, self._my_id, self.scheduler, release_lock=True)
                self.info("【自动观星台】指令失败后触发角色数据同步...")
                await self.context.event_bus.emit("trigger_character_sync_now")
            else: # 未知结果
                 self.warning(f"指令 '{pending_command}' 的回复无法判断结果 ({text[:50]}...)，序列中断。")
                 await _clear_star_platform_state(redis_client, self._my_id, self.scheduler, release_lock=True)
                 await self.context.event_bus.emit("trigger_character_sync_now")

        except ValueError:
            self.error(f"Redis 中的 pending MsgID '{expected_msg_id_str}' 无效！清理状态。")
            await _clear_star_platform_state(redis_client, self._my_id, self.scheduler, release_lock=True)
        except Exception as e:
            self.error(f"处理观星台游戏响应时出错: {e}", exc_info=True)
            await _clear_star_platform_state(redis_client, self._my_id, self.scheduler, release_lock=True)
            await self.context.event_bus.emit("trigger_character_sync_now")

