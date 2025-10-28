from sqlalchemy import create_engine, Engine 
from core.config import Config
from core.logger import logger
import os 

def get_db_engine(config: Config) -> Engine | None:
    """根据配置创建并返回 SQLAlchemy 引擎"""
    db_url = config.get("database.sqlite_url") 
    
    # --- (修正点: 添加默认值，确保路径指向 /app/data) ---
    if not db_url:
        default_db_path = "/app/data/local_data.db"
        logger.warning(f"数据库 URL 未在配置中找到，将使用默认路径: {default_db_path}")
        db_url = f"sqlite:///{default_db_path}"
    # --- (修正结束) ---

    engine = None
    db_connected = False

    try:
        # 确保目录存在 (因为 engine 不会自动创建)
        if db_url.startswith("sqlite:///"):
            db_path = db_url.split(":///")[1]
            db_dir = os.path.dirname(db_path)
            if db_dir: # 只有在有目录部分时才创建
                 os.makedirs(db_dir, exist_ok=True) # 使用 os.makedirs
                 logger.debug(f"确保数据库目录存在: {db_dir}")

        engine = create_engine(db_url, connect_args={"check_same_thread": False})
        
        # 使用 engine.connect() 测试连接
        with engine.connect() as connection:
             # 现在可以安全地获取文件名
             db_filename = os.path.basename(db_url.split(":///")[1]) if ":///" in db_url else "unknown.db"
             # 指向宿主机的相对路径
             logger.info(f"已连接到 SQLite 数据库 (位于宿主机: ./data/{db_filename})")
             db_connected = True 

    except Exception as e:
        logger.error(f"连接 SQLite 数据库 ({db_url}) 失败: {e}", exc_info=True) # 添加 exc_info
        engine = None 

    if not db_connected:
         logger.warning("SQLite 未连接，依赖数据库的功能可能无法正常工作 (例如 APScheduler 持久化)。")
         
    return engine 
