FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY src/ ./src/
COPY config.yaml ./

# 数据卷
VOLUME ["/app/data"]

# 暴露端口
EXPOSE 8000

# 启动命令
CMD ["python", "-m", "src.main"]
