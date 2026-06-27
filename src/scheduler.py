"""
定时更新任务模块
定期拉取源、测试URL、生成播放列表

优化版：并发测试所有候选URL，按流质量评分选最优，按域名限流防止触发限流
"""
import asyncio
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from .utils import setup_logging, load_config
from .fetcher import fetch_and_parse
from .tester import find_best_url, test_url_with_domain_limit
from .storage import ChannelStore, ChannelInfo
from .playlist import generate_m3u, generate_txt
from .filter import filter_channel, filter_urls, is_official_url

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
        2. 对每个频道并发测试所有候选URL（按域名限流）
        3. 按流质量评分选出最优URL
        4. 更新存储，生成播放列表
        """
        sources = self.config.get("sources", [])
        test_timeout = self.config.get("scheduler", {}).get("test_timeout", 8)
        test_bytes = self.config.get("scheduler", {}).get("test_bytes", 65536)
        channel_concurrency = self.config.get("scheduler", {}).get("test_concurrency", 10)
        domain_concurrency = self.config.get("scheduler", {}).get("domain_concurrency", 2)
        # 批次间延迟（秒），防止整体请求过快
        inter_batch_delay = self.config.get("scheduler", {}).get("inter_batch_delay", 1.0)

        total_channels = 0
        updated_channels = 0
        failed_channels = 0

        start_time = time.time()
        logger.info("=" * 60)
        logger.info(f"开始全量更新 (频道并发={channel_concurrency}, 域名并发={domain_concurrency})")

        # 收集所有频道（去重：同名频道合并候选URL）
        all_channels = []
        seen_names = set()
        raw_count = 0
        filtered_count = 0

        for source in sources:
            if not source.get("enabled", True):
                continue

            logger.info(f"处理源: {source['name']}")
            channels = await fetch_and_parse(source)

            if not channels:
                logger.warning(f"源 {source['name']} 未获取到任何频道")
                continue

            raw_count += len(channels)

            # 过滤：境外排除、地方台非数字排除、非官方域名排除
            for ch in channels:
                # 先过滤候选 URL，只保留官方域名
                ch.all_urls = filter_urls(ch.all_urls) if ch.all_urls else [ch.url]
                if not ch.all_urls:
                    ch.all_urls = [ch.url]
                # 重新设定主 URL 为第一个官方 URL
                ch.url = ch.all_urls[0]

                # 频道级别过滤
                ok, reason = filter_channel(ch)
                if not ok:
                    filtered_count += 1
                    continue

                # 去重：同名频道合并 all_urls
                if ch.name not in seen_names:
                    seen_names.add(ch.name)
                    all_channels.append(ch)
                else:
                    for existing in all_channels:
                        if existing.name == ch.name:
                            for u in ch.all_urls:
                                if u not in existing.all_urls:
                                    existing.all_urls.append(u)
                            break

            logger.info(f"源 {source['name']}: {len(channels)} 条 → 过滤后保留")

        total_channels = len(all_channels)
        logger.info(
            f"合并去重后: {total_channels} 个频道 "
            f"(原始 {raw_count}, 过滤淘汰 {filtered_count})"
        )

        # 创建共享 session 用于全量测试
        connector = aiohttp.TCPConnector(limit=channel_concurrency * 2, ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=connector) as session:
            # 分批并发测试频道
            batch_size = channel_concurrency
            for batch_idx in range(0, total_channels, batch_size):
                batch = all_channels[batch_idx:batch_idx + batch_size]
                batch_num = batch_idx // batch_size + 1
                total_batches = (total_channels + batch_size - 1) // batch_size

                # 并发测试当前批次的所有频道（每个频道内部也并发测试所有候选URL）
                tasks = [
                    self._test_one_channel(
                        session, ch, test_timeout, test_bytes, domain_concurrency
                    )
                    for ch in batch
                ]
                results = await asyncio.gather(*tasks)

                # 处理结果
                for ch, (best_url, latency, reason, quality) in zip(batch, results):
                    if best_url:
                        ch.url = best_url
                        ch.latency = latency
                        ch.status = "ok"
                        ch.last_test = datetime.now().isoformat()
                        # 保存质量信息到 channel 对象（可选）
                        ch._quality = quality
                        updated_channels += 1
                    else:
                        ch.status = "failed"
                        failed_channels += 1

                    self.store.set(ch)

                logger.info(
                    f"批次 {batch_num}/{total_batches} 完成: "
                    f"{batch_idx + 1}-{min(batch_idx + batch_size, total_channels)}/{total_channels}, "
                    f"有效={updated_channels}, 失效={failed_channels}"
                )

                # 批次间延迟（防止触发限流）
                if inter_batch_delay > 0 and batch_idx + batch_size < total_channels:
                    await asyncio.sleep(inter_batch_delay)

        # 清空旧数据，确保过滤后列表干净
        self.store.clear()

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

    async def _test_one_channel(
        self, session, ch, timeout: int, test_bytes: int, domain_concurrency: int
    ) -> tuple:
        """
        测试单个频道的所有候选URL，返回最优结果
        返回: (best_url, latency_ms, reason, quality_dict)
        """
        if len(ch.all_urls) <= 1:
            # 只有一个URL，直接测试
            url, ok, latency, reason, quality = await test_url_with_domain_limit(
                ch.url, session, timeout, domain_concurrency, test_bytes
            )
            return (url if ok else None, latency, reason, quality)

        # 多个候选URL：并发测试所有，按质量评分选最优
        results = await self._test_all_urls(
            session, ch.all_urls, timeout, test_bytes, domain_concurrency
        )

        # 按评分排序取最优
        valid = [r for r in results if r[1]]  # r[1] = is_valid
        if valid:
            # 用与 tester.py 相同的评分逻辑
            from .tester import _score_url
            valid.sort(key=lambda r: _score_url((r[0], r[1], r[2], r[3], r[4])), reverse=True)
            best_url, _, latency, reason, quality = valid[0]
            return best_url, latency, reason, quality

        # 全部失败
        reasons = [r[3] for r in results]
        return None, -1, "; ".join(reasons), {}

    async def _test_all_urls(
        self, session, urls: list, timeout: int, test_bytes: int, domain_concurrency: int
    ) -> list:
        """
        并发测试一个频道的所有候选URL（带域名限流）
        返回: list of (url, is_valid, latency_ms, reason, quality_dict)
        """
        tasks = [
            test_url_with_domain_limit(url, session, timeout, domain_concurrency, test_bytes)
            for url in urls
        ]
        return await asyncio.gather(*tasks)

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

        config = self.config.get("scheduler", {})
        best_url, latency, reason, quality = await find_best_url(
            ch.all_urls,
            timeout=config.get("test_timeout", 8),
            concurrency=config.get("test_concurrency", 10),
            domain_concurrency=config.get("domain_concurrency", 2),
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
