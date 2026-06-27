"""
定时更新任务模块
定期拉取源、测试URL、生成播放列表
"""
import asyncio
import time
from datetime import datetime
from pathlib import Path

import aiohttp

from .utils import setup_logging, load_config
from .fetcher import fetch_and_parse
from .tester import find_best_url, test_url
from .storage import ChannelStore
from .playlist import generate_m3u, generate_txt

logger = setup_logging(load_config().get("logging", {}))


class Scheduler:
    """定时更新调度器"""

    def __init__(self, store: ChannelStore, config: dict):
        self.store = store
        self.config = config
        self._running = False
        self._task = None

    async def start(self):
        """启动调度器"""
        self._running = True
        if self.config.get("scheduler", {}).get("run_on_startup", True):
            logger.info("启动时立即执行全量更新...")
            await self.full_update()

        interval = self.config.get("scheduler", {}).get("full_update_interval", 43200)
        self._task = asyncio.create_task(self._loop(interval))

    async def _loop(self, interval: int):
        """定时循环"""
        while self._running:
            await asyncio.sleep(interval)
            if self._running:
                logger.info(f"定时全量更新触发（间隔 {interval}s）")
                await self.full_update()

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    async def full_update(self):
        """
        全量更新：
        1. 遍历所有启用的源，获取频道列表
        2. 对每个频道测试URL有效性
        3. 更新存储
        4. 生成播放列表
        """
        sources = self.config.get("sources", [])
        test_timeout = self.config.get("scheduler", {}).get("test_timeout", 8)
        inter_delay = self.config.get("scheduler", {}).get("inter_channel_delay", 5)
        concurrency = self.config.get("scheduler", {}).get("test_concurrency", 10)

        total_channels = 0
        updated_channels = 0
        failed_channels = 0

        start_time = time.time()
        logger.info("=" * 60)
        logger.info("开始全量更新")

        # 收集所有频道（去重：同名频道只保留第一个源的）
        all_channels = []
        seen_names = set()

        for source in sources:
            if not source.get("enabled", True):
                continue

            logger.info(f"处理源: {source['name']}")
            channels = await fetch_and_parse(source)

            if not channels:
                logger.warning(f"源 {source['name']} 未获取到任何频道")
                continue

            # 去重：只添加未出现过的频道名
            new_count = 0
            for ch in channels:
                if ch.name not in seen_names:
                    seen_names.add(ch.name)
                    all_channels.append(ch)
                    new_count += 1

            logger.info(f"源 {source['name']}: {len(channels)} 条，新增 {new_count} 条")

        total_channels = len(all_channels)
        logger.info(f"合并后总频道数: {total_channels}")

        # 创建aiohttp session用于批量测试
        connector = aiohttp.TCPConnector(limit=concurrency, ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=connector) as session:

            for i, ch in enumerate(all_channels):
                logger.info(f"[{i+1}/{total_channels}] 测试: {ch.name} ({ch.group})")

                # 测试所有候选URL，找最优
                if len(ch.all_urls) > 1:
                    best_url, latency, reason = await self._test_channel_urls(
                        session, ch.all_urls, test_timeout
                    )
                else:
                    # _test_single_url 返回 (url, is_valid, latency, reason)
                    url, is_valid, latency, reason = await self._test_single_url(
                        session, ch.url, test_timeout
                    )
                    best_url = url if is_valid else None

                if best_url:
                    ch.url = best_url
                    ch.latency = latency
                    ch.status = "ok"
                    ch.last_test = datetime.now().isoformat()
                    updated_channels += 1
                    logger.info(f"  ✓ 有效 (延迟 {latency:.0f}ms) [{reason}]")
                else:
                    ch.status = "failed"
                    failed_channels += 1
                    logger.warning(f"  ✗ 全部失效 [{reason}]")

                # 保存到存储
                self.store.set(ch)

                # 限流延迟
                if inter_delay > 0 and i < total_channels - 1:
                    await asyncio.sleep(inter_delay)

        # 保存 + 生成播放列表
        self.store.save()
        await self._generate_playlists()

        elapsed = time.time() - start_time
        logger.info("=" * 60)
        logger.info(
            f"全量更新完成: "
            f"总频道 {total_channels}, "
            f"有效 {updated_channels}, "
            f"失效 {failed_channels}, "
            f"耗时 {elapsed:.1f}s"
        )

    async def _test_channel_urls(
        self, session, urls: list, timeout: int
    ) -> tuple:
        """
        测试多个URL，返回最优的那个
        返回: (best_url, latency_ms, reason) 或 (None, -1, reason)
        """
        results = []
        for url in urls:
            url_result, ok, latency, reason = await self._test_single_url(session, url, timeout)
            results.append((url, ok, latency, reason))

        # 按延迟排序，取最快的有效URL
        valid = [(url, latency, reason) for url, ok, latency, reason in results if ok]
        if valid:
            valid.sort(key=lambda x: x[1])
            return valid[0][0], valid[0][1], valid[0][2]

        # 全部失败
        reasons = [r[3] for r in results]
        return None, -1, "; ".join(reasons)

    async def _test_single_url(
        self, session, url: str, timeout: int
    ) -> tuple:
        """
        测试单个URL
        返回: (url, is_valid, latency_ms, reason)
        """
        reason = ""
        try:
            start = time.time()
            async with session.head(
                url,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=True,
            ) as resp:
                latency = (time.time() - start) * 1000

                if resp.status in (200, 301, 302, 303, 307, 308):
                    ct = resp.headers.get("Content-Type", "").lower()
                    # 流媒体类型或直接重定向都算有效
                    if any(t in ct for t in ["video", "audio", "mpegurl", "x-mpegurl", "octet-stream", "stream", "flv"]):
                        return url, True, latency, f"OK ({ct[:30]})"
                    if resp.status in (301, 302, 303, 307, 308):
                        return url, True, latency, f"Redirect (HTTP {resp.status})"
                    # 200但类型不明，尝试读取少量数据
                    if resp.status == 200:
                        return await self._test_with_get(session, url, timeout, latency)
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

    async def _test_with_get(
        self, session, url: str, timeout: int, head_latency: float
    ) -> tuple:
        """GET请求读取少量数据确认"""
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

    async def _generate_playlists(self):
        """生成M3U和TXT播放列表"""
        output_config = self.config.get("output", {})
        output_dir = output_config.get("dir", "./data")
        sort_by_group = output_config.get("sort_by_group", True)
        only_valid = output_config.get("only_valid", True)

        channels = self.store.get_all()

        if only_valid:
            channels = [ch for ch in channels if ch.status == "ok"]

        if sort_by_group:
            channels.sort(key=lambda ch: (ch.group, ch.name))

        # 生成M3U
        m3u_path = str(Path(output_dir) / output_config.get("m3u_filename", "playlist.m3u"))
        generate_m3u(channels, m3u_path, self.config)

        # 生成TXT
        txt_path = str(Path(output_dir) / output_config.get("txt_filename", "playlist.txt"))
        generate_txt(channels, txt_path, self.config)

        logger.info(f"播放列表已生成: {m3u_path}, {txt_path}")

    async def update_single_channel(self, channel_id: str) -> bool:
        """更新单个频道（按需触发）"""
        ch = self.store.get(channel_id)
        if not ch:
            return False

        connector = aiohttp.TCPConnector(limit=5)
        async with aiohttp.ClientSession(connector=connector) as session:
            best_url, latency, reason = await self._test_channel_urls(
                session, ch.all_urls, self.config.get("scheduler", {}).get("test_timeout", 8)
            )

        if best_url:
            ch.url = best_url
            ch.latency = latency
            ch.status = "ok"
            ch.last_test = datetime.now().isoformat()
        else:
            ch.status = "failed"

        self.store.set(ch)
        self.store.save()
        await self._generate_playlists()
        return ch.status == "ok"
