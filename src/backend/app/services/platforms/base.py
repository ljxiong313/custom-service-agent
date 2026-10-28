"""平台适配器基类与公共件（P7）。

约定：
- 配置全部来自 `platforms.<name>` 节（见 config.simple.yaml），mock=true 时不外呼；
- mock 模式下验签直接放行、回复落外发箱（B1-2 起写 OutboxRecord 表，重启不丢）——
  与企微通道同一演示策略，本地无需任何平台资质即可跑通全链路；
- 真实模式的签名算法按各平台公开文档实现，但端点/方法名会随平台版本调整，
  接入前必须核对 docs/平台接入.md 与官方最新文档；
- 配置没填齐时不再裸抛：出站写 status=skipped 的外发记录并 warning，
  缺哪项、去哪节补都在 error 文案里（readiness() 提供同一份静态自检清单）；
- 真实外呼（mock=false）的媒体链接必须是**绝对地址**：`server.public_base_url` 留空时
  买家拿到的是点不开的相对路径，这类出站一律降级（正文换成可读提示、媒体落 skipped），
  详见 mark_unreachable_media()/skip_unreachable_media()；
- 真实外呼异常（failed）由 outbox.record 排入退避重试队列，重投见 outbound_retry.py；
- readiness() 里标 ★ 的口径项带 needs_review=True：值"配了"不等于"配对了"。
"""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import re
from collections.abc import Callable
from typing import ClassVar

from loguru import logger

from app.config import get_settings
from app.services import outbox
from app.utils import media_url

PLATFORM_ORDER = ("taobao", "jd", "douyin", "pdd")

# 缺项提示要指到文档具体章节，否则运维只能从头读一遍接入指南
_DOC_SECTION = {"taobao": "第 3 节《淘宝 / 天猫》", "jd": "第 4 节《京东》",
                "pdd": "第 5 节《拼多多》", "douyin": "第 6 节《抖店 / 飞鸽》"}

# callback_verify=none 之类的风险每次回调都会命中，只提醒一次
_warned: set[str] = set()

# 真实外发时正文里的相对附件链接（/api/files/x.png?exp=…&sig=…）买家一定点不开：
# 相对路径没有主机名，平台客户端不会替我们补。前一个字符是单词字符就说明它挂在域名后面
# （绝对地址，public_base_url 配好了），不动它。
_RELATIVE_MEDIA_RE = re.compile(
    rf"(?<![\w.-]){re.escape(media_url.MEDIA_URL_PREFIX)}[^\s,;，；)]+")

# 死链换成这句人话：至少让买家/客服知道附件在哪儿，而不是点一个打不开的链接
NO_PUBLIC_BASE_HINT = "（附件需在客服工作台查看，链接未配置公网地址）"

# docs/平台接入.md 里标 ★ 的字段：口径按公开文档实现，但平台版本会改，
# 接真实平台前必须人工核对（readiness 用 needs_review 把这个"待办"透出到设置页）
_NEEDS_REVIEW: dict[str, set[str]] = {
    name: {"gateway", "reply_method", "to_uid_field", "content_field", "ack",
           "media_method", "media_url_field", "media_type_field"}
    for name in PLATFORM_ORDER}
# 只有抖店的多版本并存口径没把握（TOP 同族 md5 各家文档一致）
_NEEDS_REVIEW["douyin"].add("callback_verify")


@dataclasses.dataclass
class InboundMessage:
    """解析后的平台入站消息。"""

    platform: str
    shop_id: str
    buyer_id: str            # 平台侧买家标识（openid/加密uid）
    text: str
    external_msg_id: str = ""  # 平台消息 id（幂等去重用）


def conf(platform: str) -> dict:
    return get_settings().get_dotted(f"platforms.{platform}", {}) or {}


def is_mock(platform: str) -> bool:
    return bool(conf(platform).get("mock", True))


def warn_once(key: str, message: str) -> None:
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(message)


def allow_none_verify(platform: str) -> bool:
    """callback_verify=none 是本地演示的逃生门。

    开发形态：放行 + 显眼告警（readiness 里同项标风险）。
    生产形态：直接拒绝——回调免 JWT，不验签等于任何人都能伪造买家消息驱动 Agent、
    往别人会话里写内容、触发外发。启动自检也会把这条列成 fatal。
    """
    warn_once(f"callback_verify:{platform}",
              f"平台 {platform} 回调验签已跳过（callback_verify=none），伪造推送可直接写入会话；"
              f"仅限本地演示，上线前请把 platforms.{platform}.callback_verify 改回 sign")
    return not get_settings().is_production()


