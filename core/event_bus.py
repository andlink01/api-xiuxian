import asyncio
from collections import defaultdict
import inspect 
from core.logger import logger 

class EventBus:
    def __init__(self):
        self._listeners = defaultdict(list)
        self._running_tasks = set() 

    def on(self, event_name: str, callback):
        """注册一个事件监听器"""
        if not asyncio.iscoroutinefunction(callback):
             logger.error(f"事件监听器注册失败: '{getattr(callback, '__name__', 'Unknown')}' 必须是一个 async 函数。")
             return 
             
        self._listeners[event_name].append(callback)
        # --- (修改: 使用 DEBUG 级别) ---
        logger.debug(f"EventBus: 已注册事件 '{event_name}' 的监听器: {getattr(callback, '__module__', '?')}.{getattr(callback, '__name__', 'Unknown')}")
        # --- (修改结束) ---

    async def emit(self, event_name: str, *args, **kwargs):
        """
        异步触发一个事件，为每个监听器创建一个独立的任务。
        """
        logger.debug(f"EventBus: 触发事件: '{event_name}' (Args: {len(args)}, Kwargs: {len(kwargs)})") 
        listeners_to_run = self._listeners.get(event_name) 
        
        # --- (新增 Debug 日志) ---
        if listeners_to_run:
            logger.debug(f"EventBus: 找到 {len(listeners_to_run)} 个监听器 for '{event_name}':")
            for i, listener in enumerate(listeners_to_run):
                 logger.debug(f"  [{i+1}] {getattr(listener, '__module__', '?')}.{getattr(listener, '__name__', 'Unknown')}")
            # --- (新增结束) ---
            
            tasks = []
            for listener in listeners_to_run:
                listener_name = f"{getattr(listener, '__module__', '?')}.{getattr(listener, '__name__', 'Unknown')}" # (修改: 包含模块名)
                logger.debug(f"EventBus: 为事件 '{event_name}' 创建任务: {listener_name}") # (修改: 改为 DEBUG)
                
                task = asyncio.create_task(self._execute_listener(listener, event_name, args, kwargs)) 
                self._running_tasks.add(task)
                task.add_done_callback(self._running_tasks.discard)
                tasks.append(task)
            
            logger.debug(f"EventBus: 已为事件 '{event_name}' 创建 {len(tasks)} 个监听任务。") 
        else: 
           logger.debug(f"EventBus: 事件 '{event_name}' 没有注册任何监听器。") 

    async def _execute_listener(self, callback, event_name: str, args: tuple, kwargs: dict):
        """安全地执行单个事件监听器"""
        callback_name = f"{getattr(callback, '__module__', '?')}.{getattr(callback, '__name__', 'Unknown')}" # (修改: 包含模块名)
        # --- (新增 Debug 日志) ---
        logger.debug(f"EventBus: 开始执行监听器 '{callback_name}' for event '{event_name}'...")
        try:
            await callback(*args, **kwargs) 
            logger.debug(f"EventBus: 监听器 '{callback_name}' 执行完毕。") # (新增)
        # --- (新增结束) ---
        except Exception as e:
            log_message = (
                f"执行事件 '{event_name}' 的监听器 '{callback_name}' 时出错: "
                f"{e.__class__.__name__}: {e}. (Args: {len(args)}, Kwargs: {len(kwargs)})"
            )
            logger.error(log_message, exc_info=True)
