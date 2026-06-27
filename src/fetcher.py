"""
标准频道获取模块
从 migu_video 社区的 interface.txt 获取已验证的官方直连URL，
按 standard_channels.json 进行频道名匹配，无需直接调用咪咕API。
"""
import asyncio
import json
import os
from typing import List, Optional, Dict, Any

import aiohttp

from .utils import setup_logging, load_config
from .storage import ChannelInfo
from .migu_m3u import fetch_interface_txt, parse_m3u8_content, match_channel

logger = setup_logging(load_config().get("logging", {}))

# 标准频道表路径
STANDARD_CHANNELS_FILE = os.path.join(os.path.dirname(__file__), "standard_channels.json")


def load_standard_channels() -> List[Dict[str, Any]]:
    """加载标准频道对照表"""
    try:
        with open(STANDARD_CHANNELS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        channels = data.get("channels", [])
        logger.info(f"加载标准频道表: {len(channels)} 个频道")
        return channels
    except Exception as e:
        logger.error(f"加载标准频道表失败: {e}")
        return []


async def fetch_all_standard_channels(
    concurrency: int = 5,
    analyze_streams: bool = False,
    config: Optional[dict] = None,
) -> List[ChannelInfo]:
    """
    从标准频道表获取频道URL
    
    两种模式：
    1. 代理模式（migu_proxy_url 已配置）：
       - 直接用 source_id 拼接代理地址 http://proxy:6001/source_id
       - 不拉取 interface.txt，代理内部实时处理鉴权
    2. 静态模式（migu_proxy_url 为空）：
       - 从 interface.txt 拉取静态URL并匹配频道名
       - URL含过期鉴权参数，换IP后可能失效
    
    Args:
        concurrency: 并发数（保留兼容）
        analyze_streams: 是否分析流信息
        config: 完整配置（用于读取 migu_proxy_url）
    
    Returns:
        ChannelInfo 列表
    """
    channels_config = load_standard_channels()
    if not channels_config:
        logger.error("标准频道表为空，无法获取频道列表")
        return []

    # 检查是否启用代理模式
    if config is None:
        config = load_config()
    proxy_url = (
        config.get("standard_channels", {}).get("migu_proxy_url", "").strip()
    )
    
    if proxy_url:
        return _fetch_via_proxy(channels_config, proxy_url)
    else:
        return await _fetch_via_interface_txt(channels_config, concurrency, analyze_streams)


def _fetch_via_proxy(
    channels_config: List[Dict[str, Any]],
    proxy_url: str,
) -> List[ChannelInfo]:
    """代理模式：用 source_id 拼接 migu2026 代理地址"""
    proxy_url = proxy_url.rstrip("/")
    results = []
    skipped = 0

    for ch in channels_config:
        source_id = ch.get("source_id", "")
        # 只处理有数字咪咕ID的频道（source_id 为纯数字）
        if not source_id.isdigit():
            logger.debug(
                f"⊘ {ch['name']}: source_id={source_id!r} 非咪咕ID，跳过（需第三方源补充）"
            )
            skipped += 1
            continue

        info = ChannelInfo(
            name=ch["name"],
            url=f"{proxy_url}/{source_id}",
            group=ch.get("group", "未分类"),
            logo=ch.get("logo", ""),
            channel_id=ch["id"],
            tvg_id=ch["id"],
            source="migu_proxy",
        )
        info.all_urls = [info.url]
        results.append(info)
        logger.debug(f"✓ {ch['name']}: {proxy_url}/{source_id}")

    logger.info(
        f"代理模式完成: {len(results)}/{len(channels_config)} 个频道 "
        f"(跳过 {skipped} 个无咪咕ID)"
    )
    return results


async def _fetch_via_interface_txt(
    channels_config: List[Dict[str, Any]],
    concurrency: int = 5,
    analyze_streams: bool = False,
) -> List[ChannelInfo]:
    """静态模式：从 interface.txt 拉取并匹配（旧逻辑）"""


async def fetch_and_parse(source_config: dict) -> List[ChannelInfo]:
    """
    兼容旧接口：根据源配置获取并解析频道列表
    如果 source_type == "standard"，则使用标准频道表
    否则按原有逻辑处理（M3U、web_scrape）
    """
    source_type = source_config.get("type", "m3u")
    name = source_config.get("name", "未知源")

    if source_type == "standard":
        # 使用标准频道表
        logger.info(f"使用标准频道表获取: {name}")
        return await fetch_all_standard_channels()
    elif source_type == "m3u":
        # 兼容旧逻辑：从M3U URL获取
        url = source_config.get("url", "")
        if not url:
            logger.warning(f"源 {name} 的URL为空，跳过")
            return []
        
        from .fetcher import fetch_m3u_content, parse_m3u_content
        content = await fetch_m3u_content(url)
        if not content:
            return []
        return parse_m3u_content(content, source_name=name)
    elif source_type == "web_scrape":
        logger.warning(f"web_scrape 类型暂未实现: {name}")
        return []
    else:
        logger.warning(f"未知的源类型: {source_type}")
        return []


# 保留原有函数以保持向后兼容
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
    #EXTINF:-1 tvg-id="xxx" tvg-name="xxx" group-title="xxx",频道名
    http://...
    """
    import re
    channels = []
    lines = content.splitlines()

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF"):
            name = ""
            tvg_id = ""
            group = "未分类"
            logo = ""

            # 提取tvg-name
            m = re.search(r'tvg-name="([^"]*)"', line)
            if m:
                name = m.group(1)
            else:
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
