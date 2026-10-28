# 智能客服 Agent 镜像：前端构建 + 后端运行，单容器同源交付。
#
# 为什么把前端也打进来：之前 compose 只管后端，前端要"另找静态托管 + 反代 /api"，
# 于是 CORS 白名单成了一个每次部署都要手改的地方。现在 serve_frontend=true 直接由
# 后端挂 dist，同源、无 CORS、少一个组件；仍然想分开部署的话把 server.serve_frontend
# 关掉，本镜像照常只暴露 API。
#
# 多副本：B3-8 起确认表/记忆任务落库、扫描与重试走租约选主，可以 scale；
# 但 BM25 缓存/工具发现缓存/限流桶是每进程一份，扩容前读 docs/运维手册.md。
#
# CI 里会真的 build + up + 打冒烟脚本（.github/workflows/ci.yml 的 docker job）。

# ---------- 阶段 1：前端构建（产物只有 dist/，运行期不需要 node） ----------
FROM node:20-alpine AS webbuild
WORKDIR /web
COPY src/frontend/package.json src/frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY src/frontend/ ./
RUN npm run build            # vue-tsc --noEmit 先跑，类型不过就不出镜像

# ---------- 阶段 2：后端 ----------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖单独一层：改代码不重装包
COPY src/backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt
# 国内网络可换成：RUN pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 后端源码整体拷进来：data/（数据库与密钥）已被 .dockerignore 挡住，不会进层。
# 默认配置模板 config.simple.yaml 也在其中；真实密钥用 CS_CONFIG 指向挂载进来的
# config.local.yaml，绝不烤进镜像。alembic 迁移目录一起走，镜像内可跑 upgrade。
COPY src/backend/ ./

# 前端产物：由 server.serve_frontend 决定是否挂载
COPY --from=webbuild /web/dist ./web

RUN mkdir -p /app/data/uploads /app/data/logs /app/data/chroma \
    && useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser
VOLUME /app/data

EXPOSE 7861
# /ready 而不是 /health：就绪探针会确认库可查、迁移已跑、安全自检无致命项，
# 编排系统据此摘掉"活着但没法服务"的实例
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7861/ready',timeout=4).status==200 else 1)"

CMD ["python", "run.py"]