def doc_hint(platform: str, key: str) -> str:
    return (f"请在 docs/平台接入.md {_DOC_SECTION.get(platform, '第 7 节 checklist')} "
            f"核对后填入 platforms.{platform}.{key}")


def filled(c: dict, key: str) -> bool:
    return bool(str(c.get(key, "") or "").strip())


def public_base_url() -> str:
    """买家点附件链接时的落点；留空意味着平台出站只能给出相对路径。"""
    return str(get_settings().get_dotted("server.public_base_url", "") or "").strip()


def is_absolute(url: str) -> bool:
    return url.startswith(("http://", "https://"))


def unreachable_attachment(platform: str, url: str) -> bool:
    """真实外发 + 相对链接 = 买家收到一条打不开的死链（缺 server.public_base_url）。"""
    return not is_mock(platform) and bool(url) and not is_absolute(url)


def mark_unreachable_media(platform: str, text: str) -> str:
    """真实外发的正文里，把相对附件链接替换成可读提示；绝对链接与 mock 出站原样保留。

    `media_url.absolute_url()` 在没配 `server.public_base_url` 时只能给出
    `/api/files/…?exp=…&sig=…`：它在平台客户端里没有主机名，点了什么也不会发生。
    与其发死链，不如在正文里写明"附件需在客服工作台查看"。
    """
    if is_mock(platform) or not _RELATIVE_MEDIA_RE.search(text or ""):
        return text
    return _RELATIVE_MEDIA_RE.sub(NO_PUBLIC_BASE_HINT, text)


def outbox_records(platform: str, limit: int = 50) -> list[dict]:
    """该渠道最近的外发记录（时间正序，形状与旧内存记录一致）。"""
    return outbox.recent(platform, limit)


def record_out(platform: str, *, kind: str, to: str, text: str, api: str,
               status: str, error: str | None = None, extra: dict | None = None) -> dict:
    """落一条外发记录。extra 是**重试要用的参数**（shop_id/url/name/media_type）：
    没有它们，outbound_retry 就只能重发文本，媒体行永远缺地址可重试。"""
    rec = {"platform": platform, "to": to, "text": text, "api": api,
           "mock": status == "mocked"}
    if error:
        rec["error"] = error
    if extra:
        rec.update(extra)
    return outbox.record(platform, kind, to=to, body=text, payload=rec,
                         status=status, error=error)


def missing_out_config(platform: str, c: dict, *, method_key: str = "reply_method") -> str:
    """真实外呼前的静态检查：返回缺失配置项（顿号拼接），齐全则空串。"""
    adapter = get_adapter(platform)
    token_keys = tuple(adapter.token_keys) if adapter else ("access_token",)
    missing = [method_key] if not filled(c, method_key) else []
    missing += [k for k in ("app_key", "app_secret") if not filled(c, k)]
    if not any(filled(c, k) for k in token_keys):
        missing.append(token_keys[0])
    return "、".join(missing)


def skip_notice(platform: str, *, kind: str, to: str, text: str, api: str, missing: str,
                extra: dict | None = None) -> dict:
    """未配置不等于发送成功：落一条 skipped，让外发箱与 /readiness 都能解释"为什么没发"。"""
    first = missing.split("、")[0]
    error = f"平台 {platform} 未真实外发：缺 {missing}；{doc_hint(platform, first)}"
    logger.warning(error)
    return record_out(platform, kind=kind, to=to, text=text, api=api,
                      status="skipped", error=error, extra=extra)


def skip_unreachable_media(platform: str, *, to: str, text: str, api: str, url: str,
                           extra: dict | None = None) -> dict:
    """附件链接是相对路径时不打网关：买家点不开，平台也只会回一个无意义的参数错误。

    口径与 skip_notice 一致——status=skipped，error 指名该补的配置项（server 节而非
    platforms 节，这里刻意不走 doc_hint，免得把运维指向 platforms.<name>.public_base_url）。
    """
    error = (f"平台 {platform} 未真实外发附件：链接 {url} 是相对路径，买家点不开；"
             "请在 server.public_base_url 填公网可达地址（末尾不带 /，见 docs/平台接入.md 第 7 节）")
    logger.warning(error)
    return record_out(platform, kind="media", to=to, text=text, api=api,
                      status="skipped", error=error, extra=extra)


