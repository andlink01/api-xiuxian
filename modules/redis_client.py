import redis.asyncio as aioredis
import redis
import asyncio
from core.config import Config
from core.logger import logger
import json
from typing import Callable, Any, Coroutine, Dict # <--- 修正：导入 Coroutine, Dict

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
        self._channel_handlers: Dict[str, Callable[[str, Any], Coroutine[Any, Any, None]]] = {}
        self._pubsub_ready_event = asyncio.Event()
        self._listener_task: asyncio.Task | None = None

    async def connect(self):
        # 连接主客户端 (逻辑不变)
        if self.client:
             try:
                 await asyncio.wait_for(self.client.ping(), timeout=5.0)
             except Exception as e:
                  logger.warning(f"Redis 主客户端 ping 失败 ({e.__class__.__name__})，尝试重新连接...")
                  await self.close() # close 会清理 pubsub
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
                await self.close_pubsub() # 先关闭旧的
                self._pubsub_client = None
                self._pubsub_connection = None # 确保 connection 也被清理
                self._pubsub_ready_event.clear() # 重置事件

        if not self._pubsub_client:
            try:
                self._pubsub_client = aioredis.Redis(
                     host=self.host, port=self.port, db=self.db, password=self.password,
                     decode_responses=True, socket_connect_timeout=5, socket_keepalive=True
                )
                await asyncio.wait_for(self._pubsub_client.ping(), timeout=5.0)
                logger.info(f"已连接到 Redis (PubSub 客户端): {self.host}:{self.port}")
                self._pubsub_connection = self._pubsub_client.pubsub(ignore_subscribe_messages=True)
                # 确保监听任务只启动一次
                if self._listener_task is None or self._listener_task.done():
                    self._listener_task = asyncio.create_task(self._listen_for_messages(), name="redis_pubsub_listener")
                    logger.info("已创建后台 Redis PubSub 消息监听器 (等待订阅)。")
                else:
                    logger.debug("后台 Redis PubSub 消息监听器已在运行。")
                # 重新订阅之前注册的频道 (如果存在)
                await self._resubscribe_channels()

            except Exception as e:
                logger.error(f"连接 Redis (PubSub 客户端) 或创建/重订阅失败 ({self.host}:{self.port}): {e.__class__.__name__}: {e}")
                await self.close_pubsub() # 出错时关闭

    async def _cleanup_on_error(self):
        """主客户端连接错误时清理资源"""
        self.client = None
        if self.pool:
            try: await self.pool.disconnect()
            except Exception as e: logger.warning(f"关闭主连接池出错: {e}")
            self.pool = None

    async def close(self):
        """关闭所有 Redis 连接"""
        await self.close_pubsub() # 先关闭 pubsub
        if self.client:
             try:
                 await self.client.close()
                 if self.pool: # 检查 pool 是否存在
                    await self.pool.disconnect() # 确保连接池关闭
                 logger.debug("Redis 主客户端及连接池已关闭。")
             except Exception as e:
                 logger.warning(f"关闭 Redis 主客户端或连接池时出错: {e}")
             finally:
                  self.client = None
                  self.pool = None

    async def close_pubsub(self):
        """仅关闭 PubSub 相关的连接和任务"""
        logger.debug("正在关闭 Redis PubSub 连接和监听任务...")
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try: await self._listener_task
            except asyncio.CancelledError: logger.debug("后台 PubSub 监听任务已取消。")
            except Exception as e: logger.warning(f"等待后台 PubSub 监听任务取消时出错: {e}")
        self._listener_task = None

        if self._pubsub_connection:
            try:
                try: # 尝试取消订阅
                    if self._channel_handlers:
                         await self._pubsub_connection.unsubscribe(*self._channel_handlers.keys())
                except Exception: pass
                await self._pubsub_connection.close()
                logger.debug("Redis PubSub 连接对象已关闭。")
            except Exception as e: logger.warning(f"关闭 Redis PubSub 连接对象时出错: {e}")
            finally:
                self._pubsub_connection = None
                self._pubsub_ready_event.clear() # 重置事件

        if self._pubsub_client:
             try:
                 await self._pubsub_client.close()
                 if hasattr(self._pubsub_client, 'connection_pool'):
                     await self._pubsub_client.connection_pool.disconnect()
                 logger.debug("Redis PubSub 客户端及连接池已关闭。")
             except Exception as e: logger.warning(f"关闭 Redis PubSub 客户端或连接池时出错: {e}")
             finally: self._pubsub_client = None
        logger.debug("Redis PubSub 关闭完成。")

    def get_client(self) -> aioredis.Redis | None:
        """获取主 Redis 客户端 (用于 GET, SET 等)"""
        return self.client

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
        """注册指定频道的处理器并确保已订阅"""
        if channel in self._channel_handlers:
            logger.warning(f"频道 '{channel}' 的处理器已被覆盖。")
        self._channel_handlers[channel] = handler
        logger.info(f"已注册 Redis 频道 '{channel}' 的处理器。")

        if not self._pubsub_connection:
             logger.error(f"无法订阅频道 '{channel}'：PubSub 连接未建立。")
             return

        try:
            await self._pubsub_connection.subscribe(channel)
            logger.info(f"已成功订阅 Redis 频道 '{channel}'。")
            if not self._pubsub_ready_event.is_set():
                self._pubsub_ready_event.set()
                logger.info("PubSub 监听器已就绪 (首次订阅成功)。")
            asyncio.create_task(self._emit_channel_ready_event(channel))
        except Exception as e:
            logger.error(f"订阅 Redis 频道 '{channel}' 失败: {e}")
            await self.close_pubsub()
            self._pubsub_ready_event.clear()

    async def _emit_channel_ready_event(self, channel: str):
         """在事件循环的下一个迭代中发出频道就绪事件"""
         await asyncio.sleep(0) # 确保在当前任务完成后执行
         try:
             from core.context import get_global_context
             context = get_global_context()
             if context and context.event_bus:
                 await context.event_bus.emit(f"redis_subscription_ready:{channel}")
                 logger.debug(f"已发出事件: redis_subscription_ready:{channel}")
         except Exception as e:
             logger.error(f"发出频道就绪事件 redis_subscription_ready:{channel} 时出错: {e}")

    async def _listen_for_messages(self):
        """后台任务，持续监听所有已订阅频道的消息"""
        logger.info("PubSub 监听任务已启动，等待 PubSub 就绪事件...")
        await self._pubsub_ready_event.wait() # 等待至少一个订阅成功
        # --- 新增: 添加短暂延时 ---
        await asyncio.sleep(0.5) # 给连接一点时间稳定
        # --- 新增结束 ---
        logger.info("PubSub 监听器开始监听消息...")

        while True:
            if not self._pubsub_connection:
                logger.warning("PubSub 监听器：连接丢失，等待重连...")
                self._pubsub_ready_event.clear() # 需要重新设置就绪状态
                await asyncio.sleep(10) # 简单等待
                # --- 修改: 再次等待事件并添加延时 ---
                await self._pubsub_ready_event.wait()
                await asyncio.sleep(0.5)
                logger.info("PubSub 监听器重新就绪，继续监听...")
                # --- 修改结束 ---
                continue
            try:
                message = await self._pubsub_connection.get_message(ignore_subscribe_messages=True, timeout=10)
                if message is None:
                    # logger.debug("PubSub 监听器：等待消息...")
                    await asyncio.sleep(0.1) # 短暂休眠
                    continue

                if message.get("type") == "message":
                    channel = message.get("channel")
                    data_str = message.get("data")
                    if channel and data_str and channel in self._channel_handlers:
                        logger.debug(f"收到来自 Redis 频道 '{channel}' 的消息: {data_str[:200]}...")
                        handler = self._channel_handlers[channel]
                        try:
                            data = json.loads(data_str)
                            asyncio.create_task(handler(channel, data))
                        except json.JSONDecodeError:
                             logger.error(f"处理频道 '{channel}' 消息时解析 JSON 失败: {data_str[:200]}...")
                        except Exception as handler_e:
                             logger.error(f"调用频道 '{channel}' 处理器时出错: {handler_e}", exc_info=True)
                    elif channel and channel not in self._channel_handlers:
                         logger.warning(f"收到频道 '{channel}' 的消息，但未找到对应的处理器。")

            except redis.exceptions.TimeoutError: # 捕获 get_message 的超时
                 logger.debug("PubSub get_message 超时，继续监听...")
                 continue
            except redis.exceptions.ConnectionError as conn_err:
                 logger.error(f"PubSub 监听器: Redis 连接错误: {conn_err}。尝试关闭并等待重连...")
                 await self.close_pubsub() # 关闭 pubsub 相关连接
                 await asyncio.sleep(15) # 等待一段时间后外层 connect 会尝试重连
            except asyncio.CancelledError:
                 logger.info("PubSub 监听器任务被取消。")
                 break # 退出循环
            except Exception as e:
                 # 捕获 RuntimeError (例如 subscribe/psubscribe 未调用)
                 if isinstance(e, RuntimeError) and "pubsub connection not set" in str(e):
                      logger.error(f"PubSub 监听器运行时错误: {e}。可能订阅尚未完成，将重试。")
                      await asyncio.sleep(1) # 短暂等待后重试
                 else:
                      logger.error(f"PubSub 监听器发生意外错误: {e}", exc_info=True)
                      await asyncio.sleep(5) # 发生未知错误时稍作等待

    async def _resubscribe_channels(self):
        """重新订阅所有已注册的频道 (在连接恢复后)"""
        # --- 修改: 不再需要等待事件 ---
        # await self._pubsub_ready_event.wait() # 移除等待
        if self._pubsub_connection: # 检查连接是否存在
            channels_to_subscribe = list(self._channel_handlers.keys())
            if channels_to_subscribe:
                try:
                    logger.info(f"正在重新订阅 Redis 频道: {', '.join(channels_to_subscribe)}")
                    await self._pubsub_connection.subscribe(*channels_to_subscribe)
                    logger.info("Redis 频道重新订阅完成。")
                    # --- 修改: 订阅成功后设置事件 ---
                    if not self._pubsub_ready_event.is_set():
                         self._pubsub_ready_event.set()
                         logger.info("PubSub 监听器已就绪 (重订阅成功)。")
                    # --- 修改结束 ---
                    for channel in channels_to_subscribe:
                        asyncio.create_task(self._emit_channel_ready_event(channel))
                except Exception as e:
                    logger.error(f"重新订阅 Redis 频道失败: {e}")
                    await self.close_pubsub()
                    self._pubsub_ready_event.clear()
            else:
                logger.debug("没有需要重新订阅的 Redis 频道。")
                if not self._pubsub_ready_event.is_set():
                    self._pubsub_ready_event.set()
                    logger.info("PubSub 监听器已就绪 (无频道订阅)。")
    # --- 修改结束 ---

