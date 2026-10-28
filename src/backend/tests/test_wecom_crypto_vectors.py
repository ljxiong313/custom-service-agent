"""企微回调加解密的**固定测试向量**回归：钉住官方文档口径，防止实现漂移。

向量来源（可信度说明，诚实优先）：
`app/services/wecom/crypto.py` 里这几组常量逐字抄自企业微信开发者中心
《加解密方案说明》"举例说明"一节的官方样例（也即官方各语言算法库 Sample 用的那一组）：

    corpId(=receive_id)  wx5823bf96d3bd56c7
    token                QDG6eK
    EncodingAESKey       jWmYm7qr5nMoAUwZRjGtBxmz3KA1tkAj3ykkR6q2B2C
    timestamp            1409659813      nonce 1372623149
    msg_signature        477715d11cdb4164915debcba66cb864d751f3e6

它们**不是**我们从自己实现里导出来的"自洽值"：签名与解密结果都直接取官方文档给出的期望值，
本文件的断言是"实现算出来的 == 文档写的"。这一点在写用例时已用官方样例逐条复算过。

但请把结论限制在它能覆盖的范围里：
- 覆盖的是**口径**（四要素排序拼串 sha1、AESKey=Base64(43位+'=')、IV=密钥前 16 字节、
  random16+len4+msg+receiveid 包结构、PKCS7 按 32 填充、receive_id 校验）；
- **没有**覆盖与企微服务器的真实回路——本地没有可注册的 corp_id/token，回调 URL 验证、
  POST 推送、以及"平台改版后的实际行为"仍未端到端联调过。docs/待办与风险.md 里
  "企微真实回路未验证"的既有结论**不变**，本文件不推翻它。

密文侧的向量另有一类：`encrypt()` 的随机段可注入（random_prefix），用来和"按文档手工拼出来的
字节串"逐字节比对。那部分是**实现自洽性测试**（期望值来自文档描述的结构 + 手工构造，
不是企微服务器给过的报文），文件里逐条标注了性质。
"""
from __future__ import annotations

import base64
import struct

import pytest
from Crypto.Cipher import AES
from fastapi import HTTPException

from app.services.wecom import crypto

# ---- 官方文档《加解密方案说明》"举例说明"的样例值（逐字照抄，勿改成"更好看"的值）----
TOKEN = "QDG6eK"
ENCODING_AES_KEY = "jWmYm7qr5nMoAUwZRjGtBxmz3KA1tkAj3ykkR6q2B2C"
CORP_ID = "wx5823bf96d3bd56c7"
TIMESTAMP = "1409659813"
NONCE = "1372623149"
EXPECTED_SIGNATURE = "477715d11cdb4164915debcba66cb864d751f3e6"
MSG_ENCRYPT = (
    "RypEvHKD8QQKFhvQ6QleEB4J58tiPdvo+rtK1I9qca6aM/wvqnLSV5zEPeusUiX5L5X/0lWfrf0QADHHhGd3Qcz"
    "cdCUpj911L3vg3W/sYYvuJTs3TUUkSUXxaccAS0qhxchrRYt66wiSpGLYL42aM6A8dTT+6k4aSknmPj48kzJs8qLjvd4Xg"
    "pue06DOdnLxAUHzM6+kDZ+HMZfJYuR+LtwGc2hgf5gsijff0ekUNXZiqATP7PF5mZxZ3Izoun1s4zG4LUMnvw2r+KqCKIw"
    "+3IQH03v+BCA9nMELNqbSf6tiWSrXJB3LAVGUcallcrw8V2t9EL4EhzJWrQUax5wLVMNS0+rUPA3k22Ncx4XXZS9o0MBH2"
    "7Bo6BpNelZpS+/uh9KsNlY6bHCmJU9p8g7m3fVKn28H3KDYA5Pl/T8Z1ptDAVe0lXdQ2YoyyH2uyPIGHBZZIs2pDBS8R07"
    "+qN+E7Q=="
)
# 官方文档里这段密文解密后的明文（msg_len=284，尾部 receive_id 即 corpId）
EXPECTED_PLAIN = (
    "<xml><ToUserName><![CDATA[wx5823bf96d3bd56c7]]></ToUserName>\n"
    "<FromUserName><![CDATA[mycreate]]></FromUserName>\n"
    "<CreateTime>1409659813</CreateTime>\n"
    "<MsgType><![CDATA[text]]></MsgType>\n"
    "<Content><![CDATA[hello]]></Content>\n"
    "<MsgId>4561255354251345929</MsgId>\n"
    "<AgentID>218</AgentID>\n"
    "</xml>"
)

FIXED_PREFIX = b"0123456789abcdef"      # 注入用的 16 字节随机段（仅测试可复算）


# ---- 密钥推导：AESKey = Base64Decode(EncodingAESKey + "=")，IV = 前 16 字节 ----

