import importlib
import pkgutil
import asyncio
from pathlib import Path
from core.logger import logger
from plugins.base_plugin import BasePlugin, AppContext
import redis # 导入 redis 用于同步检查

loaded_plugins_status = {} # 内存中记录实际加载状态

def load_plugins(context: AppContext):
    """
    Dynamically load plugins from the 'plugins' directory, checking user-specific status in Redis.
    """
    plugins_path = Path(__file__).parent
    logger.info(f"【插件加载】开始加载插件，路径: {plugins_path}...")

    plugin_name_map = context.plugin_name_map # 从 Context 获取中文名映射
    my_id = None
    if context.telegram_client:
        try:
             if context.telegram_client._me: my_id = context.telegram_client._me.id
        except Exception: logger.warning("【插件加载】无法在插件加载时获取助手 User ID，将为所有用户启用插件。")

    can_check_redis = bool(context.redis and my_id)
    if not can_check_redis: logger.warning(f"【插件加载】无法检查用户 {my_id or '?'} 的插件启用状态 (Redis或TG ID不可用)，将尝试加载所有插件。")
    redis_client = context.redis.get_client() if can_check_redis else None

    plugin_names = []
    for _, name, _ in pkgutil.iter_modules([str(plugins_path)]):
        if name and name.endswith("_plugin") and name != "base_plugin": plugin_names.append(name)
        elif name and name != "base_plugin": logger.debug(f"【插件加载】跳过非插件文件 (或 base_plugin): {name}")

    for name in plugin_names:
        status_in_redis = 'enabled' # 默认启用
        # --- 暂时跳过 Redis 状态检查的逻辑 ---
        # if can_check_redis and redis_client:
        #     plugin_status_key_user = f"plugin_status:{my_id}:{name}"
        #     try:
        #         logger.debug(f"【插件加载】检查插件 {name} 的 Redis 状态...")
        #         status_val = await redis_client.get(plugin_status_key_user)
        #         status_in_redis = status_val if status_val in ['enabled', 'disabled'] else 'enabled' # 默认启用
        #     except Exception as e:
        #         logger.error(f"【插件加载】检查插件 {name} 状态时 Redis 出错: {e}，将尝试加载。")
        #         status_in_redis = 'enabled'
        # --- 结束跳过 ---

        # 特殊处理：强制启用某些核心插件 (如果需要)
        # if name == "cultivation_plugin": status_in_redis = 'enabled'

        if status_in_redis == 'disabled':
            cn_name = plugin_name_map.get(name, name)
            logger.info(f"【插件加载】插件【{cn_name}】({name}) 配置为禁用，跳过加载。")
            loaded_plugins_status[name] = 'disabled'
            continue
        elif status_in_redis != 'enabled':
             cn_name = plugin_name_map.get(name, name)
             logger.warning(f"【插件加载】插件【{cn_name}】({name}) 在 Redis 中的状态值 '{status_in_redis}' 无效，将尝试加载。")

        try:
            module = importlib.import_module(f"plugins.{name}")
            if hasattr(module, "Plugin"):
                PluginClass = getattr(module, "Plugin")
                if issubclass(PluginClass, BasePlugin):
                    cn_name = plugin_name_map.get(name, name)
                    logger.info(f"【插件加载】开始加载【{cn_name}】({name})...")
                    plugin_instance = PluginClass(context, name, cn_name)
                    plugin_instance.register()
                    logger.info(f"【插件加载】成功加载并注册【{cn_name}】({name})")
                    loaded_plugins_status[name] = 'enabled'
                else:
                    logger.warning(f"【插件加载】在 {name} 中找到 'Plugin' 类, 但它不是 BasePlugin 的子类。")
                    loaded_plugins_status[name] = 'load_error'
            else:
                logger.warning(f"【插件加载】在 {name} 中未找到 'Plugin' 类。")
                loaded_plugins_status[name] = 'load_error'
        except Exception as e:
            cn_name = plugin_name_map.get(name, name)
            logger.error(f"【插件加载】加载插件【{cn_name}】({name}) 失败: {e}", exc_info=True)
            loaded_plugins_status[name] = 'load_error'

    # 检查是否有文件存在但未被处理
    all_plugin_files = {name for _, name, _ in pkgutil.iter_modules([str(plugins_path)]) if name and name.endswith("_plugin") and name != "base_plugin"}
    for name in all_plugin_files:
        if name not in loaded_plugins_status:
             cn_name = plugin_name_map.get(name, name)
             loaded_plugins_status[name] = 'not_loaded' # 标记为未加载
             logger.warning(f"【插件加载】插件文件【{cn_name}】({name}) 存在但未被加载。")

    logger.info(f"【插件加载】插件加载完成。状态记录: {loaded_plugins_status}")

