"""数据库表定义（SQLModel）。

P0: User / Conversation / Message
P1: KnowledgeFile / Chunk / Product（商品知识库双通道：向量文档 + 结构化商品表）
P2: ConversationSummary（中期摘要）/ MemoryFact（长期画像，向量存 Chroma，正文存此表）
P3: HandoffTicket（转人工工单）+ Conversation.handoff_status 状态机字段
P4: McpServer（MCP 服务配置）
P7: ExternalConversation（平台买家会话映射）/ InboundDedupe（入站消息幂等）
P8: Conversation 生命周期字段（status/last_customer_msg_at/end_prompted_at）
B1: User.role（admin/user 门禁）/ IngestJob（重建任务）/ OutboxRecord（出站落库）

注意：本文件禁止使用 `from __future__ import annotations`——SQLAlchemy/SQLModel
需要在运行时解析 `list["Conversation"]` 等 Relationship 注解，PEP 56 惰性注解会破坏 mapper 初始化。
表结构由启动时 create_all 生成：新增表/索引会自动建，但**不会 ALTER 已有列**——
新增列在 `db/migrate.py` 的 ADDITIVE_COLUMNS 里登记一行即可（幂等，本地与生产都不用删库）；
只有改既有列的类型/约束才需要重建库。
"""
import datetime
from typing import Protocol

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel


class _HasId(Protocol):
    """"有自增主键的行"——SQLModel 基类本身不带 id 字段，用它描述 pk() 真正需要的东西。"""

    id: int | None


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def pk(row: _HasId) -> int:
    """取"已经落库"的行的主键。

    自增主键只能声明成 `id: int | None`（新建未 flush 时确实是 None），但 add + commit
    之后必然是 int——类型检查器看不出这条时间线，于是每个用 `conv.id` 当外键的地方都得
    挂一个 `# type: ignore[arg-type]`。集中成断言有两个好处：调用点自解释，而且真忘了
    commit/refresh 时立刻在这里炸出栈，而不是静默写进一条 conversation_id 为空的脏数据。
    """
    identity = row.id
    assert identity is not None, f"{type(row).__name__} 尚未落库，没有可用主键"
    return identity


class User(SQLModel, table=True):
    """系统用户。客服 Agent 的登录主体；长期记忆（P2）按 user_id 归属。

    role（B1-7）：admin / user。运营台刻意允许跨用户读与代答（一个客服团队共用工作台），
    但"能在本机拉起子进程"的 MCP 服务器配置、知识库写操作与角色变更只给 admin。
    """

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    email: str | None = Field(default=None, unique=True)
    hashed_password: str
    role: str = Field(default="user", index=True)
    created_at: datetime.datetime = Field(default_factory=utcnow)

    conversations: list["Conversation"] = Relationship(back_populates="user")


class Conversation(SQLModel, table=True):
    """会话。短期记忆以会话为单位取历史消息。

    handoff_status 转人工状态机（P3，P8 改为确认式）：
    bot →（Agent 询问/关键词触发）awaiting_confirm →（买家确认）pending_human
    →（客服接单/回复）human →（结单）bot；否定回复则回 bot 继续智能接待。
    status 会话生命周期（P8）：active → 空闲超时提示 → 无回复终结 closed。
    """

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    title: str = Field(default="新会话")
    handoff_status: str = Field(default="bot", index=True)
    status: str = Field(default="active", index=True)   # active / closed（P8 终结，入站拒收）
    created_at: datetime.datetime = Field(default_factory=utcnow)
    updated_at: datetime.datetime = Field(default_factory=utcnow)
    last_customer_msg_at: datetime.datetime | None = Field(default=None)  # P8 空闲计时锚点
    end_prompted_at: datetime.datetime | None = Field(default=None)       # P8 结束提示已发出时间

    user: User | None = Relationship(back_populates="conversations")
    messages: list["Message"] = Relationship(back_populates="conversation")


class Message(SQLModel, table=True):
    """会话消息。role: user / assistant / system。

    meta_json 预留给后续阶段：RAG 引用来源、工具调用记录、转人工标记等。
    """

    id: int | None = Field(default=None, primary_key=True)
    conversation_id: int = Field(foreign_key="conversation.id", index=True)
    role: str = Field(index=True)
    content: str
    meta_json: str | None = Field(default=None)
    created_at: datetime.datetime = Field(default_factory=utcnow)

    conversation: Conversation | None = Relationship(back_populates="messages")


# =============================================================================
# P1 知识库
# =============================================================================

