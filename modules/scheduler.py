from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from modules.db import get_db_engine 
from core.config import Config 
from core.logger import logger

class Scheduler:
    def __init__(self, config: Config):
        self.config = config 
        # --- (修正点: 在这里调用 get_db_engine) ---
        # get_db_engine 现在返回 Engine 或 None
        engine = get_db_engine(self.config) 
        # --- (修正结束) ---
        
        if not engine:
            logger.error("数据库引擎未初始化或连接失败，APScheduler 将使用内存存储。")
            jobstores = {'default': None} 
            self.using_db = False # 标记未使用数据库
        else:
            try:
                # 尝试使用 SQLAlchemyJobStore
                jobstores = {
                    'default': SQLAlchemyJobStore(engine=engine)
                }
                # 可以在这里添加一个小的测试，看 jobstore 是否能工作
                # test_scheduler = AsyncIOScheduler(jobstores=jobstores) # 临时创建测试
                # test_scheduler.add_job(lambda: None, id='_test_job_store') # 尝试添加任务
                # test_scheduler.remove_job('_test_job_store') # 移除测试任务
                logger.info("APScheduler 将使用 SQLAlchemyJobStore 进行持久化。")
                self.using_db = True # 标记使用数据库
            except Exception as e:
                 logger.error(f"初始化 SQLAlchemyJobStore 时出错: {e}。APScheduler 将回退到内存存储。", exc_info=True)
                 jobstores = {'default': None} 
                 self.using_db = False

        # 创建最终的 scheduler 实例
        self.scheduler = AsyncIOScheduler(jobstores=jobstores)
        logger.info("APScheduler 已初始化。")

    def start(self):
        try:
            self.scheduler.start()
            status = "使用数据库持久化" if self.using_db else "使用内存存储"
            logger.info(f"APScheduler 已启动 ({status})。")
        except Exception as e:
            logger.error(f"启动 APScheduler 失败: {e}")

    def add_job(self, *args, **kwargs):
        # 添加日志记录，方便调试 Pickle Error
        func = args[0] if args else kwargs.get('func')
        job_id = kwargs.get('id', func.__name__ if func else 'unknown')
        logger.debug(f"尝试添加任务到 APScheduler: id='{job_id}', func='{func.__name__ if func else 'N/A'}'")
        try:
            job = self.scheduler.add_job(*args, **kwargs)
            logger.info(f"成功添加/更新任务: id='{job_id}'")
            return job
        except TypeError as e:
             # 特别捕获 Pickle Error
             if "cannot pickle" in str(e):
                  logger.error(f"添加任务 '{job_id}' 失败 (Pickle Error): {e}。任务函数或其参数包含无法序列化的对象。请确保任务函数是顶级函数或静态方法，并且不传递复杂的实例作为参数。", exc_info=True)
             else:
                  logger.error(f"添加任务 '{job_id}' 时发生 TypeError: {e}", exc_info=True)
             return None # 添加失败
        except Exception as e:
             logger.error(f"添加任务 '{job_id}' 时发生未知错误: {e}", exc_info=True)
             return None # 添加失败


    def get_instance(self) -> AsyncIOScheduler: 
        return self.scheduler
