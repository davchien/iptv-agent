# IPTV Agent

轻量级 IPTV 直播代理服务。定期抓取频道列表、测试直连 URL 有效性，生成播放列表，并通过 HTTP 302 重定向提供直播流访问（不代理音视频数据流）。

## 功能特性

- 📡 **多源支持**：从多个 M3U 源自动抓取频道列表
- 🔍 **智能测试**：异步并发测试 URL 有效性，自动选择最优源
- 📝 **双格式输出**：同时生成 M3U 和 TXT 格式播放列表
- 🔀 **重定向服务**：返回 302 重定向至直连 URL，节省带宽
- ⏰ **定时更新**：每 12 小时自动更新，支持手动触发
- 🛡️ **限流保护**：令牌桶算法防止滥用
- 🐳 **容器化**：提供 Dockerfile 和 docker-compose 配置
- 📊 **健康检查**：`/health` 端点供 Docker 健康检测

## 快速开始

### 方式一：Docker（推荐）

```bash
# 构建并启动
docker-compose up -d

# 查看日志
docker-compose logs -f

# 停止
docker-compose down
```

### 方式二：本地运行

```bash
# 安装依赖
pip install -r requirements.txt

# 启动服务
python -m src.main
```

## API 文档

服务启动后访问 `http://localhost:8000/docs` 查看交互式 API 文档。

### 主要端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 服务首页，返回端点列表 |
| `/health` | GET | 健康检查 |
| `/play/{channel_name}` | GET | 重定向到频道直连 URL |
| `/channels` | GET | 查看所有频道（支持过滤） |
| `/channels/{id}` | GET | 查看单个频道详情 |
| `/playlist.m3u` | GET | 下载 M3U 播放列表 |
| `/playlist.txt` | GET | 下载 TXT 播放列表 |
| `/groups` | GET | 获取所有分组 |
| `/update` | POST | 手动触发全量更新 |

### 播放列表使用

生成的播放列表中的 URL 格式为：

```
http://your-server:8000/play/CCTV1
```

播放器请求此 URL 时，服务返回 302 重定向至当前有效的直连 URL。

## 配置说明

编辑 `config.yaml` 自定义配置：

```yaml
sources:
  - name: "源名称"
    type: "m3u"
    url: "https://..."
    enabled: true

scheduler:
  full_update_interval: 43200  # 12小时
  inter_channel_delay: 5        # 频道间延迟（秒）
  test_timeout: 8               # 测试超时（秒）

server:
  host: "0.0.0.0"
  port: 8000

ratelimit:
  requests_per_second: 2
  enabled: true
```

## 项目结构

```
iptv-agent/
├── src/
│   ├── __init__.py
│   ├── main.py          # FastAPI 入口
│   ├── scheduler.py     # 定时更新任务
│   ├── fetcher.py       # M3U 源抓取
│   ├── tester.py        # URL 有效性测试
│   ├── storage.py       # 频道存储
│   ├── playlist.py      # 播放列表生成
│   └── utils.py         # 工具函数
├── config.yaml          # 配置文件
├── requirements.txt     # Python 依赖
├── Dockerfile           # Docker 镜像构建
├── docker-compose.yml   # Docker Compose 配置
└── README.md            # 本文件
```

## 许可证

MIT License