class KnowledgeFile(SQLModel, table=True):
    """知识库文件。status: pending / parsing / ready / failed。

    图片文件解析时调视觉模型生成 image2text 描述（需配置 vl_model 与有效 key）。
    """

    id: int | None = Field(default=None, primary_key=True)
    name: str                       # 原始文件名
    path: str                       # data/uploads/ 下存储路径
    file_type: str                  # excel / pdf / image / text / docx
    size: int = 0
    status: str = Field(default="pending", index=True)
    error: str | None = None
    enabled: bool = True            # 禁用后检索不召回（向量仍在，可逆）
    chunk_count: int = 0
    created_at: datetime.datetime = Field(default_factory=utcnow)


class Chunk(SQLModel, table=True):
    """文本分块。正文与元数据存此表（BM25/展示用），向量存 Chroma（id 一致）。"""

    id: int | None = Field(default=None, primary_key=True)
    file_id: int = Field(foreign_key="knowledgefile.id", index=True)
    seq: int = 0                    # 文件内序号
    text: str
    tokens: int = 0
    created_at: datetime.datetime = Field(default_factory=utcnow)


class Product(SQLModel, table=True):
    """结构化商品表（Excel 导入）。价格/库存类问题走 SQL 精确查询，避免向量数字幻觉。"""

    id: int | None = Field(default=None, primary_key=True)
    sku: str = Field(default="", index=True)
    name: str = Field(index=True)
    price: float | None = None
    stock: int | None = None
    category: str = ""
    specs: str = ""                 # 规格/参数拼接文本
    tags: str = ""
    file_id: int | None = Field(default=None, foreign_key="knowledgefile.id", index=True)
    created_at: datetime.datetime = Field(default_factory=utcnow)


# =============================================================================
# P2 记忆
# =============================================================================

class ConversationSummary(SQLModel, table=True):
    """中期记忆：会话滚动摘要。覆盖 until_message_id 之前的历史。"""

    id: int | None = Field(default=None, primary_key=True)
    conversation_id: int = Field(foreign_key="conversation.id", unique=True, index=True)
    summary: str
    until_message_id: int = 0       # 已摘要到的消息 id（增量更新游标）
    updated_at: datetime.datetime = Field(default_factory=utcnow)


class MemoryFact(SQLModel, table=True):
    """长期记忆：用户画像事实（向量存 Chroma collection user_memory，id 一致）。"""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    fact: str                       # 一句话事实，如「用户偏好性价比高的国产品牌」
    category: str = "preference"    # preference / profile / case
    source_conversation_id: int | None = None
    created_at: datetime.datetime = Field(default_factory=utcnow)


# =============================================================================
# P3 转人工
# =============================================================================

