# =============================================================================
# ROCK Rocklet FC3 Custom Container Dockerfile
# =============================================================================
#
# 方案 A：使用自定义容器部署 rocklet（推荐）
#
# 使用方式:
#   1. 在项目根目录构建镜像:
#      docker build -t rock-rocklet:latest -f rock/deployments/fc3_rocklet/container/Dockerfile .
#   2. 推送到 ACR:
#      docker tag rock-rocklet:latest registry.cn-hangzhou.aliyuncs.com/your-namespace/rock-rocklet:latest
#      docker push registry.cn-hangzhou.aliyuncs.com/your-namespace/rock-rocklet:latest
#   3. 修改 s.yaml 中的镜像地址，执行: s deploy
#
# =============================================================================

FROM python:3.10-slim

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 设置工作目录
WORKDIR /code

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY pyproject.toml ./
COPY requirements_admin.txt ./

# 安装 Python 依赖（先安装依赖，利用 Docker 缓存）
RUN pip install --upgrade pip setuptools wheel && \
    pip install -r requirements_admin.txt

# 复制项目代码
COPY rock/ ./rock/

# 安装项目
RUN pip install -e .

# 创建非 root 用户（可选，提高安全性）
# RUN useradd -m -s /bin/bash rocklet && chown -R rocklet:rocklet /code
# USER rocklet

# 暴露端口（FC3 Custom Container 默认使用 9000）
EXPOSE 9000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:9000/is_alive || exit 1

# 启动命令
# FC3 会设置 FC_SERVER_PORT 环境变量，默认为 9000
CMD ["python", "-m", "rock.rocklet", "--host", "0.0.0.0", "--port", "9000"]
