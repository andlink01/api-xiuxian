import yaml
import logging
import sys 
import os 

# logger 需要在使用前被定义，但 setup_logging 可能依赖 Config
# 我们先用基本配置，之后 main.py 会重新配置 logger
logging.basicConfig(level=logging.WARNING) 
logger = logging.getLogger("GameAssistant.Config") # 使用特定名字

CONFIG_PATH="config.yaml" # 定义默认路径常量

# --- (修正点: 检查 setup 状态函数) ---
def check_setup_needed(path=CONFIG_PATH):
    if not os.path.exists(path):
        logger.warning(f"配置文件 {path} 未找到，假定需要设置。")
        return True 
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
            # 如果是字典，并且只包含 setup_needed: true，则认为是 setup 模式
            # 或者如果文件为空，也认为是 setup 模式
            if data is None: 
                logger.warning(f"配置文件 {path} 为空，假定需要设置。")
                return True
            if isinstance(data, dict) and data.get("setup_needed") and len(data) == 1:
                logger.info(f"检测到 'setup_needed: true'。")
                return True
    except Exception as e:
        logger.error(f"检查配置文件 {path} 时出错: {e}，假定需要设置。")
        return True # 读取失败，也认为是需要 setup
    logger.debug(f"配置文件 {path} 正常，不需要设置。")
    return False

# --- (修正点: 将标志作为类属性或模块变量) ---
SETUP_NEEDED_FLAG = check_setup_needed()
# --- (修正结束) ---


class Config:
    # --- (修正点: 将标志作为类属性) ---
    SETUP_NEEDED_FLAG = SETUP_NEEDED_FLAG # 方便外部访问 config.SETUP_NEEDED_FLAG
    # --- (修正结束) ---

    def __init__(self, path=CONFIG_PATH):
        self.config_path = path
        self.config_data = {} # 确保初始化为空字典
        try:
            # 只有在非 setup 模式下才尝试加载完整配置并强制要求有效
            if not self.SETUP_NEEDED_FLAG:
                logger.debug(f"加载配置文件: {self.config_path}")
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    self.config_data = yaml.safe_load(f)
                
                if not isinstance(self.config_data, dict):
                    # 在非 setup 模式下，无效配置是致命错误
                    logger.critical(f"配置文件 {self.config_path} 内容格式不正确 (应为字典)。程序无法运行。")
                    sys.exit(1)
                # 检查是否意外包含了 setup_needed: true
                if self.config_data.get("setup_needed"):
                     logger.warning(f"配置文件 {self.config_path} 似乎是一个未完成的设置文件。请删除 'setup_needed' 行或完成设置。")
                     # 可以选择退出或继续使用（可能部分配置缺失）
                     # sys.exit(1) 

            else:
                 # 在 setup 模式下，允许 config_data 为空
                 logger.warning(f"处于设置模式或配置文件无效，将使用空配置进行初始化。")
                 self.config_data = {}

        except FileNotFoundError:
            # 在非 setup 模式下找不到文件是致命错误
            if not self.SETUP_NEEDED_FLAG:
                 logger.critical(f"配置文件 {self.config_path} 未找到。程序无法运行。请先运行设置。")
                 sys.exit(1)
            else:
                 # setup 模式下找不到文件是正常的
                 logger.warning(f"配置文件 {self.config_path} 未找到。将进入设置模式。")
                 self.config_data = {}
        except Exception as e:
            logger.error(f"加载配置文件 {self.config_path} 失败: {e}")
            if not self.SETUP_NEEDED_FLAG:
                 logger.critical("无法加载配置文件。程序无法运行。")
                 sys.exit(1)
            else:
                 logger.warning("加载配置文件失败。将进入设置模式。")
                 self.config_data = {}

    def get(self, key, default=None):
        # 如果 config_data 为空 (setup 模式或加载失败)，直接返回 default
        if not self.config_data: 
             # logger.debug(f"Config is empty, returning default for key '{key}'.")
             return default

        keys = key.split('.')
        value = self.config_data
        try:
            current_key_path = []
            for k in keys:
                current_key_path.append(k)
                if not isinstance(value, dict): 
                    # logger.warning(f"Config path '{'.'.join(current_key_path[:-1])}' is not a dictionary, cannot get '{k}'.")
                    return default
                value = value[k] # 可能触发 KeyError
            # 如果找到的值是 None，也返回 default
            return value if value is not None else default
        except KeyError:
            # logger.debug(f"Key '{key}' not found in config.")
            return default
        except TypeError: # 例如 value 变成了 None 后尝试访问 value[k]
             # logger.warning(f"TypeError accessing key '{key}'. Path might be incorrect.")
             return default

# 全局实例已移至 main.py
