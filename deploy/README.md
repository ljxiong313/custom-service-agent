# deploy/ —— 多店生产部署交付物

前提口径（这三条决定了下面所有文件的形状）：

| 已定的事 | 带来的要求 |
|---|---|
| 有电商平台开放资质 | 平台可以 `mock: false` 真实外呼，于是 `public_base_url`、回调验签、★ 方法名核对全部变成上线阻塞项 |
| 多个店并发 | **一店一套实例 + 一店一个库 + 一店一套向量目录**；不是 `--scale`，也不是一套代码塞多店配置（理由见下） |
| 使用全新库 | 直接 `alembic upgrade head` 建表即可，**不需要** `stamp 0001`（stamp 只用于以前用 `migrate_mode: auto` 跑出来的旧库） |

## 文件清单

| 文件 | 是什么 | 装到服务器哪里 |
|---|---|---|
| `shop.example.yaml` | 一店一份的生产配置模板（`CS_CONFIG` 是**整文件替换**，所以每节都写全了） | `/etc/cs-agent/shop-a.yaml`，`chmod 600` |
| `env.shop.example` | 上面那些 `${VAR}` 的取值来源：密钥 + 每店不同的值 | `/etc/cs-agent/shop-a.env`，`chmod 600` |
| `docker-compose.shop.yml` | 多店 compose：共享 Postgres + 每店一段独立 service | 仓库里的 `deploy/`，就地 `docker compose -f` |
| `postgres.create-dbs.sql` | 首启时按店 `CREATE DATABASE` | compose 挂进 `/docker-entrypoint-initdb.d/`（只在建卷时执行） |
| `nginx.shop.conf` | 按域名分店的反代，含 SSE 必需的关缓冲配置 | `/etc/nginx/conf.d/cs-agent.conf` |
| `cs-agent@.service` | 不用 docker 时的 systemd 模板单元（`%i` = 店名） | `/etc/systemd/system/` |
| `cs-backup@.service` / `.timer` | 每店每日快照与定时器 | 同上 |

`deploy/env/`、`deploy/etc/`、`deploy/data/` 是真实密钥与数据的落点，已在根 `.gitignore` 里按目录挡住——只允许 `*.example` 入库。

## 一、先选形态

- **容器（推荐，两台以上店时最省事）**：用 `docker-compose.shop.yml`。镜像已经把前端 `dist/` 打进去，`CS_SERVE_FRONTEND=true` 同源挂出来，不需要额外的静态站点与 CORS 配置。
- **裸机 systemd**：用 `cs-agent@.service`。适合已经有 Python 运维习惯、要在机器上直接看日志与 venv 的场景。

两种形态的**配置文件是同一份**（`shop.example.yaml` + `env.shop.example`），切换形态不用重写配置，只换进程管理器。

> 别把仓库根的 `docker-compose.yml` 和这份混着用：前者是"单机单店演示形态"（SQLite + 7861），
> 两者同时 `up` 一定撞 7861 端口，而且一个用 SQLite、一个用 Postgres，你会对着两个库找数据到底在哪。
> 上线只留这一份 `deploy/docker-compose.shop.yml`。

## 二、上线顺序（容器形态，逐条执行）

```bash
# 0) 机器上要有 docker + compose、git，以及一个域名解析到本机
ssh your-server
git clone <repo> /srv/cs-agent && cd /srv/cs-agent

# 1) 建密钥与配置目录（这三个目录都不会进 git）
mkdir -p deploy/env deploy/etc deploy/data

# 2) Postgres 的超级用户口令（compose 里 ${POSTGRES_PASSWORD} 取它，不给就拒绝启动）
printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 24)" | tee deploy/env/postgres.env
chmod 600 deploy/env/postgres.env

# 3) 改建库清单：店名定下来再往下走，库名后面要写进每店 env
vi deploy/postgres.create-dbs.sql          # CREATE DATABASE cs_shop_a; ...

# 4) 店 A 的配置与密钥
cp deploy/shop.example.yaml deploy/etc/shop-a.yaml
cp deploy/env.shop.example  deploy/env/shop-a.env
chmod 600 deploy/env/shop-a.env
vi deploy/env/shop-a.env                   # 必填：CS_DB_NAME / CS_JWT_SECRET / CS_MODEL_API_KEY / CS_PUBLIC_BASE_URL / CS_DB_PASSWORD
echo "CS_JWT_SECRET=$(openssl rand -hex 32)"   # 每店独立生成，别复用

# 5) 起库和店 A（build 一次，所有店共用同一个镜像；只给 shop-a 建，别写全量 build）
docker compose --env-file deploy/env/postgres.env -f deploy/docker-compose.shop.yml build shop-a
docker compose --env-file deploy/env/postgres.env -f deploy/docker-compose.shop.yml up -d postgres shop-a

# 6) 起没起来只看 /ready（它不需要登录，会查库可查 + 迁移已跑 + 无致命安全项）
curl -fsS localhost:7861/ready
docker compose --env-file deploy/env/postgres.env -f deploy/docker-compose.shop.yml logs --tail=50 shop-a
```

