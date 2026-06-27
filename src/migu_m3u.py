"""
从 migu_video 社区的 interface.txt 获取已验证的咪咕直播源URL

migu_video 项目通过 GitHub Actions 定时调用咪咕API
并完成签名/加密(encrypt)计算，最终输出可直接播放的M3U8列表。
我们直接解析这个列表，按标准频道名匹配，无需自己调API。
"""
import re
import asyncio
import aiohttp
from typing import Optional, Dict, List, Tuple

from .storage import ChannelInfo
from .utils import setup_logging, load_config

logger = setup_logging(load_config().get("logging", {}))

# 默认的 interface.txt 地址
DEFAULT_M3U_URLS = [
    "https://raw.githubusercontent.com/develop202/migu_video/main/interface.txt",
    "https://gh-proxy.com/https://raw.githubusercontent.com/develop202/migu_video/main/interface.txt",
]


def normalize_name(name: str) -> str:
    """
    标准化频道名用于模糊匹配：
    - 去除空格、连字符、下划线
    - 转小写
    - 去除括号内容
    """
    name = re.sub(r"[（(][^)）]*[)）]", "", name)  # 去括号
    name = re.sub(r"[\s\-_·]+", "", name)  # 去空格/连字符
    return name.lower()


def parse_m3u8_content(content: str) -> Dict[str, List[Tuple[str, str]]]:
    """
    解析 M3U8 内容，提取频道信息

    返回: {normalized_name: [(group, url), ...]}
    注意: 同名频道可能有多个备用URL（按出现顺序，第一个最新）
    """
    channels: Dict[str, List[Tuple[str, str]]] = {}
    current_name = ""
    current_group = ""

    for line in content.splitlines():
        line = line.strip()
        if not line or line == "#EXTM3U":
            continue

        if line.startswith("#EXTINF:"):
            # 提取 tvg-name 和 group-title
            name_match = re.search(r'tvg-name="([^"]*)"', line)
            group_match = re.search(r'group-title="([^"]*)"', line)
            if name_match:
                current_name = name_match.group(1)
            else:
                # 回退：取逗号后的内容
                parts = line.split(",", 1)
                if len(parts) > 1:
                    current_name = parts[1].strip()
            if group_match:
                current_group = group_match.group(1)

        elif line.startswith("http") and current_name:
            norm = normalize_name(current_name)
            if norm not in channels:
                channels[norm] = []
            channels[norm].append((current_group, line, current_name))

    logger.info(f"解析M3U8完成: {len(channels)} 个唯一频道")
    return channels


def _url_quality_score(url: str) -> int:
    """
    URL 质量评分，分数越低越好（优先选择）。
    优先级：无鉴权参数 > 简单鉴权 > 复杂鉴权（encrypt+client_ip）
    """
    has_encrypt = "encrypt=" in url.lower() and "encrypt=&\"" not in url.lower()
    has_client_ip = "client_ip=" in url.lower()
    has_timestamp = "timestamp=" in url.lower()

    if not has_encrypt and not has_client_ip and not has_timestamp:
        return 0  # 最佳：无鉴权
    elif not has_encrypt:
        return 1  # 较好：无 encrypt（仅 timestamp 等）
    elif has_encrypt and has_client_ip:
        return 3  # 最差：IP 锁定的过期 URL
    else:
        return 2  # 中间


def _sort_urls(urls: List[str]) -> List[str]:
    """按质量排序 URL：优先选无鉴权、无 IP 锁定的"""
    return sorted(urls, key=_url_quality_score)


