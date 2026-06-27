"""
工具模块：日志、配置加载、URL安全编码
"""
import json
import logging
import re
from pathlib import Path
from typing import Any

import yaml


def setup_logging(config: dict) -> logging.Logger:
    """配置结构化日志"""
    level = getattr(logging, config.get("level", "INFO").upper())
    fmt = config.get("format", "json")
    log_file = config.get("file", "")

    if fmt == "json":
        class JsonFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                log_record = {
                    "timestamp": self.formatTime(record, self.datefmt),
                    "level": record.levelname,
                    "module": record.module,
                    "message": record.getMessage(),
                }
                if hasattr(record, "extra"):
                    log_record.update(record.extra)
                return json.dumps(log_record, ensure_ascii=False)

        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    handler: logging.Handler
    if log_file:
        handler = logging.FileHandler(log_file, encoding="utf-8")
    else:
        handler = logging.StreamHandler()

    handler.setFormatter(formatter)

    logger = logging.getLogger("iptv_agent")
    logger.setLevel(level)
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def load_config(config_path: str = None) -> dict:
    """加载配置文件，支持环境变量覆盖"""
    if config_path is None:
        config_path = Path(__file__).parent.parent / "config.yaml"
    else:
        config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # 环境变量覆盖（IPTVAgent_ 前缀）
    import os
    for key in ("FULL_UPDATE_INTERVAL", "TEST_TIMEOUT", "SERVER_PORT"):
        env_key = f"IPTVAgent_{key}"
        if env_key in os.environ:
            section = key.split("_")[0].lower()
            if section == "server":
                config["server"]["port"] = int(os.environ[env_key])
            else:
                config[section][key.split("_", 1)[1].lower()] = int(os.environ[env_key])

    return config


def url_safe_encode(name: str) -> str:
    """将频道名编码为URL安全字符串"""
    import urllib.parse
    return urllib.parse.quote(name, safe="")


def url_safe_decode(encoded: str) -> str:
    """解码URL安全字符串为频道名"""
    import urllib.parse
    return urllib.parse.unquote(encoded)


def slugify(name: str) -> str:
    """生成频道名的slug（用于路由）"""
    # 保留中文，将其他字符转为下划线
    name = re.sub(r'[^\w\u4e00-\u9fff]', '_', name)
    name = re.sub(r'_+', '_', name).strip('_')
    return name


def pinyin_transform(name: str) -> str:
    """尝试将中文转为拼音（需要 pypinyin 库，可选）"""
    try:
        from pypinyin import lazy_pinyin
        return ''.join(lazy_pinyin(name))
    except ImportError:
        return slugify(name)
