import os
import sys
import yaml
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import pytz
import logging

from core.config import Config, SETUP_NEEDED_FLAG, CONFIG_PATH
from core.event_bus import EventBus
from plugins.base_plugin import AppContext
from core.context import set_global_context
from core.logger import initialize_logger, get_logger

config = Config()

logger = initialize_logger(config)

from modules.telegram_client import TelegramClient
from modules.redis_client import RedisClient
from modules.http_client import HTTPClient
from modules.gemini_client import GeminiClient
from modules.scheduler import Scheduler
from modules.game_data_manager import GameDataManager
from plugins import load_plugins, loaded_plugins_status

PLUGIN_NAME_MAP = {
    "admin_plugin": "管理",
    "query_character_plugin": "查询角色",
    "query_inventory_plugin": "查询背包",
    "query_shop_plugin": "查询商店",
    "sync_plugin": "手动同步",
    "plugin_manager_plugin": "插件管理",
    "config_plugin": "配置管理",
    "log_plugin": "日志查看",
    "character_sync_plugin": "角色同步",
    "cultivation_plugin": "自动闭关",
    "inventory_sync_plugin": "背包同步(空壳)",
    "item_sync_plugin": "物品同步",
    "message_logger_plugin": "消息记录",
    "shop_sync_plugin": "商店同步",
    "xuangu_exam_plugin": "玄骨考校",
    "herb_garden_plugin": "自动药园",
    "yindao_plugin": "自动引道",
    "sect_checkin_plugin": "自动点卯",
    "sect_teach_plugin": "自动传功",
    "pagoda_plugin": "自动闯塔",
    "auto_learn_recipe_plugin": "自动学习配方",
    "marketplace_transfer_plugin": "市场资源转移",
    "recipe_manager_plugin": "配方管理",
    "smart_crafting_plugin": "智能炼制",
    "knowledge_plugin": "知识管理",
    "cache_query_plugin": "缓存查询",
    "game_event_notifier_plugin": "游戏事件通知",
    "nascent_soul_plugin": "自动元婴出窍",
    "demon_lord_event_plugin": "魔君降临", # 新增
}

event_bus = EventBus()
sys.path.append('.')

@asynccontextmanager
async def lifespan(ctx: AppContext):
    logger.info("应用程序启动中...")
    await ctx.redis.connect()
    await ctx.http.create_session()
    logger.info("加载插件...")
    ctx.plugin_name_map = PLUGIN_NAME_MAP
    load_plugins(ctx)
    ctx.plugin_statuses = loaded_plugins_status
    logger.info("启动调度器...")
    ctx.scheduler.start()
    logger.info("所有模块和服务已准备就绪 (除 TG 客户端连接)。")
    try: yield
    finally:
        logger.info("应用程序关闭中...")
        if ctx.http: await ctx.http.close_session()
        if ctx.redis: await ctx.redis.close()
        if ctx.scheduler and ctx.scheduler.running:
            logger.debug("正在关闭 Scheduler...")
            ctx.scheduler.shutdown(wait=False)
            logger.info("Scheduler 已关闭。")
        logger.info("所有受 lifespan 管理的服务已安全关闭。")

async def main():
    scheduler_module = Scheduler(config)
    redis_client = RedisClient(config)
    http_client = HTTPClient(config)
    gemini_client = GeminiClient(config)
    telegram_client = TelegramClient(event_bus=event_bus, config=config)

    app_context = AppContext()
    app_context.event_bus = event_bus
    app_context.scheduler = scheduler_module.get_instance()
    app_context.redis = redis_client
    app_context.http = http_client
    app_context.gemini = gemini_client
    app_context.config = config
    app_context.telegram_client = telegram_client
    app_context.data_manager = GameDataManager(app_context)
    logger.info("GameDataManager 已实例化并添加到 AppContext。")

    set_global_context(app_context)

    try:
        if not config.SETUP_NEEDED_FLAG:
             async with lifespan(app_context):
                 await telegram_client.run()
        else:
             logger.critical("!!! 系统仍处于设置模式或配置文件无效。请先运行设置。")
    except (KeyboardInterrupt, SystemExit):
        logger.info("收到退出信号。")
        if telegram_client and hasattr(telegram_client, 'app') and telegram_client.app.is_connected:
             logger.info("尝试停止 Telegram 客户端...")
             await telegram_client.app.stop()
    except Exception as e:
        logger.error(f"应用程序意外崩溃: {e}", exc_info=True)
        if telegram_client and hasattr(telegram_client, 'app') and telegram_client.app.is_connected:
             logger.info("崩溃后尝试停止 Telegram 客户端...")
             try: await telegram_client.app.stop()
             except Exception as stop_e: logger.error(f"崩溃后停止 TG 客户端失败: {stop_e}")

if __name__ == "__main__":
    os.makedirs("logs", exist_ok=True); os.makedirs("data", exist_ok=True)
    if SETUP_NEEDED_FLAG:
        logger.warning("进入设置模式...")
        try: from core.setup import run_setup; asyncio.run(run_setup(CONFIG_PATH)); logger.info("设置脚本已完成。请重启。"); sys.exit(0)
        except Exception as e: logger.error(f"\n设置过程出错: {e}", exc_info=True); sys.exit(1)
    else:
        try: asyncio.run(main())
        except Exception as e: logger.error(f"启动过程出错: {e}", exc_info=True)

