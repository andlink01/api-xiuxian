import logging
from typing import Optional

# 导入 AppContext 的定义，但要小心循环导入
# 我们只在这里用于类型注解
if __name__ == "__main__":
    # This is to avoid circular import if run directly, though it shouldn't be.
    pass

# 类型提示
from plugins.base_plugin import AppContext

logger = logging.getLogger("GameAssistant.Context")

_global_app_context: Optional[AppContext] = None

def set_global_context(ctx: AppContext):
    """
    由 main.py 在启动时调用，用于设置全局上下文。
    """
    global _global_app_context
    if _global_app_context is not None:
        logger.warning("全局上下文已被设置，现正被覆盖。")
    _global_app_context = ctx
    logger.debug(f"全局上下文已设置: {ctx}")

def get_global_context() -> Optional[AppContext]:
    """
    由插件的定时任务 (APScheduler jobs) 调用，用于在运行时获取上下文。
    """
    if _global_app_context is None:
        # 这个错误是致命的，表明任务在 context 准备好之前就运行了
        logger.critical("get_global_context() 被调用，但 _global_app_context 仍为 None！")
    return _global_app_context

