# Tunely Server — 构建上下文为本目录（python/）
#
# 用法（在仓库根目录）:
#   docker build -t tunely-server -f python/Dockerfile python/
#
# 运行示例:
#   docker run -d -p 8000:8000 -p 9080:9080 \
#     -e WS_TUNNEL_TCP_LISTEN="9080:dsh" \
#     -v tunely-data:/data \
#     tunely-server \
#     tunely serve --host 0.0.0.0 --port 8000 --domain example.com \
#       --database sqlite+aiosqlite:////data/tunely.db --api-key <KEY>
#
# 注意:
# - serve CLI 不读取 TUNELY_* 环境变量（host/port/database/api-key 走命令行参数）
# - TCP 监听无 CLI 参数，经 WS_TUNNEL_TCP_LISTEN / WS_TUNNEL_TCP_LISTEN_HOST /
#   WS_TUNNEL_TCP_TARGET_DOMAIN 环境变量注入
# - 容器内监听地址必须是 0.0.0.0（本机裸跑时才建议 127.0.0.1），
#   公网暴露面由端口映射与宿主防火墙控制
FROM python:3.12-slim

# 国内构建可传 --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DEFAULT_TIMEOUT=120

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir .

EXPOSE 8000
ENTRYPOINT ["tunely"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