def guarded_send(platform: str, *, kind: str, to: str, text: str, api: str,
                 missing: str, real: Callable[[], dict],
                 extra: dict | None = None) -> dict:
    """mock / skipped / sent / failed 四态统一收口：每条出站都留痕。

    只有真实外呼异常才继续抛出，配置没齐（skipped）按"不影响会话落库"的既有语义返回。
    failed 行由 outbox.record 直接排进退避重试队列（见 services/platforms/outbound_retry.py）。
    """
    if is_mock(platform):
        return record_out(platform, kind=kind, to=to, text=text, api=api, status="mocked",
                          extra=extra)
    if missing:
        return skip_notice(platform, kind=kind, to=to, text=text, api=api,
                           missing=missing, extra=extra)
    try:
        result = real()
    except Exception as e:
        record_out(platform, kind=kind, to=to, text=text, api=api, status="failed",
                   error=str(e), extra=extra)
        logger.error("平台 {} {} 外发失败: {}", platform, kind, e)
        raise
    rec = record_out(platform, kind=kind, to=to, text=text, api=api, status="sent",
                     extra=extra)
    # 平台网关返回的是它自己的报文（没有 status 字段），调用方（deliver_notice /
    # deliver_media / handle_inbound）统一按 status 判断是否真的送达——不合并的话，
    # 真实发送成功的回复会被读成"未送达"，运营台看到的外发状态也会错。
    return {**(result if isinstance(result, dict) else {"result": result}), **rec}


# ---- 出站/入站就绪度自检（B2-1）：纯静态，不发任何网络请求 ----

def _item(platform: str, key: str, label: str, chain: str, present: bool, *,
          required: bool = True, secret: bool = False, note: str = "") -> dict:
    # needs_review：docs/平台接入.md 里标 ★ 的口径项——值"配了"不等于"配对了"，
    # 平台文档随版本变，必须由人对着官方文档核一遍（设置页据此打"需要人工核对"徽标）。
    return {"key": key, "path": f"platforms.{platform}.{key}", "label": label,
            "chain": chain, "required": required, "present": present,
            "secret": secret, "note": note,
            "needs_review": key in _NEEDS_REVIEW.get(platform, set())}


def _split_missing(missing: str) -> list[str]:
    return [k for k in missing.split("、") if k]