def match_channel(std_name: str, m3u_channels: Dict[str, List[Tuple[str, str]]]) -> Optional[List[str]]:
    """
    将标准频道名匹配到 M3U 频道

    匹配策略：
    1. 标准化后全字匹配
    2. 标准化后包含匹配
    3. 关键词匹配（如 "CCTV-1" 匹配 "CCTV1"）

    返回的 URL 列表按质量排序：无鉴权 URL 在前，IP 锁定的过期 URL 在后
    """
    norm = normalize_name(std_name)

    # 策略1: 精确匹配
    if norm in m3u_channels:
        items = m3u_channels[norm]
        urls = [url for _, url, _ in items]
        return _sort_urls(urls)

    # 策略2: 包含匹配
    for m3u_norm, items in m3u_channels.items():
        if norm in m3u_norm or m3u_norm in norm:
            urls = [url for _, url, _ in items]
            return _sort_urls(urls)

    # 策略3: 关键词匹配
    # 提取数字部分：如 "cctv-1" → "cctv1"
    keywords = [w for w in norm if w.isalnum()]
    keyword_str = "".join(keywords)

    for m3u_norm, items in m3u_channels.items():
        m3u_keywords = "".join(w for w in m3u_norm if w.isalnum())
        if keyword_str in m3u_keywords or m3u_keywords in keyword_str:
            urls = [url for _, url, _ in items]
            return _sort_urls(urls)

    # 策略4: 中文名缩写匹配
    # 如 "黑龙江卫视" 匹配 "heilongjiang"
    # 提取中文字
    chinese_chars = re.findall(r"[\u4e00-\u9fff]+", std_name)
    if chinese_chars:
        cn = "".join(chinese_chars)
        for m3u_norm, items in m3u_channels.items():
            m3u_cn = "".join(re.findall(r"[\u4e00-\u9fff]+", items[0][2]))
            if cn == m3u_cn:
                urls = [url for _, url, _ in items]
                return _sort_urls(urls)

    return None


async def fetch_interface_txt(
    url: Optional[str] = None,
    timeout: int = 30,
) -> Optional[str]:
    """
    拉取 interface.txt 内容

    参数:
        url: 接口文件地址，默认使用 DEFAULT_M3U_URLS
        timeout: 超时秒数

    返回: 文件内容字符串，失败返回 None
    """
    urls = [url] if url else DEFAULT_M3U_URLS

    for attempt_url in urls:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    attempt_url,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    headers={"User-Agent": "iptv-agent/1.0"},
                ) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        logger.info(f"成功拉取 interface.txt: {attempt_url} ({len(text)} bytes)")
                        return text
                    else:
                        logger.warning(f"拉取失败 {attempt_url}: HTTP {resp.status}")
        except Exception as e:
            logger.warning(f"拉取失败 {attempt_url}: {e}")

    return None


async def fetch_channels_from_migu(
    url: Optional[str] = None,
) -> Dict[str, List[str]]:
    """
    一键拉取 + 解析 + 建索引

    返回: {normalized_name: [url1, url2, ...]}
    """
    content = await fetch_interface_txt(url)
    if not content:
        return {}

    channels = parse_m3u8_content(content)

    # 转换为更简单的格式
    result = {}
    for norm_name, items in channels.items():
        result[norm_name] = [url for _, url, _ in items]

    return result


async def get_channels_for_standard_list(
    standard_channels: List[dict],
    url: Optional[str] = None,
) -> List[ChannelInfo]:
    """
    获取标准频道列表中每个频道的官方直连URL

    参数:
        standard_channels: standard_channels.json 中的频道列表
        url: interface.txt 地址

    返回: ChannelInfo 列表
    """
    content = await fetch_interface_txt(url)
    if not content:
        logger.error("无法获取 interface.txt，标准频道表获取失败")
        return []

    m3u_channels = parse_m3u8_content(content)

    results = []
    matched = 0
    unmatched = 0

    for ch in standard_channels:
        std_name = ch["name"]
        urls = match_channel(std_name, m3u_channels)

        if urls:
            channel = ChannelInfo(
                name=ch["name"],
                group=ch.get("group", "未分类"),
                url=urls[0],  # 第一个URL（最新）
                logo=ch.get("logo", ""),
            )
            channel.all_urls = urls  # 所有备用URL
            results.append(channel)
            matched += 1
            logger.debug(f"✓ {std_name}: 匹配成功 ({len(urls)} 个备用URL)")
        else:
            unmatched += 1
            logger.debug(f"✗ {std_name}: 在 interface.txt 中未找到")

    logger.info(f"标准频道匹配完成: {matched}/{matched + unmatched} (成功/总数)")
    return results


if __name__ == "__main__":
    async def test():
        content = await fetch_interface_txt()
        if content:
            channels = parse_m3u8_content(content)
            print(f"解析到 {len(channels)} 个频道")
            # 测试匹配
            test_names = ["CCTV-1 综合", "湖南卫视", "黑龙江卫视", "浙江卫视"]
            for name in test_names:
                urls = match_channel(name, channels)
                print(f"  {name}: {'✓' if urls else '✗'} ({len(urls) if urls else 0} URLs)")

    asyncio.run(test())
