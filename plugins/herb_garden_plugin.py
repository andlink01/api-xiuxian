import logging
import asyncio
import json
import random
import pytz # 重新导入 pytz
from datetime import datetime, timedelta # 重新导入 datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple # 重新导入 Tuple
from plugins.base_plugin import BasePlugin, AppContext
from core.context import get_global_context
from apscheduler.jobstores.base import JobLookupError
from pyrogram.types import Message # 导入 Message
import re # 导入 re

# --- 常量 ---
HERB_GARDEN_JOB_ID = "herb_garden_check_job"
# --- 修改: 锁 Key 包含 user_id 占位符 ---
HERB_GARDEN_ACTION_LOCK_KEY_FORMAT = "herb_garden:action_lock:{}" # 操作锁格式
# --- 修改结束 ---
HERB_GARDEN_ACTION_LOCK_TTL = 300 # 锁 TTL (5分钟，覆盖整个序列)

# Redis Keys for State Machine (这些已包含 user_id，无需修改)
HERB_GARDEN_COMMAND_LIST_KEY_PREFIX = "herb_garden:cmd_list:"
HERB_GARDEN_COMMAND_INDEX_KEY_PREFIX = "herb_garden:cmd_index:"
HERB_GARDEN_PENDING_MSG_ID_KEY_PREFIX = "herb_garden:pending_msgid:"
HERB_GARDEN_PENDING_COMMAND_KEY_PREFIX = "herb_garden:pending_cmd:"
STATE_TTL = 360 # 指令列表和索引的 TTL (6分钟)

HERB_GARDEN_RESPONSE_TIMEOUT = 120 # 等待响应的超时时间 (秒)
HERB_GARDEN_TIMEOUT_JOB_ID_PREFIX = "herb_garden_timeout:" # 超时任务ID前缀

# 指令（不含购买）
GARDEN_COMMANDS_CORE = {".采药", ".浇水", ".除草", ".除虫", ".播种"}
GARDEN_COMMAND_BUY_SEED = ".兑换" # 单独处理购买种子

# 关键词
GARDEN_KEYWORDS: Dict[str, Dict[str, List[str]]] = {
    ".采药": {"success": ["一键采药完成！", "收获了："], "no_need": ["没有需要【采药】的地块"], "fail": []},
    ".浇水": {"success": ["一键浇水完成！", "补充了水分"], "no_need": ["没有需要【浇水】的地块"], "fail": []},
    ".除草": {"success": ["一键除草完成！", "清理了杂草"], "no_need": ["没有需要【除草】的地块"], "fail": []},
    ".除虫": {"success": ["一键除虫完成！", "消灭了害虫"], "no_need": ["没有需要【除虫】的地块"], "fail": []},
    ".播种": {"success": ["播种成功！"], "no_need": ["没有空闲地块"], "fail": ["种子数量不足"]},
    ".兑换": {"success": ["兑换成功！", "获得了【.+?种子】"], "no_need": [], "fail": ["宗门贡献不足"]},
}
# --- 常量结束 ---

# --- 辅助函数 (保持不变) ---
def find_item_id_by_name(items_dict: Dict[str, Dict], item_name: str) -> Optional[str]:
    if not items_dict or not item_name: return None
    name_to_find = item_name.strip()
    for item_id, item_details in items_dict.items():
        if isinstance(item_details, dict) and item_details.get("name") == name_to_find:
            return item_id
    return None

def find_item_in_inventory(inventory_items: List[Dict], item_name: str) -> Optional[Dict]:
    if not inventory_items or not item_name: return None
    name_to_find = item_name.strip()
    for item in inventory_items:
        if isinstance(item, dict) and item.get("name") == name_to_find:
            return item
    return None
# --- 辅助函数结束 ---