def readiness(platform: str) -> dict:
    """单平台就绪度清单：每项是否已配、影响哪条链路，以及 ready_in/ready_out。

    没有真实凭据是预期状态，所以这里只报告"缺了会怎样"，不判定为错误。
    """
    adapter = get_adapter(platform)
    c = conf(platform)
    token_keys = tuple(adapter.token_keys) if adapter else ("access_token", "session_key")
    enabled = bool(c.get("enabled", False))
    mock = is_mock(platform)
    secret_ok = filled(c, "app_secret")
    verify = str(c.get("callback_verify", "sign") or "sign").strip().lower()

    items = [
        _item(platform, "enabled", "渠道总闸：false 时回调一律 409，买家消息进不来",
              "in", enabled, required=False),
        _item(platform, "mock", "演示模式：true 时验签放行、回复只落外发箱",
              "out", "mock" in c, required=False),
        _item(platform, "gateway", "消息发送网关地址", "out", filled(c, "gateway"),
              required=False, note="留空时按代码内置网关地址"),
        _item(platform, "reply_method", "客服消息发送 API 方法名：缺它就无法给买家回消息",
              "out", filled(c, "reply_method")),
        _item(platform, "app_key", "平台应用凭证", "out", filled(c, "app_key"), secret=True),
        _item(platform, "app_secret", "签名密钥：出站签名与回调验签共用",
              "out", secret_ok, secret=True, note="入站验签同样依赖"),
        _item(platform, token_keys[0], "卖家授权令牌", "out", filled(c, token_keys[0]),
              secret=True, required=len(token_keys) == 1,
              note="" if len(token_keys) == 1 else f"与 {'/'.join(token_keys)} 任一即可"),
        _item(platform, "callback_verify", "回调验签方式（sign / none）", "sign",
              filled(c, "callback_verify"), required=False,
              note="none 跳过验签，仅限本地调试"),
        _item(platform, "ack", "回调即时应答体", "in", "ack" in c, required=False),
        _item(platform, "to_uid_field", "回复参数中收件买家字段名", "out",
              filled(c, "to_uid_field"), required=False, note="留空用代码默认值"),
        _item(platform, "content_field", "回复参数中消息内容字段名", "out",
              filled(c, "content_field"), required=False, note="留空用代码默认值"),
        _item(platform, "media_method", "媒体消息发送 API：缺它时附件只能降级为 [附件] 链接文本",
              "media", filled(c, "media_method"), required=False),
        _item(platform, "media_url_field", "媒体参数中图片/文件地址字段名", "media",
              filled(c, "media_url_field"), required=False, note="留空用代码默认值"),
        _item(platform, "media_type_field", "媒体参数中消息类型字段名", "media",
              filled(c, "media_type_field"), required=False, note="留空用代码默认值"),
    ]
    if len(token_keys) > 1:
        items.append(_item(platform, token_keys[1], "卖家授权令牌（历史字段名）", "out",
                           filled(c, token_keys[1]), secret=True, required=False,
                           note=f"与 {token_keys[0]} 二选一"))

    missing_out = _split_missing(missing_out_config(platform, c))
    missing_media = _split_missing(missing_out_config(platform, c, method_key="media_method"))
    missing_in: list[str] = []
    if not enabled:
        missing_in.append("enabled")
    if not mock and verify != "none" and not secret_ok:
        missing_in.append("app_secret")

    risks: list[dict] = []
    if verify == "none":
        risks.append({"key": "callback_verify", "level": "risk",
                      "message": "回调验签已跳过（callback_verify=none），任何人可伪造买家推送写库，"
                                 "上线前必须改回 sign"})
    if not mock and missing_out:
        risks.append({"key": missing_out[0], "level": "warn",
                      "message": f"真实模式已开但出站参数不齐（缺 {'、'.join(missing_out)}），"
                                 "买家回复会以 skipped 记录留在外发箱，不会真正送达"})
    # 真实模式下没配公网地址：附件链接只能是 /api/files/… 的相对路径（买家点不开）。
    # 出站正文会把它换成"附件需在客服工作台查看"的可读提示，但附件本身确实送不到。
    base_url = public_base_url()
    if enabled and not mock and not base_url:
        risks.append({"key": "public_base_url", "level": "risk",
                      "message": f"server.public_base_url 未配置：买家收到的附件链接是相对路径"
                                 f"（/api/files/…?exp=&sig=），在平台客户端里点不开；"
                                 f"媒体出站会记 skipped，正文降级为{NO_PUBLIC_BASE_HINT}，"
                                 f"要真的送达附件必须填公网可达地址（末尾不带 /）"})
    if mock:
        risks.append({"key": "mock", "level": "info",
                      "message": "mock=true：外发只记外发箱，买家不会真实收到（演示预期）"})

    return {"platform": platform, "enabled": enabled, "mock": mock,
            "callback_verify": verify, "gateway": str(c.get("gateway", "") or ""),
            "public_base_url": base_url,
            "ready_in": enabled and (mock or verify == "none" or secret_ok),
            "ready_out": mock or not missing_out,
            "ready_media": mock or not missing_media,
            "missing_in": missing_in, "missing_out": missing_out,
            "missing_media": missing_media,
            # ★ 待人工核对口径的字段全集（与是否已配无关：配了也要核）
            "needs_review_keys": [i["key"] for i in items if i["needs_review"]],
            "items": items, "risks": risks}


def md5_sign_top(params: dict[str, str], secret: str) -> str:
    """淘宝 TOP / 拼多多风格通用签名：MD5(secret + k1v1k2v2… + secret) 大写十六进制。

    （TOP 亦可 hmac-md5，由配置 sign_method 决定；此处按 md5 实现，核对文档。）
    """
    base = secret + "".join(f"{k}{params[k]}" for k in sorted(params)) + secret
    return hashlib.md5(base.encode("utf-8")).hexdigest().upper()


def hmac_sha256_sign(message: str, secret: str) -> str:
    """抖店开放平台签名：HMAC-SHA256(secret, sign_str).hexdigest()。"""
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"),
                    hashlib.sha256).hexdigest()


