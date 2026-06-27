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
) -> List[ChannelInfo]:
    """
    从标准频道表 + interface.txt 获取所有频道的直连URL
    
    流程:
    1. 从 GitHub 拉取 interface.txt（migu_video 社区输出）
    2. 解析M3U8格式，提取频道名和URL
    3. 按标准频道表进行名称匹配
    4. 返回匹配成功的 ChannelInfo 列表
    
    Args:
        concurrency: 并发数（保留兼容）
        analyze_streams: 是否分析流信息（默认关闭，URL已验证可用）
    
    Returns:
        ChannelInfo 列表（仅含匹配成功且已认证的官方URL）
    """
    channels_config = load_standard_channels()
    if not channels_config:
        logger.error("标准频道表为空，无法获取频道列表")
        return []

    logger.info(f"从 interface.txt 拉取最新直播源 (标准频道表: {len(channels_config)} 个)...")

    # 拉取并解析 interface.txt
    content = await fetch_interface_txt()
    if not content:
        logger.error("无法获取 interface.txt")
        return []

    m3u_channels = parse_m3u8_content(content)
    logger.info(f"interface.txt 解析完成: {len(m3u_channels)} 个频道")

    # 按标准频道表匹配
    results = []
    matched = 0

    for ch in channels_config:
        std_name = ch["name"]
        urls = match_channel(std_name, m3u_channels)

        if urls:
            info = ChannelInfo(
                name=ch["name"],
                url=urls[0],  # 第一个URL（最新）
                group=ch.get("group", "未分类"),
                logo=ch.get("logo", ""),
                channel_id=ch["id"],
                tvg_id=ch["id"],
                source="migu_m3u",
            )
            info.all_urls = urls  # 所有备用URL
            results.append(info)
            matched += 1
            logger.debug(f"✓ {std_name}: 匹配成功 ({len(urls)} 备用URL)")
        else:
            logger.debug(f"✗ {std_name}: interface.txt 中未收录")

    logger.info(f"标准频道匹配完成: {matched}/{len(channels_config)} (成功/总数)")

    # 可选：分析流信息
    if analyze_streams and results:
        from .stream_analyzer import analyze_stream
        logger.info("分析流信息（分辨率等）...")
        analyze_tasks = [analyze_stream(ch_info.url, fallback_name=ch_info.name) for ch_info in results]
        analyses = await asyncio.gather(*analyze_tasks)
        for ch_info, analysis in zip(results, analyses):
            if analysis.get("resolution"):
                ch_info.resolution = f"{analysis['resolution'][0]}x{analysis['resolution'][1]}"
                ch_info.resolution_label = analysis.get("resolution_label", "")

    return results


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