# --- 清理状态函数 ---
async def _clear_garden_state(redis_client, user_id: int, scheduler, release_lock: bool = True):
    """清理药园相关的 Redis 状态和超时任务，并根据需要释放操作锁"""
    cleanup_logger = logging.getLogger("HerbGardenPlugin.Cleanup")
    cleanup_logger.info(f"开始清理用户 {user_id} 的药园状态...")
    list_key = f"{HERB_GARDEN_COMMAND_LIST_KEY_PREFIX}{user_id}"
    index_key = f"{HERB_GARDEN_COMMAND_INDEX_KEY_PREFIX}{user_id}"
    pending_msg_key = f"{HERB_GARDEN_PENDING_MSG_ID_KEY_PREFIX}{user_id}"
    pending_cmd_key = f"{HERB_GARDEN_PENDING_COMMAND_KEY_PREFIX}{user_id}"
    keys_to_delete = [list_key, index_key, pending_msg_key, pending_cmd_key]
    # --- 修改: 格式化锁 Key ---
    lock_key = HERB_GARDEN_ACTION_LOCK_KEY_FORMAT.format(user_id) if user_id else None
    # --- 修改结束 ---

    try:
        if redis_client:
            deleted_count = await redis_client.delete(*keys_to_delete)
            cleanup_logger.info(f"已从 Redis 清理 {deleted_count} 个药园状态键。")
        else:
            cleanup_logger.error("无法清理 Redis 状态：Redis 客户端无效。")

        if scheduler:
            # ... (移除超时任务逻辑保持不变) ...
            jobs_removed = 0
            timeout_pattern = f"{HERB_GARDEN_TIMEOUT_JOB_ID_PREFIX}{user_id}:*"
            try:
                matching_jobs = [job.id for job in scheduler.get_jobs() if job.id.startswith(f"{HERB_GARDEN_TIMEOUT_JOB_ID_PREFIX}{user_id}:")]
                cleanup_logger.debug(f"找到 {len(matching_jobs)} 个匹配的超时任务: {matching_jobs}")
                for job_id in matching_jobs:
                    try:
                        await asyncio.to_thread(scheduler.remove_job, job_id)
                        jobs_removed += 1
                        cleanup_logger.debug(f"已移除超时任务: {job_id}")
                    except JobLookupError:
                        cleanup_logger.debug(f"移除超时任务 {job_id} 时未找到。")
                    except Exception as e_rem_job:
                        cleanup_logger.warning(f"移除超时任务 {job_id} 时出错: {e_rem_job}")
                if jobs_removed > 0:
                    cleanup_logger.info(f"已移除 {jobs_removed} 个药园超时任务。")
            except Exception as e_get_jobs:
                 cleanup_logger.error(f"获取或移除超时任务时出错: {e_get_jobs}")
        else:
             cleanup_logger.warning("无法移除超时任务：Scheduler 无效。")

        # --- 修改: 使用格式化后的 lock_key ---
        if release_lock and redis_client and lock_key:
            try:
                deleted_lock = await redis_client.delete(lock_key)
                if deleted_lock > 0: cleanup_logger.info(f"已释放药园操作锁 ({lock_key})。")
            except Exception as e_lock:
                cleanup_logger.error(f"释放药园操作锁 ({lock_key}) 时出错: {e_lock}")
        # --- 修改结束 ---

    except Exception as e_clean:
        cleanup_logger.error(f"清理药园状态时发生意外错误: {e_clean}", exc_info=True)


# --- 超时处理函数 ---
async def _handle_garden_timeout(user_id: int, expected_msg_id: int):
    """处理等待药园操作响应超时"""
    timeout_logger = logging.getLogger("HerbGardenPlugin.Timeout")
    timeout_logger.warning(f"【自动药园】超时任务触发：检查 MsgID {expected_msg_id} 的响应是否超时...")
    context = get_global_context()
    if not context or not context.redis or not context.event_bus or not context.scheduler:
        timeout_logger.error("【自动药园】无法处理超时：核心服务不可用。")
        return

    redis_client = context.redis.get_client()
    if not redis_client: timeout_logger.error("【自动药园】无法处理超时：Redis 未连接。"); return

    pending_msg_key = f"{HERB_GARDEN_PENDING_MSG_ID_KEY_PREFIX}{user_id}"
    try:
        current_pending_msg_id_str = await redis_client.get(pending_msg_key)
        if current_pending_msg_id_str and current_pending_msg_id_str.isdigit() and int(current_pending_msg_id_str) == expected_msg_id:
            timeout_logger.warning(f"【自动药园】确认超时！等待 MsgID {expected_msg_id} 的响应超时。")
            # --- 修改: 调用清理函数时传递 user_id ---
            await _clear_garden_state(redis_client, user_id, context.scheduler, release_lock=True)
            # --- 修改结束 ---
            timeout_logger.info("【自动药园】超时后触发角色数据同步...")
            await context.event_bus.emit("trigger_character_sync_now")
        else:
            timeout_logger.info(f"【自动药园】超时任务触发，但当前等待MsgID是 '{current_pending_msg_id_str}' (不是 {expected_msg_id}) 或无等待，忽略。")
    except ValueError:
        timeout_logger.error(f"【自动药园】Redis 中的 pending MsgID '{current_pending_msg_id_str}' 无效，清理状态。")
        # --- 修改: 调用清理函数时传递 user_id ---
        await _clear_garden_state(redis_client, user_id, context.scheduler, release_lock=True)
        # --- 修改结束 ---
        await context.event_bus.emit("trigger_character_sync_now")
    except Exception as e:
        timeout_logger.error(f"【自动药园】处理药园响应超时状态时出错: {e}", exc_info=True)
        # --- 修改: 调用清理函数时传递 user_id ---
        await _clear_garden_state(redis_client, user_id, context.scheduler, release_lock=True) # 强制清理
        # --- 修改结束 ---
        await context.event_bus.emit("trigger_character_sync_now")


