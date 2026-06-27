"""
iptv-agent 主入口
FastAPI 服务：重定向路由 + 健康检查 + 播放列表下载
"""
import asyncio
import time
from pathlib import Path
from datetime import datetime

import aiohttp
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from .utils import setup_logging, load_config, url_safe_decode
from .storage import ChannelStore
from .scheduler import Scheduler
from .migu_api import fetch_stream_url

logger = setup_logging(load_config().get("logging", {}))

# ============================================================
# 初始化
# ============================================================
app = FastAPI(
    title="IPTV Agent",
    description="轻量级 IPTV 直播代理服务 - 仅返回302重定向，不代理数据流",
    version="1.0.0",
)

# 配置立即加载（limiter需要）
config = load_config()

# 延迟初始化（在startup事件中完成）
store = None
scheduler = None
http_session: aiohttp.ClientSession = None  # 共享 session，复用连接

# 限流
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[f"{config.get('ratelimit', {}).get('requests_per_second', 2)}/second"],
)
if config.get("ratelimit", {}).get("enabled", True):
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ============================================================
# 启动 / 关闭事件
# ============================================================
@app.on_event("startup")
async def startup():
    global config, store, scheduler, http_session
    print("=== IPTV Agent 启动中 ===")
    logger.info("iptv-agent 启动中...")
    
    # 加载配置
    config = load_config()
    print(f"配置已加载，数据目录: {config.get('output', {}).get('dir', './data')}")
    
    # 初始化 HTTP 会话（复用连接）
    connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
    http_session = aiohttp.ClientSession(connector=connector)
    
    # 初始化存储（从磁盘加载已有数据）
    data_dir = config.get("output", {}).get("dir", "./data")
    store = ChannelStore(data_dir=data_dir)
    print(f"存储已初始化，加载了 {store.count()} 个频道")
    logger.info(f"已从 {data_dir} 加载 {store.count()} 个频道")
    
    # 初始化调度器
    scheduler = Scheduler(store, config)
    
    # 是否立即执行全量更新
    if config.get("scheduler", {}).get("run_on_startup", False):
        logger.info("启动时执行全量更新...")
        asyncio.create_task(scheduler.full_update())
    else:
        logger.info("跳过启动时更新（run_on_startup=false），使用已有数据")
    
    print(f"=== 启动完成，频道数: {store.count()} ===")


@app.on_event("shutdown")
async def shutdown():
    global http_session
    logger.info("iptv-agent 关闭中...")
    if scheduler:
        await scheduler.stop()
    if http_session:
        await http_session.close()


# ============================================================
# 路由
# ============================================================

@app.get("/")
async def root():
    """首页：返回简单说明"""
    return {
        "service": "iptv-agent",
        "version": "1.0.0",
        "endpoints": {
            "play": "/play/{channel_name}  - 重定向到频道直连URL",
            "live": "/live/{channel_id}    - 咪咕鉴权实时代理（内置）",
            "playlist": "/playlist.m3u  - 下载M3U播放列表",
            "playlist_txt": "/playlist.txt  - 下载TXT播放列表",
            "health": "/health  - 健康检查",
            "channels": "/channels  - 查看所有频道（JSON）",
            "update": "/update  - 触发全量更新",
        },
        "stats": {
            "total_channels": store.count(),
            "last_update": "启动时更新" if scheduler else "未知",
        },
    }


