"""
频道状态存储模块
内存存储 + JSON文件持久化
"""
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .utils import setup_logging, load_config

logger = setup_logging(load_config().get("logging", {}))


class ChannelInfo:
    """单个频道的信息"""

    def __init__(
        self,
        name: str,
        url: str,
        group: str = "未分类",
        logo: str = "",
        channel_id: str = "",
        source: str = "",
        status: str = "unknown",  # unknown, ok, failed
        latency: float = -1,
        last_test: str = "",
        tvg_id: str = "",
    ):
        self.name = name
        self.url = url  # 当前有效直连URL
        self.group = group
        self.logo = logo
        self.channel_id = channel_id or name
        self.tvg_id = tvg_id
        self.source = source
        self.status = status
        self.latency = latency  # 响应时间（毫秒）
        self.last_test = last_test
        self.all_urls: List[str] = []  # 所有候选URL

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "url": self.url,
            "group": self.group,
            "logo": self.logo,
            "channel_id": self.channel_id,
            "tvg_id": self.tvg_id,
            "source": self.source,
            "status": self.status,
            "latency": self.latency,
            "last_test": self.last_test,
            "all_urls": self.all_urls,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ChannelInfo":
        ch = cls(
            name=data["name"],
            url=data["url"],
            group=data.get("group", "未分类"),
            logo=data.get("logo", ""),
            channel_id=data.get("channel_id", ""),
            source=data.get("source", ""),
            status=data.get("status", "unknown"),
            latency=data.get("latency", -1),
            last_test=data.get("last_test", ""),
            tvg_id=data.get("tvg_id", ""),
        )
        ch.all_urls = data.get("all_urls", [data["url"]])
        return ch


class ChannelStore:
    """线程安全的频道存储"""

    def __init__(self, data_dir: str = "./data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._store: Dict[str, ChannelInfo] = {}  # key: channel_id
        self._lock = threading.RLock()
        self._load()

    def _load(self):
        """从JSON文件加载"""
        json_path = self.data_dir / "channels.json"
        if not json_path.exists():
            logger.info("channels.json 不存在，跳过加载")
            return
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            with self._lock:
                for item in data:
                    ch = ChannelInfo.from_dict(item)
                    self._store[ch.channel_id] = ch
            logger.info(f"从磁盘加载了 {len(self._store)} 个频道")
        except Exception as e:
            logger.error(f"加载 channels.json 失败: {e}")

    def save(self):
        """持久化到JSON文件"""
        with self._lock:
            data = [ch.to_dict() for ch in self._store.values()]
        json_path = self.data_dir / "channels.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"已保存 {len(data)} 个频道到 {json_path}")

    def set(self, channel: ChannelInfo):
        with self._lock:
            self._store[channel.channel_id] = channel

    def get(self, channel_id: str) -> Optional[ChannelInfo]:
        with self._lock:
            return self._store.get(channel_id)

    def get_by_name(self, name: str) -> Optional[ChannelInfo]:
        """按频道名查找（模糊匹配）"""
        with self._lock:
            for ch in self._store.values():
                if ch.name == name or ch.channel_id == name:
                    return ch
            # 模糊匹配
            for ch in self._store.values():
                if name in ch.name or ch.name in name:
                    return ch
        return None

    def get_all(self) -> List[ChannelInfo]:
        with self._lock:
            return list(self._store.values())

    def get_by_group(self, group: str) -> List[ChannelInfo]:
        with self._lock:
            return [ch for ch in self._store.values() if ch.group == group]

    def get_groups(self) -> List[str]:
        with self._lock:
            return sorted(set(ch.group for ch in self._store.values()))

    def count(self) -> int:
        with self._lock:
            return len(self._store)

    def clear(self):
        with self._lock:
            self._store.clear()