async def _check_herb_garden():
    """
    由 APScheduler 调度的顶层函数，用于检查和管理药园。
    """
    task_logger = logging.getLogger("HerbGardenPlugin.Task")
    task_logger.info("【自动药园】任务启动，开始检查药园状态...")

    context = get_global_context()
    if not context or not context.data_manager or not context.redis or not context.telegram_client:
        task_logger.error("【自动药园】无法获取核心服务，任务终止。")
        return

    config = context.config
    redis_client = context.redis.get_client()
    data_manager = context.data_manager
    my_id = context.telegram_client._my_id

    if not config.get("herb_garden.enabled", False):
        task_logger.info("【自动药园】功能未启用，跳过检查。")
        return

    # --- 修改: 在获取锁之前检查 my_id ---
    if not my_id:
        task_logger.warning("【自动药园】无法获取助手 User ID，任务终止。")
        return
    # --- 修改结束 ---

    lock_acquired = False
    # --- 修改: 格式化锁 Key ---
    lock_key = HERB_GARDEN_ACTION_LOCK_KEY_FORMAT.format(my_id)
    # --- 修改结束 ---

    if redis_client:
        try:
            task_logger.info(f"【自动药园】尝试获取 Redis 操作锁 ({lock_key})...")
            lock_acquired = await redis_client.set(lock_key, "1", ex=HERB_GARDEN_ACTION_LOCK_TTL, nx=True)
            if not lock_acquired:
                task_logger.info(f"【自动药园】获取操作锁 ({lock_key}) 失败，上次操作序列可能仍在进行中，跳过本次检查。")
                return
            task_logger.info(f"【自动药园】成功获取 Redis 操作锁 ({lock_key})。")

            list_key = f"{HERB_GARDEN_COMMAND_LIST_KEY_PREFIX}{my_id}"
            if await redis_client.exists(list_key):
                task_logger.info(f"【自动药园】检测到用户 {my_id} 存在未完成的操作序列，跳过本次检查。")
                # --- 修改: 释放获取到的锁 ---
                try: await redis_client.delete(lock_key)
                except Exception as del_err: task_logger.warning(f"释放锁 {lock_key} 失败: {del_err}")
                lock_acquired = False # 标记锁已释放
                # --- 修改结束 ---
                return
        except Exception as e:
            task_logger.error(f"【自动药园】检查或设置 Redis 锁/状态失败: {e}，为安全起见跳过本次检查。")
            if lock_acquired:
                 try: await redis_client.delete(lock_key)
                 except Exception as del_err: task_logger.warning(f"异常后释放锁 {lock_key} 失败: {del_err}")
            return
    else:
        task_logger.error("【自动药园】Redis 未连接，无法检查锁/状态，任务终止。")
        return

    commands_to_send: List[str] = []

    try:
        # my_id 在前面已检查
        task_logger.info("【自动药园】正在通过 DataManager 强制获取角色状态、宗门、背包和商店数据...")
        # ... (数据获取逻辑不变) ...
        char_status = await data_manager.get_character_status(my_id, use_cache=False)
        sect_info = await data_manager.get_sect_info(my_id, use_cache=False)
        inventory_cache = await data_manager.get_inventory(my_id, use_cache=False)
        shop_items_dict = await data_manager.get_shop_data(my_id, use_cache=False)

        if not char_status:
            task_logger.error("【自动药园】无法从 API 获取角色状态，任务终止。")
            return # finally 会释放锁
        task_logger.info("【自动药园】获取实时数据完成。")

        # ... (宗门检查、药园状态分析、指令生成逻辑不变) ...
        sect_name = sect_info.get("sect_name") if isinstance(sect_info, dict) else None
        if sect_name != "黄枫谷":
            task_logger.info(f"检测到宗门为 '{sect_name}' (不是黄枫谷)，功能禁用。")
            try:
                if context.scheduler: await asyncio.to_thread(context.scheduler.remove_job, HERB_GARDEN_JOB_ID) # 使用 asyncio.to_thread
                task_logger.info("已移除自动药园定时任务。")
            except JobLookupError: task_logger.info("定时任务已不存在。")
            except Exception as e: task_logger.error(f"移除定时任务时出错: {e}")
            return # finally 会释放锁

        garden_data = char_status.get("herb_garden")
        if not isinstance(garden_data, dict):
            task_logger.error("实时数据中 `herb_garden` 缺失或格式不正确，任务终止。")
            return # finally 会释放锁
        size = garden_data.get("size", 0); plots = garden_data.get("plots")
        empty_plots = 0; ready_plots = 0; dry_plots = 0; weed_plots = 0; pest_plots = 0; growing_plots = 0
        if isinstance(plots, dict):
            occupied_plot_count = 0
            for plot_id, plot_info in plots.items():
                if isinstance(plot_info, dict):
                    occupied_plot_count += 1
                    status = plot_info.get("status")
                    if status == "ready": ready_plots += 1
                    elif status == "dry": dry_plots += 1
                    elif status == "weeds": weed_plots += 1
                    elif status == "pests": pest_plots += 1
                    elif status == "growing": growing_plots +=1
                else: task_logger.warning(f"地块 {plot_id} 数据格式不正确: {plot_info}，视为异常/空闲。")
            empty_plots = size - occupied_plot_count
            if empty_plots < 0: task_logger.warning(f"空闲地块数为负({empty_plots})，修正为0。"); empty_plots = 0
        else: task_logger.error("实时地块数据格式不正确（不是字典），任务终止。"); return # finally 会释放锁
        task_logger.info(f"【自动药园】状态分析: 共{size} | {empty_plots}空闲 | {ready_plots}成熟 | {dry_plots}干涸 | {weed_plots}杂草 | {pest_plots}害虫 | {growing_plots}生长中")

        maintenance_commands = []; sow_commands = []

        if ready_plots > 0:
            commands_to_send.append(".采药"); task_logger.info(f"决策：添加指令 '.采药' ({ready_plots} 块成熟)")

        if dry_plots > 0: maintenance_commands.append(".浇水"); task_logger.info(f"待办：添加 '.浇水' ({dry_plots} 干涸)")
        if weed_plots > 0: maintenance_commands.append(".除草"); task_logger.info(f"待办：添加 '.除草' ({weed_plots} 杂草)")
        if pest_plots > 0: maintenance_commands.append(".除虫"); task_logger.info(f"待办：添加 '.除虫' ({pest_plots} 害虫)")

        random.shuffle(maintenance_commands)
        commands_to_send.extend(maintenance_commands)
        if maintenance_commands: task_logger.info(f"决策：添加维护指令 (顺序: {', '.join(maintenance_commands)})")

        plots_to_sow = empty_plots + ready_plots
        if plots_to_sow > 0:
            task_logger.info(f"计算播种需求: {plots_to_sow} (空闲{empty_plots} + 成熟{ready_plots})")
            target_seed_name = config.get("herb_garden.target_seed_name")
            if not target_seed_name: task_logger.warning("需要播种但未配置 target_seed_name。")
            else:
                current_seed_count = 0
                if isinstance(inventory_cache, dict):
                    inventory_seeds = inventory_cache.get("items_by_type", {}).get("seed", [])
                    seed_item_in_inv = find_item_in_inventory(inventory_seeds, target_seed_name)
                    if seed_item_in_inv: current_seed_count = seed_item_in_inv.get("quantity", 0)
                else: task_logger.warning("无法获取背包信息，种子数视为 0。")
                task_logger.info(f"目标种子: '{target_seed_name}', 当前持有: {current_seed_count}")

                seeds_needed = plots_to_sow
                min_seed_reserve = config.get("herb_garden.min_seed_reserve", 0)
                seeds_available_to_sow = max(0, current_seed_count - min_seed_reserve)
                seeds_still_needed_after_reserve = max(0, seeds_needed - seeds_available_to_sow)
                task_logger.info(f"播种可用(扣除保留{min_seed_reserve}): {seeds_available_to_sow}, 仍需: {seeds_still_needed_after_reserve}")

                buy_needed = seeds_still_needed_after_reserve; buy_command = None; actual_buy_quantity = 0

                if buy_needed > 0:
                    task_logger.info(f"需购买 {buy_needed} 颗 '{target_seed_name}'...")
                    seed_price = 0; target_seed_id = None
                    buy_quantity_config = config.get("herb_garden.buy_seed_quantity", buy_needed)
                    actual_buy_quantity = max(buy_needed, buy_quantity_config)

                    if isinstance(shop_items_dict, dict):
                        target_seed_id = find_item_id_by_name(shop_items_dict, target_seed_name)
                        if target_seed_id:
                            shop_item = shop_items_dict.get(target_seed_id)
                            if isinstance(shop_item, dict): seed_price = shop_item.get("price", 0)
                        else: task_logger.warning(f"未在商店找到 '{target_seed_name}'。")
                    else: task_logger.warning("无法获取商店信息。")

                    if seed_price > 0:
                        contribution = sect_info.get("sect_contribution", 0) if isinstance(sect_info, dict) else 0
                        total_cost = seed_price * actual_buy_quantity
                        task_logger.info(f"种子价格: {seed_price}, 计划购买: {actual_buy_quantity}, 花费: {total_cost}, 持有: {contribution} 贡献。")
                        if contribution >= total_cost:
                            buy_command = f".兑换 {target_seed_name}*{actual_buy_quantity}"
                            sow_commands.append(buy_command); task_logger.info(f"决策：贡献充足，添加购买指令 '{buy_command}'")
                        else: task_logger.warning(f"贡献不足({contribution}<{total_cost})，无法购买。本次不播种。"); plots_to_sow = 0
                    else: task_logger.error(f"无法找到种子 '{target_seed_name}' 价格，无法购买。本次不播种。"); plots_to_sow = 0

                if plots_to_sow > 0:
                    final_seed_count = seeds_available_to_sow + (actual_buy_quantity if buy_command else 0)
                    if final_seed_count >= seeds_needed:
                        sow_command = f".播种 {target_seed_name}"
                        if sow_command not in sow_commands:
                            sow_commands.append(sow_command); task_logger.info(f"决策：添加播种指令 '{sow_command}' (预计播种 {plots_to_sow} 块)")
                    else: task_logger.warning(f"最终计算种子仍不足 (可用{seeds_available_to_sow} + 购买{actual_buy_quantity if buy_command else 0} < 需要{seeds_needed})，本次不执行播种。")

            commands_to_send.extend(sow_commands)


        if commands_to_send:
            task_logger.info(f"【自动药园】本周期生成指令序列: {', '.join(commands_to_send)}")
            if redis_client and my_id:
                list_key = f"{HERB_GARDEN_COMMAND_LIST_KEY_PREFIX}{my_id}"
                index_key = f"{HERB_GARDEN_COMMAND_INDEX_KEY_PREFIX}{my_id}"
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
                    lock_acquired = False # 锁不由 finally 块释放，由序列完成或超时释放
                else:
                     task_logger.error(f"发送第一个指令 '{first_command}' 失败！清理状态并释放锁。")
                     # --- 修改: 调用清理函数 ---
                     await _clear_garden_state(redis_client, my_id, context.scheduler, release_lock=True)
                     # --- 修改结束 ---
                     lock_acquired = False
            else:
                task_logger.error("无法启动指令序列：Redis 或 User ID 不可用。")
                lock_acquired = True # 标记需要 finally 释放
        else:
            task_logger.info("【自动药园】本周期无操作需要执行。")
            lock_acquired = True # 标记需要 finally 释放

    except Exception as outer_e:
         task_logger.error(f"【自动药园】在 _check_herb_garden 主逻辑中发生意外错误: {outer_e}", exc_info=True)
         lock_acquired = True # 标记需要 finally 释放

    finally:
        # --- 修改: 仅在 lock_acquired 为 True 时释放锁 ---
        if lock_acquired and redis_client:
            try:
                task_logger.info(f"【自动药园】检查结束或出错，释放 Redis 操作锁 ({lock_key})...")
                deleted = await redis_client.delete(lock_key)
                if deleted: task_logger.info("操作锁已释放。")
            except Exception as e_lock_final:
                task_logger.error(f"【自动药园】释放 Redis 锁 ({lock_key}) 时出错: {e_lock_final}")
        elif not lock_acquired and redis_client: # 锁已被序列持有
             try:
                 list_key_final = f"{HERB_GARDEN_COMMAND_LIST_KEY_PREFIX}{my_id}"
                 if await redis_client.exists(list_key_final):
                      task_logger.info(f"【自动药园】任务结束，操作锁 ({lock_key}) 由正在执行的指令序列持有。")
             except Exception as e_check:
                  task_logger.warning(f"检查指令列表是否存在时出错: {e_check}")
        # --- 修改结束 ---


