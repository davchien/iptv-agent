"""
标准频道获取模块
只从官方渠道获取直连URL，以 standard_channels.json 为唯一基准
支持：咪咕API、央视CDN、芒果TV、浙江广电等官方源
"""
import asyncio
import json
import os
from typing import List, Optional, Dict, Any
from urllib.parse import urljoin

import aiohttp

from .utils import setup_logging, load_config
from .storage import ChannelInfo
from .miguvideo_api import batch_get_urls as migu_batch_get
from .miguvideo_api import get_cctv_cdn_url
from .stream_analyzer import analyze_stream, batch_analyze

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


async def fetch_cctv_cdn_channel(channel: Dict) -> Optional[ChannelInfo]:
    """从央视官方CDN获取央视频道"""
    source_id = channel.get("source_id", "")
    url = await get_cctv_cdn_url(source_id)
    if not url:
        return None

    return ChannelInfo(
        name=channel["name"],
        url=url,
        group=channel.get("group", "央视"),
        logo=channel.get("logo", ""),
        channel_id=channel["id"],
        tvg_id=channel["id"],
        source="cctv_cdn",
    )


async def fetch_miguvideo_channel(channel: Dict) -> Optional[ChannelInfo]:
    """从咪咕API获取频道直连URL"""
    source_id = channel.get("source_id", "")
    if not source_id:
        return None

    url = await __import__("src.miguvideo_api", fromlist=["get_final_url"]).get_final_url(source_id, rate_type=3)
    if not url:
        logger.warning(f"咪咕API获取失败: {channel['name']} (pid={source_id})")
        return None

    return ChannelInfo(
        name=channel["name"],
        url=url,
        group=channel.get("group", "未分类"),
        logo=channel.get("logo", ""),
        channel_id=channel["id"],
        tvg_id=channel["id"],
        source="miguvideo",
    )


async def fetch_mgtv_channel(channel: Dict) -> Optional[ChannelInfo]:
    """从芒果TV（湖南卫视官方）获取"""
    source_id = channel.get("source_id", "")
    # 芒果TV URL模板（需要从官网或API获取，这里使用观察到的格式）
    # 实际应该调用芒果TV的API，这里先用模板
    url = f"http://hlsal-ldvt.qing.mgtv.com/nn_live/nn_x64/{source_id}/index.m3u8"
    
    # 验证URL是否可访问
    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    return ChannelInfo(
                        name=channel["name"],
                        url=url,
                        group=channel.get("group", "卫视"),
                        logo=channel.get("logo", ""),
                        channel_id=channel["id"],
                        tvg_id=channel["id"],
                        source="mgtv",
                    )
    except Exception:
        pass

    logger.warning(f"芒果TV获取失败: {channel['name']}")
    return None


async def fetch_cztv_channel(channel: Dict) -> Optional[ChannelInfo]:
    """从浙江广电官方CDN获取"""
    source_id = channel.get("source_id", "")
    # 浙江广电URL模板
    url = f"http://ali-xwl.cztv.com/live/channel{source_id}1080Plxw.m3u8"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    return ChannelInfo(
                        name=channel["name"],
                        url=url,
                        group=channel.get("group", "卫视"),
                        logo=channel.get("logo", ""),
                        channel_id=channel["id"],
                        tvg_id=channel["id"],
                        source="cztv",
                    )
    except Exception:
        pass

    logger.warning(f"浙江广电获取失败: {channel['name']}")
    return None


# 来源类型 → 获取函数映射
SOURCE_FETCHERS = {
    "miguvideo": fetch_miguvideo_channel,
    "cctv_cdn": fetch_cctv_cdn_channel,
    "mgtv": fetch_mgtv_channel,
    "cztv": fetch_cztv_channel,
}


async def fetch_all_standard_channels(
    concurrency: int = 5,
    analyze_streams: bool = True,
) -> List[ChannelInfo]:
    """
    从标准频道表获取所有频道的直连URL
    
    Args:
        concurrency: 并发数
        analyze_streams: 是否分析流信息（分辨率等）
    
    Returns:
        ChannelInfo 列表（仅含标准频道）
    """
    channels_config = load_standard_channels()
    if not channels_config:
        logger.error("标准频道表为空，无法获取频道列表")
        return []

    # 按来源类型分组，批量获取（咪咕可以批量）
    migu_pids = []
    other_channels = []

    for ch in channels_config:
        source = ch.get("source", "miguvideo")
        if source == "miguvideo":
            migu_pids.append((ch["source_id"], ch["name"]))
            other_channels.append(ch)
        else:
            other_channels.append(ch)

    # 批量从咪咕API获取URL
    logger.info(f"从咪咕API批量获取 {len(migu_pids)} 个频道...")
    migu_results = await migu_batch_get(migu_pids, rate_type=3, concurrency=concurrency)

    # 构建结果
    results = []
    migu_idx = 0

    for ch in channels_config:
        source = ch.get("source", "miguvideo")
        
        if source == "miguvideo":
            pid = ch["source_id"]
            url = migu_results.get(pid, "")
            if url:
                info = ChannelInfo(
                    name=ch["name"],
                    url=url,
                    group=ch.get("group", "未分类"),
                    logo=ch.get("logo", ""),
                    channel_id=ch["id"],
                    tvg_id=ch["id"],
                    source="miguvideo",
                )
                info.all_urls = [url]
                results.append(info)
                logger.info(f"✓ {ch['name']}: 咪咕API获取成功")
            else:
                logger.warning(f"✗ {ch['name']}: 咪咕API获取失败")
            migu_idx += 1
        else:
            # 其他来源（央视CDN、芒果TV等）
            fetcher_func = SOURCE_FETCHERS.get(source)
            if fetcher_func:
                info = await fetcher_func(ch)
                if info:
                    info.all_urls = [info.url]
                    results.append(info)
                    logger.info(f"✓ {ch['name']}: {source} 获取成功")
                else:
                    logger.warning(f"✗ {ch['name']}: {source} 获取失败")
            else:
                logger.warning(f"? {ch['name']}: 未知来源类型 {source}")

    logger.info(f"标准频道获取完成: 成功 {len(results)}/{len(channels_config)}")

    # 可选：分析流信息（分辨率等）
    if analyze_streams and results:
        logger.info("分析流信息（分辨率等）...")
        analyze_tasks = []
        for ch_info in results:
            analyze_tasks.append(analyze_stream(ch_info.url, fallback_name=ch_info.name))
        
        analyses = await asyncio.gather(*analyze_tasks)
        for ch_info, analysis in zip(results, analyses):
            if analysis.get("resolution"):
                ch_info.resolution = f"{analysis['resolution'][0]}x{analysis['resolution'][1]}"
                ch_info.resolution_label = analysis.get("resolution_label", "")
                logger.debug(f"{ch_info.name}: {ch_info.resolution}")

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
