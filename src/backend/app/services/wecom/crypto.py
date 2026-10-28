"""企微回调消息加解密（官方 WXBizMsgCrypt 协议的精简实现）。

官方口径（《加解密方案说明》，developer.work.weixin.qq.com/document/path/90968）：
- AESKey = Base64Decode(EncodingAESKey + "=")，长度固定 32 字节；
- AES-256-CBC，IV = AESKey 前 16 字节，数据按 **PKCS7 填充到 32 的倍数**（不是 16）；
- 明文字符串 = random(16B) + msg_len(4B 网络字节序) + msg + receive_id；
- msg_signature = sha1(sorted([token, timestamp, nonce, msgencrypt]) 直接拼接)。

分层口径：**"口径计算"全在本模块的模块级纯函数里**（aes_key_from / signature / pad /
unpad / pack_plain / unpack_plain / encrypt_b64 / decrypt_b64 / extract_encrypt），
它们不碰网络、不碰数据库、不依赖 FastAPI，参数不合法一律抛 ValueError；
`WecomCrypto` 只是把配置 + 这些纯函数粘起来，并把 ValueError 翻译成 HTTP 400。
这样官方公开文档里的那组固定测试向量可以直接对纯函数断言
（见 tests/test_wecom_crypto_vectors.py），不必先造一个 WecomCrypto 才能算签名。

诚实声明：以上"实现与官方文档给的样例报文自洽"，**不等于与企微服务器联调过**——
本地没有真实 corp_id/token，回调 URL 验证与消息接收的真实回路仍未端到端验证过
（结论见 docs/待办与风险.md）。签名/长度/填充这类口径改错了，测试会红；
平台侧还可能有文档没写到的行为，接入前请按官方库对一遍。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import struct
import uuid
import xml.etree.ElementTree as ET

from Crypto.Cipher import AES
from fastapi import HTTPException

# 企微的 PKCS7 分组是 32 字节（不是 AES 的 16），填错一截尾部会被对端判成非法报文
BLOCK_SIZE = 32
RANDOM_PREFIX_LEN = 16
ENCODING_AES_KEY_LEN = 43


class WeComCryptoError(ValueError):
    """口径层面的失败（密钥不合法、填充错、长度越界、receive_id 不匹配）。

    继承 ValueError：调用方（HTTP 层）只需区分"这不是我们的报文"和"代码 bug"。
    """


# ---- 密钥与签名 ----

def aes_key_from(encoding_aes_key: str) -> bytes:
    """AESKey = Base64Decode(EncodingAESKey + '=')，并校验长度必须是 32 字节。"""
    if len(encoding_aes_key or "") != ENCODING_AES_KEY_LEN:
        raise WeComCryptoError(
            f"EncodingAESKey 长度必须是 {ENCODING_AES_KEY_LEN} 位，当前 {len(encoding_aes_key or '')}")
    try:
        key = base64.b64decode(encoding_aes_key + "=", validate=True)
    except (binascii.Error, ValueError) as e:
        raise WeComCryptoError("EncodingAESKey 不是合法的 Base64 字符集") from e
    if len(key) != 32:
        raise WeComCryptoError(f"EncodingAESKey 解出的 AESKey 应为 32 字节，实际 {len(key)}")
    return key


def signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """msg_signature = sha1(四要素按字典序排序后**无分隔拼接**)。

    钉住两个易错点：签名对象是 base64 密文字符串本身（不是解码后的字节），
    且比较的是 str，绝不能把 bytes 混进 sorted()（会在比较时 TypeError）。
    """
    return hashlib.sha1("".join(sorted([token, timestamp, nonce, encrypt])).encode("utf-8")).hexdigest()


def verify_signature(expected: str, actual: str) -> bool:
    """定长比较（防时序侧信道）；两边都归一化成小写十六进制。"""
    return bool(expected) and bool(actual) and hmac.compare_digest(expected.lower(), actual.lower())


# ---- 明文包：random(16) + len(4) + msg + receive_id，PKCS7(32) ----

def pad(data: bytes, block_size: int = BLOCK_SIZE) -> bytes:
    pad_len = block_size - len(data) % block_size
    return data + bytes([pad_len]) * pad_len


def unpad(data: bytes, block_size: int = BLOCK_SIZE) -> bytes:
    """校验式去填充：填充字节必须在 1..block_size 且尾部全部一致，否则报文被动过。"""
    if not data:
        raise WeComCryptoError("解密后的数据为空")
    pad_len = data[-1]
    if not 1 <= pad_len <= block_size or data[-pad_len:] != bytes([pad_len]) * pad_len:
        raise WeComCryptoError("PKCS7 填充不合法（报文不是企微口径或已被截断）")
    return data[:-pad_len]


def pack_plain(msg: str, receive_id: str, *, random_prefix: bytes) -> bytes:
    """按官方结构拼出**已填充**的明文包（纯函数：随机段由调用方给定，测试可复算）。"""
    if len(random_prefix) != RANDOM_PREFIX_LEN:
        raise WeComCryptoError(f"随机段必须是 {RANDOM_PREFIX_LEN} 字节")
    raw = msg.encode("utf-8")
    return pad(random_prefix + struct.pack("!I", len(raw)) + raw + receive_id.encode("utf-8"))


def unpack_plain(data: bytes, *, receive_id: str | None = None) -> tuple[str, str]:
    """去填充 → 剥 16 字节随机段与 4 字节长度 → 返回 (msg, receive_id)。

    receive_id 传入时做校验（企微用它区分"是不是发给我的"：自建应用是 corp_id，
    服务商场景是 suite_id/corp_id，别在这里写死语义）。
    """
    content = unpad(data)[RANDOM_PREFIX_LEN:]
    if len(content) < 4:
        raise WeComCryptoError("明文包长度不足，取不出 msg_len")
    msg_len = struct.unpack("!I", content[:4])[0]
    body = content[4:]
    if msg_len > len(body):
        raise WeComCryptoError(f"msg_len={msg_len} 超出包体 {len(body)} 字节")
    msg_bytes, tail = body[:msg_len], body[msg_len:]
    try:
        msg = msg_bytes.decode("utf-8")
        found_id = tail.decode("utf-8")
    except UnicodeDecodeError as e:
        raise WeComCryptoError("明文包不是 UTF-8（密钥或报文被换错过）") from e
    if receive_id is not None and found_id != receive_id:
        raise WeComCryptoError(f"receive_id 与 corp_id 不匹配：{found_id!r} != {receive_id!r}")
    return msg, found_id


# ---- AES ----

def encrypt_b64(key: bytes, packed: bytes) -> str:
    """AES-256-CBC(IV=key[:16]) 后 Base64；输入必须是已填充的包（长度 32 的倍数）。"""
    if len(packed) % BLOCK_SIZE:
        raise WeComCryptoError("待加密数据未按 32 字节填充")
    cipher = AES.new(key, AES.MODE_CBC, key[:16])
    return base64.b64encode(cipher.encrypt(packed)).decode("ascii")


def decrypt_b64(key: bytes, ciphertext_b64: str) -> bytes:
    """Base64 解码 + AES 解密，返回**未去填充**的原始字节。

    这里的 ValueError 全部收敛成 WeComCryptoError：回调是免 JWT 的外部入口，
    随手喂进来的垃圾报文应该得到 400，而不是带堆栈的 500。
    """
    try:
        raw = base64.b64decode(ciphertext_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise WeComCryptoError("密文不是合法 Base64") from e
    if len(raw) % 16 or not raw:
        raise WeComCryptoError("密文长度不是 AES 分组整数倍")
    try:
        return AES.new(key, AES.MODE_CBC, key[:16]).decrypt(raw)
    except ValueError as e:                     # pycryptodome 的对齐/密钥错误
        raise WeComCryptoError(f"AES 解密失败: {e}") from e


def extract_encrypt(post_data: str) -> str:
    """从回调 POST 的密文 XML 里取 <Encrypt> 文本；body 为空/XML 不合法/缺密文段都报错。

    原来这段逻辑散在回调路由里（ET.fromstring(...).findtext("Encrypt") or ""），
    坏报文会抛 ET.ParseError 变成 500；收敛成纯函数后统一成 WeComCryptoError → 400，
    也才能不脱开 HTTP 单独测口径。
    """
    xml_text = post_data if isinstance(post_data, str) else str(post_data)
    if not xml_text.strip():
        raise WeComCryptoError("回调 body 为空")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise WeComCryptoError("回调 body 不是合法 XML") from e
    encrypt = (root.findtext("Encrypt") or "").strip()
    if not encrypt:
        raise WeComCryptoError("回调 XML 缺少 <Encrypt> 密文段")
    return encrypt


class WecomCrypto:
    """配置 + 纯函数的粘合层：把 WeComCryptoError 翻成 HTTP 400（回调免 JWT，只回人话）。"""

    def __init__(self, token: str, encoding_aes_key: str, corp_id: str):
        self.token = token
        self.corp_id = corp_id
        try:
            self.key = aes_key_from(encoding_aes_key)
        except WeComCryptoError as e:
            raise HTTPException(400, f"企微回调配置不可用：{e}") from e

    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        """GET 验证：核对签名后解密 echostr 原样返回明文。"""
        self._check(msg_signature, timestamp, nonce, echostr)
        return self.decrypt(echostr)

    def verify_body(self, msg_signature: str, timestamp: str, nonce: str, encrypt: str) -> str:
        self._check(msg_signature, timestamp, nonce, encrypt)
        return self.decrypt(encrypt)

    def _check(self, msg_signature: str, timestamp: str, nonce: str, encrypt: str) -> None:
        if not verify_signature(signature(self.token, timestamp, nonce, encrypt), msg_signature):
            raise HTTPException(400, "回调签名校验失败")

    def decrypt(self, ciphertext_b64: str) -> str:
        try:
            msg, _ = unpack_plain(decrypt_b64(self.key, ciphertext_b64), receive_id=self.corp_id)
        except WeComCryptoError as e:
            raise HTTPException(400, f"回调解密失败：{e}") from e
        return msg

    def encrypt(self, plaintext: str, *, random_prefix: bytes | None = None) -> str:
        """加密成企微密文（Base64）。random_prefix 仅供测试注入固定随机段，生产留空。"""
        packed = pack_plain(plaintext, self.corp_id,
                            random_prefix=random_prefix or uuid.uuid4().hex[:RANDOM_PREFIX_LEN].encode())
        return encrypt_b64(self.key, packed)


def client_ip(request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else ""))
