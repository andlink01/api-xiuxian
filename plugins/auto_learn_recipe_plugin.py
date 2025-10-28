import logging
import asyncio
import json
import random
from typing import Optional, List, Dict
from plugins.base_plugin import BasePlugin, AppContext
from core.context import get_global_context
from apscheduler.jobstores.base import JobLookupError

# --- 常量 ---
LEARN_RECIPE_JOB_ID = 'auto_learn_recipe_job' # 定时检查任务 ID
LEARN_RECIPE_COMMAND_FORMAT = ".学习 {}" # 学习指令格式，{} 会被替换为配方名称
REDIS_LEARN_RECIPE_LOCK_KEY = "learn_recipe:action_lock" # Redis 操作锁 Key
LEARN_RECIPE_LOCK_TTL = 120 # 锁的 TTL (秒)

async def _check_and_learn_recipes():
    """由 APScheduler 调度的函数，检查背包中是否有未学习的配方并尝试学习"""
    logger = logging.getLogger("AutoLearnRecipePlugin.Check")
    logger.info("【自动学习配方】任务启动，开始检查...")

    context = get_global_context()
    # --- 修改: 增加 data_manager 检查 ---
    if not context or not context.data_manager or not context.redis or not context.telegram_client:
        logger.error("【自动学习配方】无法执行：核心服务 (DataManager/Redis/TGClient) 不可用。")
        return
    # --- 修改结束 ---

    config = context.config
    data_manager = context.data_manager # 获取 data_manager 实例

    auto_enabled = config.get("auto_learn_recipe.enabled", False)
    if not auto_enabled:
        logger.info("【自动学习配方】已被禁用，跳过本次执行。")
        return

    redis_client = context.redis.get_client()
    lock_acquired = False
    # --- 获取 Redis 锁 (逻辑不变) ---
    if redis_client:
        try:
            lock_acquired = await redis_client.set(REDIS_LEARN_RECIPE_LOCK_KEY, "1", ex=LEARN_RECIPE_LOCK_TTL, nx=True)
            if not lock_acquired:
                logger.info("【自动学习配方】获取操作锁失败，上次检查可能仍在进行中，跳过本次。")
                return
            logger.info("【自动学习配方】成功获取操作锁。")
        except Exception as e:
            logger.error(f"【自动学习配方】检查或设置 Redis 锁失败: {e}，为安全起见跳过本次检查。")
            return
    else:
        logger.error("【自动学习配方】Redis 未连接，无法检查锁，任务终止。")
        return
    # --- 锁逻辑结束 ---

    # 使用 finally 确保锁会被释放
    try:
        my_id = context.telegram_client._my_id
        if not my_id:
            logger.warning("【自动学习配方】无法获取助手 User ID，跳过本次检查。")
            return # finally 会释放锁

        learned_recipes_ids: List[str] = []
        inventory_recipes: List[Dict] = []

        # --- 修改: 使用 DataManager 获取数据 ---
        # 1. 获取已学配方列表
        try:
            logger.info("【自动学习配方】正在通过 DataManager 获取已学配方缓存...")
            # get_learned_recipes 直接返回 known_ids 列表或 None
            known_ids_list = await data_manager.get_learned_recipes(my_id, use_cache=True)
            if known_ids_list is not None: # 检查是否为 None (获取失败)
                if isinstance(known_ids_list, list):
                    learned_recipes_ids = known_ids_list
                    logger.info(f"【自动学习配方】获取到已学习配方 {len(learned_recipes_ids)} 个。")
                else:
                    # GDM 返回了非列表数据，记录错误
                    logger.error(f"【自动学习配方】DataManager 返回的已学配方数据格式不正确 (非列表)，类型: {type(known_ids_list)}，本次跳过。")
                    learned_recipes_ids = [] # 出错时置空
            else:
                logger.warning("【自动学习配方】无法从 DataManager 获取已学配方缓存，跳过本次检查。")
                return # finally 会释放锁
        except Exception as e:
            logger.error(f"【自动学习配方】通过 DataManager 获取已学配方时出错: {e}", exc_info=True)
            return # finally 会释放锁

        # 2. 获取背包中的配方列表
        try:
            logger.info("【自动学习配方】正在通过 DataManager 获取背包缓存...")
            inventory_cache = await data_manager.get_inventory(my_id, use_cache=True)
            if inventory_cache and isinstance(inventory_cache, dict):
                # 从背包缓存的 'items_by_type' 中获取配方列表
                inventory_recipes = inventory_cache.get("items_by_type", {}).get("recipe", [])
                if isinstance(inventory_recipes, list):
                     logger.info(f"【自动学习配方】获取到背包中配方 {len(inventory_recipes)} 个。")
                else:
                    logger.error(f"【自动学习配方】背包缓存中的配方数据格式不正确（非列表），类型: {type(inventory_recipes)}，本次跳过。")
                    inventory_recipes = [] # 出错时置空
            else:
                logger.warning("【自动学习配方】无法从 DataManager 获取背包缓存，跳过本次检查。")
                return # finally 会释放锁
        except Exception as e:
            logger.error(f"【自动学习配方】通过 DataManager 获取背包数据时出错: {e}", exc_info=True)
            return # finally 会释放锁
        # --- 修改结束 ---

        # 3. 查找第一个未学习的配方 (逻辑不变)
        recipe_to_learn_name: Optional[str] = None
        recipe_to_learn_id: Optional[str] = None
        learned_set = set(learned_recipes_ids) # 转换为集合以便快速查找
        for recipe in inventory_recipes:
            if isinstance(recipe, dict):
                item_id = recipe.get("item_id")
                item_name = recipe.get("name")
                if item_id and item_name:
                    if item_id not in learned_set:
                        recipe_to_learn_id = item_id; recipe_to_learn_name = item_name
                        logger.info(f"【自动学习配方】发现未学习配方: '{recipe_to_learn_name}' (ID: {recipe_to_learn_id})")
                        break # 每次只学习一个
                else: logger.warning(f"【自动学习配方】背包中的配方条目缺少 ID 或名称: {recipe}")
            else: logger.warning(f"【自动学习配方】背包配方列表中的条目格式不正确（不是字典）: {recipe}")

        # 4. 发送学习指令并触发同步 (逻辑不变)
        if recipe_to_learn_name:
            learn_command = LEARN_RECIPE_COMMAND_FORMAT.format(recipe_to_learn_name)
            try:
                logger.info(f"【自动学习配方】准备将学习指令 '{learn_command}' 加入发送队列...")
                success = await context.telegram_client.send_game_command(learn_command)
                if success:
                    logger.info(f"【自动学习配方】指令 '{learn_command}' 已成功加入发送队列。")
                    logger.info("【自动学习配方】触发角色数据同步...")
                    try:
                        # 触发同步以更新已学配方列表
                        await context.event_bus.emit("trigger_character_sync_now")
                    except Exception as sync_e:
                        logger.error(f"【自动学习配方】尝试在发送学习指令后触发角色同步时出错: {sync_e}", exc_info=True)
                else:
                     logger.error(f"【自动学习配方】将学习指令 '{learn_command}' 加入队列失败。")
            except Exception as e:
                logger.error(f"【自动学习配方】将学习指令 '{learn_command}' 加入队列时出错: {e}", exc_info=True)
        else:
            logger.info("【自动学习配方】背包中未发现需要学习的新配方。")

    # 确保锁在函数结束或异常时被释放 (逻辑不变)
    finally:
        if lock_acquired and redis_client:
            try:
                await redis_client.delete(REDIS_LEARN_RECIPE_LOCK_KEY)
                logger.info("【自动学习配方】操作锁已释放。")
            except Exception as e:
                logger.error(f"【自动学习配方】释放 Redis 锁时出错: {e}")

