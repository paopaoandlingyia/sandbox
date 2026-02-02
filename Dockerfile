# 使用轻量级 Python 镜像
FROM python:3.11-slim

# 安装更丰富的工具集，让它更像一个真正的 Linux
RUN apt-get update && apt-get install -y \
    curl \
    git \
    wget \
    procps \
    net-tools \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 复制依赖配置
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制服务端代码
COPY main.py .

# 创建工作空间
RUN mkdir -p /workspace && chmod 777 /workspace
ENV WORKSPACE_DIR=/workspace

# 暴露端口（Hugging Face 默认使用 7860）
EXPOSE 7860

# 启动服务（注意：是 main:app 而不是 main.py:app）
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