@app.get("/health")
async def health():
    """健康检查（Docker HEALTHCHECK 用）"""
    return {
        "status": "ok",
        "channels": store.count(),
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/play/{channel_query}")
@limiter.limit(f"{config.get('ratelimit', {}).get('requests_per_second', 2)}/second")
async def play_channel(request: Request, channel_query: str):
    """
    重定向到频道直连URL
    查找策略（方案C）：
    1. 精确匹配 channel_id
    2. 精确匹配 tvg_id
    3. 精确匹配 name
    4. 模糊匹配 name（包含关系）
    """
    # URL解码
    decoded = url_safe_decode(channel_query)

    # 查找频道
    ch = _find_channel(decoded)

    if not ch:
        logger.warning(f"频道未找到: {decoded}")
        raise HTTPException(status_code=404, detail=f"频道未找到: {decoded}")

    if ch.status != "ok":
        logger.warning(f"频道当前无效: {ch.name} ({ch.status})")
        raise HTTPException(status_code=502, detail=f"频道当前无效: {ch.name}")

    # 返回302重定向
    logger.info(f"重定向: {ch.name} -> {ch.url[:80]}...")
    return RedirectResponse(url=ch.url, status_code=302)


@app.get("/channels")
async def list_channels(group: str = None, status: str = None, q: str = None):
    """
    查看所有频道（JSON）
    参数：
      group: 按分组过滤
      status: 按状态过滤（ok/failed）
      q: 搜索关键字
    """
    channels = store.get_all()

    if group:
        channels = [ch for ch in channels if ch.group == group]
    if status:
        channels = [ch for ch in channels if ch.status == status]
    if q:
        channels = [ch for ch in channels if q in ch.name]

    return {
        "total": len(channels),
        "groups": store.get_groups(),
        "channels": [
            {
                "channel_id": ch.channel_id,
                "name": ch.name,
                "group": ch.group,
                "status": ch.status,
                "latency": ch.latency,
                "last_test": ch.last_test,
                "url": ch.url[:100] + "..." if len(ch.url) > 100 else ch.url,
            }
            for ch in channels
        ],
    }


@app.get("/channels/{channel_id}")
async def get_channel(channel_id: str):
    """获取单个频道详情"""
    ch = store.get(channel_id) or store.get_by_name(channel_id)
    if not ch:
        raise HTTPException(status_code=404, detail="频道未找到")

    return {
        "channel_id": ch.channel_id,
        "name": ch.name,
        "group": ch.group,
        "logo": ch.logo,
        "tvg_id": ch.tvg_id,
        "source": ch.source,
        "status": ch.status,
        "latency": ch.latency,
        "last_test": ch.last_test,
        "url": ch.url,
        "all_urls_count": len(ch.all_urls),
    }


@app.post("/update")
async def trigger_update():
    """手动触发全量更新"""
    asyncio.create_task(scheduler.full_update())
    return {"status": "update_started", "message": "全量更新已触发，请稍后查看 /health"}


@app.get("/playlist.m3u")
async def get_m3u_playlist():
    """下载M3U播放列表"""
    m3u_path = Path(config.get("output", {}).get("dir", "./data")) / "playlist.m3u"
    if not m3u_path.exists():
        raise HTTPException(status_code=404, detail="播放列表尚未生成，请稍候")
    return FileResponse(
        path=str(m3u_path),
        media_type="audio/x-mpegurl",
        filename="playlist.m3u",
    )


@app.get("/playlist.txt")
async def get_txt_playlist():
    """下载TXT播放列表"""
    txt_path = Path(config.get("output", {}).get("dir", "./data")) / "playlist.txt"
    if not txt_path.exists():
        raise HTTPException(status_code=404, detail="播放列表尚未生成，请稍候")
    return FileResponse(
        path=str(txt_path),
        media_type="text/plain",
        filename="playlist.txt",
    )


@app.get("/groups")
async def get_groups():
    """获取所有分组"""
    return {"groups": store.get_groups()}


# ════════════════════════════════════════════════════════════
# 咪咕鉴权代理（内置，无需外部容器）
# ════════════════════════════════════════════════════════════

# 流地址缓存：{channel_id: (expires_at, stream_url)}
_stream_cache: dict[str, tuple[float, str]] = {}
CACHE_TTL = 3 * 60 * 60  # 3 小时


@app.get("/live/{channel_id}")
async def live_redirect(request: Request, channel_id: str):
    """
    咪咕直播 302 重定向代理

    当播放器请求 /live/608807420 时：
    1. 检查缓存（3小时有效）
    2. 调用咪咕 API 实时鉴权获取流地址
    3. 返回 302 重定向到真实流地址

    无需部署外部 migu2026 容器，iptv-agent 自身完成鉴权。
    """
    # 检查缓存
    cache_entry = _stream_cache.get(channel_id)
    if cache_entry:
        expires, cached_url = cache_entry
        if time.time() < expires:
            logger.debug(f"使用缓存: {channel_id}")
            return RedirectResponse(url=cached_url, status_code=302)

    # 实时获取
    logger.info(f"获取流地址: {channel_id}")
    stream_url = await fetch_stream_url(http_session, channel_id)

    if not stream_url:
        raise HTTPException(status_code=502, detail=f"无法获取频道 {channel_id} 的流地址")

    # 写入缓存
    _stream_cache[channel_id] = (time.time() + CACHE_TTL, stream_url)

    return RedirectResponse(url=stream_url, status_code=302)


# ============================================================
# 辅助函数
# ============================================================
def _find_channel(query: str) -> object:
    """
    查找频道（方案C）：
    1. 精确匹配 channel_id
    2. 精确匹配 tvg_id
    3. 精确匹配 name
    4. 模糊匹配 name
    """
    # 1. channel_id 精确匹配
    ch = store.get(query)
    if ch:
        return ch

    # 2. tvg_id 精确匹配
    for ch in store.get_all():
        if ch.tvg_id == query:
            return ch

    # 3. name 精确匹配
    ch = store.get_by_name(query)
    if ch:
        return ch

    # 4. 模糊匹配（query 在 name 中，或 name 在 query 中）
    for ch in store.get_all():
        if query in ch.name or ch.name in query:
            return ch

    return None


# ============================================================
# 主程序入口
# ============================================================
if __name__ == "__main__":
    import uvicorn
    server_config = config.get("server", {})
    host = server_config.get("host", "0.0.0.0")
    port = server_config.get("port", 8000)

    logger.info(f"启动服务: http://{host}:{port}")
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_config=None,  # 使用自定义日志
    )
