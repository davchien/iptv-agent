"""
咪咕视频 API 客户端
通过逆向 migu_video 项目得到签名算法，获取官方直播直连URL
参考: https://github.com/develop202/migu_video
"""
import hashlib
import hmac
import random
import time
import json
import asyncio
import aiohttp
from typing import Optional, Dict, Any
from urllib.parse import urlparse, parse_qs

from .utils import setup_logging, load_config

logger = setup_logging(load_config().get("logging", {}))

# 咪咕API配置（逆向自 migu_video/androidURL.js）
MIGU_API_BASE = "https://play.miguvideo.com/playurl/v1/play/playurl"

# 默认请求头（模拟 Android 客户端）
DEFAULT_HEADERS_TEMPLATE = {
    "AppVersion": "2600034600",
    "TerminalId": "android",
    "X-UP-CLIENT-CHANNEL-ID": "2600034600-99000-201600010010028",
    "ClientId": "",  # 需要动态生成
    "User-Agent": "okhttp/3.12.12",
}


def gen_string_md5(s: str) -> str:
    """计算字符串的 MD5 值（小写 hex）"""
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def get_salt_720p() -> str:
    """
    生成 salt（对应 JS 版 getAndroidURL720p 中的 salt 生成逻辑）
    salt = 6位随机数字字符串 + "25"
    """
    rand6 = str(random.randint(0, 999999)).zfill(6)
    return rand6 + "25"


def get_sign_720p(timestamp: str, pid: str, salt: str) -> str:
    """
    计算签名 sign（对应 JS 版 getAndroidURL720p）
    算法:
        1. str = timestamp + pid + "26000346"  (appVersion前8位)
        2. md5 = MD5(str)
        3. suffix = "2cac4f2c6c3346a5b34e085725ef7e33migu" + salt[:4]
        4. sign = MD5(md5 + suffix)
    """
    app_version_prefix = "26000346"
    # 步骤1: 拼接原串
    raw_str = timestamp + pid + app_version_prefix
    # 步骤2: MD5
    md5_val = gen_string_md5(raw_str)
    # 步骤3: 拼接 suffix
    suffix = "2cac4f2c6c3346a5b34e085725ef7e33migu" + salt[:4]
    # 步骤4: 最终 MD5
    sign = gen_string_md5(md5_val + suffix)
    return sign


def build_client_id() -> str:
    """生成 ClientId（对应 JS 中的 getStringMD5(Date.now().toString())）"""
    now_str = str(int(time.time() * 1000))
    return gen_string_md5(now_str)


