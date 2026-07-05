"""
Agent Memory - 时间窗口管理

实现滚动时间窗口删除功能
"""

import threading
import time
from typing import Optional, Dict, Callable
from datetime import datetime, timedelta
import logging

from ..models import MemoryLayer
from ..config import TimeWindowConfig

logger = logging.getLogger(__name__)


class TimeWindowManager:
    """
    时间窗口管理器
    
    提供滚动时间窗口删除功能：
    - 自动删除过期记忆
    - 支持按层配置不同的窗口
    - 后台线程定期检查
    """
    
    def __init__(
        self,
        config: Optional[TimeWindowConfig] = None,
        delete_callback: Optional[Callable] = None
    ):
        """
        初始化时间窗口管理器
        
        Args:
            config: 时间窗口配置
            delete_callback: 删除回调函数
        """
        self.config = config or TimeWindowConfig()
        self.delete_callback = delete_callback
        
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
    
    def start(self):
        """启动后台检查线程"""
        if not self.config.enable_rolling_delete:
            logger.info("Rolling delete is disabled")
            return
        
        if self._running:
            logger.warning("TimeWindowManager is already running")
            return
        
        self._running = True
        self._thread = threading.Thread(
            target=self._check_loop,
            daemon=True,
            name="TimeWindowManager"
        )
        self._thread.start()
        logger.info("TimeWindowManager started")
    
    def stop(self):
        """停止后台检查线程"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("TimeWindowManager stopped")
    
    def _check_loop(self):
        """后台检查循环"""
        while self._running:
            try:
                self._check_and_delete()
            except Exception as e:
                logger.error(f"Error in time window check: {e}")
            
            # 等待下一次检查
            time.sleep(self.config.rolling_check_interval)
    
    def _check_and_delete(self):
        """检查并删除过期记忆"""
        if not self.delete_callback:
            return
        
        now = datetime.now()
        
        # 按层检查
        for layer_value, window_days in self.config.layer_windows.items():
            try:
                layer = MemoryLayer.from_string(layer_value)
            except ValueError:
                continue
            
            cutoff_time = now - timedelta(days=window_days)
            
            logger.debug(
                f"Checking layer {layer_value} for memories before {cutoff_time}"
            )
            
            # 调用删除回调
            try:
                deleted_count = self.delete_callback(
                    layer=layer,
                    before_time=cutoff_time
                )
                if deleted_count > 0:
                    logger.info(
                        f"Deleted {deleted_count} expired memories "
                        f"from layer {layer_value}"
                    )
            except Exception as e:
                logger.error(f"Failed to delete expired memories: {e}")
    
    def get_window_for_layer(self, layer: MemoryLayer) -> int:
        """
        获取指定层的时间窗口（天）
        
        Args:
            layer: 记忆层
        
        Returns:
            时间窗口（天）
        """
        return self.config.layer_windows.get(
            layer.value,
            self.config.rolling_window_days
        )
    
    def set_window_for_layer(self, layer: MemoryLayer, days: int):
        """
        设置指定层的时间窗口
        
        Args:
            layer: 记忆层
            days: 时间窗口（天）
        """
        with self._lock:
            self.config.layer_windows[layer.value] = days
    
    def is_expired(
        self,
        layer: MemoryLayer,
        event_time: datetime,
        reference_time: Optional[datetime] = None
    ) -> bool:
        """
        检查记忆是否过期
        
        Args:
            layer: 记忆层
            event_time: 事件时间
            reference_time: 参考时间
        
        Returns:
            是否过期
        """
        if reference_time is None:
            reference_time = datetime.now()
        
        window_days = self.get_window_for_layer(layer)
        cutoff_time = reference_time - timedelta(days=window_days)
        
        return event_time < cutoff_time