def test_aes_key_derivation_matches_official_vector():
    key = crypto.aes_key_from(ENCODING_AES_KEY)
    assert len(key) == 32
    # 官方样例的 AESKey 逐字节期望值（十六进制）；IV 就是它的前 16 字节
    assert key.hex() == ("8d69989bbaabe67328014c194631ad07"
                         "19b3dca035b64023df292447aab60760")
    assert key[:16].hex() == "8d69989bbaabe67328014c194631ad07"


def test_aes_key_rejects_wrong_length_and_non_base64():
    """配置里塞错 EncodingAESKey 要在构造期就说人话，而不是在 b64decode 处抛裸异常。"""
    for bad in ("", "short", "K" * 42, "K" * 44, "!" * 43):
        with pytest.raises(crypto.WeComCryptoError):
            crypto.aes_key_from(bad)


# ---- 签名口径：sha1(sorted([token, timestamp, nonce, msgencrypt])) ----

def test_signature_matches_official_documented_value():
    """**官方向量**：文档同时给了 msgencrypt 与期望 msg_signature，可本地复算。"""
    assert crypto.signature(TOKEN, TIMESTAMP, NONCE, MSG_ENCRYPT) == EXPECTED_SIGNATURE
    assert crypto.verify_signature(EXPECTED_SIGNATURE, EXPECTED_SIGNATURE.upper()) is True


def test_signature_is_over_the_base64_encrypt_string_not_decoded_bytes():
    """B3-2 静态检查抓出的真实缺陷：早期实现先 b64decode 再签，
    既对不上官方向量，还会在 sorted() 里拿 bytes 和 str 比较直接 TypeError。
    """
    assert crypto.signature(TOKEN, TIMESTAMP, NONCE, MSG_ENCRYPT) == EXPECTED_SIGNATURE
    decoded = base64.b64decode(MSG_ENCRYPT)
    with pytest.raises(TypeError):
        crypto.signature(TOKEN, TIMESTAMP, NONCE, decoded)        # type: ignore[arg-type]
    # 万一 bytes 被"顺手解码成 str"，签名同样对不上官方向量——口径就是 base64 文本本身
    assert crypto.signature(TOKEN, TIMESTAMP, NONCE,
                            decoded.decode("latin-1")) != EXPECTED_SIGNATURE


def test_signature_covers_timestamp_and_nonce_so_replay_needs_reissue():
    """换时间戳/换随机串都必须让签名失效（企微重推时会重新签名，我们不额外做时效窗）。

    注意：本实现**不校验 timestamp 是否过期**——时间戳只参与签名，不参与"新鲜度"判断。
    这是有意的：平台重推（超时重试）会带旧时间戳，硬卡窗口会把合法重推判成攻击。
    防重放靠上层 InboundDedupe 那类幂等键，不靠这里。
    """
    other_ts = crypto.signature(TOKEN, "1409659814", NONCE, MSG_ENCRYPT)
    assert other_ts != EXPECTED_SIGNATURE
    assert crypto.verify_signature(EXPECTED_SIGNATURE, other_ts) is False


# ---- 解密：官方密文 → 官方明文 + receive_id 校验 ----

def test_decrypt_official_ciphertext_yields_official_plaintext():
    """**官方向量**：把文档给的 msgencrypt 解回文档给的明文 XML，长度与 receive_id 一并钉住。"""
    key = crypto.aes_key_from(ENCODING_AES_KEY)
    msg, receive_id = crypto.unpack_plain(crypto.decrypt_b64(key, MSG_ENCRYPT))
    assert msg == EXPECTED_PLAIN
    assert len(msg.encode("utf-8")) == 284          # 包头 msg_len 字段就是 284
    assert receive_id == CORP_ID
    # 带期望 receive_id 的入口同样通过（自建应用场景 receive_id == corp_id）
    assert crypto.unpack_plain(crypto.decrypt_b64(key, MSG_ENCRYPT),
                               receive_id=CORP_ID)[0] == EXPECTED_PLAIN


def test_decrypt_rejects_another_corps_receive_id():
    """密文是给别的企业的（receive_id 不匹配）必须拒绝，不能当成自己的消息处理。"""
    key = crypto.aes_key_from(ENCODING_AES_KEY)
    with pytest.raises(crypto.WeComCryptoError, match="receive_id"):
        crypto.unpack_plain(crypto.decrypt_b64(key, MSG_ENCRYPT), receive_id="ww_other_corp")


def test_decrypt_failure_is_a_coded_error_not_a_stack_trace():
    """回调免 JWT，垃圾报文只能得到 400：Base64 非法、长度不对齐都收敛成 WeComCryptoError。"""
    key = crypto.aes_key_from(ENCODING_AES_KEY)
    for junk in ("这不是 base64!!!", "", "A", base64.b64encode(b"not-aligned").decode()):
        with pytest.raises(crypto.WeComCryptoError):
            crypto.decrypt_b64(key, junk)


# ---- 明文包结构：random(16) + msg_len(4B 网络序) + msg + receive_id，PKCS7(32) ----

