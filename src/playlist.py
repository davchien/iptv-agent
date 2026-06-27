"""
播放列表生成模块
生成标准M3U和TXT格式，URL使用重定向地址
"""
from pathlib import Path
from typing import List

from .storage import ChannelInfo
from .utils import setup_logging, load_config, url_safe_encode

logger = setup_logging(load_config().get("logging", {}))


def _get_base_url(config: dict) -> str:
    """获取重定向基础URL"""
    server_host = config.get("server", {}).get("host", "0.0.0.0")
    server_port = config.get("server", {}).get("port", 8000)
    route_prefix = config.get("server", {}).get("route_prefix", "")

    # 如果host是0.0.0.0，替换为localhost（播放列表里的占位符）
    if server_host == "0.0.0.0":
        host = "localhost"
    else:
        host = server_host

    return f"http://{host}:{server_port}{route_prefix}"


def generate_m3u(channels: List[ChannelInfo], output_path: str, config: dict = None):
    """
    生成标准M3U格式播放列表
    URL使用 /play/{channel_id} 重定向地址
    """
    config = config or load_config()
    base_url = _get_base_url(config)
    use_redirect = config.get("output", {}).get("use_redirect_urls", True)

    lines = ['#EXTM3U url-tvg="http://epg.server/xmltv.xml"']
    current_group = None

    for ch in channels:
        # 分组分隔
        if ch.group != current_group:
            current_group = ch.group
            lines.append(f"\n# {current_group}")

        # EXTINF行
        extinf = f'#EXTINF:-1'
        if ch.tvg_id:
            extinf += f' tvg-id="{ch.tvg_id}"'
        extinf += f' tvg-name="{ch.name}"'
        if ch.logo:
            extinf += f' tvg-logo="{ch.logo}"'
        extinf += f' group-title="{ch.group}"'
        extinf += f',{ch.name}'
        lines.append(extinf)

        # URL
        if use_redirect:
            # 咪咕代理频道使用 /live/ 端点（实时鉴权）
            if ch.source == "migu_proxy":
                play_url = f"{base_url}/live/{ch.url}"
            else:
                play_url = f"{base_url}/play/{url_safe_encode(ch.channel_id)}"
        else:
            play_url = ch.url
        lines.append(play_url)

    # 写入
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    logger.info(f"M3U已生成: {output_path} ({len(channels)} 频道)")


def generate_txt(channels: List[ChannelInfo], output_path: str, config: dict = None):
    """生成TXT格式播放列表"""
    config = config or load_config()
    base_url = _get_base_url(config)
    use_redirect = config.get("output", {}).get("use_redirect_urls", True)

    lines = []
    current_group = None

    for ch in channels:
        if ch.group != current_group:
            current_group = ch.group
            lines.append(f"\n# {current_group}")
            lines.append("-" * 40)

        if use_redirect:
            if ch.source == "migu_proxy":
                url = f"{base_url}/live/{ch.url}"
            else:
                url = f"{base_url}/play/{url_safe_encode(ch.channel_id)}"
        else:
            url = ch.url

        lines.append(f"{ch.name},{url}")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    logger.info(f"TXT已生成: {output_path} ({len(channels)} 频道)")