class Adapter:
    """平台适配器接口。

    入站侧（verify_signature/parse_message）与各平台的报文构造留给子类覆写；
    出站"留痕 + 未配置降级"的策略统一在 send_reply/send_media 里，子类只给 real 调用。
    """

    name: ClassVar[str] = ""
    # 能满足"卖家授权令牌"的配置键（任一即可）：淘宝历史字段名 session_key 也算
    # ClassVar：子类（_TopLike/Jd/Pdd）按平台报文差异整批覆写这些键名，
    # 声明成实例变量的话 mypy 会拒绝"类里覆盖实例属性"。
    token_keys: ClassVar[tuple[str, ...]] = ("access_token",)

    def verify_signature(self, query: dict[str, str], body: bytes,
                         headers: dict[str, str]) -> bool:
        raise NotImplementedError

    def parse_message(self, query: dict[str, str], body: bytes) -> InboundMessage | None:
        raise NotImplementedError

    def send_reply(self, shop_id: str, buyer_id: str, text: str) -> dict:
        c = conf(self.name)
        api = str(c.get("reply_method", "") or "")
        # 降级必须发生在构造 real 闭包**之前**：闭包里的 text 才是买家真正收到的正文，
        # 只在 guarded_send 的记录里改文案，等于"外发箱看着对、买家收到死链"。
        text = mark_unreachable_media(self.name, text)
        return guarded_send(self.name, kind="text", to=buyer_id or shop_id, text=text, api=api,
                            missing=missing_out_config(self.name, c),
                            extra=self._retry_params(shop_id, buyer_id),
                            real=lambda: self._post_reply(c, api, shop_id, buyer_id, text))

    def send_media(self, shop_id: str, buyer_id: str, url: str, name: str = "",
                   media_type: str = "file") -> dict:
        """媒体出站（B2-2）：按 platforms.<name>.media_method 决定走哪条外呼。

        media_method 未配置时不假装成功——落 skipped，body 保留 [附件] name: url 降级文本，
        买家侧至少还能看到链接（该文案由运营台正文一并带上，这里不重复外发一条文本）。
        真实模式下链接还是相对路径（没配 server.public_base_url）时同样落 skipped：
        买家点不开的东西不该发给平台，退化成一句"附件需在客服工作台查看"的正文提示。
        """
        c = conf(self.name)
        api = str(c.get("media_method", "") or "")
        fallback = mark_unreachable_media(self.name, f"[附件] {name or '文件'}: {url}")
        extra = {**self._retry_params(shop_id, buyer_id),
                 "url": url, "name": name, "media_type": media_type}
        missing = missing_out_config(self.name, c, method_key="media_method")
        # 先问"能不能调这条 API"，再问"调了买家能用吗"：两个都没配时优先报缺 media_method，
        # 否则运维会先去补公网地址，回头还是发不出去。
        if not missing and unreachable_attachment(self.name, url):
            return skip_unreachable_media(self.name, to=buyer_id or shop_id, text=fallback,
                                          api=api, url=url, extra=extra)
        return guarded_send(self.name, kind="media", to=buyer_id or shop_id, text=fallback,
                            api=api,
                            missing=missing,
                            extra=extra,
                            real=lambda: self._post_media(c, api, shop_id, buyer_id, url,
                                                          media_type))

    def _retry_params(self, shop_id: str, buyer_id: str) -> dict:
        """重试一次外呼所需、但 OutboxRecord 现有列装不下的参数（留在 payload 里）。"""
        return {"shop_id": shop_id, "buyer_id": buyer_id}

    def call_reply(self, shop_id: str, buyer_id: str, text: str) -> dict:
        """只外呼、不写外发箱：重试执行器用（结果写回原行，见 outbound_retry.retry_once）。

        走 send_reply 会每重试一次就多一条外发记录，死信数与告警都会被自己制造的行污染。
        """
        c = conf(self.name)
        return self._post_reply(c, str(c.get("reply_method", "") or ""), shop_id, buyer_id, text)

    def call_media(self, shop_id: str, buyer_id: str, url: str,
                   media_type: str = "file") -> dict:
        c = conf(self.name)
        return self._post_media(c, str(c.get("media_method", "") or ""),
                                shop_id, buyer_id, url, media_type)

    def token(self, c: dict) -> str:
        for key in self.token_keys:
            if filled(c, key):
                return str(c[key])
        return ""

    def _post_reply(self, c: dict, method: str, shop_id: str, buyer_id: str, text: str) -> dict:
        raise NotImplementedError

    def _post_media(self, c: dict, method: str, shop_id: str, buyer_id: str, url: str,
                    media_type: str) -> dict:
        raise NotImplementedError


ADAPTERS: dict[str, Adapter] = {}


def register(adapter: Adapter) -> None:
    ADAPTERS[adapter.name] = adapter


def get_adapter(platform: str) -> Adapter | None:
    return ADAPTERS.get(platform)


# ---- 各平台回复 API 的通用 HTTP 骨架（子类给出 url/payload/sign 三要素） ----

def http_post_json(url: str, payload: dict, headers: dict[str, str] | None = None) -> dict:
    import httpx

    resp = httpx.post(url, json=payload, headers=headers or {}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def http_post_form(url: str, form: dict[str, str]) -> dict:
    import httpx

    resp = httpx.post(url, data=form, timeout=15)
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text[:500]}


# 平台回调统一要求返回的 ack 由 api 层给出；这里暴露可注入的发送函数供测试替换
Sender = Callable[[str, str, str], dict]
