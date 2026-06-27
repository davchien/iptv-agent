"""
咪咕视频流地址获取模块 (720p 标清模式)

完整移植自 develop202/migu_video 项目:
  - getAndroidURL720p  → fetch_stream_url()
  - getddCalcuURL720p  → _build_auth_url()
  - getddCalcu720p     → _ddcalcu_720p()
  - getStringMD5       → _md5()
  - getDateString      → _date_str()

720p 模式无需 WebAssembly，纯 MD5 + 字符串操作即可完成鉴权。
"""
import hashlib
import math
import random
import time
from datetime import datetime
from typing import Optional

import aiohttp

from .utils import setup_logging, load_config

logger = setup_logging(load_config().get("logging", {}))

# ── 常量 ──────────────────────────────────────────────────
BASE_URL = "https://play.miguvideo.com/playurl/v1/play/playurl"
APP_VERSION = "2600034600"
APP_VERSION_ID = APP_VERSION + "-99000-201600010010028"
DDCALCU_KEYS = "cdabyzwxkl"

# 特殊频道：CCTV5 / CCTV5+ 开 FLV 后无法回放
_NO_FLV_CHANNELS = {"641886683", "641886773"}


def _md5(s: str) -> str:
    """MD5 哈希（小写 hex）"""
    return hashlib.md5(s.encode("utf-8")).hexdigest().lower()


def _date_str(dt: Optional[datetime] = None) -> str:
    """YYYYMMDD 格式日期字符串"""
    d = dt or datetime.now()
    return f"{d.year}{d.month:02d}{d.day:02d}"


def _ddcalcu_720p(pu_data: str, program_id: str) -> str:
    """
    ddCalcu 字符交换算法 (720p 版本)

    算法：遍历 puData 字符串，每次取首尾字符交错排列，
    并在特定位置插入基于日期和节目ID的密钥字符。

    Args:
        pu_data: URL 中的 puData 参数值
        program_id: 节目 ID（数字字符串）

    Returns:
        ddCalcu 字符串
    """
    length = len(pu_data)
    result: list[str] = []

    for i in range(length // 2):
        # 从末尾取一个
        result.append(pu_data[length - i - 1])
        # 从开头取一个
        result.append(pu_data[i])

        if i == 1:
            result.append("v")  # 720p 固定 'v'
        elif i == 2:
            # 用日期第3位选择密钥字符
            date_key_char = _date_str()[2]  # YYYYMMDD → 第3位
            idx = int(date_key_char) if date_key_char.isdigit() else 0
            result.append(DDCALCU_KEYS[idx % len(DDCALCU_KEYS)])
        elif i == 3:
            # 用节目ID第7位选择密钥字符
            if len(program_id) > 6:
                pid_char = program_id[6]
                idx = int(pid_char) if pid_char.isdigit() else 0
                result.append(DDCALCU_KEYS[idx % len(DDCALCU_KEYS)])
            else:
                result.append("c")
        elif i == 4:
            result.append("a")  # 720p 固定 'a'

    return "".join(result)


def _build_auth_url(raw_url: str, program_id: str) -> str:
    """
    给原始流地址添加 ddCalcu 鉴权参数

    Args:
        raw_url: playurl API 返回的原始流地址（含 puData 参数）
        program_id: 节目 ID

    Returns:
        带 ddCalcu 鉴权的完整流地址
    """
    pu_data = raw_url.split("&puData=", 1)[1] if "&puData=" in raw_url else ""
    if not pu_data:
        logger.warning(f"URL 中未找到 puData 参数，返回原始地址")
        return raw_url

    ddcalcu = _ddcalcu_720p(pu_data, program_id)
    return f"{raw_url}&ddCalcu={ddcalcu}&sv=10004&ct=android"


async def fetch_stream_url(
    session: aiohttp.ClientSession,
    channel_id: str,
    timeout: int = 6,
) -> Optional[str]:
    """
    获取频道实时流地址 (Android 720p 模式)

    流程：
    1. 计算 MD5 签名 → 构造请求参数
    2. GET https://play.miguvideo.com/playurl/v1/play/playurl
    3. 解析 body.urlInfo.url → ddCalcu 鉴权 → 返回最终流地址

    Args:
        session: aiohttp 会话
        channel_id: 咪咕节目 ID（如 "608807420"）
        timeout: 请求超时秒数

    Returns:
        带鉴权的流地址，失败返回 None
    """
    if not channel_id.isdigit():
        logger.warning(f"无效频道ID: {channel_id}")
        return None

    # ── 1. 构造签名字符串 ──
    timestamp = str(int(time.time() * 1000))
    sign_str = timestamp + channel_id + APP_VERSION[:8]
    md5_val = _md5(sign_str)

    # 盐值：6位随机数 + "25"
    salt_num = str(random.randint(0, 999999)).zfill(6)
    salt = salt_num + "25"

    # sign = MD5(md5_val + suffix)
    suffix = f"2cac4f2c6c3346a5b34e085725ef7e33migu{salt[:4]}"
    sign = _md5(md5_val + suffix)

    # ── 2. 构造请求 ──
    params = (
        f"?sign={sign}"
        f"&rateType=3"
        f"&contId={channel_id}"
        f"&timestamp={timestamp}"
        f"&salt={salt}"
        f"&flvEnable=true"
        f"&super4k=true"
        f"&4kvivid=true&2Kvivid=true&vivid=2"  # HDR
        f"&h265N=true"  # H.265
    )

    headers = {
        "AppVersion": APP_VERSION,
        "TerminalId": "android",
        "X-UP-CLIENT-CHANNEL-ID": APP_VERSION_ID,
        # ClientId 使用 MD5 时间戳（与社区版一致，进程级不变）
        "ClientId": _md5(str(int(time.time()))),
        "User-Agent": "okhttp/4.9.0",
    }

    # CCTV5 / CCTV5+ 不加 appCode（避免 FLV 回放问题）
    if channel_id not in _NO_FLV_CHANNELS:
        headers["appCode"] = "miguvideo_default_android"

    # ── 3. 请求 API ──
    url = BASE_URL + params
    logger.debug(f"请求咪咕API: {url[:120]}...")

    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                logger.error(f"咪咕API返回 {resp.status}: {channel_id}")
                return None

            data = await resp.json()
    except aiohttp.ClientError as e:
        logger.error(f"咪咕API请求失败 ({channel_id}): {e}")
        return None
    except Exception as e:
        logger.error(f"咪咕API响应解析失败 ({channel_id}): {e}")
        return None

    if not data:
        return None

    # ── 4. 解析响应 ──
    rid = data.get("rid", "")
    if rid == "TIPS_NEED_MEMBER":
        logger.warning(f"频道 {channel_id} 需要会员，720p 模式不支持")
        return None

    body = data.get("body", {})
    url_info = body.get("urlInfo", {})
    stream_url = url_info.get("url", "")

    if not stream_url:
        logger.warning(f"频道 {channel_id}: 未获取到流地址 (rid={rid})")
        return None

    # ── 5. ddCalcu 鉴权 ──
    program_id = body.get("content", {}).get("contId", channel_id)
    auth_url = _build_auth_url(stream_url, program_id)

    logger.debug(f"频道 {channel_id}: 鉴权成功 ({len(auth_url)} 字符)")
    return auth_url


async def fetch_stream_url_simple(channel_id: str, timeout: int = 6) -> Optional[str]:
    """
    简化版：自动创建 session 获取流地址
    适合单次调用场景
    """
    connector = aiohttp.TCPConnector(limit=1)
    async with aiohttp.ClientSession(connector=connector) as session:
        return await fetch_stream_url(session, channel_id, timeout)
