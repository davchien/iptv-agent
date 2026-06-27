"""
URL有效性测试模块
异步并发测试多个URL，返回最优可用URL
"""
import asyncio
from typing import List, Tuple

import aiohttp

from .utils import setup_logging, load_config

logger = setup_logging(load_config().get("logging", {}))


async def test_url(
    url: str,
    session: aiohttp.ClientSession = None,
    timeout: int = 8,
) -> Tuple[str, bool, float, str]:
    """
    测试单个URL的有效性
    返回: (url, is_valid, latency_ms, reason)
    
    如果没有提供session，会创建一个临时session（不推荐，每个URL都创建session效率低）
    推荐用法：在调用方创建session，然后传入
    """
    own_session = False
    if session is None:
        session = aiohttp.ClientSession()
        own_session = True

    reason = ""
    try:
        start = asyncio.get_event_loop().time()
        async with session.head(
            url,
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=True,
        ) as resp:
            latency = (asyncio.get_event_loop().time() - start) * 1000

            if resp.status in (200, 301, 302, 303, 307, 308):
                ct = resp.headers.get("Content-Type", "").lower()
                if any(t in ct for t in ["video", "audio", "mpegurl", "x-mpegurl", "octet-stream", "stream", "flv"]):
                    return url, True, latency, f"OK ({ct[:30]})"
                if resp.status in (301, 302, 303, 307, 308):
                    return url, True, latency, f"Redirect (HTTP {resp.status})"
                if resp.status == 200:
                    return await _test_with_get(session, url, timeout, latency)
                return url, True, latency, f"OK (HTTP {resp.status})"
            else:
                reason = f"HTTP {resp.status}"
                return url, False, latency, reason

    except asyncio.TimeoutError:
        return url, False, timeout * 1000, "Timeout"
    except aiohttp.ClientError as e:
        return url, False, -1, f"ClientError: {str(e)[:50]}"
    except Exception as e:
        return url, False, -1, f"Error: {str(e)[:50]}"
    finally:
        if own_session:
            await session.close()


async def _test_with_get(
    session: aiohttp.ClientSession,
    url: str,
    timeout: int,
    head_latency: float,
) -> Tuple[str, bool, float, str]:
    """用GET请求获取少量数据确认是否为有效流"""
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=True,
        ) as resp:
            if resp.status == 200:
                chunk = await resp.content.read(1024)
                if chunk:
                    return url, True, head_latency, f"OK (data {len(chunk)}B)"
            return url, True, head_latency, f"OK (HTTP {resp.status})"
    except Exception as e:
        return url, False, -1, f"GET Error: {str(e)[:50]}"


async def test_urls(
    urls: List[str],
    session: aiohttp.ClientSession = None,
    timeout: int = 8,
    concurrency: int = 10,
) -> List[Tuple[str, bool, float, str]]:
    """
    并发测试多个URL
    如果没有提供session，会自动创建一个（测试完成后自动关闭）
    """
    own_session = False
    if session is None:
        connector = aiohttp.TCPConnector(limit=concurrency, ttl_dns_cache=300)
        session = aiohttp.ClientSession(connector=connector)
        own_session = True

    try:
        tasks = [test_url(url, session, timeout) for url in urls]
        results = await asyncio.gather(*tasks)
        return list(results)
    finally:
        if own_session:
            await session.close()


async def find_best_url(
    urls: List[str],
    timeout: int = 8,
    concurrency: int = 10,
) -> Tuple[str, float, str]:
    """
    从多个候选URL中找到最优的一个
    返回: (best_url, latency_ms, reason)
    如果全部失败，返回 (urls[0], -1, "all_failed")
    """
    if not urls:
        return "", -1, "no_url"

    results = await test_urls(urls, session=None, timeout=timeout, concurrency=concurrency)

    valid = [(url, latency, reason) for url, ok, latency, reason in results if ok]
    if valid:
        valid.sort(key=lambda x: x[1])
        best_url, best_latency, reason = valid[0]
        logger.debug(f"最优URL: {best_url[:60]}... 延迟 {best_latency:.0f}ms")
        return best_url, best_latency, reason

    reasons = [r[3] for r in results]
    logger.warning(f"所有候选URL均无效: {'; '.join(reasons)}")
    return urls[0], -1, "all_failed"
