"""企微回调消息加解密（官方 WXBizMsgCrypt 协议的精简实现）。

算法：AES-256-CBC，密钥 = Base64(encoding_aes_key + '=')，IV = 密钥前 16 字节；
明文结构 = random(16B) + msg_len(4B 网络序) + msg + receive_id，PKCS7(块32) 填充。
签名 = sha1(sorted([token, timestamp, nonce, encrypt]))。
"""
from __future__ import annotations

import base64
import hashlib
import struct
import uuid

from Crypto.Cipher import AES
from fastapi import HTTPException


class WecomCrypto:
    def __init__(self, token: str, encoding_aes_key: str, corp_id: str):
        self.token = token
        self.corp_id = corp_id
        self.key = base64.b64decode(encoding_aes_key + "=")

    # ---- 签名 ----
    def _signature(self, timestamp: str, nonce: str, encrypt: str) -> str:
        items = sorted([self.token, timestamp, nonce, encrypt])
        return hashlib.sha1("".join(items).encode()).hexdigest()

    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        """GET 验证：核对签名后解密 echostr 原样返回明文。"""
        if self._signature(timestamp, nonce, echostr) != msg_signature:
            raise HTTPException(400, "回调签名校验失败")
        return self.decrypt(echostr)

    def verify_body(self, msg_signature: str, timestamp: str, nonce: str, encrypt: str) -> str:
        if self._signature(timestamp, nonce, encrypt) != msg_signature:
            raise HTTPException(400, "回调签名校验失败")
        return self.decrypt(encrypt)

    # ---- AES ----
    def decrypt(self, ciphertext_b64: str) -> str:
        cipher = AES.new(self.key, AES.MODE_CBC, self.key[:16])
        plain = cipher.decrypt(base64.b64decode(ciphertext_b64))
        plain = plain[:-plain[-1]]  # 去 PKCS7 填充（企微块大小为 32）
        content = plain[16:]
        msg_len = struct.unpack("!I", content[:4])[0]
        msg = content[4:4 + msg_len].decode("utf-8")
        receive_id = content[4 + msg_len:].decode("utf-8")
        if receive_id != self.corp_id:
            raise HTTPException(400, "receive_id 与 corp_id 不匹配")
        return msg

    def encrypt(self, plaintext: str) -> str:
        raw = plaintext.encode("utf-8")
        msg = (uuid.uuid4().hex[:16].encode()
               + struct.pack("!I", len(raw)) + raw + self.corp_id.encode())
        pad = 32 - len(msg) % 32
        msg += bytes([pad]) * pad
        cipher = AES.new(self.key, AES.MODE_CBC, self.key[:16])
        return base64.b64encode(cipher.encrypt(msg)).decode()


def client_ip(request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else ""))
