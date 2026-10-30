"""演示数据种子：一条命令把每个界面都喂满，用于界面截图、交付演示与验收走查。

为什么需要它：六个界面读的全是数据库，空库启动时满屏 `el-empty`，既没法演示也没法做
界面走查；而靠 smoke_test.py 那种脚本一条条发消息灌数据，灌出来的永远是"SKU001 多少钱"
这种测试话术，看着就不像真实客流。本脚本直接写库，产出的是一份**像样的一线运营数据**：
多渠道会话、带引用与工具痕迹的对话、转人工工单与外发箱四种状态、知识库切片与商品、
买家画像与摘要、满意度评分（含差评原文）、模型用量流水、MCP 确认卡片。

安全边界（都是不可逆操作，所以宁可写得啰嗦）：
- `server.env=production` 直接拒绝执行；
- 默认只往"空库或纯演示库"里写（判定见 `_looks_like_demo_db`），命中真实数据就要求显式
  `--force-any-db`；
- `--reset` 是清全表，同样受这条判定保护；
- `--serve` 用独立的 `data/demo/` 目录（已 gitignore），不碰开发者本机的 `data/cs_agent.db`。

用法（仓库根或 src/backend 下）：
    py scripts/seed_demo_data.py --serve                 # 隔离演示库 + 灌数 + 同源托管前端 dist
    py scripts/seed_demo_data.py --serve --port 7870      # 换端口
    py scripts/seed_demo_data.py                          # 只灌当前 CS_CONFIG 指向的库
    py scripts/seed_demo_data.py --serve --reset          # 清空演示库重来
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import secrets
import struct
import sys
import uuid
import zlib
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")
BACKEND_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIST = BACKEND_ROOT.parent / "frontend" / "dist"
sys.path.insert(0, str(BACKEND_ROOT))

# 注意：`app.*` 一律在函数里延迟导入——`app.db.session` 在**导入时**就按 CS_CONFIG 建 engine，
# 而 --serve 要先写出隔离配置再让 app 读它，模块顶层 import 会把 engine 钉死在默认库上。

# ================================ 账号与命名约定 ================================
# "这个库是不是演示库"就靠这两个前缀判定，所以别改命名。
DEMO_USER_PREFIX = "demo_"          # 站内可见账号：demo_admin / demo_staff_wang / demo_buyer_chen
DEMO_SHADOW_PREFIX = "pl_demo_"     # 平台影子账号（真实入站是 pl_<摘要>，这里换个前缀好识别）
DEMO_PASSWORD = "Demo@2026"

ADMIN = {"username": "demo_admin", "email": "admin@example.com", "nick": "运营负责人"}
STAFF = [
    {"username": "demo_staff_wang", "email": "wang@example.com", "nick": "王晓琳"},
    {"username": "demo_staff_li", "email": "li@example.com", "nick": "李伟"},
]
BUYERS = {
    "chen": {"username": "demo_buyer_chen", "email": "chen@example.com", "nick": "陈默"},
    "zhou": {"username": "demo_buyer_zhou", "email": "zhou@example.com", "nick": "周雅"},
    "lin": {"username": "demo_buyer_lin", "email": "lin@example.com", "nick": "林小雨"},
}
SHOP_BY_PLATFORM = {"taobao": "美妆旗舰店", "jd": "自营美妆店",
                    "pdd": "官方折扣店", "douyin": "抖店直播间"}

NOW = datetime.datetime.now(datetime.UTC)


def _ago(days: float = 0, minutes: int = 0, seconds: int = 0) -> datetime.datetime:
    """SQLModel 的 UTCDateTime 拒绝 naive datetime，这里统一产出带时区的时间。"""
    return NOW - datetime.timedelta(days=days, minutes=minutes, seconds=seconds)


def _shadow_username(platform: str, buyer: str) -> str:
    return f"{DEMO_SHADOW_PREFIX}{platform}_{buyer}"


# 相邻两条消息的默认间隔（秒）。看板的「平均会话时长」「首响时长」都是从这些时间差算出来的，
# 全用 20/35 秒会把会话压成半分钟，数字假到一眼能看穿；剧本里可按会话覆盖（慢响应的样本
# 就是要拉长）。间隔再大也不能把消息排到"未来"，见 seed_conversations 里的夹紧。
CADENCE_SECONDS = 260


def _say(msg: str) -> None:
    """--serve 会一直不退出，stdout 被重定向到文件时是块缓冲：不 flush 就永远看不到灌数摘要。"""
    print(msg, flush=True)


# ================================ 商品与语料 ================================
# 口径与 scripts/demo_corpus.py 对齐（SKU001=129 元、烟酰胺精华 199 元、7 天无理由 /
# 满 199 包邮 / 会员 9 折），否则演示对话里的报价会和知识库互相打脸。
PRODUCT_HEADER = ("商品名称", "SKU", "价格", "库存", "分类", "标签", "规格")
PRODUCTS: list[tuple[str, str, float, int, str, str, str]] = [
    ("清爽保湿面霜50g", "SKU001", 129, 35, "护肤", "保湿;敏感肌", "50g/瓶装"),
    ("氨基酸洁面乳", "SKU002", 59.9, 120, "清洁", "温和;敏感肌", "100ml"),
    ("烟酰胺精华原液", "SKU003", 199, 8, "精华", "提亮", "30ml"),
    ("胶原蛋白饮", "SKU004", 358, 0, "口服美容", "礼盒", "10瓶/箱"),
    ("舒缓保湿喷雾", "SKU005", 89, 64, "护肤", "补水;便携", "150ml"),
    ("视黄醇夜间精华", "SKU006", 268, 21, "精华", "抗老;夜间", "30ml"),
    ("防晒霜SPF50+", "SKU007", 119, 9, "防晒", "清爽;日常", "50ml"),
    ("卸妆膏经典版", "SKU008", 79, 210, "清洁", "乳化快", "100g"),
    ("会员礼盒三件套", "SKU009", 399, 12, "礼盒", "会员价;限量", "面霜+精华+洁面"),
    ("胶原蛋白饮补充装", "SKU010", 168, 47, "口服美容", "实惠装", "5瓶/袋"),
]

POLICY_TEXT = """售后政策
1. 7天无理由退换（未拆封）。
2. 满199元包邮，会员9折。
3. 客服工作时间 9:00-21:00。
4. 质量问题 15 天内包退包换，运费由店铺承担；非质量问题退换货运费由买家承担。
5. 礼盒类商品拆封后不支持无理由退货。
6. 优惠券订单退款按实付金额退回，优惠券不补发。

退货与退款细则
7. 退货受理范围：未拆封的 7 天无理由退换；拆封后仅限质量问题、发错货、物流破损三类受理。
8. 退款时限：仓库签收验货 1-2 个工作日，通过后 3-7 个工作日原路退回；银行卡退款以发卡行为准。
9. 换货时限：验货通过后 24 小时内重新寄出，物流单号同步到原会话；缺货SKU 需等补货，最长 15 天。
10. 退款金额含商品实付与原始运费；加急费、礼品包装费一经产生不退。

物流与签收
11. 物流异常（显示签收但未收到、长期无轨迹）需人工向快递发起查件，48 小时内给结论；确认丢件按补发或全额退款二选一。
12. 签收时请当面验货，外箱破损可拒收并拍照留证；已签收的破损需在 48 小时内提供开箱视频。
13. 偏远地区（新疆/西藏/内蒙/海南）默认加 8 元运费，满 199 元仍包邮。

发票与凭证
14. 电子发票下单备注抬头与税号，3 个工作日内开出；跨月订单需人工补开，需提供订单号。
15. 拆封后过敏需提供医院或皮肤科诊断凭证，客服按质量问题通道协商退换与补偿。