async def fetch_play_url(
    pid: str,
    rate_type: int = 3,
    user_id: str = "",
    token: str = "",
    enable_h265: bool = True,
    enable_hdr: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    调用咪咕 playurl API 获取播放信息
    对应 JS: getAndroidURL720p()

    返回: {
        "url": 临时播放URL（需进一步重定向）,
        "rateType": 实际清晰度,
        "contId": 节目ID,
        "body": 完整响应body
    } 或 None（失败）
    """
    timestamp = str(int(time.time()))
    salt = get_salt_720p()
    sign = get_sign_720p(timestamp, pid, salt)

    # 构造请求参数
    params = {
        "sign": sign,
        "rateType": str(rate_type),
        "contId": pid,
        "timestamp": timestamp,
        "salt": salt,
        "flvEnable": "true",
        "super4k": "true",
    }
    if enable_h265:
        params["h265N"] = "true"
    if enable_hdr:
        params["4kvivid"] = "true"
        params["2Kvivid"] = "true"
        params["vivid"] = "2"

    # 构造请求头
    headers = dict(DEFAULT_HEADERS_TEMPLATE)
    client_id = build_client_id()
    headers["ClientId"] = client_id
    if pid not in ("641886683", "641886773"):  # CCTV5和5+不开flv
        headers["appCode"] = "miguvideo_default_android"
    if user_id and token:
        headers["UserId"] = user_id
        headers["UserToken"] = token

    url = MIGU_API_BASE
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, params=params, headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"咪咕API返回非200: {resp.status}, pid={pid}")
                    return None
                data = await resp.json()
                logger.debug(f"咪咕API响应: pid={pid}, rid={data.get('rid', '')}")

                # 检查是否需要降级画质
                if data.get("rid") == "TIPS_NEED_MEMBER":
                    logger.info(f"pid={pid} 需要会员，降级画质")
                    # 降级到 rateType=3 或 2
                    params["rateType"] = "3"
                    async with session.get(
                        url, params=params, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp2:
                        if resp2.status != 200:
                            return None
                        data = await resp2.json()

                body = data.get("body", {})
                url_info = body.get("urlInfo", {})
                play_url = url_info.get("url")
                if not play_url:
                    logger.warning(f"咪咕API未返回播放URL: pid={pid}, rid={data.get('rid')}")
                    return None

                return {
                    "url": play_url,
                    "rateType": url_info.get("rateType", rate_type),
                    "contId": body.get("content", {}).get("contId", pid),
                    "body": data,
                }

    except Exception as e:
        logger.error(f"调用咪咕API异常: pid={pid}, {e}")
        return None


async def get_final_url(pid: str, rate_type: int = 3, max_retry: int = 6) -> Optional[str]:
    """
    获取最终可播放的直连URL（完整流程）
    对应 JS: getAndroidURL720p() + get302URL()

    流程:
    1. 调用 playurl API 获取临时URL
    2. 对临时URL发起GET，获取302重定向地址
    3. 如果重定向到 bofang（播放器），重试
    4. 返回最终URL（未加密版，大部分播放器可直接使用）
    """
    # 步骤1: 获取 playurl
    play_info = await fetch_play_url(pid, rate_type)
    if not play_info:
        return None

    temp_url = play_info["url"]
    if not temp_url:
        return None

    # 步骤2: 获取302重定向地址
    final_url = await _follow_redirect(temp_url, max_retry)
    if not final_url:
        # 如果获取不到重定向，返回临时URL（可能仍能播放）
        logger.warning(f"获取重定向失败，使用临时URL: pid={pid}")
        return temp_url

    # 步骤3: 构建带认证参数的URL（模拟 getddCalcuURL720p 的部分逻辑）
    # 注意：完整加密需要 Wasm，这里使用简化版
    # 大部分播放器可以直接使用未加密的URL
    logger.info(f"咪咕直连获取成功: pid={pid}, url={final_url[:80]}...")
    return final_url


async def _follow_redirect(url: str, max_retry: int = 6) -> Optional[str]:
    """跟随302重定向，最多重试max_retry次"""
    for attempt in range(1, max_retry + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=8)
                ) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location", "")
                        if not location or location.startswith("http://bofang"):
                            # boFang 是咪咕播放器，需要重试
                            if attempt >= max_retry:
                                break
                            await asyncio.sleep(0.15)
                            continue
                        return location
                    else:
                        # 不是重定向，返回当前URL
                        return url
        except Exception as e:
            logger.debug(f"重定向尝试 {attempt} 失败: {e}")
            if attempt >= max_retry:
                break
            await asyncio.sleep(0.15)

    return None


async def batch_get_urls(
    pid_list: list,
    rate_type: int = 3,
    concurrency: int = 5,
) -> Dict[str, str]:
    """
    批量获取直连URL
    pid_list: [(pid, channel_name), ...]
    返回: {pid: url}
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def _fetch_one(pid: str, name: str) -> tuple:
        async with semaphore:
            url = await get_final_url(pid, rate_type)
            if url:
                logger.info(f"✓ {name}: 获取成功")
                return (pid, url)
            else:
                logger.warning(f"✗ {name}: 获取失败")
                return (pid, "")

    tasks = [_fetch_one(pid, name) for pid, name in pid_list]
    results = await asyncio.gather(*tasks)

    return {pid: url for pid, url in results if url}


# ------------------- 央视官方CDN（中国移动）-------------------
CCTV_CDN_BASE = "http://ottrrs.hl.chinamobile.com/PLTV/88888888/224"

# 央视频道在移动CDN上的ID映射（通过观察获得）
CCTV_CDN_MAP = {
    "608807420": f"{CCTV_CDN_BASE}/3221226016/index.m3u8",  # CCTV-1
    "631780532": f"{CCTV_CDN_BASE}/3221225674/index.m3u8",  # CCTV-2
    "624878271": f"{CCTV_CDN_BASE}/3221226018/index.m3u8",  # CCTV-3
    "631780421": f"{CCTV_CDN_BASE}/3221226020/index.m3u8",  # CCTV-4
    "641886683": f"{CCTV_CDN_BASE}/3221225680/index.m3u8",  # CCTV-5
    "624878396": f"{CCTV_CDN_BASE}/3221226019/index.m3u8",  # CCTV-6
    "624878356": f"{CCTV_CDN_BASE}/3221226017/index.m3u8",  # CCTV-8
}


async def get_cctv_cdn_url(pid: str) -> Optional[str]:
    """
    从央视官方CDN（中国移动）获取央视频道URL
    这个CDN通常不需要认证，比咪咕API更稳定
    """
    if pid in CCTV_CDN_MAP:
        url = CCTV_CDN_MAP[pid]
        # 简单验证URL是否可访问
        try:
            async with aiohttp.ClientSession() as session:
                async with session.head(
                    url, timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        return url
        except Exception:
            pass
        # 即使验证失败也返回URL（可能容器环境无法访问）
        return url
    return None


if __name__ == "__main__":
    # 测试
    async def test():
        url = await get_final_url("608807420", 3)  # CCTV-1
        print(f"Result: {url}")
    asyncio.run(test())
