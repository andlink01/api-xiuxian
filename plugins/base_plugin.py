import logging
# --- 导入 AppContext 类型提示 ---
from typing import TYPE_CHECKING, Optional, Dict # 添加 Optional, Dict
if TYPE_CHECKING:
    from modules.telegram_client import TelegramClient
    from modules.redis_client import RedisClient
    from modules.http_client import HTTPClient
    from modules.gemini_client import GeminiClient
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from modules.game_data_manager import GameDataManager
    from core.config import Config
    from core.event_bus import EventBus
# --- 类型提示结束 ---

class AppContext:
    """存放共享模块的容器"""
    def __init__(self):
        self.event_bus: Optional['EventBus'] = None
        self.scheduler: Optional['AsyncIOScheduler'] = None
        self.redis: Optional['RedisClient'] = None
        self.http: Optional['HTTPClient'] = None
        self.gemini: Optional['GeminiClient'] = None
        self.config: Optional['Config'] = None
        self.plugin_name_map: Dict[str, str] = {} # 插件英文名到中文名的映射
        self.data_manager: Optional['GameDataManager'] = None # GameDataManager 实例
        self.telegram_client: Optional['TelegramClient'] = None # TelegramClient 实例
        self.plugin_statuses: Dict[str, str] = {} # 插件加载状态

class BasePlugin:
    """所有插件的基类"""
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        self.context = context
        self.event_bus = context.event_bus
        self.scheduler = context.scheduler # 直接引用 AsyncIOScheduler 实例
        self.redis = context.redis
        self.http = context.http
        self.gemini = context.gemini
        self.config = context.config
        self.data_manager = context.data_manager
        # --- (修改: 获取 logger 名称) ---
        # 原: self.logger = logging.getLogger(f"Plugin.{self.plugin_name}")
        # 改: 确保是 GameAssistant 的子 logger
        self.logger = logging.getLogger(f"GameAssistant.Plugin.{plugin_name}")
        # --- (修改结束) ---
        self.plugin_name = plugin_name
        self.cn_name = cn_name or plugin_name


    # --- 日志辅助方法 (保持不变) ---
    def _log(self, level: int, msg: str, *args, **kwargs):
        prefix = f"【{self.cn_name}】"
        # 使用 self.logger 记录日志
        self.logger.log(level, f"{prefix} {msg}", *args, **kwargs)
    def debug(self, msg: str, *args, **kwargs): self._log(logging.DEBUG, msg, *args, **kwargs)
    def info(self, msg: str, *args, **kwargs): self._log(logging.INFO, msg, *args, **kwargs)
    def warning(self, msg: str, *args, **kwargs): self._log(logging.WARNING, msg, *args, **kwargs)
    def error(self, msg: str, *args, **kwargs): self._log(logging.ERROR, msg, *args, **kwargs)
    def critical(self, msg: str, *args, **kwargs): self._log(logging.CRITICAL, msg, *args, **kwargs)
    # --- 日志辅助方法结束 ---

    def register(self):
        """插件注册方法，必须由子类实现"""
        raise NotImplementedError("插件必须实现 register() 方法")

# --- 添加 Optional 和 Dict 导入 ---
# from typing import Optional, Dict # 已移到顶部
# --- 添加结束 ---

