import logging
import sys
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from logging import StreamHandler # 导入 StreamHandler
from core.config import Config

class LocalTimeFormatterBase(logging.Formatter):
    """基础 Formatter，仅用于提供本地时间转换"""
    converter = datetime.fromtimestamp
    def formatTime(self, record, datefmt=None):
        dt = self.converter(record.created)
        s = dt.strftime("%Y-%m-%d %H:%M:%S") + f",{int(dt.microsecond / 1000):03d}"
        return s

# --- (修改: MultiLineFormatter - 调整分隔符和结尾换行) ---
class MultiLineFormatter(LocalTimeFormatterBase):
    """自定义 Formatter，实现多行日志输出和分隔符"""

    def format(self, record):
        log_header = f"{self.formatTime(record)} - {record.name} - {record.levelname}"
        log_message = record.getMessage()

        if record.exc_info:
            exc_text = self.formatException(record.exc_info)
            log_message += '\n' + exc_text
        if record.stack_info:
             stack_text = self.formatStack(record.stack_info)
             log_message += '\n' + stack_text

        # 定义分隔符
        separator = "=" * 45

        # 组合格式: 头 + 换行 + 消息 + 换行 + 分隔符 + 换行 + 空行
        return f"{log_header}\n{log_message}\n{separator}\n\n" # 结尾包含两个换行符

# --- (修改结束) ---

class ReopenableMixin:
    """Mixin 类，提供重新打开文件的能力"""
    # 这个 Mixin 保持不变
    def emit(self, record):
        if self.stream is None or (hasattr(self.stream, 'name') and not os.path.exists(self.stream.name)):
            try:
                if self.stream:
                    self.stream.close()
                    self.stream = None
                self.stream = self._open()
            except Exception:
                self.handleError(record)
                return
        # 让子类实现具体的写入逻辑
        super().emit(record) # 调用继承链中的下一个 emit


# --- (修改: 自定义 Handlers - 直接写入 Formatter 结果) ---
class SeparatedStreamHandler(StreamHandler):
    """自定义 StreamHandler，处理多行格式并写入"""
    def emit(self, record):
        """Emit a record."""
        try:
            msg = self.format(record) # 获取包含所有换行和分隔符的完整字符串
            stream = self.stream
            # 直接写入 Formatter 返回的完整字符串
            stream.write(msg) # msg 已经包含了结尾的 \n==...==\n\n
            stream.flush()
        except RecursionError:
             raise
        except Exception:
             self.handleError(record)

class SeparatedRotatingFileHandler(ReopenableMixin, RotatingFileHandler):
    """自定义 RotatingFileHandler，处理多行格式并写入，支持重新打开"""

    def emit(self, record):
         """Emit a record."""
         # --- ReopenableMixin 检查逻辑 ---
         if self.stream is None or (hasattr(self.stream, 'name') and not os.path.exists(self.stream.name)):
             try:
                 if self.stream:
                     self.stream.close()
                     self.stream = None
                 self.stream = self._open()
             except Exception:
                 self.handleError(record)
                 return
         # --- 检查逻辑结束 ---

         try:
             msg = self.format(record) # 获取包含所有换行和分隔符的完整字符串
             stream = self.stream
             # 检查是否需要轮转
             if self.shouldRollover(record):
                  self.doRollover()
             # 直接写入 Formatter 返回的完整字符串
             stream.write(msg) # msg 已经包含了结尾的 \n==...==\n\n
             stream.flush()
         except RecursionError:
              raise
         except Exception:
              self.handleError(record)
# --- (修改结束) ---


def setup_logging(config: Config):
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    log_file_path = os.path.join(log_dir, "game_assistant.log")

    # 使用新的 MultiLineFormatter
    log_formatter = MultiLineFormatter()

    # 使用自定义 Handlers
    stream_handler = SeparatedStreamHandler(sys.stdout)
    file_handler = SeparatedRotatingFileHandler(
        log_file_path,
        maxBytes=10 * 1024 * 1024, # 10 MB
        backupCount=5,
        encoding='utf-8'
    )

    stream_handler.setFormatter(log_formatter)
    file_handler.setFormatter(log_formatter)

    # 配置根 logger (保持不变)
    root_logger = logging.getLogger()
    log_level_str = config.get("logging.level", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)
    root_logger.setLevel(log_level)

    if root_logger.hasHandlers():
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
            handler.close()

    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)

    # GameAssistant logger (保持不变)
    ga_logger = logging.getLogger("GameAssistant")
    ga_logger.setLevel(log_level)
    ga_logger.propagate = True
    if ga_logger.hasHandlers():
        for handler in ga_logger.handlers[:]:
            ga_logger.removeHandler(handler)
            handler.close()

    # 设置其他库的日志级别 (保持不变)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("pyrogram").setLevel(logging.WARNING)

    ga_logger.info(f"日志系统已启动 (根级别: {log_level_str})，主日志文件位于宿主机: ./logs/game_assistant.log")

    return ga_logger

_logger_instance = None

def get_logger():
    """获取 logger 实例 (获取 GameAssistant logger)"""
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = logging.getLogger("GameAssistant")
    return _logger_instance


def initialize_logger(config: Config):
    """初始化全局 logger 实例 (配置根 logger)"""
    global _logger_instance
    setup_logging(config) # 配置根 logger
    _logger_instance = logging.getLogger("GameAssistant") # 确保全局变量指向 GameAssistant
    preinit_logger = logging.getLogger("GameAssistant.PreInit")
    if preinit_logger.hasHandlers():
        preinit_logger.handlers.clear()
        preinit_logger.propagate = False
    return _logger_instance

logger = get_logger()