class HandoffTicket(SQLModel, table=True):
    """转人工工单。status: open（待接）/ handling（处理中）/ closed（已结）。"""

    id: int | None = Field(default=None, primary_key=True)
    conversation_id: int = Field(foreign_key="conversation.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    reason: str                     # 触发原因（用户请求/连续未解决/敏感操作）
    summary: str = ""               # 会话摘要，供客服快速接手
    status: str = Field(default="open", index=True)
    staff: str = ""                 # 接手的客服（企微 userid 或工作台用户名）
    created_at: datetime.datetime = Field(default_factory=utcnow)
    closed_at: datetime.datetime | None = None


# =============================================================================
# P4 MCP
# =============================================================================

class McpServer(SQLModel, table=True):
    """MCP 服务配置。transport: stdio / sse / streamable_http。

    tools_json 缓存最近一次连接发现的工具清单（含描述），供管理页展示。
    confirm_tools 逗号分隔需人工确认的工具名（默认全部写操作保守处理在 manager 内判定）。
    """

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    transport: str = "stdio"
    command: str = ""               # stdio: 启动命令
    args: str = ""                  # stdio: 参数（空格分隔）
    url: str = ""                   # sse / streamable_http 端点
    enabled: bool = True
    require_confirm: bool = True    # 该服务器所有工具调用前均需前端确认卡片
    status: str = "disconnected"    # connected / disconnected / error
    tools_json: str | None = None
    created_at: datetime.datetime = Field(default_factory=utcnow)


# =============================================================================
# P7 外部电商平台
# =============================================================================

class ExternalConversation(SQLModel, table=True):
    """平台买家会话 → 内部 Conversation 的映射。

    平台买家无 JWT 登录：入站时按 (platform, shop_id, buyer_id) 找/建影子 User 与
    Conversation，复用全部既有链路（RAG/记忆/转人工状态机）。
    同一买家默认落同一会话（单店铺单活跃线程的简化假设；生产可按平台会话 id 分流）。
    """

    id: int | None = Field(default=None, primary_key=True)
    platform: str = Field(index=True)      # taobao / jd / douyin / pdd
    shop_id: str = Field(default="", index=True)
    buyer_id: str = Field(index=True)      # 平台侧买家标识（openid / 加密 uid）
    conversation_id: int = Field(foreign_key="conversation.id", unique=True, index=True)
    created_at: datetime.datetime = Field(default_factory=utcnow)


class InboundDedupe(SQLModel, table=True):
    """平台入站消息去重（P7）：平台侧推送普遍超时重发，同一条消息只处理一次。

    幂等键取 (platform, shop_id, buyer_id, external_msg_id)——多数平台的 msg_id
    只在会话范围内唯一，跨买家复用同一 id 是合法的，不能全局去重。
    平台没给 msg_id 时不写本表（见 platforms/inbound.py），不做无依据的猜测去重。
    """

    __table_args__ = (UniqueConstraint("platform", "shop_id", "buyer_id", "external_msg_id"),)

    id: int | None = Field(default=None, primary_key=True)
    platform: str = Field(index=True)
    shop_id: str = Field(default="")
    buyer_id: str = Field(default="")
    external_msg_id: str = Field(index=True)
    created_at: datetime.datetime = Field(default_factory=utcnow)


# =============================================================================
# B1 进程内状态落库
# =============================================================================

class IngestJob(SQLModel, table=True):
    """知识库重建任务（B1-2/B1-4）。

    旧实现只把任务交给 BackgroundTasks 且用 KnowledgeFile.status=parsing 当"在跑"标记：
    进程重启后线程消失，文件永远卡 in parsing，页面既看不到进度也重新点不动。
    现在任务本身有行，queued/running 才代表真在跑，重启时统一转 interrupted 可重试。
    """

    id: int | None = Field(default=None, primary_key=True)
    file_id: int = Field(foreign_key="knowledgefile.id", index=True)
    file_name: str = ""
    kind: str = Field(default="reingest", index=True)   # upload / reingest / reindex
    status: str = Field(default="queued", index=True)   # queued / running / done / failed / interrupted
    processed: int = 0                                  # 已完成的分块数
    total: int = 0                                      # 计划处理的分块数
    error: str | None = None
    created_at: datetime.datetime = Field(default_factory=utcnow)
    updated_at: datetime.datetime = Field(default_factory=utcnow)


class OutboxRecord(SQLModel, table=True):
    """出站消息落库（B1-2）：企微卡片与各平台 mock 外发原来只活在进程内存里，重启即丢。

    运营台/设置页的外发箱读这张表；channel=wecom 归转人工卡片，其余为平台名。

    重试三列（出站失败退避重试）：失败不再"写一次就完"。
    - attempts：已尝试外呼的次数（首次发送算第 1 次；0 = 本行从未参与自动重试，
      例如企微卡片与 mock/skipped 记录）；
    - next_retry_at：到点才重试；**失败行且 attempts>0 而 next_retry_at 为空就是死信**
      （重试次数用尽，等人工处理）；
    - last_error：最近一次失败的报文，排查时不必再去翻日志。
    时间一律带时区（SQLModel 的 UTCDateTime 拒绝 naive datetime）。
    """

    id: int | None = Field(default=None, primary_key=True)
    channel: str = Field(index=True)        # wecom / taobao / jd / pdd / douyin
    kind: str = Field(default="text")       # text / textcard / media
    to: str = ""                            # 接收方标识（企微 userid / 买家 id）
    title: str = ""
    body: str = ""                          # 正文或描述
    payload_json: str = "{}"                # 完整发送载荷（mock 时也记，便于对照真实报文）
    status: str = Field(default="mocked", index=True)  # mocked / sent / failed / skipped
    error: str | None = None
    attempts: int = 0                       # 已尝试外呼次数（退避重试用）
    # 不加索引：取到期行本来就按 status（已建索引）过滤，小表扫不到哪儿去，
    # 而 create_all 只会给**新库**建索引，老库补出来的列带不带索引口径不一致更容易误导。
    next_retry_at: datetime.datetime | None = None
    last_error: str | None = None           # 最近一次重试失败的报文
    created_at: datetime.datetime = Field(default_factory=utcnow, index=True)


# =============================================================================
# B3-7 计量与可观测
# =============================================================================

class LlmCall(SQLModel, table=True):
    """每次模型外呼的用量流水（B3-7）。

    没有这张表就没有成本、没有错误率、也没有"这个月哪个模型烧了多少钱"的答案——
    用量只存在于调用的一瞬间，事后补不回来。
    embedding/rerank 两类接口不返回 usage_metadata，按本地估算并标 estimated=True，
    读的人必须能分辨"模型上报值"与"本地估算值"。
    """

    id: int | None = Field(default=None, primary_key=True)
    kind: str = Field(index=True)                  # chat / embedding / rerank / vision
    provider: str = ""                             # multi_models 下的配置名
    model: str = ""
    conversation_id: int | None = Field(default=None, index=True)
    user_id: int | None = Field(default=None, index=True)
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated: bool = False                        # True = 本地估算，不是模型上报
    cost_est: float | None = None                  # 按 llm.pricing 估算的金额（未配价为 None）
    latency_ms: int = 0
    attempts: int = 1                              # >1 说明走过备用模型链
    status: str = Field(default="ok", index=True)  # ok / error
    error: str | None = None
    created_at: datetime.datetime = Field(default_factory=utcnow, index=True)


class ToolConfirmRequest(SQLModel, table=True):
    """MCP 写操作确认请求（B3-8 落库）。

    旧实现是进程内的 asyncio.Future 表：确认请求只能由同一个进程裁决，多副本下
    点卡片的请求很可能落到"没有这个 future"的进程上，必然超时拒绝；进程重启更会把
    正在等待的对话直接挂死。改成状态机后，等待方按轮询读库、任何进程都能裁决，
    重启也不丢待办。

    status: pending → approved / rejected / expired（无人应答超时）
    """

    id: int | None = Field(default=None, primary_key=True)
    request_id: str = Field(index=True, unique=True)   # 下发给前端的短 id（12 位 hex）
    conversation_id: int = Field(foreign_key="conversation.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    tool_name: str = ""
    args_json: str = "{}"
    status: str = Field(default="pending", index=True)
    decided_by: int | None = Field(default=None, foreign_key="user.id")
    created_at: datetime.datetime = Field(default_factory=utcnow, index=True)
    decided_at: datetime.datetime | None = None


# =============================================================================
# B3-8 多进程协调
# =============================================================================

class WorkerLease(SQLModel, table=True):
    """后台任务的租约选主（B3-8）：谁持约谁干活，续约靠时间戳自然失效。

    只有一个主键列 name，所以"抢约"是一条带条件的 UPDATE（行已存在）或一次 INSERT
    （首次），两条都是原子的。持约进程被杀掉不再续约时，下一个扫描周期就有别的进程
    接管——不需要外部协调服务（ZooKeeper/Redis），代价是切换要等一个 TTL。
    """

    name: str = Field(primary_key=True)               # session_sweeper / memory_pipeline / ...
    holder: str = Field(default="", index=True)       # 进程标识：hostname:pid:uuid4 前 8 位
    epoch: int = 0                                    # 每次成功持约 +1，便于排查"抖动/抢占"
    lease_expires_at: datetime.datetime | None = None
    updated_at: datetime.datetime = Field(default_factory=utcnow)


class MemoryJob(SQLModel, table=True):
    """记忆管道任务（B3-8 落库）。

    旧实现是 `asyncio.create_task` + 进程内 `_running` 去重集合：重启时正在抽取的
    摘要/画像直接蒸发（当轮对话不受影响，但那几轮的长期记忆就永久缺了一段），
    多进程部署还会对同一会话并发抽取、写出重复画像。
    现在任务有行、抢占用带条件的 UPDATE，启动时把中断的收回重跑。

    status: queued → running → done / failed
    stage:  both / summary / facts（哪一段没跑成，重跑时只补那一段）
    """

    id: int | None = Field(default=None, primary_key=True)
    conversation_id: int = Field(foreign_key="conversation.id", index=True)
    user_id: int = Field(default=0, index=True)
    stage: str = Field(default="both", index=True)
    status: str = Field(default="queued", index=True)
    attempts: int = 0
    error: str | None = None
    created_at: datetime.datetime = Field(default_factory=utcnow, index=True)
    updated_at: datetime.datetime = Field(default_factory=utcnow)


class SessionRating(SQLModel, table=True):
    """会话满意度评分（B3-13）。

    一行一会话（unique）：买家重复点回访链接只算第一票，后面按"已评价"拒绝，
    否则最后一个分数会覆盖前面的，均分就变成了"最后一次心情"。

    回访链接带 HMAC 签名且**只指向这一个会话**（免 JWT，因为买家不在我们站内登录），
    所以拿到链接的人只能给这一会话投一次票。满意度是趋势指标，不是审计级数据：
    同一个人换会话链接、或把链接转给同事代填，都挡不住，别拿它做绩效唯一依据。
    """

    id: int | None = Field(default=None, primary_key=True)
    conversation_id: int = Field(foreign_key="conversation.id", unique=True, index=True)
    rating: int                                  # 1..5
    comment: str = ""
    source: str = Field(default="survey_link", index=True)   # survey_link / operator
    created_at: datetime.datetime = Field(default_factory=utcnow, index=True)
