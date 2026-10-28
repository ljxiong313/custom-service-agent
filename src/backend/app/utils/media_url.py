"""媒体文件的签名访问 URL（安全整改：/api/files 原来是无鉴权静态挂载）。

口径：
- 库里（Message.meta_json 的 attachments）只存裸路径 `/api/files/<文件名>`，
  读取接口返回前才签名，这样改密钥/改 TTL 不需要回写历史数据；
- 签名 = HMAC-SHA256(密钥, "文件名|过期时间戳")，密钥取 `security.media_url_secret`，
  留空则回落 `security.jwt_secret`；
- 买家在平台客户端里点开的链接不可能带我们的 Bearer token，所以平台外发也走同一套
  签名 URL（拼成绝对地址，见 absolute_url）——签名即临时访问凭证。
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from pathlib import Path

from app.config import BACKEND_ROOT, get_settings

MEDIA_URL_PREFIX = "/api/files/"

# 服务端落盘用的文件名白名单：uuid16 + 可选短后缀。上传时就按这个规则生成，
# 读取时再校验一次，双保险挡掉手写的 ../ 路径穿越。
_FNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")

# 运营可上传/可内联预览的类型 → Content-Type。没有 html/svg/js/xml：这些能在浏览器里
# 执行脚本，用本站 origin 打开就是存储型 XSS；也没有可执行后缀。
MEDIA_TYPES = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
    "pdf": "application/pdf",
    "txt": "text/plain; charset=utf-8", "md": "text/plain; charset=utf-8",
    "csv": "text/plain; charset=utf-8", "log": "text/plain; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "zip": "application/zip",
}
ALLOWED_MEDIA_EXT = frozenset(MEDIA_TYPES)


def media_type_for(fname: str) -> str:
    """按后缀给 Content-Type；未知后缀一律 octet-stream（不交给浏览器解释）。"""
    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    return MEDIA_TYPES.get(ext, "application/octet-stream")


def _secret() -> str:
    s = get_settings()
    return str(s.get_dotted("security.media_url_secret") or s.get_dotted("security.jwt_secret") or "")


def _ttl() -> int:
    return int(get_settings().get_dotted("security.media_url_ttl_seconds", 86400))


def fname_of(url: str) -> str:
    """从 /api/files/<fname>[?...] 或裸文件名里取出文件名（去掉查询串与路径）。"""
    tail = url.split("?", 1)[0]
    if MEDIA_URL_PREFIX in tail:
        tail = tail.split(MEDIA_URL_PREFIX, 1)[1]
    return tail.rsplit("/", 1)[-1]


def is_safe_fname(fname: str) -> bool:
    return bool(_FNAME_RE.fullmatch(fname)) and ".." not in fname and not fname.startswith(".")


def _sign(fname: str, exp: int) -> str:
    msg = f"{fname}|{exp}".encode()
    return hmac.new(_secret().encode("utf-8"), msg, hashlib.sha256).hexdigest()[:32]


def sign_url(url: str, ttl: int | None = None) -> str:
    """给 /api/files 链接追加 ?exp=&sig=；非媒体链接原样返回。"""
    if MEDIA_URL_PREFIX not in url:
        return url
    fname = fname_of(url)
    if not is_safe_fname(fname):
        return url
    base = url.split("?", 1)[0]
    exp = int(time.time()) + (ttl if ttl is not None else _ttl())
    return f"{base}?exp={exp}&sig={_sign(fname, exp)}"


def bare_url(url: str) -> str:
    """落库前的规范化：去掉签名查询串，只保留 `/api/files/<文件名>`。

    前端把上传接口返回的（已签名）url 原样回传，直接入库会让历史消息里的链接
    跟着 exp 一起过期；签名只在读取时现算（见 sign_url），所以存储形态必须是裸路径。
    """
    if MEDIA_URL_PREFIX not in url:
        return url
    return MEDIA_URL_PREFIX + fname_of(url)


def verify(fname: str, exp: str | None, sig: str | None) -> bool:
    """校验签名与有效期；文件名非法、密钥未配置、过期、不匹配都拒绝。"""
    if not is_safe_fname(fname) or not _secret():
        return False
    try:
        exp_int = int(str(exp))
    except (TypeError, ValueError):
        return False
    if exp_int < int(time.time()):
        return False
    if not sig:
        return False
    return hmac.compare_digest(sig, _sign(fname, exp_int))


def media_root() -> Path:
    return BACKEND_ROOT / "data" / "uploads" / "media"


def absolute_url(url: str) -> str:
    """把签名后的相对链接补成公网绝对地址（平台买家点得开的前提）。

    `server.public_base_url` 留空时按原样返回相对路径：演示形态没有公网域名，
    此时平台出站正文里会带上可读的缺配置提示（见 platforms/base.py 的就绪自检）。
    """
    signed = sign_url(url)
    base = str(get_settings().get_dotted("server.public_base_url", "") or "").rstrip("/")
    if not base or signed.startswith("http"):
        return signed
    return f"{base}{signed}"
