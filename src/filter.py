"""
频道过滤器模块
规则：
1. URL 必须来自运营商/电视台官方/CDN 域名（咪咕、CCTV、芒果TV、央广等）
2. 排除境外频道（港澳台、国际、海外等）
3. 地方台只保留数字频道
4. 纯 IP 地址的直连不予考虑
"""
import re
from collections import Counter
from urllib.parse import urlparse
from typing import List, Tuple

from .storage import ChannelInfo
from .utils import setup_logging, load_config

logger = setup_logging(load_config().get("logging", {}))

# ============================================================
# 官方/运营商/CDN 域名白名单
# 匹配方式：域名本身或其子域名均视为合规
# ============================================================
OFFICIAL_DOMAINS = {
    # 咪咕（中国移动）运营商 CDN
    "miguvideo.com",
    "migu.cn",
    "migucloud.com",
    # 咪咕容器代理
    "iqw.asia",
    # CCTV / 央视频道官方
    "cctv.com",
    "cntv.cn",
    "cgtn.com",
    # 芒果TV（湖南卫视官方）
    "mgtv.com",
    # 央广
    "cnr.cn",
    # 浙江广电
    "cztv.com",
    # 百视通 / 东方明珠
    "bestv.cn",
    # 湖南日报（华声在线）
    "voc.com.cn",
    # 阿里 CDN（熊猫直播等官方源）
    "myalicdn.com",
}

# ============================================================
# 境外/港澳台 分组（整体排除）
# ============================================================
FOREIGN_GROUPS = {
    "港澳台", "境外", "国际", "海外", "台湾", "香港", "澳门",
    "foreign", "overseas", "international",
}

# 境外频道名模式（纯英文名且非中国频道）
FOREIGN_NAME_PATTERNS = [
    r"^BBC\b", r"^CNN\b", r"^NHK\b", r"^Fox\b", r"^DW\b",
    r"^KIX\b", r"^HKS\b", r"^TVB\b", r"^Good\b", r"^DaAi\b",
    r"^EBC\b", r"^CTi\b", r"^FTV\b", r"^TTV\b", r"^ETV\b",
    r"^Momo\b", r"^Celestial\b", r"^tvN\b", r"^Bloomberg\b",
    r"^Pet Club\b", r"^Supreme Master\b", r"^Global News\b",
    r"^SET iNews\b", r"^Taiwan\b", r"^Genius\b",
    r"^Dali TV\b", r"^mnews\b", r"^ET Mall\b",
]

# ============================================================
# 全国性分组（保留全部，不按地方台规则过滤）
# ============================================================
NATIONAL_GROUPS = {
    "央视频道", "卫视台", "卫视频道", "超清频道", "央视台",
}

# 地方台分组关键词（命中则按"只保留数字频道"过滤）
REGIONAL_GROUP_KEYWORDS = ["地区", "台"]


def is_official_url(url: str) -> bool:
    """
    检查 URL 是否来自官方/运营商/CDN 域名
    纯 IP 地址返回 False
    """
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        # 去掉端口号
        if ":" in domain:
            domain = domain.split(":")[0]

        # 纯 IP 地址排除
        parts = domain.split(".")
        if len(parts) < 2:
            return False
        # 检查是否全为数字（IP地址）
        if all(p.isdigit() for p in parts):
            return False

        # 匹配白名单（支持子域名）
        for official in OFFICIAL_DOMAINS:
            if domain == official or domain.endswith("." + official):
                return True

        return False
    except Exception:
        return False


def is_foreign_channel(name: str, group: str) -> bool:
    """检查是否为境外频道"""
    # 分组命中
    if group in FOREIGN_GROUPS:
        return True

    # 频道名命中已知境外频道
    for pattern in FOREIGN_NAME_PATTERNS:
        if re.match(pattern, name, re.IGNORECASE):
            return True

    # 纯英文名且不以中国频道前缀开头
    if re.match(r"^[A-Za-z\s0-9]+$", name):
        china_prefixes = ("CCTV", "CGTN", "CETV", "CR", "CN", "CNR")
        if not name.upper().startswith(china_prefixes):
            return True

    return False


def is_regional_group(group: str) -> bool:
    """检查是否为地方台分组"""
    if group in NATIONAL_GROUPS:
        return False
    for kw in REGIONAL_GROUP_KEYWORDS:
        if kw in group:
            return True
    return False


def is_digital_channel(name: str) -> bool:
    """检查是否为数字频道"""
    return "数字" in name


def filter_channel(ch: ChannelInfo) -> Tuple[bool, str]:
    """
    过滤单个频道
    返回: (是否通过, 原因)
    """
    # 规则1：排除境外频道
    if is_foreign_channel(ch.name, ch.group):
        return False, f"境外频道:{ch.name}"

    # 规则2：地方台只保留数字频道
    if is_regional_group(ch.group) and not is_digital_channel(ch.name):
        return False, f"地方台:{ch.name}({ch.group})"

    # 规则3：URL 必须来自官方域名
    if not is_official_url(ch.url):
        return False, f"非官方源:{ch.name}"

    return True, "OK"


def filter_channels(channels: List[ChannelInfo]) -> List[ChannelInfo]:
    """
    批量过滤频道列表
    保留通过所有规则的频道
    """
    passed: List[ChannelInfo] = []
    rejected: List[Tuple[str, str]] = []

    for ch in channels:
        ok, reason = filter_channel(ch)
        if ok:
            passed.append(ch)
        else:
            rejected.append((ch.name, reason))

    logger.info(f"频道过滤: 通过 {len(passed)}/{len(channels)}，淘汰 {len(rejected)}")
    if rejected:
        cats = Counter()
        for _, reason in rejected:
            cat = reason.split(":")[0]
            cats[cat] += 1
        for cat, count in cats.most_common():
            logger.info(f"  淘汰-{cat}: {count}个")

    return passed


def filter_urls(urls: List[str]) -> List[str]:
    """过滤候选 URL 列表，只保留官方域名的 URL"""
    return [url for url in urls if is_official_url(url)]
