"""四大电商平台适配器（P7）：taobao / jd / pdd / douyin。

真实签名列按各开放平台公开文档实现，但网关地址、系统参数与消息 API 方法名
会随平台版本变化——填入真实参数前，逐项核对 docs/平台接入.md 中标注 ★ 的项。

mock 模式（默认）：验签放行、回复落外发箱，用于无资质环境下的全链路演示/冒烟。
出站/媒体的"未配置怎么办"统一在 base.guarded_send 里，本模块只负责各平台报文差异。
"""
from __future__ import annotations

import hmac
import json
import time
from typing import ClassVar

from .base import (
    Adapter,
    InboundMessage,
    conf,
    guard_none_verify,
    hmac_sha256_sign,
    http_post_form,
    http_post_json,
    is_mock,
    md5_sign_top,
    register,
)


def _extract(payload: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        v = payload.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


class _TopLike(Adapter):
    """淘宝 TOP / 京东 / 拼多多 同族网关：form 提交 + MD5(secret+排序拼串+secret) 签名。

    入站：mock 用固定 JSON；真实模式下淘宝走 TMC 长连接（非本 webhook，见文档），
    京东/拼多多的消息推送字段差异集中在 _parse 的子类覆写里。
    """

    # 子类覆写的字段名差异（平台推送 JSON 的键）
    buyer_keys: ClassVar[tuple[str, ...]] = ("buyer_id", "buyerOpenId", "uid")
    shop_keys: ClassVar[tuple[str, ...]] = ("shop_id", "shopId", "store_id")
    text_keys: ClassVar[tuple[str, ...]] = ("text", "content", "msg")
    token_keys: ClassVar[tuple[str, ...]] = ("access_token", "session_key")

    gateway = ""

    def verify_signature(self, query: dict[str, str], body: bytes,
                         headers: dict[str, str]) -> bool:
        if is_mock(self.name):
            return True
        mode = conf(self.name).get("callback_verify", "sign")
        if mode == "none":
            guard_none_verify(self.name)
            return True
        sign = query.get("sign", "")
        secret = conf(self.name).get("app_secret", "")
        params = {k: v for k, v in query.items() if k != "sign"}
        return bool(secret) and md5_sign_top(params, secret) == sign.upper()

    def parse_message(self, query: dict[str, str], body: bytes) -> InboundMessage | None:
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        payload = {k.lower() if isinstance(k, str) else k: v for k, v in payload.items()}
        buyer = _extract(payload, self.buyer_keys)
        text = _extract(payload, self.text_keys)
        if not buyer or not text:
            return None
        return InboundMessage(platform=self.name, shop_id=_extract(payload, self.shop_keys),
                              buyer_id=buyer, text=text[:4000],
                              external_msg_id=_extract(payload, ("msg_id", "msgid", "message_id")))

    def _post_reply(self, c: dict, method: str, shop_id: str, buyer_id: str, text: str) -> dict:
        params = self._system_params(c, method, self.token(c))
        params.update(self._reply_fields(c, shop_id, buyer_id, text))
        params["sign"] = md5_sign_top(params, c["app_secret"])
        return http_post_form(c.get("gateway") or self.gateway, params)

    def _post_media(self, c: dict, method: str, shop_id: str, buyer_id: str, url: str,
                    media_type: str) -> dict:
        params = self._system_params(c, method, self.token(c))
        params[c.get("to_uid_field") or "to_uid"] = buyer_id
        params[c.get("media_url_field") or "pic_url"] = url
        params[c.get("media_type_field") or "msg_type"] = media_type
        params["sign"] = md5_sign_top(params, c["app_secret"])
        return http_post_form(c.get("gateway") or self.gateway, params)

    def _system_params(self, c: dict, method: str, token: str) -> dict[str, str]:
        return {"method": method, "app_key": c["app_key"], "session": token,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "format": "json",
                "v": "2.0", "sign_method": "md5"}

    def _reply_fields(self, c: dict, shop_id: str, buyer_id: str, text: str) -> dict[str, str]:
        return {c.get("to_uid_field") or "to_uid": buyer_id,
                c.get("content_field") or "content": text}


class Taobao(_TopLike):
    """淘宝/天猫：入站真实模式为 TMC 消息服务（SDK 长连接），本 webhook 仅承接 mock/兼容推送。"""

    name = "taobao"
    gateway = "https://gw.api.taobao.com/router/rest"
    buyer_keys = ("buyer_id", "buyer_nick", "uid")


class Jd(_TopLike):
    """京东：京麦/宙斯开放网关，系统参数用 access_token 而非 session。"""

    name = "jd"
    gateway = "https://api.jd.com/routerjson"
    buyer_keys = ("buyer_pin", "buyer_id", "open_id")
    token_keys = ("access_token",)

    def _system_params(self, c, method, token):
        p = super()._system_params(c, method, token)
        p.pop("session")
        p["access_token"] = token
        return p


class Pdd(_TopLike):
    """拼多多：网关系统参数为 type/client_id，签名规则与 TOP 同族。"""

    name = "pdd"
    gateway = "https://gateway.pinduoduo.com/api"
    buyer_keys = ("uid", "buyer_uid", "buyer_id")
    token_keys = ("access_token",)

    def _system_params(self, c, method, token):
        return {"type": method, "client_id": c["app_key"],
                "access_token": token, "timestamp": str(int(time.time())),
                "response_type": "json"}


class Douyin(Adapter):
    """抖店（飞鸽客服）：JSON 网关 + HMAC-SHA256 签名 + param_json 排序拼串。"""

    name = "douyin"

    def verify_signature(self, query, body, headers):
        if is_mock(self.name):
            return True
        # 抖店推送验签口径多版本并存：appsecret 拼串 md5 或 header 签名，核对文档后选其一
        c = conf(self.name)
        if c.get("callback_verify", "sign") == "none":
            guard_none_verify(self.name)
            return True
        sign = headers.get("x-doudian-sign") or query.get("sign", "")
        secret = c.get("app_secret", "")
        expect = hmac_sha256_sign(query.get("timestamp", "") + body.decode("utf-8", "ignore"),
                                  secret) if secret else ""
        return expect != "" and hmac.compare_digest(sign.lower(), expect)

    def parse_message(self, query, body):
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        buyer = _extract(data, ("uid", "buyer_id", "sender_id", "open_id"))
        text = _extract(data, ("content", "text", "msg"))
        if not buyer or not text:
            return None
        return InboundMessage(platform=self.name,
                              shop_id=_extract(data, ("shop_id", "store_id")),
                              buyer_id=buyer, text=text[:4000],
                              external_msg_id=_extract(data, ("msg_id", "message_id")))

    def _call(self, c: dict, method: str, biz: dict) -> dict:
        param_json = json.dumps(biz, ensure_ascii=False, separators=(",", ":"))
        ts = str(int(time.time()))
        token = self.token(c)
        sign_src = f"{c['app_key']}{ts}{method}{param_json}{token}"
        payload = {"app_key": c["app_key"], "method": method, "timestamp": ts,
                   "access_token": token, "param_json": param_json,
                   "sign": hmac_sha256_sign(sign_src, c["app_secret"]), "sign_method": "hmac-sha256",
                   "v": "2"}
        url = (c.get("gateway") or "https://openapi.jinritemai.com/") + method
        return http_post_json(url, payload)

    def _biz_base(self, c: dict, shop_id: str, buyer_id: str) -> dict:
        biz = {c.get("to_uid_field") or "sender_uid": buyer_id}
        if shop_id:
            biz["shop_id"] = shop_id
        return biz

    def _post_reply(self, c: dict, method: str, shop_id: str, buyer_id: str, text: str) -> dict:
        biz = self._biz_base(c, shop_id, buyer_id)
        biz[c.get("content_field") or "content"] = text
        return self._call(c, method, biz)

    def _post_media(self, c: dict, method: str, shop_id: str, buyer_id: str, url: str,
                    media_type: str) -> dict:
        biz = self._biz_base(c, shop_id, buyer_id)
        biz[c.get("media_url_field") or "pic_url"] = url
        biz[c.get("media_type_field") or "msg_type"] = media_type
        return self._call(c, method, biz)


for _a in (Taobao(), Jd(), Pdd(), Douyin()):
    register(_a)