`/ready` 非 200 就是启动自检里有 fatal（默认密钥、回调不验签、CORS 通配、真实外呼却没配 `public_base_url`），容器会 crash-loop——这是故意的：裸奔的代价是数据泄漏，crash 的代价是部署失败，后者可回滚。

```bash
# 7) 反代 + 证书
sudo cp deploy/nginx.shop.conf /etc/nginx/conf.d/cs-agent.conf
sudo vi /etc/nginx/conf.d/cs-agent.conf          # server_name / 证书路径 / upstream 端口
sudo nginx -t && sudo nginx -s reload
sudo certbot --nginx -d cs-a.example.com         # 或挂平台/自购证书

# 8) 第一个管理员账号
# production 下 allow_registration: false，但"库还空着"是唯一例外（自举用）。
# 打开客服台 https://cs-a.example.com/ 注册第一个 admin；库里一旦有账号，注册入口就自动关上，
# 之后开账号由 admin 在设置页做。所以这一步要在接通平台之前完成，别留着空库等人来注册。

# 9) 能力总览与平台就绪度：这两个接口都要登录（system/status 要 token，readiness 要 staff+），
#    所以必须排在第 8 步之后。它们是"还缺什么"的权威清单，不要靠猜配置。
TOKEN=$(curl -sS -X POST https://cs-a.example.com/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"username":"你的admin用户名","password":"你的口令"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')

curl -sS -H "Authorization: Bearer $TOKEN" https://cs-a.example.com/api/v1/system/status \
  | python3 -m json.tool | head -40          # model_ready / capabilities / 队列 / 外发 / checks
curl -sS -H "Authorization: Bearer $TOKEN" https://cs-a.example.com/api/v1/platforms/readiness \
  | python3 -m json.tool                     # 每个平台的 missing_keys 与 ★ needs_review
# capabilities 里 embedding 不 ready 时，检索会静默降级成只走 BM25：不报错，但召回质量掉了。
# 套餐类 key 常常只放行对话模型，这四类（chat/embedding/vision/rerank）要分别确认。

# 10) 平台接入：顺序不能反
#   ① 先只 enabled: false 起服务 → ② 按 readiness 的 needs_review 逐条核对带 ★ 的
#      reply_method / media_method / gateway / to_uid_field / content_field / ack 结构 /
#      media_url_field（这些从未与官方文档核对过，★ 就是留给人工的）
#   → ③ 平台后台配回调 URL https://<域名>/api/platform/<平台>/callback 并完成 URL 验证
#   → ④ callback_verify 必须是 sign（写 none 在生产直接拒绝启动）
#   → ⑤ 才改 mock: false，且一次只开一个平台灰度 24 小时
```

企微转人工不需要电商资质，建议**第一批就接通**（`wecom.mock` 必须 `false`，否则卡片只进外发箱，客服永远收不到）。

## 三、加一家店（最小动作清单）

一店一套是刻意的隔离，加店 = 复制四样东西，每样只改店名。下面以第三家店 `shop-c` 为例
（第二家店照 compose 里现成的 `shop-b` 段改）：

1. `deploy/postgres.create-dbs.sql` 加一行 `CREATE DATABASE cs_shop_c;`，然后手工建库（initdb 脚本只在空卷时执行一次）：
   `docker compose --env-file deploy/env/postgres.env -f deploy/docker-compose.shop.yml exec postgres psql -U cs_agent -c 'CREATE DATABASE cs_shop_c'`
2. `cp deploy/etc/shop-a.yaml deploy/etc/shop-c.yaml`，改 `agent.name`（店名/禁语/发货口径每店不同，这正是每店一份配置的顺带收益）。
3. `cp deploy/env/shop-a.env deploy/env/shop-c.env`，改 `CS_DB_NAME`、`CS_PUBLIC_BASE_URL`、`CS_JWT_SECRET`（重新生成）、`CS_BACKUP_OUT`、各平台 `app_key/app_secret/access_token`（**按店铺授权，绝不能跨店复用**）。
4. compose：把文件末尾注释着的 `shop-b` 段解开（或照它加一段），改 `etc/shop-*.yaml`、`env/shop-*.env`、`./data/shop-*`、宿主端口四处；**不要带 `build` 那节**（整份文件只由 shop-a 负责构建镜像，多段带 build 会把同一份源码构建多次）。nginx 加一个 `upstream` + `server` 块，备份加 `systemctl enable --now cs-backup@shop-c.timer`（并把 `OnCalendar` 与别的店错开）。

