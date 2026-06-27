"""
M3U源抓取与解析模块
支持从远程URL获取M3U文件并解析为ChannelInfo列表
"""
import asyncio
import re
from typing import List, Tuple
from urllib.parse import urlparse

import aiohttp

from .utils import setup_logging, load_config
from .storage import ChannelInfo

logger = setup_logging(load_config().get("logging", {}))


async def fetch_m3u_content(url: str, timeout: int = 15) -> str:
    """异步获取M3U文件内容"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status == 200:
                    content = await resp.text(encoding="utf-8")
                    logger.info(f"成功获取M3U: {url} ({len(content)} bytes)")
                    return content
                else:
                    logger.warning(f"获取M3U失败: {url} HTTP {resp.status}")
                    return ""
    except Exception as e:
        logger.error(f"获取M3U异常: {url} {e}")
        return ""


def parse_m3u_content(content: str, source_name: str = "") -> List[ChannelInfo]:
    """
    解析M3U内容，返回ChannelInfo列表
    支持标准M3U格式：
    #EXTINF:-1 tvg-id="xxx" tvg-name="xxx" group-title="xxx" tvg-logo="xxx",频道名
    http://...
    """
    channels: List[ChannelInfo] = []
    lines = content.splitlines()

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF"):
            # 解析EXTINF行
            name = ""
            tvg_id = ""
            group = "未分类"
            logo = ""

            # 提取tvg-name
            m = re.search(r'tvg-name="([^"]*)"', line)
            if m:
                name = m.group(1)
            else:
                # 逗号后的名称
                if "," in line:
                    name = line.split(",", 1)[1].strip()

            # 提取tvg-id
            m = re.search(r'tvg-id="([^"]*)"', line)
            if m:
                tvg_id = m.group(1)

            # 提取group-title
            m = re.search(r'group-title="([^"]*)"', line)
            if m:
                group = m.group(1)

            # 提取tvg-logo
            m = re.search(r'tvg-logo="([^"]*)"', line)
            if m:
                logo = m.group(1)

            # 下一行是URL
            if i + 1 < len(lines):
                url_line = lines[i + 1].strip()
                if url_line and not url_line.startswith("#"):
                    # 确定channel_id
                    channel_id = tvg_id or name
                    ch = ChannelInfo(
                        name=name,
                        url=url_line,
                        group=group,
                        logo=logo,
                        channel_id=channel_id,
                        tvg_id=tvg_id,
                        source=source_name,
                    )
                    ch.all_urls = [url_line]
                    channels.append(ch)
                    i += 2
                    continue
        i += 1

    logger.info(f"解析M3U完成: {source_name}，获得 {len(channels)} 个频道")
    return channels


async def fetch_and_parse(source_config: dict) -> List[ChannelInfo]:
    """
    根据源配置获取并解析频道列表
    source_config: config.yaml 中的单个source配置
    """
    source_type = source_config.get("type", "m3u")
    url = source_config.get("url", "")
    name = source_config.get("name", "未知源")

    if not url:
        logger.warning(f"源 {name} 的URL为空，跳过")
        return []

    if source_type == "m3u":
        content = await fetch_m3u_content(url)
        if not content:
            return []
        return parse_m3u_content(content, source_name=name)
    elif source_type == "web_scrape":
        # 网页抓取（后续扩展）
        logger.warning(f"web_scrape 类型暂未实现: {name}")
        return []
    else:
        logger.warning(f"未知的源类型: {source_type}")
        return []
