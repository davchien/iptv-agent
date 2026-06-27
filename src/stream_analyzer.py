"""
HLS 流分析器
从 m3u8 流量中识别频道名和分辨率
1. 解析 m3u8 manifest 获取分辨率
2. 通过 URL 模式匹配识别频道名
3. （可选）解析 TS 流获取元数据
"""
import re
import asyncio
import aiohttp
from typing import Optional, Dict, Tuple
from urllib.parse import urlparse, urljoin

from .utils import setup_logging, load_config

logger = setup_logging(load_config().get("logging", {}))


# ------------------------------------------------------------
# URL → 频道名 映射表（根据观察到的 URL 特征）
# ------------------------------------------------------------
URL_CHANNEL_MAP = {
    # 咪咕 CDN
    r"cctv(\d+)hd": lambda m: f"CCTV-{m.group(1)}",
    r"cctv1hd": "CCTV-1 综合",
    r"cctv2hd": "CCTV-2 财经",
    r"cctv3hd": "CCTV-3 综艺",
    r"cctv4hd": "CCTV-4 中文国际",
    r"cctv5hd": "CCTV-5 体育",
    r"cctv5plus": "CCTV-5+ 体育赛事",
    r"cctv6hd": "CCTV-6 电影",
    r"cctv7hd": "CCTV-7 国防军事",
    r"cctv8hd": "CCTV-8 电视剧",
    r"cctv9hd": "CCTV-9 纪录",
    r"cctv10hd": "CCTV-10 科教",
    r"cctv11hd": "CCTV-11 戏曲",
    r"cctv12hd": "CCTV-12 社会与法",
    r"cctv13": "CCTV-13 新闻",
    r"cctv14hd": "CCTV-14 少儿",
    r"cctv15hd": "CCTV-15 音乐",
    r"cctv16": "CCTV-16 奥林匹克",
    r"cctv17hd": "CCTV-17 农业农村",
    # 卫视频道
    r"hunan": "湖南卫视",
    r"zhejiang|zjws": "浙江卫视",
    r"jiangsu|jsws": "江苏卫视",
    r"dongfang|dfl|dfws": "东方卫视",
    r"guangdong|gdws": "广东卫视",
    r"beijing|bjws": "北京卫视",
    r"hubei|hbws": "湖北卫视",
    r"shandong|sdws": "山东卫视",
    r"liaoning|lnws": "辽宁卫视",
    r"dongnan|dnws": "东南卫视",
    r"chongqing|cqws": "重庆卫视",
    r"jilin|jws": "吉林卫视",
    r"jiangxi|jxws": "江西卫视",
    # 地方台关键词
    r"cztv": "浙江广电",
    r"mgtv": "芒果TV",
    r"bestv": "百视通",
    r"hljtv": "黑龙江卫视",
    r"snrtv": "陕西卫视",
    r"nbs": "南京广电",
    r"thmz": "无锡广电",
    r"hrbtv": "哈尔滨广电",
}


def identify_channel_by_url(url: str, fallback_name: str = "") -> str:
    """
    通过 URL 路径/域名特征识别频道名
    返回: 识别出的频道名，如无法识别则返回 fallback_name
    """
    url_lower = url.lower()

    for pattern, name in URL_CHANNEL_MAP.items():
        if re.search(pattern, url_lower):
            if callable(name):
                continue  # 跳过需要额外处理的正则
            return name

    # 尝试从 URL 路径中提取频道关键词
    # 例如: /cctv/cctv1hd/1200/index.m3u8 → CCTV-1
    m = re.search(r"cctv(\d+)", url_lower)
    if m:
        return f"CCTV-{m.group(1)}"

    m = re.search(r"/(\w+hd|\w+ws)/", url_lower)
    if m:
        key = m.group(1)
        mapping = {
            "hunanhd": "湖南卫视",
            "zhejiangws": "浙江卫视",
            "jiangsuws": "江苏卫视",
            "dongfangws": "东方卫视",
            "guangdongws": "广东卫视",
        }
        if key in mapping:
            return mapping[key]

    return fallback_name


