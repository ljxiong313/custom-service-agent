# 智能客服 Agent（AgentCustomService）

基于 **FastAPI + LangChain + Vue3 + Element Plus** 全自建的电商智能客服系统，
架构参考作者的另一个项目 `AgentChat`（仅参考设计思路，代码全新重建）。

## 核心能力（P0–P8 已全部落地）

| 能力 | 阶段 | 说明 |
|---|---|---|
| 流式多轮对话 | P0 | SSE 流式输出、会话管理、JWT 登录 |
| RAG 商品知识库 | P1 | Excel/PDF/图片/TXT 上传解析；向量+BM25 混合检索（RRF 融合 + rerank）；引用溯源；Excel 同步写结构化商品表，价格/库存走 SQL 精确查询 |
| 三级记忆 | P2 | 短期 token 滑窗 / 中期会话滚动摘要 / 长期用户画像（向量召回，管理页可删） |
| 企业微信转人工 | P3 | 关键词/模型判定先**询问买家**、回复确认后才建单（P8 两步制，见下）；自建应用 textcard 推送客服，回调回复回写会话；状态机 bot↔awaiting_confirm↔pending_human↔human；未配企微时 mock 外发箱演示 |
| MCP | P4 | stdio/sse/streamable_http 服务器接入，工具自动发现并 bind；写操作确认卡片（180s 超时默认拒绝） |
| 管理控制台 | P5 | 知识库/商品/检索测试、转人工工作台、记忆管理、MCP 管理、设置；聊天内工具过程、引用、人工客服徽标展示 |
| 电商平台接入 | P7 | 淘宝/京东/拼多多/抖店 webhook 回调 + 自动回复；影子用户/会话映射复用同一 Agent 管道；客服工作台回复自动送达平台买家；mock 模式无资质可演示全链路（真实参数见 docs/平台接入.md） |
| 会话运营台 | P8 | /chat 变为「客（买家）↔我（Agent）」全渠道视图；转人工两步制（Agent 询问→客户确认才建单转企微）；5 分钟空闲提示结束、再 1 分钟无回复自动终结会话并强制关闭未结工单；运营可代答（买家无感人工/AI），支持粘贴截图/发送文件 |

## 快速开始

### 后端（端口 7861）

```bash
cd src/backend
py -m pip install -r requirements-dev.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
# 创建本地覆盖配置（含真实 api_key），勿提交：
#   复制 config.simple.yaml -> config.local.yaml，填写 multi_models.dashscope.api_key
#   然后 export CS_CONFIG=config.local.yaml
py run.py                    # host/port 读配置里的 server.*，单 worker、不开 reload
```

- 健康检查 `GET /health`，接口文档 `GET /docs`
- **只能单进程部署**：会话生命周期扫描和 MCP 工具确认表是进程内状态，多 worker 会
  重复提示、重复结单、确认必然超时（`app/main.py` 有检测与告警）。
- 数据库默认 SQLite：`src/backend/data/cs_agent.db`（自动创建，相对路径锚定到后端根目录，
  换 cwd 启动不会漂移）
- **模型 key 需要哪些能力**：`chat`（对话/摘要/画像）、`embedding`（向量化）、`rerank`（重排）、
  `vl`（图片知识库）是四类独立权限。订阅套餐类 key（如 token-plan）常只放行对话模型，
  此时向量化会 404/403，检索自动降级 BM25——不是代码问题，需换放行 embedding 的标准 key。
- **未配置有效 api_key 时的降级行为**：对话报可读错误；上传自动跳过向量化，检索降级
  BM25 关键词；图片解析不可用；转人工/工单回路不受影响（不依赖模型）。

### 前端（端口 8091）

```bash
cd src/frontend
npm install        # 本机建议 volta run npm install
npm run dev
```

浏览器打开 <http://localhost:8091>，注册任意测试账号。`/api` 由 Vite 代理到 7861。
页面：会话运营台（/chat，P8 起为「客=买家 ↔ 我=Agent」全渠道视图）、知识库（/knowledge）、
转人工工作台（/handoff）、记忆（/memory）、MCP（/mcp）、设置（/settings）。

### 质量闸门（离线，不需要后端在跑）

```bash
py -m pytest                    # 164 项单测：临时 SQLite + 临时 Chroma，模型 key 留空走降级路径
py -m ruff check src eval       # 120 列，规则 E,F,W,I,B,UP,C4,SIM；刻意不跑 ruff format
py -m mypy                      # 覆盖 app/ 与 eval/，口径 0 error
cd src/frontend && volta run npm run typecheck
```

配置都在仓库根 `pyproject.toml`（pytest 的 `testpaths` 限定了收集范围，去掉会把
`scripts/smoke_test.py` 当测试文件导入并在模块末尾 `sys.exit()` 上直接 INTERNALERROR）。

### 演示数据与验证

```bash
cd src/backend
py scripts/smoke_test.py       # 45 项端到端冒烟（P1 入库/分块可视/重新入库/重建向量、P3 转人工闭环、P4 MCP 发现）
py scripts/platform_smoke.py   # 29 项平台+生命周期冒烟（P7 回调映射/幂等去重 / P8 确认式转人工、代答外发、超时终结后重新开一轮 / 运营台手动建单+看板统计）
py ../../eval/run_eval.py      # 评测 9 项：检索召回/商品精确查询/对话质量断言
CS_CONFIG=config.local.yaml py scripts/reindex_vectors.py   # 换 embedding 模型或补建向量（以 Chunk 表为准）
CS_CONFIG=config.local.yaml py scripts/reingest_files.py --all  # 改了 parser/chunker 后，用原文件重建切片+向量+商品
```

