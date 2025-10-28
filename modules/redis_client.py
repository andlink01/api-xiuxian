import redis.asyncio as aioredis
import redis
import asyncio
from core.config import Config
from core.logger import logger
import json
from typing import Callable, Any, Coroutine # <--- 修正：导入 Coroutine

class RedisClient:
    def __init__(self, config: Config):
        self.config = config
        self.host = self.config.get("redis.host", "localhost")
        try:
             self.port = int(self.config.get("redis.port", 6379))
        except (ValueError, TypeError):
             logger.warning(f"Redis 端口配置无效 ('{self.config.get('redis.port')}'), 使用默认端口 6379。")
             self.port = 6379
        try:
             self.db = int(self.config.get("redis.db", 0))
        except (ValueError, TypeError):
             logger.warning(f"Redis 数据库配置无效 ('{self.config.get('redis.db')}'), 使用默认数据库 0。")
             self.db = 0
        self.password = self.config.get("redis.password", None)
        self.pool = None
        self.client: aioredis.Redis | None = None
        self._pubsub_client: aioredis.Redis | None = None # PubSub 专用客户端
        self._pubsub_connection: aioredis.PubSub | None = None # PubSub 连接对象
        self._subscribe_tasks: dict[str, asyncio.Task] = {} # 存储订阅任务

    async def connect(self):
        # 连接主客户端
        if self.client:
             try:
                 await asyncio.wait_for(self.client.ping(), timeout=5.0)
             except Exception as e:
                  logger.warning(f"Redis 主客户端 ping 失败 ({e.__class__.__name__})，尝试重新连接...")
                  await self.close()
                  self.client = None
                  self.pool = None

        if not self.client:
            try:
                self.pool = aioredis.ConnectionPool(
                    host=self.host, port=self.port, db=self.db, password=self.password,
                    decode_responses=True, socket_connect_timeout=5, socket_keepalive=True
                )
                self.client = aioredis.Redis.from_pool(self.pool)
                await asyncio.wait_for(self.client.ping(), timeout=5.0)
                logger.info(f"已连接到 Redis (主客户端): {self.host}:{self.port}")
            except Exception as e:
                logger.error(f"连接 Redis (主客户端) 失败 ({self.host}:{self.port}): {e.__class__.__name__}: {e}")
                await self._cleanup_on_error()

        # 连接 PubSub 客户端
        if self._pubsub_client:
            try:
                await asyncio.wait_for(self._pubsub_client.ping(), timeout=5.0)
            except Exception as e:
                logger.warning(f"Redis PubSub 客户端 ping 失败 ({e.__class__.__name__})，尝试重新连接...")
                await self.close_pubsub()
                self._pubsub_client = None

        if not self._pubsub_client:
            try:
                self._pubsub_client = aioredis.Redis(
                     host=self.host, port=self.port, db=self.db, password=self.password,
                     decode_responses=True, socket_connect_timeout=5, socket_keepalive=True
                )
                await asyncio.wait_for(self._pubsub_client.ping(), timeout=5.0)
                logger.info(f"已连接到 Redis (PubSub 客户端): {self.host}:{self.port}")
            except Exception as e:
                logger.error(f"连接 Redis (PubSub 客户端) 失败 ({self.host}:{self.port}): {e.__class__.__name__}: {e}")
                await self.close_pubsub()


    async def _cleanup_on_error(self):
        """主客户端连接错误时清理资源"""
        self.client = None
        if self.pool:
            try: self.pool.disconnect_on_connect_error = True
            except: pass
            self.pool = None

    async def close(self):
        """关闭所有 Redis 连接"""
        await self.close_pubsub()
        if self.client:
             try:
                 await self.client.close()
                 logger.debug("Redis 主客户端已关闭。")
             except Exception as e:
                 logger.warning(f"关闭 Redis 主客户端时出错: {e}")
             finally:
                  self.client = None
        if self.pool:
            try:
                logger.debug("Redis 连接池已关闭。")
            except Exception as e:
                 logger.warning(f"关闭 Redis 连接池时出错: {e}")
            finally:
                 self.pool = None

    async def close_pubsub(self):
        """仅关闭 PubSub 相关的连接和任务"""
        logger.debug("正在关闭 Redis PubSub 连接和任务...")
        for channel, task in list(self._subscribe_tasks.items()):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    logger.debug(f"订阅任务 '{channel}' 已取消。")
                except Exception as e:
                    logger.warning(f"等待订阅任务 '{channel}' 取消时出错: {e}")
        self._subscribe_tasks.clear()

        if self._pubsub_connection:
            try:
                await self._pubsub_connection.close()
                logger.debug("Redis PubSub 连接对象已关闭。")
            except Exception as e:
                 logger.warning(f"关闭 Redis PubSub 连接对象时出错: {e}")
            finally:
                self._pubsub_connection = None

        if self._pubsub_client:
             try:
                 await self._pubsub_client.close()
                 logger.debug("Redis PubSub 客户端已关闭。")
             except Exception as e:
                 logger.warning(f"关闭 Redis PubSub 客户端时出错: {e}")
             finally:
                  self._pubsub_client = None
        logger.debug("Redis PubSub 关闭完成。")


    def get_client(self) -> aioredis.Redis | None:
        """获取主 Redis 客户端 (用于 GET, SET 等)"""
        return self.client

    # --- Pub/Sub 方法 ---
    async def publish(self, channel: str, message: Any):
        """向指定频道发布消息 (序列化为 JSON)"""
        client = self.get_client()
        if not client:
            logger.error(f"无法发布到频道 '{channel}'：Redis 主客户端未连接。")
            return False
        try:
            message_json = json.dumps(message, ensure_ascii=False)
            await client.publish(channel, message_json)
            logger.debug(f"已向 Redis 频道 '{channel}' 发布消息: {message_json[:100]}...")
            return True
        except TypeError as e:
            logger.error(f"发布到频道 '{channel}' 时序列化消息失败: {e}. 消息: {message}")
            return False
        except Exception as e:
            logger.error(f"发布到频道 '{channel}' 时出错: {e}", exc_info=True)
            return False

    async def subscribe(self, channel: str, handler: Callable[[str, Any], Coroutine[Any, Any, None]]):
        """订阅指定频道，并在收到消息时调用异步 handler"""
        if not self._pubsub_client:
            logger.error(f"无法订阅频道 '{channel}'：Redis PubSub 客户端未连接。")
            return

        if channel in self._subscribe_tasks and not self._subscribe_tasks[channel].done():
            logger.warning(f"已存在对频道 '{channel}' 的订阅任务，跳过重复订阅。")
            return

        async def listen_task():
            if not self._pubsub_client:
                logger.error(f"订阅任务 '{channel}': PubSub 客户端丢失，任务退出。")
                return
            if not self._pubsub_connection:
                 # 创建一个新的 PubSub 对象，确保隔离性
                 pubsub_conn = self._pubsub_client.pubsub(ignore_subscribe_messages=True)
                 # 将其保存在任务的局部变量中可能更安全，或者需要更好的管理 _pubsub_connection 的生命周期
                 # 这里暂时简化处理，假设 _pubsub_connection 由第一个订阅者创建
                 # 注意：这个共享 _pubsub_connection 的方式在多个订阅者时可能有问题，更好的方式是每个 task 有自己的 pubsub 对象
                 # 但 redis-py 的 pubsub 对象通常建议单个连接使用
                 if not self.__class__._pubsub_connection: # 使用类变量暂存（需要加锁或更好管理）
                    self.__class__._pubsub_connection = pubsub_conn # 这种共享方式可能有隐患

            logger.info(f"开始监听 Redis 频道 '{channel}'...")
            try:
                # 使用当前任务关联的 pubsub 连接对象
                current_pubsub = self.__class__._pubsub_connection or self._pubsub_client.pubsub(ignore_subscribe_messages=True)
                if not self.__class__._pubsub_connection: self.__class__._pubsub_connection = current_pubsub

                await current_pubsub.subscribe(channel)
                async for message in current_pubsub.listen():
                    if message is None:
                        await asyncio.sleep(1)
                        continue
                    if message.get("type") == "message" and message.get("channel") == channel:
                        data_str = message.get("data")
                        logger.debug(f"收到来自 Redis 频道 '{channel}' 的消息: {data_str[:200]}...")
                        try:
                            data = json.loads(data_str)
                            await handler(channel, data)
                        except json.JSONDecodeError:
                             logger.error(f"处理频道 '{channel}' 消息时解析 JSON 失败: {data_str[:200]}...")
                        except Exception as handler_e:
                             logger.error(f"处理频道 '{channel}' 消息时 handler 出错: {handler_e}", exc_info=True)
            except asyncio.CancelledError:
                 logger.info(f"订阅任务 '{channel}' 被取消。")
                 if current_pubsub:
                     try: await current_pubsub.unsubscribe(channel)
                     except: pass
            except redis.exceptions.ConnectionError as conn_err:
                 logger.error(f"订阅任务 '{channel}': Redis 连接错误: {conn_err}。任务将退出，等待重连。")
                 await self.close_pubsub() # 尝试关闭旧连接
            except Exception as e:
                 logger.error(f"订阅任务 '{channel}' 发生意外错误: {e}", exc_info=True)
            finally:
                 logger.info(f"结束监听 Redis 频道 '{channel}'。")
                 self._subscribe_tasks.pop(channel, None)

        task = asyncio.create_task(listen_task())
        self._subscribe_tasks[channel] = task

    # --- Pub/Sub 方法结束 ---

# 类变量用于共享 PubSub 连接（需要改进或移除）
RedisClient._pubsub_connection = None