class Plugin(BasePlugin):
    """
    自动药园管理插件（仅限黄枫谷）
    """
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        self.load_config()
        self._my_id: Optional[int] = None

        if self.config_enabled:
            self.info(f"插件已加载并启用。检查间隔: {self.check_interval} 分钟。")
        else:
            self.info("插件已加载但未启用 (请在 config.yaml 中设置 herb_garden.enabled: true)。")

    def load_config(self):
        self.config_enabled = self.config.get("herb_garden.enabled", False)
        self.check_interval = self.config.get("herb_garden.check_interval_minutes", 5)

    def register(self):
        """注册定时检查任务和事件监听器"""
        # ... (注册逻辑不变) ...
        if not self.config_enabled:
            return

        if self.check_interval < 1:
            self.check_interval = 1
            self.warning("药园检查间隔 'check_interval_minutes' 不能小于 1，已重置为 1 分钟。")

        try:
            if self.scheduler:
                self.scheduler.add_job(
                    _check_herb_garden, trigger='interval', minutes=self.check_interval,
                    id=HERB_GARDEN_JOB_ID, replace_existing=True, misfire_grace_time=60
                )
                self.info(f"已注册药园定时检查任务 (每 {self.check_interval} 分钟)。")
                self.event_bus.on("telegram_client_started", self._initialize_id)
                self.event_bus.on("game_command_sent", self.handle_command_sent)
                self.event_bus.on("game_response_received", self.handle_game_response)
                self.info("已注册药园相关的 game_command_sent 和 game_response_received 事件监听器。")
            else:
                 self.error("无法注册药园定时任务或监听器：Scheduler 不可用。")
        except Exception as e:
            self.error(f"注册药园定时任务或监听器时出错: {e}", exc_info=True)

    async def _initialize_id(self):
        # ... (逻辑不变) ...
        if self.context.telegram_client:
             self._my_id = await self.context.telegram_client.get_my_id()
             self.info(f"已缓存当前 User ID: {self._my_id}")

    async def handle_command_sent(self, sent_message: Message, command_text: str):
        """监听药园指令发送成功，设置等待状态 (MsgID, Command) 和超时"""
        # ... (逻辑不变) ...
        if not self._my_id:
             self._my_id = await self.context.telegram_client.get_my_id()
             if not self._my_id: self.error("无法获取 User ID"); return
        redis_client = self.redis.get_client()
        if not redis_client: self.error("Redis 未连接"); return

        list_key = f"{HERB_GARDEN_COMMAND_LIST_KEY_PREFIX}{self._my_id}"
        index_key = f"{HERB_GARDEN_COMMAND_INDEX_KEY_PREFIX}{self._my_id}"
        pending_msg_key = f"{HERB_GARDEN_PENDING_MSG_ID_KEY_PREFIX}{self._my_id}"
        pending_cmd_key = f"{HERB_GARDEN_PENDING_COMMAND_KEY_PREFIX}{self._my_id}"

        try:
            current_index_str = await redis_client.get(index_key)
            if current_index_str is None: return # 不在序列中

            current_index = int(current_index_str)
            commands_in_list = await redis_client.lrange(list_key, 0, -1)
            if not commands_in_list or current_index >= len(commands_in_list):
                 self.warning(f"指令 '{command_text}' 发送，但 Redis 列表为空或索引 ({current_index}) 越界！清理状态。")
                 # --- 修改: 调用清理函数 ---
                 await _clear_garden_state(redis_client, self._my_id, self.scheduler, release_lock=True)
                 # --- 修改结束 ---
                 return

            expected_command = commands_in_list[current_index]
            command_base_sent = command_text.split()[0]
            command_base_expected = expected_command.split()[0]

            # 确保发送的是当前序列期望的指令
            if command_base_sent != command_base_expected:
                 self.debug(f"发送的指令 '{command_base_sent}' 与期望的 '{command_base_expected}' 不符，忽略。")
                 return

            self.info(f"【自动药园】监听到序列指令 '{command_text}' 已发送 (MsgID: {sent_message.id})，设置等待状态和超时。")

            timeout_job_id = f"{HERB_GARDEN_TIMEOUT_JOB_ID_PREFIX}{self._my_id}:{sent_message.id}"
            timeout_seconds = HERB_GARDEN_RESPONSE_TIMEOUT

            async with redis_client.pipeline(transaction=True) as pipe:
                pipe.set(pending_msg_key, str(sent_message.id), ex=timeout_seconds + 60)
                pipe.set(pending_cmd_key, expected_command, ex=timeout_seconds + 60)
                await pipe.execute()
            self.info(f"Redis 等待状态已设置 (MsgID: {sent_message.id}, Cmd: '{expected_command}')。")

            if self.scheduler:
                run_at = datetime.now(pytz.utc) + timedelta(seconds=timeout_seconds)
                self.scheduler.add_job(
                    _handle_garden_timeout, trigger='date', run_date=run_at,
                    args=[self._my_id, sent_message.id],
                    id=timeout_job_id, replace_existing=True, misfire_grace_time=10
                )
                self.info(f"已安排超时检查任务 '{timeout_job_id}'。")
            else:
                 self.error("无法安排超时任务：Scheduler 不可用。清理状态。")
                 # --- 修改: 调用清理函数 ---
                 await _clear_garden_state(redis_client, self._my_id, self.scheduler, release_lock=True)
                 # --- 修改结束 ---

        except ValueError:
             self.error(f"Redis 中的索引 '{current_index_str}' 无效！清理状态。")
             # --- 修改: 调用清理函数 ---
             await _clear_garden_state(redis_client, self._my_id, self.scheduler, release_lock=True)
             # --- 修改结束 ---
        except Exception as e:
            self.error(f"处理指令发送事件时出错: {e}", exc_info=True)
            # --- 修改: 调用清理函数 ---
            await _clear_garden_state(redis_client, self._my_id, self.scheduler, release_lock=True)
            # --- 修改结束 ---

    async def handle_game_response(self, message: Message, is_reply_to_me: bool, is_mentioning_me: bool):
        """处理游戏响应，推进药园操作序列"""
        # ... (大部分逻辑不变) ...
        if not self.config_enabled or not is_reply_to_me: return
        text = message.text or message.caption
        if not text: return
        if not self._my_id: self.error("无法获取 User ID"); return
        redis_client = self.redis.get_client()
        if not redis_client: self.error("Redis 未连接"); return

        pending_msg_key = f"{HERB_GARDEN_PENDING_MSG_ID_KEY_PREFIX}{self._my_id}"
        pending_cmd_key = f"{HERB_GARDEN_PENDING_COMMAND_KEY_PREFIX}{self._my_id}"
        list_key = f"{HERB_GARDEN_COMMAND_LIST_KEY_PREFIX}{self._my_id}"
        index_key = f"{HERB_GARDEN_COMMAND_INDEX_KEY_PREFIX}{self._my_id}"
        expected_msg_id_str = None

        try:
            expected_msg_id_str = await redis_client.get(pending_msg_key)
            if not expected_msg_id_str or not expected_msg_id_str.isdigit(): return
            expected_msg_id = int(expected_msg_id_str)
            if message.reply_to_message_id != expected_msg_id: return

            pending_command = await redis_client.get(pending_cmd_key)
            if not pending_command:
                self.warning(f"匹配到 MsgID {expected_msg_id}，但无法获取等待的指令内容！清理状态。")
                # --- 修改: 调用清理函数 ---
                await _clear_garden_state(redis_client, self._my_id, self.scheduler, release_lock=True)
                # --- 修改结束 ---
                return

            self.info(f"【自动药园】收到对指令 '{pending_command}' (MsgID: {expected_msg_id}) 的回复，检查结果...")

            # ... (结果判断逻辑不变) ...
            is_success = False; is_no_need = False; is_fail = False; result_type = "unknown"
            command_base = pending_command.split()[0]
            keywords = GARDEN_KEYWORDS.get(command_base)
            if keywords:
                if any(re.search(kw, text, re.IGNORECASE) for kw in keywords.get("success", [])):
                    is_success = True; result_type = "成功"
                elif any(re.search(kw, text, re.IGNORECASE) for kw in keywords.get("no_need", [])):
                    is_no_need = True; result_type = "无需操作"
                elif any(re.search(kw, text, re.IGNORECASE) for kw in keywords.get("fail", [])):
                     is_fail = True; result_type = "失败"
            else:
                 self.warning(f"【自动药园】无法找到指令 '{command_base}' 的关键词定义，将按失败处理。")
                 is_fail = True; result_type = "未知指令失败"
            log_method = self.info if is_success or is_no_need else self.warning
            log_method(f"指令 '{pending_command}' 执行结果: {result_type}")


            # 清理当前指令的等待状态和超时任务
            await redis_client.delete(pending_msg_key, pending_cmd_key)
            timeout_job_id = f"{HERB_GARDEN_TIMEOUT_JOB_ID_PREFIX}{self._my_id}:{expected_msg_id}"
            try:
                if self.scheduler: await asyncio.to_thread(self.scheduler.remove_job, timeout_job_id)
            except JobLookupError: pass
            except Exception as e_rem: self.warning(f"移除超时任务 '{timeout_job_id}' 失败: {e_rem}")

            if is_success or is_no_need:
                # ... (推进序列逻辑不变) ...
                current_index_str = await redis_client.get(index_key)
                commands_in_list = await redis_client.lrange(list_key, 0, -1)
                if current_index_str is None or not commands_in_list:
                     self.warning("无法获取指令列表或索引，序列中断。清理状态。")
                     # --- 修改: 调用清理函数 ---
                     await _clear_garden_state(redis_client, self._my_id, self.scheduler, release_lock=True)
                     # --- 修改结束 ---
                     return

                current_index = int(current_index_str)
                next_index = current_index + 1

                if next_index < len(commands_in_list):
                    next_command = commands_in_list[next_index]
                    self.info(f"序列指令 {current_index + 1}/{len(commands_in_list)} 处理完成，准备发送下一条: '{next_command}'")
                    await redis_client.set(index_key, str(next_index), ex=STATE_TTL)
                    success = await self.context.telegram_client.send_game_command(next_command)
                    if not success:
                         self.error(f"发送下一条指令 '{next_command}' 失败！清理状态。")
                         # --- 修改: 调用清理函数 ---
                         await _clear_garden_state(redis_client, self._my_id, self.scheduler, release_lock=True)
                         # --- 修改结束 ---
                else:
                    self.info(f"序列指令 {next_index}/{len(commands_in_list)} 全部处理完成！")
                    # --- 修改: 调用清理函数 ---
                    await _clear_garden_state(redis_client, self._my_id, self.scheduler, release_lock=True)
                    # --- 修改结束 ---
                    self.info("【自动药园】序列完成后触发角色数据同步...")
                    await self.context.event_bus.emit("trigger_character_sync_now")
            elif is_fail:
                self.error(f"指令 '{pending_command}' 执行失败！序列中断。")
                # --- 修改: 调用清理函数 ---
                await _clear_garden_state(redis_client, self._my_id, self.scheduler, release_lock=True)
                # --- 修改结束 ---
                self.info("【自动药园】指令失败后触发角色数据同步...")
                await self.context.event_bus.emit("trigger_character_sync_now")
            else: # 未知结果
                 self.warning(f"指令 '{pending_command}' 的回复无法判断结果 ({text[:50]}...)，序列中断。")
                 # --- 修改: 调用清理函数 ---
                 await _clear_garden_state(redis_client, self._my_id, self.scheduler, release_lock=True)
                 # --- 修改结束 ---
                 await self.context.event_bus.emit("trigger_character_sync_now")

        except ValueError:
            self.error(f"Redis 中的 pending MsgID '{expected_msg_id_str}' 无效！清理状态。")
            # --- 修改: 调用清理函数 ---
            await _clear_garden_state(redis_client, self._my_id, self.scheduler, release_lock=True)
            # --- 修改结束 ---
        except Exception as e:
            self.error(f"处理药园游戏响应时出错: {e}", exc_info=True)
            # --- 修改: 调用清理函数 ---
            await _clear_garden_state(redis_client, self._my_id, self.scheduler, release_lock=True)
            # --- 修改结束 ---
            await self.context.event_bus.emit("trigger_character_sync_now")