**为什么不能用 `docker compose up --scale cs-agent=N`**：scale 出来的副本共用同一份 `CS_DB_NAME` 与同一个 chroma 目录，那是"同一个店多进程"，不是多店。店与店之间要隔离的是**数据**，代码依据三条：

1. 平台凭证按 `platforms.<name>` 整节读取，一个平台只有一套 `app_key/access_token`（[app/services/platforms/base.py:72](file:///E:/WWW/custom-service-agent/src/backend/app/services/platforms/base.py#L72)），而 TOP session / 京东 access_token 是按店铺授权的——同平台两店共用一套 token，回复会发错店。
2. `KnowledgeFile` 与 `Product` 没有 `shop_id` 字段（[app/db/models.py:108、:137](file:///E:/WWW/custom-service-agent/src/backend/app/db/models.py#L108)），知识库与商品表全实例共享：A 店的退货政策会被召回给 B 店的买家。
3. Chroma 是本地目录 + 进程内单句柄（`lru_cache` 缓存按路径打开的 `PersistentClient`，[app/services/rag/vectorstore.py:62](file:///E:/WWW/custom-service-agent/src/backend/app/services/rag/vectorstore.py#L62)），跨进程/跨机共用同一目录属于未验证路径。

## 四、备份

容器形态没有 systemd timer，用 `compose run` 挂 cron（`-T` 关伪终端，否则 cron 里会报 "the input device is not a TTY"）：

```bash
# crontab -e（每天 03:20；各店时间错开，别在同一分钟一起拉全量 dump，
# 磁盘 IO 尖峰会把当时的对话请求一起拖慢）
20 3 * * * cd /srv/cs-agent && docker compose --env-file deploy/env/postgres.env \
  -f deploy/docker-compose.shop.yml run --rm -T shop-a \
  python scripts/backup_data.py --out /app/data/backups --keep 14 \
  >> /var/log/cs-backup-shop-a.log 2>&1
```

`--out /app/data/backups` 落在该店自己的挂载卷里（`deploy/data/shop-a/backups`），快照天然按店分开——
轮转是"按目录算份数"的，两店共用一个目录会互相删对方的快照。

裸机形态直接 `systemctl enable --now cs-backup@shop-a.timer`（见 `cs-backup@.timer` 的注释，含错开时间的 drop-in 写法）。

为什么用 `scripts/backup_data.py` 而不是自己拼 `pg_dump + tar`：它已经处理了 SQLite 的 WAL 一致性、MySQL/PG 口令不上命令行（`ps` 能看见）、以及每份快照配 `manifest.json`。恢复走 `scripts/restore_data.py`（默认只校验，动数据要 `--yes`），**备份没被恢复演练过就等于没有备份**——第一次上线请在本机跑一次 `--verify-only`，口径见 [docs/运维手册.md](../docs/运维手册.md) §6/§9。

## 五、这几个坑我已经替你写在文件里了，别改回去

| 症状 | 原因 | 在哪个文件里已经写了 |
|---|---|---|
| 回答整段憋到最后一次性出来，甚至 504 | 反代默认缓冲上游响应，SSE 被攒住 | `nginx.shop.conf` 的 `proxy_buffering off` 段 |
| 附件链接买家点不开，日志没报错、状态不是 failed | `public_base_url` 没配 → 给的是相对路径 | `shop.example.yaml` 第 ③ 项；有真实外呼时生产直接拒绝启动 |
| 限流形同不存在 | 阈值是**每进程每键**，`workers>1` 或每店多副本时整体放行量 ×N | `shop.example.yaml` 的 `rate_limit` 注释、compose 的 `CS_SERVER_WORKERS=1` |
| 启动报 `No module named 'psycopg'` | 驱动在 `requirements.txt` 里（已补），用旧镜像或自己精简了依赖就会缺 | `src/backend/requirements.txt` |
| 配置里写了 `${VAR:?说明}`，DSN 原样进程序、连不上库还看不出原因 | 本项目插值只认 `${VAR}` 和 `${VAR:-默认}`，**不认** shell 的 `:?` 语法 | `shop.example.yaml` 的 `database.url` 注释（库名用哨兵默认值兜底，漏配就连一个不存在的库， loud fail） |
| 旧库切 `migrate_mode: alembic` 报 `table xxx already exists` | 旧库要先 `stamp 0001`；**全新库不要 stamp** | `运维手册.md` §7、`postgres.create-dbs.sql` 抬头 |

## 六、回滚

一次只改一个变量：镜像 tag、配置、数据库迁移三者分开回滚。步骤与"什么情况下回滚数据库"的判断在 [docs/运维手册.md](../docs/运维手册.md) §9。
