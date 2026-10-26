# 后端镜像。**单 worker**：sweeper 与 MCP 确认表是进程内状态，多副本/多 worker 会重复
# 提示、重复结单，确认请求还会落到没有那个 future 的进程上（见 app/main.py）。
#
# 本机没有 docker，这份文件**未经构建验证**——第一次用请就地核对：
#   docker build -t cs-agent . && docker run --rm -p 7861:7861 cs-agent
# 装不上编译依赖时，先确认基础镜像版本与 requirements.txt 的区间是否还匹配。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖单独一层：改代码不重装包
COPY src/backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt
# 国内网络可换成：RUN pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY src/backend/app ./app
COPY src/backend/scripts ./scripts
COPY src/backend/run.py ./
# 默认配置模板；真实密钥用 CS_CONFIG 指向挂载进来的 config.local.yaml，绝不烤进镜像
COPY src/backend/config.simple.yaml ./

RUN mkdir -p /app/data
VOLUME /app/data

EXPOSE 7861
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7861/health',timeout=3).status==200 else 1)"

CMD ["python", "run.py"]