def test_pack_plain_matches_documented_byte_layout():
    """按文档逐字段核对包结构（自洽向量：期望值由文档描述手工拼出）。"""
    packed = crypto.pack_plain("123456789", CORP_ID, random_prefix=FIXED_PREFIX)
    assert len(packed) % crypto.BLOCK_SIZE == 0
    assert packed[:16] == FIXED_PREFIX
    assert packed[16:20] == struct.pack("!I", 9)               # 网络字节序（大端）
    assert packed[20:29] == b"123456789"
    assert packed[29:29 + len(CORP_ID)] == CORP_ID.encode()
    pad_len = 32 - (29 + len(CORP_ID)) % 32
    assert packed[29 + len(CORP_ID):] == bytes([pad_len]) * pad_len
    assert crypto.unpack_plain(packed) == ("123456789", CORP_ID)


def test_encrypt_matches_hand_built_official_algorithm():
    """**实现自洽性测试**：期望密文由测试自己按文档步骤（AES-CBC + 手工填充）拼出，
    与 crypto.encrypt() 的产物逐字节相等。它证明"我们的实现 = 文档写的步骤"，
    不代表这条报文来自企微服务器。
    """
    key = crypto.aes_key_from(ENCODING_AES_KEY)
    body = FIXED_PREFIX + struct.pack("!I", 5) + b"hello" + CORP_ID.encode()
    pad_len = 32 - len(body) % 32
    body = body + bytes([pad_len]) * pad_len
    hand_built = base64.b64encode(
        AES.new(key, AES.MODE_CBC, key[:16]).encrypt(body)).decode("ascii")

    made_by_us = crypto.WecomCrypto(TOKEN, ENCODING_AES_KEY, CORP_ID).encrypt(
        "hello", random_prefix=FIXED_PREFIX)
    assert made_by_us == hand_built
    # 随机段不注入时每次都不一样（16 字节随机段进密文），但不能影响解密结果
    other = crypto.WecomCrypto(TOKEN, ENCODING_AES_KEY, CORP_ID).encrypt("hello")
    assert other != made_by_us
    assert crypto.WecomCrypto(TOKEN, ENCODING_AES_KEY, CORP_ID).decrypt(other) == "hello"


def test_unpad_and_unpack_reject_malformed_frames():
    """填充不合法 / msg_len 越界 / 空包：都是"这不是我们的报文"，不能 IndexError 或静默通过。"""
    with pytest.raises(crypto.WeComCryptoError, match="填充"):
        crypto.unpad(b"\x00" * 32)                       # 尾字节 0 不是合法 PKCS7
    with pytest.raises(crypto.WeComCryptoError):
        crypto.unpad(b"")
    with pytest.raises(crypto.WeComCryptoError):
        crypto.unpad(b"abc")                             # 非分组整数倍
    frame = FIXED_PREFIX + struct.pack("!I", 9999) + b"short" + CORP_ID.encode()
    with pytest.raises(crypto.WeComCryptoError, match="msg_len"):
        crypto.unpack_plain(crypto.pad(frame))
    with pytest.raises(crypto.WeComCryptoError):
        crypto.pack_plain("x", CORP_ID, random_prefix=b"too-short")


def test_wecom_crypto_translates_errors_into_http_400():
    """HTTP 层只看 400：签名错、解密错、corp 不匹配都不该冒泡成 500。"""
    c = crypto.WecomCrypto(TOKEN, ENCODING_AES_KEY, CORP_ID)
    with pytest.raises(HTTPException) as exc:            # 签名不对
        c.verify_body("deadbeef", TIMESTAMP, NONCE, MSG_ENCRYPT)
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:            # 签名对但密文不是企微口径
        good = crypto.signature(TOKEN, TIMESTAMP, NONCE, "AAAAAAAA")
        c.verify_body(good, TIMESTAMP, NONCE, "AAAAAAAA")
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:            # 别人的 receive_id
        other = crypto.WecomCrypto(TOKEN, ENCODING_AES_KEY, "ww_other_corp")
        echo = other.encrypt("hi")
        c.verify_url(crypto.signature(TOKEN, TIMESTAMP, NONCE, echo), TIMESTAMP, NONCE, echo)
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException):                   # EncodingAESKey 配错在构造期就 400
        crypto.WecomCrypto(TOKEN, "K" * 10, CORP_ID)


def test_official_vector_passes_through_the_http_surface_of_the_class():
    """把官方向量整条走一遍类接口（验签 → 解密），证明重构没把口径搬丢。"""
    c = crypto.WecomCrypto(TOKEN, ENCODING_AES_KEY, CORP_ID)
    assert c.verify_body(EXPECTED_SIGNATURE, TIMESTAMP, NONCE, MSG_ENCRYPT) == EXPECTED_PLAIN


# ---- 密文 XML 取段（原来散在回调路由里的逻辑）----

def test_extract_encrypt_edges():
    head = MSG_ENCRYPT[:24]
    assert crypto.extract_encrypt(f"<xml><Encrypt>{head}</Encrypt></xml>") == head
    for bad in ("", "   ", "<xml></xml>", "完全不是 xml", "<xml><Encrypt>  </Encrypt></xml>"):
        with pytest.raises(crypto.WeComCryptoError):
            crypto.extract_encrypt(bad)
