"""
URL有效性测试模块
异步并发测试多个URL，返回最优可用URL
新增：流质量测试（读取64KB测吞吐速度）
"""

import asyncio
import time
from typing import List, Tuple

import aiohttp

from .utils import setup_logging, load_config

logger = setup_logging(load_config().get("logging", {}))

# 每个域名的并发信号量，防止触发限流
_domain_semaphores = {}
_domain_lock = asyncio.Lock()


async def _get_domain_semaphore(domain: str, limit: int = 2) -> asyncio.Semaphore:
    """获取指定域名的信号量（懒初始化）"""
    async with _domain_lock:
        if domain not in _domain_semaphores:
            _domain_semaphores[domain] = asyncio.Semaphore(limit)
        return _domain_semaphores[domain]


def _extract_domain(url: str) -> str:
    """从URL中提取域名"""
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc
    except Exception:
        return url


async def test_url(
    url: str,
    session: aiohttp.ClientSession = None,
    timeout: int = 8,
    test_bytes: int = 65536,
) -> Tuple[str, bool, float, str, dict]:
    """
    测试单个URL的有效性，并测量流质量
    返回: (url, is_valid, latency_ms, reason, quality_dict)
    quality_dict: {"throughput_kbps": float, "bytes_read": int, "has_video": bool}
    """
    own_session = False
    if session is None:
        session = aiohttp.ClientSession()
        own_session = True

    quality = {"throughput_kbps": 0.0, "bytes_read": 0, "has_video": False}
    reason = ""
    try:
        start = time.time()
        async with session.head(
            url,
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=True,
        ) as resp:
            latency = (time.time() - start) * 1000

            if resp.status == 403:
                return url, False, latency, "HTTP 403 Forbidden", quality
            if resp.status == 429:
                return url, False, latency, "HTTP 429 Rate Limited", quality
            if resp.status not in (200, 301, 302, 303, 307, 308):
                return url, False, latency, f"HTTP {resp.status}", quality

            # 如果是重定向，记录重定向目标（不跟随，由调用方决定）
            final_url = str(resp.url) if resp.url else url

            # 尝试读取少量流数据测速
            if resp.status == 200:
                quality_result = await _probe_stream(session, url, timeout, test_bytes)
                quality.update(quality_result)

            return url, True, latency, f"OK (latency {latency:.0f}ms)", quality

    except asyncio.TimeoutError:
        return url, False, timeout * 1000, "Timeout", quality
    except aiohttp.ClientError as e:
        return url, False, -1, f"ClientError: {str(e)[:50]}", quality
    except Exception as e:
        return url, False, -1, f"Error: {str(e)[:50]}", quality
    finally:
        if own_session:
            await session.close()


async def _probe_stream(
    session: aiohttp.ClientSession,
    url: str,
    timeout: int,
    test_bytes: int,
) -> dict:
    """
    探测流质量：读取 test_bytes 字节，测量吞吐速度
    返回: {"throughput_kbps": float, "bytes_read": int, "has_video": bool}
    """
    result = {"throughput_kbps": 0.0, "bytes_read": 0, "has_video": False}
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=True,
        ) as resp:
            if resp.status != 200:
                return result

            start = time.time()
            data = await resp.content.readexactly(test_bytes)
            elapsed = time.time() - start
            if elapsed < 0.001:
                elapsed = 0.001

            result["bytes_read"] = len(data)
            result["throughput_kbps"] = len(data) * 8 / elapsed / 1000

            # 简单判断是否有视频轨道（M3U8 检测）
            if b"RESOLUTION=" in data or b"#EXTINF" in data:
                result["has_video"] = True
            # FLV 检测
            elif data[:3] == b"FLV":
                result["has_video"] = True
            # TS 检测
            elif data[:1] == b"\x47":
                result["has_video"] = True

            return result

    except asyncio.IncompleteReadError as e:
        # 读到了部分数据
        if e.partial:
            elapsed = max(time.time() - start if 'start' in dir() else 0.001, 0.001)
            result["bytes_read"] = len(e.partial)
            result["throughput_kbps"] = len(e.partial) * 8 / elapsed / 1000
            result["has_video"] = True  # 能读到数据说明是流
        return result
    except Exception:
        return result


async def test_url_with_domain_limit(
    url: str,
    session: aiohttp.ClientSession,
    timeout: int = 8,
    domain_concurrency: int = 2,
    test_bytes: int = 65536,
) -> Tuple[str, bool, float, str, dict]:
    """
    带域名限流的URL测试
    """
    domain = _extract_domain(url)
    sem = await _get_domain_semaphore(domain, domain_concurrency)
    async with sem:
        return await test_url(url, session, timeout, test_bytes)


async def test_urls(
    urls: List[str],
    session: aiohttp.ClientSession = None,
    timeout: int = 8,
    concurrency: int = 10,
    domain_concurrency: int = 2,
    test_bytes: int = 65536,
) -> List[Tuple[str, bool, float, str, dict]]:
    """
    并发测试多个URL，带域名限流
    返回: List of (url, is_valid, latency_ms, reason, quality_dict)
    """
    own_session = False
    if session is None:
        connector = aiohttp.TCPConnector(limit=concurrency, ttl_dns_cache=300)
        session = aiohttp.ClientSession(connector=connector)
        own_session = True

    try:
        tasks = [
            test_url_with_domain_limit(url, session, timeout, domain_concurrency, test_bytes)
            for url in urls
        ]
        results = await asyncio.gather(*tasks)
        return list(results)
    finally:
        if own_session:
            await session.close()


def _score_url(result: Tuple[str, bool, float, str, dict]) -> float:
    """
    给URL打分，分数越高越好
    评分规则：
    1. 无效URL = -1（排最后）
    2. 有效URL = 吞吐速度(kbps) * 0.6 + (10000 / max(latency,1)) * 0.4
    3. 有视频轨道加分
    """
    url, ok, latency, reason, quality = result
    if not ok:
        return -1.0
    throughput = quality.get("throughput_kbps", 0)
    latency_score = 10000.0 / max(latency, 1)
    score = throughput * 0.6 + latency_score * 0.4
    if quality.get("has_video"):
        score += 500  # 有视频轨道加分
    return score


async def find_best_url(
    urls: List[str],
    timeout: int = 8,
    concurrency: int = 10,
    domain_concurrency: int = 2,
) -> Tuple[str, float, str, dict]:
    """
    从多个候选URL中找到最优的一个（基于流质量评分）
    返回: (best_url, latency_ms, reason, quality_dict)
    如果全部失败，返回 ("", -1, "all_failed", {})
    """
    if not urls:
        return "", -1, "no_url", {}

    results = await test_urls(
        urls,
        session=None,
        timeout=timeout,
        concurrency=concurrency,
        domain_concurrency=domain_concurrency,
    )

    valid = [(url, ok, latency, reason, quality)
             for url, ok, latency, reason, quality in results if ok]
    if valid:
        # 按评分排序，取最高分
        valid.sort(key=lambda x: _score_url((x[0], x[1], x[2], x[3], x[4])), reverse=True)
        best_url, _, best_latency, reason, quality = valid[0]
        logger.debug(
            f"最优URL: {best_url[:60]}... "
            f"延迟 {best_latency:.0f}ms, "
            f"吞吐 {quality.get('throughput_kbps', 0):.0f}kbps"
        )
        return best_url, best_latency, reason, quality

    reasons = [r[3] for r in results]
    logger.warning(f"所有候选URL均无效: {'; '.join(reasons)}")
    return urls[0], -1, "all_failed", {}