争议处理
16. 协商不一致时升级至店铺售后主管，2 个工作日内答复；平台介入订单以平台判定为准。
17. 客服不得承诺政策外的补偿；超授权额度（单笔 200 元以上）需主管审批后执行。
"""

FAQ_TEXT = """常见问题
Q1 发货时效：工作日 16:00 前付款当日发出，周末顺延；大促期间 48 小时内发出。
Q2 快递与运费：默认中通，满199元包邮；偏远地区（新疆/西藏/内蒙）加收 8 元运费。
Q3 修改地址：未发货前可在订单页自助改地址；已发货需联系快递拦截，可能产生退回运费。
Q4 开发票：下单备注抬头与税号，电子发票 3 个工作日内开出；跨月订单需联系客服补开。
Q5 退货流程：申请退货 → 寄回商品 → 仓库签收验货（1-2 个工作日）→ 退款原路返回（3-7 个工作日）。
Q6 会员权益：会员享 9 折、生日月双倍积分、会员日（每月 18 日）专享价。
Q7 过敏处理：护肤品个体差异较大，建议耳后试用；拆封后过敏可凭医院证明协商处理。
Q8 保质期与批次：面霜/精华保质期 3 年，详情页批次为最近一次入库批次，可联系客服提供具体生产日期。
Q9 孕妇可用吗：常规护肤品按成分表判断，不含视黄醇的产品孕期可用；口服美容类（胶原蛋白饮）建议咨询医生后再饮用。
Q10 用法与顺序：洁面 → 精华 → 面霜 → 防晒（白天）；视黄醇夜间精华只在晚间使用，初次使用隔晚一次建立耐受。
Q11 油皮与干皮选哪款：油皮优先氨基酸洁面 + 清爽保湿面霜；干皮可叠加舒缓保湿喷雾。
Q11.1 精华按诉求选：提亮选烟酰胺精华，抗老选视黄醇夜间精华。
Q12 是否支持货到付款：不支持；平台下单在线支付，仓库按支付时间排单。
Q13 能否指定快递：默认中通，可备注顺丰需补差价，偏远地区不承接顺丰到付。
Q14 小样与赠品：赠品随单发出，缺少赠品请在签收 48 小时内联系客服补寄；活动赠品不支持单独退换。
Q15 订单合并与拆单：同店同地址可备注合并发货，超重或含预售商品会自动拆单，不重复收运费。
Q16 售后要提供什么：订单号、问题描述、照片或开箱视频；过敏类另需医院凭证，物流异常类需说明快递单号与签收时间。
"""

LEGACY_TEXT = """2025 年活动规则（已停用）
双十一全场 8 折，满 299 送洁面小样。
会员日 5 折封顶 50 元。
预售定金 50 抵 100，尾款需在 2025-11-11 02:00 前付清。
满 499 加赠胶原蛋白饮 1 瓶，限量 3000 份。
老客回馈券 30 元无门槛，仅限 2025 年 12 月使用。
本规则自 2026-01-01 起失效，保留仅作归档，不得作为对外答复口径。
"""

# ================================ 对话剧本 ================================
# turns = (role, content, meta_kind)。meta_kind 决定 meta_json 里写什么（引用来源 /
# 工具调用痕迹 / 人工代答徽标 / 附件），见 _meta()。这样页面上每条助手回复都能回答
# "它凭什么这么说"，截图与走查才经得起追问。
CONVERSATIONS: list[dict[str, Any]] = [
    {
        "key": "web_chen", "channel": "web", "buyer": "chen",
        "handoff": "bot", "status": "active", "days_ago": 0.02, "cadence": 90,
        "summary": "买家咨询面霜（SKU001）是否适合敏感肌与到手价，机器人给出成分、价格与券口径；"
                   "买家表示先拍一瓶。无转人工。",
        "turns": [
            ("user", "你们那个清爽保湿面霜适合敏感肌吗？", ""),
            ("assistant", "**适合**。SKU001 清爽保湿面霜 50g 主打保湿，标签里没有酒精香精，"
                          "同类敏感肌可用的洁面也在售。\n\n不过护肤品个体差异较大，"
                          "建议先在耳后试用一天再全脸用。", "rag"),
            ("user", "多少钱？用优惠券会不会还要加钱", ""),
            ("assistant", "面霜 **129 元**（SKU001，当前库存 35 件）。会员 9 折、满 199 元包邮；"
                          "用券下单后如果退款，按实付退回、券不补发。", "product_policy"),
            ("user", "好，那我先拍一瓶试试", ""),
            ("assistant", "好的～工作日 16:00 前付款当日发出，周末顺延。"
                          "收到后有任何使用问题随时找我。", "faq"),
        ],
    },
    {
        "key": "web_zhou", "channel": "web", "buyer": "zhou",
        "handoff": "human", "status": "active", "days_ago": 0.6,
        "summary": "买家反馈精华（SKU003）使用后脸颊刺痛并要求退货，已拆封；"
                   "机器人按政策转人工，人工已给出退货退款 + 补寄舒缓喷雾方案。",
        "turns": [
            ("user", "精华用了刺痛，我要退货", ""),
            ("assistant", "抱歉给你添麻烦了。先确认两点：是否已拆封、有没有红肿或皮疹？"
                          "拆封后过敏需要凭证才能协商处理，我先帮你记录情况。", "rag"),
            ("user", "已经拆封了，起了点红疹，我拍照了", "user_attach_img"),
            ("assistant", "照片看到了，情况我记下了。这单需要人工客服核实售后与补偿口径，"
                          "我现在帮你转人工，稍等。", "system_handoff"),
            ("assistant", "您好，我是人工客服王晓琳。照片已归档，按质量问题处理："
                          "这单退货退款、运费店铺承担，另外补寄一份舒缓喷雾先缓解。"
                          "快递单号稍后发您。", "wecom"),
            ("user", "好的，谢谢", ""),
        ],
        "ticket": {"reason": "买家皮肤过敏要求退货并补偿（机器人判定需人工核实凭证）",
                   "status": "handling", "staff": "demo_staff_wang"},
    },
    {
        "key": "tb_8823", "channel": "taobao", "buyer": "tb_8823",
        "handoff": "awaiting_confirm", "status": "active", "days_ago": 0.05,
        "summary": "淘宝买家催发货（三天未出），情绪激动要求转人工；机器人已按两步制发出确认询问，"
                   "等待买家回复「是」。",
        "turns": [
            ("user", "我三天前拍的面霜怎么还没发货？", ""),
            ("assistant", "抱歉让你久等了。口径是工作日 16:00 前当日发出、大促 48 小时内；"
                          "你订单的真实状态需要人工在后台核实，我不能凭猜测答复。", "faq"),
            ("user", "别查了，给我转人工！", ""),
            ("assistant", "好的，我马上帮你转人工客服处理发货问题——现在转吗？回复「是」即可。",
             "system_handoff_ask"),
        ],
    },
    {
        "key": "tb_5521", "channel": "taobao", "buyer": "tb_5521",
        "handoff": "pending_human", "status": "active", "days_ago": 0.3,
        "summary": "淘宝买家地址写错但订单已发货，需人工联系快递拦截；工单已建、尚无人接单。",
        "turns": [
            ("user", "订单地址写错了，能改吗", ""),
            ("assistant", "未发货可以在订单页自助改；已发货需要联系快递拦截，"
                          "可能产生退回运费。你要我现在转人工帮你处理吗？", "faq"),
            ("user", "转吧，已经发货了", ""),
            ("assistant", "已转人工。客服会在工作时间 9:00-21:00 内跟进拦截结果，"
                          "拦截结果出来后我同步到这里。", "system_handoff"),
        ],
        "ticket": {"reason": "已发货订单改地址（需快递拦截，可能产生退回运费）",
                   "status": "open", "staff": ""},
    },
    {
        "key": "jd_liu", "channel": "jd", "buyer": "jd_liu",
        "handoff": "bot", "status": "closed", "days_ago": 1.4,
        "rating": 5, "rating_comment": "回答很快，一次就解决了",
        "summary": "京东买家咨询胶原蛋白饮是否缺货，机器人给出缺货事实与补充装 SKU010 替代；"
                   "买家致谢后会话超时结束，评分 5 分。",
        "turns": [
            ("user", "胶原蛋白饮还有货吗", ""),
            ("assistant", "SKU004 胶原蛋白饮目前**缺货**（库存 0），礼盒装 10 瓶/箱 358 元。"
                          "如果想先喝，同系列补充装 SKU010（168 元 / 5 瓶）还有 47 件。", "product"),
            ("user", "补充装和礼盒成分一样吗？", ""),
            ("assistant", "一样的配方，只是包装不同：补充装 5 瓶/袋，礼盒 10 瓶/箱。"
                          "口服美容类建议连续 4 周以上再看体感。", "rag"),
            ("user", "懂了，谢谢", ""),
            ("assistant", "不客气。本次会话已结束，感谢你的咨询；如需继续请重新发起对话。", "closed"),
        ],
    },
    {
        "key": "jd_wang", "channel": "jd", "buyer": "jd_wang",
        "handoff": "bot", "status": "closed", "days_ago": 2.2,
        "summary": "京东买家补开跨月电子发票，机器人登记抬头税号并给出 3 个工作日时效；未参评。",
        "turns": [
            ("user", "麻烦开张发票", ""),
            ("assistant", "下单时备注公司抬头与税号即可，电子发票 3 个工作日内开出；"
                          "跨月订单需要人工补开。", "faq"),
            ("user", "已经下单了、当时没备注怎么办", ""),
            ("assistant", "把那笔订单号、抬头和税号发我，我登记后走人工补开流程。", "faq"),
            ("user", "订单号 JD202610280042，抬头 杭州某某科技有限公司，税号 91330100MA2XXXX", ""),
            ("assistant", "收到，已登记补开，开出后发送到账户绑定邮箱。", "order_note"),
        ],
    },
    {
        "key": "pdd_zhao", "channel": "pdd", "buyer": "pdd_zhao",
        "handoff": "bot", "status": "closed", "days_ago": 3.1,
        "rating": 4, "rating_comment": "挺快的，就是希望包邮门槛低一点",
        "summary": "拼多多买家咨询新疆运费与凑单，机器人给出满 199 包邮 + 偏远 8 元附加费口径；4 分。",
        "turns": [
            ("user", "新疆能发吗，运费多少", ""),
            ("assistant", "可以发，默认中通；满 199 元包邮，但新疆/西藏/内蒙要加收 8 元运费。", "faq"),
            ("user", "那我再凑个喷雾就满了吧", ""),
            ("assistant", "是的，SKU005 舒缓保湿喷雾 89 元，凑单后免基础运费"
                          "（偏远地区 8 元附加费仍按规则收）。", "product"),
        ],
    },
    {
        "key": "pdd_sun", "channel": "pdd", "buyer": "pdd_sun",
        "handoff": "bot", "status": "active", "days_ago": 0.9,
        "summary": "拼多多买家咨询视黄醇夜间精华的使用顺序与耐受建立，机器人给出用法与防晒提醒。",
        "turns": [
            ("user", "视黄醇精华怎么用不刺激", ""),
            ("assistant", "SKU006 视黄醇夜间精华只在**晚上**用：洁面 → 等脸干 → 2-3 泵 → 面霜封层。"
                          "前两周隔天用建立耐受，白天务必配防晒（SKU007 SPF50+）。", "rag"),
        ],
    },
    {
        "key": "dy_1", "channel": "douyin", "buyer": "dy_1",
        "handoff": "bot", "status": "active", "days_ago": 0.35,
        "summary": "抖音买家发详情页截图询问批次生产日期，机器人视觉解析后说明批次口径，"
                   "并说明确切生产日期需人工查仓储系统。",
        "turns": [
            ("user", "这个批次的生产日期是什么时候", "user_attach_img"),
            ("assistant", "从你发的截图看，那是详情页的批次栏。详情页写的是最近一次入库批次，"
                          "我可以按 SKU 帮你对照在售批次——你要查哪一款？", "vision"),
            ("user", "面霜", ""),
            ("assistant", "面霜 SKU001，保质期 3 年。我这里看到的是当前在售批次，"
                          "确切生产日期需要人工在仓储系统查，先按批次口径给你答复。", "rag"),
        ],
    },
    {
        "key": "dy_2", "channel": "douyin", "buyer": "dy_2",
        "handoff": "bot", "status": "closed", "days_ago": 4.6,
        "rating": 3, "rating_comment": "问题答上了，但我问的是能不能便宜，回答有点绕",
        "summary": "抖音买家想再降价，机器人说明统一定价与会员/会员日权益、无改价权限；"
                   "3 分，属「能答但不解渴」的典型样本。",
        "turns": [
            ("user", "现在有什么优惠", ""),
            ("assistant", "会员享 9 折、每月 18 日会员日专享价，另外满 199 元包邮；"
                          "用券下单后退款按实付退回、券不补发。", "policy"),
            ("user", "我的意思是能不能再降点", ""),
            ("assistant", "价格是统一定价，我这边没有改价权限。可以把需要的 SKU 发我，"
                          "我帮你算怎么凑最划算。", "policy"),
        ],
    },
    {
        "key": "dy_3", "channel": "douyin", "buyer": "dy_3",
        "handoff": "human", "status": "active", "days_ago": 1.1,
        "summary": "抖音买家要求退已拆封礼盒，政策不支持无理由；人工给出「退未拆封两件 + 20 元券」方案，"
                   "买家初步接受，工单处理中。",
        "turns": [
            ("user", "礼盒拆封了想退", ""),
            ("assistant", "按售后政策，礼盒类商品拆封后不支持无理由退货；"
                          "如果是质量问题可走退换，需要你提供凭证。这块我帮你转人工确认处理口径。",
             "system_handoff"),
            ("assistant", "您好，我是人工客服李伟。看了这笔订单：礼盒已拆封，按政策不能无理由退。"
                          "我的方案是退未拆封的两件 + 补偿 20 元券，你看能接受吗？", "operator"),
            ("user", "行吧", ""),
        ],
        "ticket": {"reason": "礼盒拆封退款诉求（政策不支持无理由，需人工协商补偿）",
                   "status": "handling", "staff": "demo_staff_li"},
    },
    {
        "key": "web_chen_2", "channel": "web", "buyer": "chen",
        "handoff": "bot", "status": "closed", "days_ago": 6.3,
        "rating": 2, "rating_comment": "问了半天没答到点上，还是要人工",
        "summary": "买家问退货寄回地址与退款到账时间，机器人两轮只给流程口径、缺仓库地址语料，"
                   "买家自行找入口；2 分，是知识库覆盖缺口的实证样本。",
        "turns": [
            ("user", "退货寄到哪里？", ""),
            ("assistant", "在订单里点「申请退货」，页面会显示寄回地址；仓库签收验货 1-2 个工作日，"
                          "退款原路返回 3-7 个工作日。", "faq"),
            ("user", "我的订单里没有那个按钮", ""),
            ("assistant", "可能原因：超出 7 天无理由时效，或该商品属于不支持无理由的礼盒类。"
                          "具体要查你的订单才能确定。", "rag"),
            ("user", "那我自己找入口吧", ""),
            ("assistant", "抱歉这次没帮你解决。会话先结束，如需继续请重新发起对话。", "closed"),
        ],
    },
    {
        "key": "web_zhou_2", "channel": "web", "buyer": "zhou",
        "handoff": "bot", "status": "closed", "days_ago": 5.1,
        "rating": 5,
        "summary": "买家咨询卸妆与洁面是否需要两步，机器人给出 SKU008+SKU002 组合与到手价；5 分。",
        "turns": [
            ("user", "卸完妆还要用洁面吗", ""),
            ("assistant", "建议「卸妆膏 + 氨基酸洁面」两步：SKU008 卸妆膏乳化后冲洗，"
                          "再用 SKU002 氨基酸洁面做二次清洁，尤其防晒或彩妆较重的日子。", "rag"),
            ("user", "两个一起买划算吗", ""),
            ("assistant", "两件套合计 138.9 元，未到 199 所以不免基础运费；"
                          "如果你是会员还能再打 9 折。", "product_policy"),
        ],
    },
    {
        "key": "tb_2001", "channel": "taobao", "buyer": "tb_2001",
        "handoff": "bot", "status": "active", "days_ago": 0.01,
        "summary": "淘宝买家咨询会员日与生日月权益，机器人给出每月 18 日会员日与双倍积分口径。",
        "turns": [
            ("user", "会员日是哪天", ""),
            ("assistant", "每月 **18 日**是会员日，有专享价；生日月还能拿双倍积分，"
                          "会员本身享全场 9 折。", "policy"),
        ],
    },
    {
        "key": "jd_chen2", "channel": "jd", "buyer": "jd_chen2",
        "handoff": "human", "status": "closed", "days_ago": 6.4, "cadence": 2700,
        "rating": 1, "rating_comment": "等太久没人理，直接退了",
        "summary": "京东买家反馈快递显示签收但未收到（疑似丢件），转人工后首响超过 40 分钟，"
                   "买家选择退款并给 1 分；看板「首响时长」的超时样本。",
        "turns": [
            ("user", "快递显示签收了我没收到，丢件了", ""),
            ("assistant", "这属于物流异常，需要人工向快递发起查件与赔付，我马上帮你转人工。",
             "system_handoff"),
            ("assistant", "很抱歉让你久等，我是人工客服王晓琳。已向快递发起查件，48 小时内给结论；"
                          "确认丢件就补发或全额退款，你选哪种？", "operator"),
            ("user", "算了，退款吧", ""),
            ("assistant", "已按丢件提交全额退款，到账 3-7 个工作日。这次体验不好，向你道歉。", "operator"),
        ],
        "ticket": {"reason": "疑似快递丢件，需查件与赔付", "status": "closed",
                   "staff": "demo_staff_wang", "handling_hours": 3.1},
    },
    {
        "key": "web_lin", "channel": "web", "buyer": "lin",
        "handoff": "awaiting_confirm", "status": "active", "days_ago": 0.4,
        "summary": "买家要求指定顺丰并以投诉施压，机器人说明快递由仓库统一分配并按两步制发确认询问，"
                   "等待买家回复。",
        "turns": [
            ("user", "能不能发顺丰，不然我投诉", ""),
            ("assistant", "默认是中通，快递由仓库统一分配，我这边不能直接指定。"
                          "需要转人工确认能否特殊安排吗？回复「是」即可。", "system_handoff_ask"),
        ],
    },
]

MEMORY_FACTS: dict[str, list[tuple[str, str]]] = {
    "chen": [("preference", "偏好保湿类面霜，选购时先看是否适合敏感肌"),
             ("profile", "会员，习惯用优惠券下单"),
             ("case", "2026-10 咨询面霜成分与发货时效，未产生售后")],
    "zhou": [("preference", "用烟酰胺精华后刺痛，之后倾向舒缓/低刺激线"),
             ("case", "2026-10 精华过敏走人工售后：退货退款 + 补寄舒缓喷雾"),
             ("profile", "习惯发实拍图说明问题")],
    "lin": [("preference", "在意快递体验，明确要求过顺丰"),
            ("case", "2026-10 因不能指定快递提出投诉倾向，需人工跟进")],
    "jd_liu": [("preference", "关注口服美容与性价比，接受补充装替代礼盒"),
               ("case", "2026-10 胶原蛋白饮缺货，已推荐 SKU010 补充装")],
    "pdd_zhao": [("profile", "收货地在新疆，报价需提示偏远地区加收 8 元运费"),
                 ("case", "2026-10 凑单咨询，评价 4 分（希望包邮门槛更低）")],
    "dy_3": [("case", "2026-10 会员礼盒拆封要求退货，政策不支持无理由，"
                      "人工方案：退未拆封两件 + 20 元券")],
    "jd_chen2": [("case", "2026-10 京东订单疑似快递丢件，人工查件后全额退款；买家给 1 分")],
    "dy_2": [("preference", "价格敏感，反复确认能否再降，适合推会员日/凑单方案")],
    "tb_8823": [("case", "2026-10 催发货三次未响应，情绪激动，需优先跟进")],
}

LLM_ERRORS = [
    "HTTP 429 Too Many Requests（触发供应商限流）",
    "ReadTimeout after 30s",
    "HTTP 503 ModelOverloaded",
]

# 各调用的输入 token 量级（下限, 上限）：chat 的输入含系统提示 + 三级记忆 + 召回切片，
# embedding 是一批切片，rerank 是 query + 候选文档，vision 还要加图像 token。
_TOKEN_RANGE_BY_KIND = {
    "chat": (1100, 2400),
    "embedding": (500, 1400),
    "rerank": (700, 1600),
    "vision": (900, 1500),
}


# ================================ meta_json 生成 ================================
def _meta(kind: str, ctx: dict[str, Any]) -> dict[str, Any] | None:
    """把"这轮回复是怎么来的"写成 meta_json，页面据此还原引用、工具痕迹与来源徽标。"""
    if not kind:
        return None
    if kind == "user_attach_img":
        return {"attachments": [ctx["attachment"]]}
    if kind == "closed":
        return {"from": "system_session_closed"}
    if kind == "system_handoff":
        return {"from": "system_handoff"}
    if kind == "system_handoff_ask":
        return {"from": "system_handoff_ask"}
    if kind == "wecom":
        return {"from": "human_agent", "staff": ctx["wecom_staff"]}
    if kind == "operator":
        return {"from": "human_operator", "staff": ctx["operator_staff"]}
    if kind == "order_note":
        return {"from": "agent", "tools": [{
            "name": "record_ticket_note", "status": "ok",
            "args": {"order_id": "JD202610280042", "note": "跨月订单补开电子发票"},
            "output": "已登记，待财务开票"}]}
    if kind == "vision":
        return {"from": "agent", "tools": [{
            "name": "inspect_image", "status": "ok",
            "args": {"image": ctx["attachment"]["name"]},
            "output": "识别到详情页批次栏文字"}]}

    cites = _citations(kind, ctx)
    tools: list[dict[str, Any]] = [{
        "name": "rag_search", "status": "ok", "args": {"query": ctx["query"]},
        "output": f"命中 {len(cites)} 条切片"}]
    if kind in ("product", "product_policy"):
        tools.append({"name": "query_product", "status": "ok",
                      "args": {"keyword": ctx["query"]}, "output": ctx["product_output"]})
    meta: dict[str, Any] = {"from": "agent", "tools": tools}
    if cites:
        meta["citations"] = [{"index": i + 1, **c} for i, c in enumerate(cites)]
    return meta


def _citations(kind: str, ctx: dict[str, Any]) -> list[dict[str, Any]]:
    # 引用必须指向库里真实存在的切片（chunk_id 能在「分块」页查到），否则一查就露馅。
    wanted = {
        "rag": [("售后政策.txt", "7天无理由"), ("常见问题FAQ.txt", "保质期")],
        "faq": [("常见问题FAQ.txt", "发货时效")],
        "policy": [("售后政策.txt", "会员9折")],
        "product_policy": [("售后政策.txt", "优惠券"), ("产品目录.xlsx", "SKU001")],
        "product": [("产品目录.xlsx", "SKU004")],
    }.get(kind, [])
    out: list[dict[str, Any]] = []
    for file_name, keyword in wanted:
        hit = ctx["find_chunk"](file_name, keyword)
        if hit:
            out.append(hit)
    return out


def _guess_query(spec: dict[str, Any]) -> str:
    first = next((t[1] for t in spec["turns"] if t[0] == "user"), "")
    return re.sub(r"\s+", "", first)[:18] or "商品咨询"


def _product_output(spec: dict[str, Any]) -> str:
    if spec["key"] in ("jd_liu", "web_zhou_2"):
        return "SKU004 胶原蛋白饮 358 元 / 库存 0；SKU010 补充装 168 元 / 库存 47"
    if spec["key"] == "pdd_zhao":
        return "SKU005 舒缓保湿喷雾 89 元 / 库存 64"
    return "SKU001 清爽保湿面霜50g 129 元 / 库存 35"


# ================================ 占位实拍图 ================================
def _write_placeholder_png(path: Path, w: int = 560, h: int = 360) -> None:
    """画一张双色占位图当"买家实拍"：stdlib（zlib + struct）编码，不为此引入图像库。"""
    band = h * 2 // 3
    rows = []
    for y in range(h):
        rgb = (58, 96, 133) if y < band else (232, 236, 241)
        rows.append(b"\x00" + bytes(rgb) * w)
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                     + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


# ================================ 隔离演示配置（--serve） ================================
def ensure_demo_config(port: int) -> Path:
    """写一份只用于演示的配置：独立 SQLite/Chroma/数据目录 + 随机密钥 + 同源托管前端。

    刻意把会话空闲终结阈值放大到几十万秒——否则截图截到一半，sweeper 就把"活跃"会话
    自动结单了，页面上的数字会变成一段谁也解释不了的历史。
    """
    demo_dir = BACKEND_ROOT / "data" / "demo"
    (demo_dir / "uploads").mkdir(parents=True, exist_ok=True)
    cfg_path = demo_dir / "config.demo.yaml"

    import yaml

    base: dict[str, Any] = {}
    template = BACKEND_ROOT / "config.simple.yaml"
    if template.exists():
        base = yaml.safe_load(template.read_text(encoding="utf-8")) or {}

    base.setdefault("server", {}).update({
        "host": "127.0.0.1", "port": port, "env": "dev",
        "public_base_url": f"http://127.0.0.1:{port}",
        "serve_frontend": True,
        "frontend_dist": str(FRONTEND_DIST) if FRONTEND_DIST.exists() else "",
        "trust_proxy_headers": False,
        "cors_origins": [],
    })
    base.setdefault("database", {}).update({
        "url": f"sqlite:///{(demo_dir / 'cs_demo.db').as_posix()}",
        "migrate_mode": "auto",
    })
    sec = base.setdefault("security", {})
    sec["jwt_secret"] = secrets.token_hex(32)
    sec.setdefault("rate_limit", {})["enabled"] = False     # 演示时连点页面不该被限流拦住
    base.setdefault("session", {}).update({
        "idle_seconds": 1000000, "close_after_seconds": 1000000, "sweep_interval_seconds": 3600})
    base.setdefault("wecom", {})["mock"] = True
    # 演示库的 chunk_size 比默认 500 小一个量级：中文按 len//2 估 token，160 ≈ 320 字一条，
    # 一份 FAQ 能切出 5-6 片，「分块」页与引用卡片才有得看（也更接近真实商家的用法）。
    rag = base.setdefault("rag", {})
    rag["chroma_path"] = (demo_dir / "chroma").as_posix()
    rag["chunk_size"] = 160
    rag["chunk_overlap"] = 40
    # 看板「成本」列读 llm.pricing 现算，没配价会整列显示「未配价」；演示时填上参考价，
    # 让用量/成本页有真实数字（口径与 config.production.example.yaml 的注释一致）。
    # 直接赋值而不是 setdefault：模板里 `pricing: {}` 是刻意留空的，setdefault 永远打不进去。
    base.setdefault("llm", {})["pricing"] = {"dashscope": {
        "qwen-plus": {"input_per_1k": 0.0008, "output_per_1k": 0.002},
        "text-embedding-v4": {"input_per_1k": 0.0005, "output_per_1k": 0},
        "qwen3-rerank": {"input_per_1k": 0.0008, "output_per_1k": 0},
        "qwen-vl-plus": {"input_per_1k": 0.0015, "output_per_1k": 0.0045},
        "default": {"input_per_1k": 0.001, "output_per_1k": 0.002},
    }}
    platforms = base.setdefault("platforms", {})
    platforms["retry_interval_seconds"] = 3600             # 别让重投循环去碰假网关
    for name in ("taobao", "jd", "pdd", "douyin"):
        platforms.setdefault(name, {})["mock"] = True

    cfg_path.write_text(yaml.safe_dump(base, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.environ["CS_CONFIG"] = str(cfg_path)
    return cfg_path


# ================================ 各表灌数 ================================
def _user_keys(session: Any) -> dict[str, Any]:
    """把演示账号按 key 取出来（key 用剧本里引用的标识，避免到处写死用户名）。"""
    from sqlmodel import select

    from app.db.models import User

    wanted = {"admin": ADMIN["username"]}
    for s in STAFF:
        wanted[s["username"]] = s["username"]
    for key, b in BUYERS.items():
        wanted[key] = b["username"]
    for spec in CONVERSATIONS:
        if spec["channel"] != "web":
            wanted[spec["buyer"]] = _shadow_username(spec["channel"], spec["buyer"])
    out: dict[str, Any] = {}
    for key, username in wanted.items():
        row = session.exec(select(User).where(User.username == username)).first()
        if row:
            out[key] = row
    return out


def seed_users(session: Any) -> int:
    from sqlmodel import select

    from app.core.security import hash_password
    from app.db.models import User

    def ensure(username: str, role: str, email: str | None) -> bool:
        if session.exec(select(User).where(User.username == username)).first():
            return False
        session.add(User(username=username, email=email, role=role,
                         hashed_password=hash_password(DEMO_PASSWORD),
                         created_at=_ago(days=30)))
        session.commit()
        return True

    made = 0
    made += ensure(ADMIN["username"], "admin", ADMIN["email"])
    for s in STAFF:
        made += ensure(s["username"], "staff", s["email"])
    for b in BUYERS.values():
        made += ensure(b["username"], "user", b["email"])
    for spec in CONVERSATIONS:
        if spec["channel"] != "web":
            made += ensure(_shadow_username(spec["channel"], spec["buyer"]), "user", None)
    return made


def _ingest_without_vectors(session: Any, kf: Any) -> None:
    """没配 embedding key 时的入库路径：只做 parse → 分块 → 商品落表，不碰向量。

    为什么不直接复用 ingest_file：它的向量化失败会写进 `KnowledgeFile.error`（前端红字
    「向量化失败」）并落一条失败记录，演示库会被这些"真故障"痕迹污染，还会把
    llm 错误率看板带偏。这里显式走"本来就只有关键词检索"的口径，页面显示正常语料。
    """
    from app.db.models import Chunk
    from app.services.rag import parser
    from app.services.rag.chunker import chunk_text
    from app.services.rag.ingest import _index_dirty, _upsert_product

    parsed = parser.parse(Path(kf.path), kf.file_type)
    texts = chunk_text(parsed.text)
    for seq, text in enumerate(texts):
        session.add(Chunk(file_id=int(kf.id), seq=seq, text=text, tokens=len(text) // 2))
    for prow in parsed.products:
        _upsert_product(session, prow, int(kf.id))
    kf.status = "ready"
    kf.chunk_count = len(texts)
    kf.error = None
    session.add(kf)
    session.commit()
    _index_dirty()


def seed_knowledge(session: Any) -> dict[str, Any]:
    """走真实入库链路（parser → chunker → 商品落表），这样「分块」页看到的就是真切片。

    配了 embedding key 时完全复用生产的 ingest_file（向量也进 Chroma，检索页可实跑）；
    没配时走 _ingest_without_vectors，BM25 关键词检索与商品精确查询仍可用。
    """
    import openpyxl
    from sqlmodel import select

    from app.core.models import manager as model_manager
    from app.db.models import Chunk, IngestJob, KnowledgeFile
    from app.services.rag import ingest, parser

    vectors_ready = model_manager.capability_ready("embedding")

    upload_dir = BACKEND_ROOT / "data" / "uploads" / "knowledge"
    upload_dir.mkdir(parents=True, exist_ok=True)

    xlsx = upload_dir / f"{uuid.uuid4().hex[:8]}_产品目录.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "products"
    ws.append(list(PRODUCT_HEADER))
    for row in PRODUCTS:
        ws.append(list(row))
    wb.save(xlsx)

    def text_doc(label: str, content: str) -> Path:
        p = upload_dir / f"{uuid.uuid4().hex[:8]}_{label}"
        p.write_text(content, encoding="utf-8")
        return p

    docs = [(xlsx, "产品目录.xlsx"),
            (text_doc("售后政策.txt", POLICY_TEXT), "售后政策.txt"),
            (text_doc("常见问题FAQ.txt", FAQ_TEXT), "常见问题FAQ.txt"),
            (text_doc("2025旧活动规则.txt", LEGACY_TEXT), "2025旧活动规则.txt")]

    files: dict[str, Any] = {}
    for i, (src, disp) in enumerate(docs):
        kf = KnowledgeFile(name=disp, path=str(src), file_type=parser.detect_type(src),
                           size=src.stat().st_size, status="parsing",
                           created_at=_ago(days=16 - i))
        session.add(kf)
        session.commit()
        session.refresh(kf)
        if vectors_ready:
            ingest.ingest_file(session, kf, purge_first=True)
        else:
            _ingest_without_vectors(session, kf)
        session.refresh(kf)
        files[disp] = kf
        # 留一条已完成的入库任务，知识库/告警页读 IngestJob 才有历史可看
        job = IngestJob(file_id=kf.id, file_name=disp, kind="upload", status="done",
                        total=kf.chunk_count, processed=kf.chunk_count,
                        created_at=kf.created_at, updated_at=kf.created_at)
        session.add(job)
        session.commit()

    # 一条失败任务（源文件丢失）+ 一条已停用语料：页面上"重试/禁用"两种态都有实物可点
    broken_src = upload_dir / f"{uuid.uuid4().hex[:8]}_历史价目表.xlsx"
    broken = KnowledgeFile(name="历史价目表.xlsx", path=str(broken_src), file_type="excel",
                           size=0, status="failed",
                           error="入库时被进程重启打断，源文件已不在 uploads 里（请删除后重新上传）",
                           created_at=_ago(days=12))
    session.add(broken)
    session.commit()
    session.refresh(broken)
    session.add(IngestJob(file_id=broken.id, file_name=broken.name, kind="upload",
                          status="interrupted", total=0, processed=0, error=broken.error,
                          created_at=broken.created_at, updated_at=_ago(days=12)))
    session.commit()

    legacy = files["2025旧活动规则.txt"]
    legacy.enabled = False
    session.add(legacy)
    session.commit()
    try:
        # 停用语料要同步改向量元数据，否则有 key 时向量侧仍会召回它（正常路径由接口做）
        from app.services.rag import vectorstore

        vectorstore.set_file_enabled(int(legacy.id), False)
    except Exception as exc:  # 演示库的向量元数据同步失败不影响页面走查
        print(f"· 停用语料的向量元数据同步失败（可忽略）：{exc}")

    by_file: dict[int, list[Any]] = {}
    for c in session.exec(select(Chunk)).all():
        by_file.setdefault(int(c.file_id), []).append(c)

    def find_chunk(file_name: str, keyword: str) -> dict[str, Any] | None:
        kf = files.get(file_name)
        if not kf or kf.id is None:
            return None
        rows = by_file.get(int(kf.id), [])
        for c in rows:
            if keyword in c.text:
                return {"file_id": kf.id, "file_name": kf.name, "chunk_id": c.id,
                        "snippet": c.text[:120]}
        return None

    return {"files": files, "find_chunk": find_chunk, "broken_id": broken.id,
            "vectors_ready": vectors_ready}


def seed_conversations(session: Any, kb: dict[str, Any]) -> dict[str, Any]:

    from app.db.models import (
        Conversation,
        ConversationSummary,
        ExternalConversation,
        InboundDedupe,
        Message,
        pk,
    )

    users = _user_keys(session)
    media_dir = BACKEND_ROOT / "data" / "uploads" / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    shot = media_dir / f"demo_{uuid.uuid4().hex[:10]}_buyer_shot.png"
    _write_placeholder_png(shot)
    attachment = {"url": f"/api/files/{shot.name}", "name": "买家实拍-面部红疹.png", "type": "image"}

    nick = {s["username"]: s["nick"] for s in STAFF}
    made: dict[str, Any] = {}
    for spec in CONVERSATIONS:
        is_web = spec["channel"] == "web"
        user = users[spec["buyer"]]
        cadence = int(spec.get("cadence", CADENCE_SECONDS))
        span = cadence * max(0, len(spec["turns"]) - 1)
        started = _ago(days=spec["days_ago"])
        # 最后一条消息留在 1 分钟前：否则"活跃会话"的时间戳跑到未来，
        # 看板的当日趋势与「多久没回复」的告警都会算出负数。
        latest_start = NOW - datetime.timedelta(seconds=span + 60)
        if started > latest_start:
            started = latest_start
        first_user = next((t[1] for t in spec["turns"] if t[0] == "user"), "新会话")
        # 网页会话的标题由 runner 取首条买家消息前 20 字；平台会话用 [platform] buyer_id
        title = first_user[:20] if is_web else f"[{spec['channel']}] {spec['buyer']}"[:64]
        conv = Conversation(user_id=pk(user), title=title, handoff_status=spec["handoff"],
                            status=spec["status"], created_at=started, updated_at=started,
                            last_customer_msg_at=started)
        session.add(conv)
        session.commit()
        session.refresh(conv)
        cid = pk(conv)

        if not is_web:
            shop = SHOP_BY_PLATFORM[spec["channel"]]
            session.add(ExternalConversation(platform=spec["channel"], shop_id=shop,
                                             buyer_id=spec["buyer"], conversation_id=cid,
                                             created_at=started))
            # 入站幂等表：真实链路每条买家消息写一行，这里按买家消息条数补齐。
            # msg_id 带每次灌数的随机后缀：(platform, shop, buyer, msg_id) 上有唯一索引，
            # 不带后缀的话"再灌一次"会直接撞 UNIQUE 崩在第一个平台会话上。
            nonce = uuid.uuid4().hex[:6]
            for n, (role, _c, _k) in enumerate(spec["turns"]):
                if role != "user":
                    continue
                session.add(InboundDedupe(platform=spec["channel"], shop_id=shop,
                                          buyer_id=spec["buyer"],
                                          external_msg_id=f"{spec['key']}-{nonce}-m{n}",
                                          created_at=started))
            session.commit()

        staff_user = spec.get("ticket", {}).get("staff") or ""
        ctx = {
            "attachment": attachment,
            "find_chunk": kb["find_chunk"],
            "query": _guess_query(spec),
            "product_output": _product_output(spec),
            "wecom_staff": nick.get(staff_user, STAFF[0]["nick"]),
            "operator_staff": nick.get(staff_user, STAFF[1]["nick"]
                                       if spec["key"] == "dy_3" else STAFF[0]["nick"]),
        }

        cursor = started
        last_user = started
        last_msg_id = 0
        handoff_at: Any = None
        for i, (role, content, kind) in enumerate(spec["turns"]):
            if i:
                cursor = cursor + datetime.timedelta(seconds=cadence)
            if role == "user":
                last_user = cursor
            if kind in ("system_handoff", "system_handoff_ask") and handoff_at is None:
                handoff_at = cursor
            meta = _meta(kind, ctx)
            msg = Message(conversation_id=cid, role=role, content=content,
                          meta_json=json.dumps(meta, ensure_ascii=False) if meta else None,
                          created_at=cursor)
            session.add(msg)
            session.commit()
            session.refresh(msg)
            last_msg_id = int(msg.id or last_msg_id)

        conv.updated_at = cursor
        conv.last_customer_msg_at = last_user
        if spec["status"] == "closed":
            conv.end_prompted_at = cursor - datetime.timedelta(minutes=6)
        session.add(conv)
        session.commit()
        session.refresh(conv)
        made[spec["key"]] = {"conv": conv, "last_at": cursor,
                             "handoff_at": handoff_at or started}

        if spec.get("summary"):
            session.add(ConversationSummary(conversation_id=cid, summary=spec["summary"],
                                            until_message_id=last_msg_id, updated_at=cursor))
            session.commit()
    return {"convs": made, "attachment_name": shot.name, "users": users}


def seed_tickets_and_outbox(session: Any, convs: dict[str, Any]) -> int:
    from app.db.models import HandoffTicket, OutboxRecord, pk

    n = 0
    for spec in CONVERSATIONS:
        t = spec.get("ticket")
        if not t:
            continue
        conv = convs["convs"][spec["key"]]["conv"]
        # 工单时间对齐到会话里那条「已转人工」系统消息：看板的首响时长取的是
        # 「工单创建 → 首条人工代答」的差值，工单挂在会话创建时刻会让它变成负数或虚高。
        created = convs["convs"][spec["key"]]["handoff_at"]
        closed_at = (created + datetime.timedelta(hours=t.get("handling_hours", 4))
                     if t["status"] == "closed" else None)
        session.add(HandoffTicket(conversation_id=pk(conv), user_id=conv.user_id,
                                  reason=t["reason"], summary=spec.get("summary", "")[:200],
                                  status=t["status"], staff=t["staff"],
                                  created_at=created, closed_at=closed_at))
        session.commit()
        n += 1
        # 企微转人工卡片：近三天的记 mocked（当前就是 mock 配置），更早的记 sent，
        # 这样外发箱两种徽标都能演示到；真实接入后由 outbox 写入，不靠种子数据。
        status = "mocked" if created > _ago(days=3) else "sent"
        session.add(OutboxRecord(
            channel="wecom", kind="textcard", to="@all",
            title=f"会话#{pk(conv)} 需要人工：{t['reason'][:14]}",
            body=t["reason"],
            payload_json=json.dumps({"touser": "@all", "msgtype": "textcard", "textcard": {
                "title": f"[{spec.get('channel')}] 转人工", "description": t["reason"],
                "url": "http://127.0.0.1:7866/handoff"}}, ensure_ascii=False),
            status=status, created_at=created))
        session.commit()
        n += 1

    # 平台外发四种状态各一条：正常送达 / 待重投 / 死信 / 媒体降级，外发箱徽标齐活
    rows = [
        OutboxRecord(channel="taobao", kind="text", to="tb_8823", title="",
                     body="好的，我马上帮你转人工客服处理发货问题——现在转吗？回复「是」即可。",
                     payload_json=json.dumps({"to_uid": "tb_8823", "content": "转人工确认询问"},
                                             ensure_ascii=False),
                     status="sent", attempts=1, created_at=_ago(minutes=25)),
        OutboxRecord(channel="douyin", kind="text", to="dy_3", title="",
                     body="按售后政策，礼盒类商品拆封后不支持无理由退货；如果是质量问题可走退换。",
                     payload_json=json.dumps({"sender_uid": "dy_3", "content": "政策口径"},
                                             ensure_ascii=False),
                     status="failed", attempts=2, error="HTTP 500 gateway timeout",
                     next_retry_at=_ago(minutes=-30), created_at=_ago(minutes=48)),
        OutboxRecord(channel="pdd", kind="text", to="pdd_zhao", title="",
                     body="可以的，满 199 元包邮，偏远地区加收 8 元运费。",
                     payload_json=json.dumps({"to_uid": "pdd_zhao", "content": "运费口径"},
                                             ensure_ascii=False),
                     status="failed", attempts=4, error="HTTP 401 access_token 已过期",
                     next_retry_at=None, last_error="重试 4 次仍失败，已进死信，需人工处理",
                     created_at=_ago(days=2)),
        OutboxRecord(channel="jd", kind="media", to="jd_chen2", title="查件回执.pdf",
                     body="[附件] 查件回执.pdf: 未配置 media_method，仅留占位文本",
                     payload_json=json.dumps({"media": "查件回执.pdf"}, ensure_ascii=False),
                     status="skipped", error="平台媒体出站未配置（media_method 待核对官方文档）",
                     created_at=_ago(days=6.3)),
    ]
    for r in rows:
        session.add(r)
    session.commit()
    return n + len(rows)


def seed_memory(session: Any, convs: dict[str, Any]) -> int:
    from sqlmodel import select

    from app.db.models import MemoryFact, MemoryJob, User, pk

    def by_username(username: str) -> Any:
        return session.exec(select(User).where(User.username == username)).first()

    n = 0
    for buyer_key, facts in MEMORY_FACTS.items():
        user = by_username(_buyer_username(buyer_key))
        if not user:
            continue
        src = None
        for spec in CONVERSATIONS:
            if spec["buyer"] == buyer_key:
                src = pk(convs["convs"][spec["key"]]["conv"])
                break
        for i, (cat, text) in enumerate(facts):
            session.add(MemoryFact(user_id=pk(user), fact=text, category=cat,
                                   source_conversation_id=src, created_at=_ago(days=9 - i * 0.6)))
            n += 1
        session.commit()

    # 记忆任务：几条 done，一条因缺 key 的 failed（记忆是增强项，失败不影响当轮对话）
    items = list(convs["convs"].items())
    for i, (_key, c) in enumerate(items[:7]):
        conv = c["conv"]
        status = "failed" if i == 6 else "done"
        session.add(MemoryJob(conversation_id=pk(conv), user_id=conv.user_id, stage="both",
                              status=status, attempts=1,
                              error=None if status == "done" else "模型未配置 api_key，跳过画像抽取",
                              created_at=_ago(days=2), updated_at=_ago(days=2)))
        n += 1
    session.commit()
    return n


def _buyer_username(buyer_key: str) -> str:
    if buyer_key in BUYERS:
        return BUYERS[buyer_key]["username"]
    for spec in CONVERSATIONS:
        if spec["buyer"] == buyer_key:
            return _shadow_username(spec["channel"], buyer_key)
    return f"{DEMO_SHADOW_PREFIX}{buyer_key}"


def seed_ratings(session: Any, convs: dict[str, Any]) -> int:
    from app.db.models import SessionRating, pk

    n = 0
    for spec in CONVERSATIONS:
        if not spec.get("rating"):
            continue
        cid = pk(convs["convs"][spec["key"]]["conv"])
        when = convs["convs"][spec["key"]]["last_at"] + datetime.timedelta(minutes=3)
        session.add(SessionRating(conversation_id=cid, rating=int(spec["rating"]),
                                  comment=spec.get("rating_comment", ""),
                                  source="survey_link", created_at=when))
        session.commit()
        n += 1
    return n


def seed_usage(session: Any, convs: dict[str, Any]) -> int:
    """用量流水：14 天 160 余条，覆盖 chat/embedding/rerank/vision 四类与错误率样本。

    单价按 `llm.pricing` 现算——没配价就是 None，看板显示「未配价」而不是 0 元。
    embedding/rerank 不返回 usage_metadata，所以标 estimated=True，读的人能分辨口径。
    """
    from app.db.models import LlmCall
    from app.services.usage import cost_of

    provider = "dashscope"
    models = {"chat": "qwen-plus", "embedding": "text-embedding-v4",
              "rerank": "gte-rerank-v2", "vision": "qwen-vl-plus"}
    kinds_cycle = ("chat", "chat", "chat", "embedding", "rerank", "vision")
    items = list(convs["convs"].values())
    rows: list[LlmCall] = []
    seq = 0
    for day in range(14):
        per_day = 6 + (day % 5) * 3           # 造点自然波动，曲线不至于像假的
        for n in range(per_day):
            seq += 1
            kind = kinds_cycle[(day * 7 + n) % len(kinds_cycle)]
            c = items[(day * 3 + n) % len(items)]
            conv = c["conv"]
            # token 数按调用类型给真实量级：chat 的输入含系统提示+三级记忆+召回切片（上千 token），
            # embedding 是一批切片，rerank 是 query+候选文档。之前按消息正文粗算会得出 60 token/次，
            # 看板的成本列因此小到像坏掉。
            lo, hi = _TOKEN_RANGE_BY_KIND[kind]
            in_tok = lo + (seq * 173 + day * 61) % (hi - lo)
            out_tok = (120 + (n * 37) % 260) if kind == "chat" else 0
            err = seq % 23 == 0
            lat = (480 + (n * 113) % 2200) if kind == "chat" else (90 + (n * 17) % 260)
            rows.append(LlmCall(
                kind=kind, provider=provider, model=models[kind],
                conversation_id=None if kind in ("embedding", "rerank") else conv.id,
                user_id=conv.user_id,
                input_tokens=in_tok, output_tokens=out_tok, total_tokens=in_tok + out_tok,
                estimated=kind in ("embedding", "rerank"),
                cost_est=None if err else cost_of(provider, models[kind], in_tok, out_tok),
                latency_ms=lat, attempts=1 if seq % 17 else 2,
                status="error" if err else "ok",
                error=None if not err else LLM_ERRORS[seq % len(LLM_ERRORS)],
                created_at=_ago(days=day, minutes=n * 9)))
    for r in rows:
        session.add(r)
    session.commit()
    return len(rows)


def seed_mcp(session: Any, convs: dict[str, Any]) -> None:
    from sqlmodel import select

    from app.db.models import McpServer, ToolConfirmRequest, pk

    tools = [
        {"name": "query_order", "description": "按订单号查询订单状态与物流轨迹",
         "inputSchema": {"order_id": "string"}},
        {"name": "refund_order", "description": "发起退款（写操作，需人工确认）",
         "inputSchema": {"order_id": "string", "amount": "number", "reason": "string"}},
    ]
    # name 上有唯一索引：不加这道判断，"再灌一次"会撞 UNIQUE 而不是追加
    existing_names = {s.name for s in session.exec(select(McpServer)).all()}
    for row in (
        McpServer(name="demo-order", transport="stdio", command="python",
                  args="scripts/demo_mcp_server.py", url="", enabled=True,
                  require_confirm=True, status="connected",
                  tools_json=json.dumps(tools, ensure_ascii=False),
                  created_at=_ago(days=18)),
        # 一条停用的 HTTP 服务器：页面上 enabled/连接态两种样式都有实物
        McpServer(name="crm-external", transport="streamable_http", command="", args="",
                  url="https://crm.example.internal/mcp", enabled=False,
                  require_confirm=True, status="disconnected", tools_json=None,
                  created_at=_ago(days=11)),
    ):
        if row.name in existing_names:
            continue
        session.add(row)
    session.commit()

    tb2 = convs["convs"]["tb_2001"]["conv"]
    dy3 = convs["convs"]["dy_3"]["conv"]
    zhou = convs["convs"]["web_zhou"]["conv"]
    jd2 = convs["convs"]["jd_chen2"]["conv"]
    decider = convs["users"]["demo_staff_wang"].id
    cards = [
        # 待确认卡片：confirm.TTL_SECONDS=180，超过 180s 的 pending 会被 prune 置 expired，
        # 页面上就什么都剩不下，所以这条刻意按"刚刚下发"写；裁决历史见后面几条。
        ToolConfirmRequest(request_id=secrets.token_hex(6), conversation_id=pk(tb2),
                           user_id=tb2.user_id, tool_name="demo-order.refund_order",
                           args_json=json.dumps({"order_id": "TB202610300118", "amount": 129,
                                                 "reason": "买家要求退未发货订单"}, ensure_ascii=False),
                           status="pending", created_at=NOW - datetime.timedelta(seconds=5),
                           decided_at=None),
        ToolConfirmRequest(request_id=secrets.token_hex(6), conversation_id=pk(dy3),
                           user_id=dy3.user_id, tool_name="demo-order.query_order",
                           args_json=json.dumps({"order_id": "DY202610290033"}, ensure_ascii=False),
                           status="approved", decided_by=None,
                           created_at=_ago(days=1.05), decided_at=_ago(days=1.05, minutes=1)),
        ToolConfirmRequest(request_id=secrets.token_hex(6), conversation_id=pk(zhou),
                           user_id=zhou.user_id, tool_name="demo-order.refund_order",
                           args_json=json.dumps({"order_id": "WEB202610290007", "amount": 199,
                                                 "reason": "过敏退货（人工已同意）"}, ensure_ascii=False),
                           status="approved", decided_by=decider,
                           created_at=_ago(days=0.6), decided_at=_ago(days=0.6, minutes=2)),
        ToolConfirmRequest(request_id=secrets.token_hex(6), conversation_id=pk(jd2),
                           user_id=jd2.user_id, tool_name="demo-order.refund_order",
                           args_json=json.dumps({"order_id": "JD202610220091", "amount": 358,
                                                 "reason": "丢件赔付金额需上级复核"}, ensure_ascii=False),
                           status="rejected", decided_by=decider,
                           created_at=_ago(days=6.2), decided_at=_ago(days=6.2, minutes=4)),
        ToolConfirmRequest(request_id=secrets.token_hex(6), conversation_id=pk(tb2),
                           user_id=tb2.user_id, tool_name="demo-order.refund_order",
                           args_json=json.dumps({"order_id": "TB202610280119", "amount": 89,
                                                 "reason": "无人应答，按超时作废"}, ensure_ascii=False),
                           status="expired", decided_by=None,
                           created_at=_ago(days=2), decided_at=_ago(days=2, minutes=2)),
    ]
    for c in cards:
        session.add(c)
    session.commit()


def rearm_pending(session: Any) -> int:
    """把待确认卡片的时间推到当下——演示数据里唯一会"自己过期"的一条。

    confirm.TTL_SECONDS 是 180 秒（没人应答就按拒绝处理并置 expired），所以灌完数几分钟后
    MCP 页的待确认卡片必然消失。截图或演示前跑一次 `--rearm` 即可，不用整库重灌。
    """
    from sqlmodel import select

    from app.db.models import ToolConfirmRequest

    now = datetime.datetime.now(datetime.UTC)
    rows = session.exec(select(ToolConfirmRequest)
                        .where(ToolConfirmRequest.status == "pending")).all()
    for r in rows:
        r.created_at = now - datetime.timedelta(seconds=5)
        session.add(r)
    session.commit()
    return len(rows)


def seed_all(session: Any) -> dict[str, Any]:
    from sqlmodel import func, select

    from app.db.models import Message

    created_users = seed_users(session)
    kb = seed_knowledge(session)
    convs = seed_conversations(session, kb)
    tickets = seed_tickets_and_outbox(session, convs)
    facts = seed_memory(session, convs)
    ratings = seed_ratings(session, convs)
    usage = seed_usage(session, convs)
    seed_mcp(session, convs)
    return {
        "新建账号": created_users,
        "知识文件": len(kb["files"]) + 1,
        "会话": len(convs["convs"]),
        "消息": int(session.exec(select(func.count()).select_from(Message)).one()),
        "工单与外发": tickets,
        "记忆条目": facts,
        "满意度评分": ratings,
        "模型用量流水": usage,
        "演示附件": convs["attachment_name"],
        "向量检索": kb["vectors_ready"],
    }


# ================================ 库判定与清库 ================================
def _looks_like_demo_db(session: Any) -> tuple[bool, str]:
    """判定"这个库可以拿来灌演示数据"：要么基本是空的，要么已有的全属于演示账号。"""
    from sqlmodel import select

    from app.db.models import Conversation, User

    users = list(session.exec(select(User)).all())
    convs = list(session.exec(select(Conversation)).all())
    if not users and not convs:
        return True, "空库"
    demo = {u.username for u in users
            if u.username.startswith((DEMO_USER_PREFIX, DEMO_SHADOW_PREFIX))}
    if not demo:
        return False, f"已有 {len(users)} 个账号 / {len(convs)} 个会话，且没有演示账号"
    owner_ids = {u.id for u in users if u.username in demo}
    foreign = [c for c in convs if c.user_id not in owner_ids]
    if foreign:
        return False, f"{len(foreign)} 个会话不属于演示账号"
    return True, f"已有 {len(demo)} 个演示账号（重复灌入会追加，要干净结果就加 --reset）"


def wipe_all(session: Any) -> None:
    from sqlmodel import SQLModel

    with session.get_bind().begin() as conn:
        for table in reversed(SQLModel.metadata.sorted_tables):
            conn.exec_driver_sql(f'DELETE FROM "{table.name}"')


# ================================ 入口 ================================
def main() -> int:
    ap = argparse.ArgumentParser(description="灌入演示数据（界面截图 / 交付演示 / 验收走查用）")
    ap.add_argument("--serve", action="store_true",
                    help="自造隔离演示配置（src/backend/data/demo/）后灌数并同源托管前端 dist")
    ap.add_argument("--port", type=int, default=7866, help="配合 --serve 的端口，默认 7866")
    ap.add_argument("--reset", action="store_true",
                    help="先清空全部表再灌（只在判定为演示库的库上允许）")
    ap.add_argument("--force-any-db", action="store_true",
                    help="跳过演示库判定，强行往当前库灌数据（危险：会污染真实数据）")
    ap.add_argument("--rearm", action="store_true",
                    help="只把 MCP 待确认卡片重新下发（卡片 TTL 180 秒会自己过期，截图前跑一次）")
    args = ap.parse_args()

    cfg_path = None
    if args.serve:
        cfg_path = ensure_demo_config(args.port)
    elif args.rearm and not os.environ.get("CS_CONFIG"):
        # --rearm 单独用时默认指向 --serve 那份演示库，省得再 export 一次
        demo_cfg = BACKEND_ROOT / "data" / "demo" / "config.demo.yaml"
        if demo_cfg.exists():
            os.environ["CS_CONFIG"] = str(demo_cfg)

    from app.config import get_settings

    if get_settings().is_production():
        raise SystemExit("server.env=production：演示数据脚本拒绝执行（别把假数据灌进生产库）")

    from app.db.session import Session, engine, init_db

    init_db()
    with Session(engine) as session:
        if args.rearm and not (args.serve or args.reset or args.force_any_db):
            n = rearm_pending(session)
            _say(f"· 已重新下发 {n} 条待确认卡片（180 秒内有效）")
            return 0
        ok, why = _looks_like_demo_db(session)
        if not ok and not args.force_any_db:
            raise SystemExit(
                f"当前库不像演示库（{why}，配置来自 {cfg_path or os.environ.get('CS_CONFIG', 'config.simple.yaml')}）。"
                "要往它灌演示数据请加 --force-any-db；想看干净的演示界面用 --serve。")
        if args.reset:
            wipe_all(session)
            _say("· 已清空全部表")
        stats = seed_all(session)
        if args.rearm:
            rearm_pending(session)

    _say("\n演示数据已就绪：")
    for k, v in stats.items():
        _say(f"  - {k}: {'是' if v is True else '否' if v is False else v}")
    _say(f"  - 配置: {cfg_path or os.environ.get('CS_CONFIG', 'config.simple.yaml')}")
    _say(f"  - 管理员: {ADMIN['username']} / {DEMO_PASSWORD}（六页全可见）")
    _say(f"  - 客服:   {STAFF[0]['username']} / {DEMO_PASSWORD}（staff，看不到 MCP 服务器配置入口）")
    _say(f"  - 买家:   {BUYERS['chen']['username']} / {DEMO_PASSWORD}（只看自己的会话）")
    if stats.get("向量检索") is False:
        _say("  提示：当前没有可用的 embedding key，知识库只建了关键词检索（BM25）与商品表，"
             "语料本身是完整的；配上 key 重新 --reset 灌一次即可得到向量。")

    if args.serve:
        import uvicorn

        from app.main import app

        if not FRONTEND_DIST.exists():
            _say("· 前端产物缺失，先跑：cd src/frontend && volta run npm run build"
                 "（现在只提供服务端 API）")
        _say(f"· 演示地址: http://127.0.0.1:{args.port}  （Ctrl+C 停止）")
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