# --- 插件类 (逻辑不变) ---
class Plugin(BasePlugin):
    """
    自动学习背包中未学习配方的插件
    """
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        self.load_config()
        if self.auto_enabled:
            self.info(f"插件已加载并启用。检查频率约为每天 {self.check_times_per_day} 次。学习指令格式: '{LEARN_RECIPE_COMMAND_FORMAT.format('<配方名>')}'")
        else:
            self.info("插件已加载但未启用。")

    def load_config(self):
        """加载配置"""
        self.auto_enabled = self.config.get("auto_learn_recipe.enabled", False)
        self.check_times_per_day = self.config.get("auto_learn_recipe.checks_per_day", 5)
        if not isinstance(self.check_times_per_day, int) or self.check_times_per_day < 1:
            self.warning(f"无效的 auto_learn_recipe.checks_per_day 配置 '{self.check_times_per_day}'，使用默认值 5")
            self.check_times_per_day = 5
        self.check_interval_hours = 24 / self.check_times_per_day
        self.jitter_seconds = int(self.check_interval_hours * 3600 / 4)

    def register(self):
        """注册定时任务"""
        if not self.auto_enabled:
            return
        try:
            if self.scheduler: # 确保 scheduler 可用
                self.scheduler.add_job(
                    _check_and_learn_recipes,
                    trigger='interval',
                    hours=self.check_interval_hours,
                    jitter=self.jitter_seconds,
                    id=LEARN_RECIPE_JOB_ID,
                    replace_existing=True,
                    misfire_grace_time=300
                )
                self.info(f"已注册自动学习配方任务 (约每 {self.check_interval_hours:.1f} 小时检查一次)。")
            else:
                 self.error("无法注册自动学习配方任务：Scheduler 不可用。")
        except Exception as e:
            self.error(f"注册自动学习配方定时任务时出错: {e}", exc_info=True)