单文件重建不用停服务手跑脚本：知识库页每行有「分块」（看切片正文与是否已建向量）、
「重新入库」（重跑解析+分块+向量化，failed 文件与 parser 改动后修复都走它）与
「重建向量」（只补向量，换 embedding 模型后用）三个按钮，任务是后台跑的，页面自动轮询状态。

演示物料：`data/demo_products.xlsx`（4 个 SKU）、`data/demo_policy.txt`（售后政策）、
`scripts/demo_mcp_server.py`（FastMCP 订单查询服务器，MCP 页可按其命令添加 `demo-order`）。
平台买家演示（无需资质）：`POST /api/platform/taobao/callback` 发 `{"buyer_id":"b1","shop_id":"s1","text":"…","msg_id":"m1"}`
→ 运营台自动出现「[taobao] b1」会话并展示 Agent 自动回复历史。
转人工演示（P8 两步制）：买家消息含"转人工/投诉"→ Agent 先询问是否转人工 → 买家回复"是"→
建单推企微 → 运营台/工作台人工回复（买家无感人工/AI，平台会话经外发箱送达）→ 结单恢复机器人；
会话空闲 5 分钟自动发结束提示，再 1 分钟无回复自动终结并强制关闭未结工单。

## 文档索引

- [项目结构说明](docs/项目结构说明.md) — 目录职责、模块导览与分层依赖规则
- [代码规范](docs/代码规范.md) — 后端/前端编码约定
- [工具规划](docs/工具规划.md) — Agent 工具清单、判断结论与实施现状
- [平台接入](docs/平台接入.md) — 四平台 webhook/签名/API 参数字典与真实对接 checklist（P7）
- [待办与风险](docs/待办与风险.md) — 部署约束、安全暴露面、未完成项、测试缺口与建议路线
- [评测用例](eval/cases.yaml) — 检索/商品/对话质量断言

## 配置体系

统一 YAML（非 .env），加载优先级：`CS_CONFIG` 环境变量指向的文件 > `config.simple.yaml`。
`config.local.yaml`（真实密钥）已被 .gitignore 排除。顶层节（与 config.simple.yaml 一一对应）：
`server`（host/port，`run.py` 读它）、`database.url`、`security`（jwt_secret/算法/有效期）、
`multi_models` + `default_model`（对话/向量/视觉/rerank 四类权限）、`llm`（超时与重试）、
`memory`（短期窗口与长期开关）、`session`（P8 空闲提示/自动终结秒数与扫描周期）、
`rag`（分块与召回参数、Chroma 路径、重建 worker 数）、`mcp.allowed_commands`、
`wecom`（自建应用与回调，`mock: true` 时不外发）、`platforms`（四电商平台的凭证/网关/回调
验签与回复参数字典，同样支持 mock）。
注意 CS_CONFIG 指向的文件是**整文件替换**（不合并默认值），本地覆盖文件需自带全部节。

## 备份与部署

```bash
cd src/backend
py scripts/backup_data.py --dry-run          # 看要备哪些、多大
py scripts/backup_data.py --keep 7           # SQLite 一致性快照 + chroma/uploads/配置，轮转留 7 份
```

数据库用 `sqlite3` 的 backup API 复制：服务开着 WAL 时手抄 `cs_agent.db` 会漏掉还在
`-wal` 里的最新写入，backup API 在 SQL 层折叠 WAL，服务不停也能拿到一致快照（备完自动
跑 `PRAGMA integrity_check`）。Chroma 目录没有在线快照接口，运行中拷出来可能撕裂——
反正 Chunk 表是唯一真相，`scripts/reindex_vectors.py` 随时能重建；要严格一致就先停服务。
快照里的 `config.local.yaml` 是明文密钥，`data/` 已被 gitignore，别把备份单独拷到别处。

容器化：`Dockerfile` + `docker-compose.yml`（后端单容器，`data/` 与 `config.local.yaml`
挂出来，前端 `npm run build` 后由任意静态托管反代 `/api`）。**这两份文件在本机没有
docker，未经构建验证**，第一次起请先核对 `/health` 和日志里的配置路径。

## 已知边界与取舍

- BM25 索引现取现建，规模假设 <10k chunk；更大语料需持久化增量索引。
- 单进程 + SQLite 是刻意的取舍：换来零外部依赖（无 Redis/Celery），代价是不能水平扩展。
- RBAC 只有 admin/user 两档，运营台跨用户读/代答放开（按一个客服团队共用工作台设计）。
- 电商四平台的真实出站参数未核对官方文档，默认 mock；平台名下的可配置骨架与就绪自检
  见 `docs/平台接入.md`。
- 完整清单（含安全暴露面、未验证项、后续路线）见 [待办与风险](docs/待办与风险.md)。

## 致谢与参考

架构与交互风格参考同栈的 `AgentChat`（Vue3 + FastAPI + LangChain）项目，
本项目为独立重建实现，不包含其源代码。