async def fetch_m3u8_header(url: str, timeout: int = 8) -> Optional[str]:
    """获取 m3u8 文件内容（前 100 行足够解析分辨率）"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=timeout),
                headers={"User-Agent": "Mozilla/5.0 (琴迅电视)"}
            ) as resp:
                if resp.status != 200:
                    return None
                # 只读取前 16KB（足够解析多码率 manifest）
                data = await resp.read()
                return data.decode("utf-8", errors="ignore")
    except Exception as e:
        logger.debug(f"获取 m3u8 失败: {url[:60]}... {e}")
        return None


def parse_resolution_from_m3u8(content: str) -> Optional[Tuple[int, int]]:
    """
    从 m3u8 内容中解析分辨率
    查找 #EXT-X-STREAM-INF:...RESOLUTION=1920x1080...
    如果是多码率 manifest，返回最高分辨率
    返回: (width, height) 或 None
    """
    max_res = None
    max_pixels = 0

    for line in content.splitlines():
        line = line.strip()
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue

        # 查找 RESOLUTION=1920x1080
        m = re.search(r"RESOLUTION=(\d+)x(\d+)", line, re.IGNORECASE)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            if w * h > max_pixels:
                max_pixels = w * h
                max_res = (w, h)

        # 同时也找 BANDWIDTH，用于评分
        bw = re.search(r"BANDWIDTH=(\d+)", line)
        if bw:
            pass  # 可用来评估码率

    return max_res


def get_resolution_label(width: int, height: int) -> str:
    """将分辨率转换为人类可读标签"""
    if width >= 3840 or height >= 2160:
        return "4K"
    elif width >= 1920 or height >= 1080:
        return "1080p"
    elif width >= 1280 or height >= 720:
        return "720p"
    elif width >= 854 or height >= 480:
        return "480p"
    else:
        return f"{height}p" if height else "未知"


async def analyze_stream(url: str, fallback_name: str = "") -> Dict:
    """
    完整分析一个直播流 URL
    返回: {
        "url": 原始URL,
        "channel_name": 识别出的频道名,
        "resolution": (width, height) 或 None,
        "resolution_label": "1080p" 等,
        "is_multi_bitrate": 是否是多码率 manifest,
        "sub_m3u8_urls": [] 子 manifest URL 列表（多码率时）,
    }
    """
    result = {
        "url": url,
        "channel_name": identify_channel_by_url(url, fallback_name),
        "resolution": None,
        "resolution_label": "未知",
        "is_multi_bitrate": False,
        "sub_m3u8_urls": [],
    }

    content = await fetch_m3u8_header(url)
    if not content:
        return result

    # 检查是否是多码率 manifest
    if "#EXT-X-STREAM-INF" in content:
        result["is_multi_bitrate"] = True

        # 提取子 manifest URL
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if line.strip().startswith("#EXT-X-STREAM-INF"):
                # 下一行是子 manifest URL
                if i + 1 < len(lines):
                    sub_url = lines[i + 1].strip()
                    if sub_url and not sub_url.startswith("#"):
                        # 拼接为完整 URL
                        full_sub_url = urljoin(url, sub_url)
                        result["sub_m3u8_urls"].append(full_sub_url)

        # 从当前 manifest 解析最高分辨率
        res = parse_resolution_from_m3u8(content)
        if res:
            result["resolution"] = res
            result["resolution_label"] = get_resolution_label(*res)

        # 如果子 manifest 不多，尝试获取第一个子 manifest 的分辨率
        if not res and result["sub_m3u8_urls"]:
            sub_content = await fetch_m3u8_header(result["sub_m3u8_urls"][0])
            if sub_content:
                # 子 manifest 可能直接是 ts 列表，或仍是多码率
                sub_res = parse_resolution_from_m3u8(sub_content)
                if sub_res:
                    result["resolution"] = sub_res
                    result["resolution_label"] = get_resolution_label(*sub_res)
    else:
        # 单码率 manifest，尝试从内容中找分辨率信息
        res = parse_resolution_from_m3u8(content)
        if res:
            result["resolution"] = res
            result["resolution_label"] = get_resolution_label(*res)

    return result


async def batch_analyze(urls: list, channel_name: str = "") -> list:
    """批量分析多个 URL"""
    semaphore = asyncio.Semaphore(10)

    async def _analyze_one(url):
        async with semaphore:
            return await analyze_stream(url, channel_name)

    results = await asyncio.gather(*[_analyze_one(url) for url in urls])
    return list(results)


# ------------------------------------------------------------
# 可选：解析 TS 流获取 SDT（服务描述表）
# 这部分需要解析 MPEG-TS 格式，比较复杂
# 大多数 HLS 流不含可用元数据，故暂不实现
# ------------------------------------------------------------
async def probe_ts_metadata(url: str, max_bytes: int = 65536) -> Dict:
    """
    从 TS 流开头读取元数据
    尝试解析 SDT（服务描述表）获取频道名
    注意：大多数 HLS 流不含此信息，返回可能为空
    """
    return {
        "service_name": "",
        "provider_name": "",
        "transport_stream_id": None,
    }


if __name__ == "__main__":
    # 测试
    async def test():
        url = "http://hlsztemgsplive.miguvideo.com:8080/wd_r2/cctv/cctv1hd/1200/index.m3u8"
        result = await analyze_stream(url, "CCTV-1")
        print(f"分析结果: {result}")

    asyncio.run(test())
