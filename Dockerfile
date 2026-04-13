# 官方Python基础镜像，兼容Railway环境
FROM python:3.11-slim

# 安装系统依赖：仅保留视频处理必备的ffmpeg和基础运行库，移除所有废弃/冗余包
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 安装Python依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制核心代码
COPY main.py .

# 启动命令
CMD ["python", "main.py"]
