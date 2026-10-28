"""会话满意度回访（B3-13，免 JWT 的公开入口，挂 /api 下）。

买家侧没有登录态，所以这一组路由不要求 Authorization——防线是链接本身：
`?conv=<会话>&exp=<过期时间戳>&sig=<HMAC>`（app/utils/feedback_url.py）。
签名只授权"给这一个会话投一票"，`SessionRating.conversation_id` 唯一约束保证
重复提交不会覆盖第一票。

页面刻意不写 JavaScript：一个 form POST + 五个 submit 按钮，既少一层攻击面，
也让"平台内置浏览器打不开脚本页"这类环境问题不会毁掉采集。
"""
from __future__ import annotations

from html import escape

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlmodel import Session, select

from app.db.models import Conversation, SessionRating
from app.db.session import engine
from app.utils import feedback_url

router = APIRouter(prefix="/feedback", tags=["feedback"])

_MIN_SCORE, _MAX_SCORE = 1, 5
_COMMENT_LIMIT = 500

_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body{{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
  margin:0;padding:28px 20px;background:#f5f7fa;color:#1f2937}}
.card{{max-width:420px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;
  border-radius:12px;padding:22px}}
h1{{font-size:17px;margin:0 0 6px}}
p{{font-size:13px;color:#6b7280;margin:0 0 16px;line-height:1.6}}
textarea{{width:100%;box-sizing:border-box;border:1px solid #d1d5db;border-radius:8px;
  padding:8px;font-size:13px;min-height:60px;margin-bottom:12px}}
.btns{{display:flex;gap:8px;flex-wrap:wrap}}
button{{flex:1 1 60px;padding:10px 0;border:1px solid #1b7ce4;background:#fff;
  color:#1b7ce4;border-radius:8px;font-size:14px;cursor:pointer}}
button:hover{{background:#1b7ce4;color:#fff}}
.done{{font-size:15px;color:#16a34a}}
.err{{font-size:14px;color:#dc2626}}
</style></head><body><div class="card">{body}</div></body></html>
"""


def _valid(conv_id: int, exp: str | None, sig: str | None) -> bool:
    return feedback_url.verify_link(conv_id, exp, sig)


def _existing_rating(session: Session, conversation_id: int) -> SessionRating | None:
    return session.exec(select(SessionRating)
                        .where(SessionRating.conversation_id == conversation_id)).first()


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=escape(title), body=body), status_code=status)


@router.get("")
def survey_page(conv: int = Query(...), exp: str = Query(""), sig: str = Query("")):
    """回访页：签名有效就给出 1-5 分的选择表单。"""
    if not _valid(conv, exp, sig):
        return _page("链接无效", '<p class="err">这个评价链接无效或已过期，'
                                 "如需帮助请重新联系客服。</p>", 403)
    with Session(engine) as session:
        rated = _existing_rating(session, conv)
    if rated:
        return _page("已收到评价", '<p class="done">感谢您的评价，我们已经收到了。</p>')
    body = (
        "<h1>本次服务还满意吗？</h1>"
        "<p>1 分＝很不满意，5 分＝非常满意。可选填一句建议。</p>"
        f'<form method="post" action="/api/feedback/rate">'
        f'<input type="hidden" name="conv" value="{conv}">'
        f'<input type="hidden" name="exp" value="{escape(exp)}">'
        f'<input type="hidden" name="sig" value="{escape(sig)}">'
        '<p><textarea name="comment" maxlength="500" placeholder="可选：哪里做得不好"></textarea></p>'
        '<div class="btns">'
        + "".join(f'<button type="submit" name="score" value="{i}">{i} 分</button>'
                  for i in range(_MIN_SCORE, _MAX_SCORE + 1))
        + "</div></form>")
    return _page("服务评价", body)


@router.post("/rate")
def submit_rate(request: Request, conv: int = Form(...), exp: str = Form(""),
                sig: str = Form(""), score: int = Form(...), comment: str = Form("")):
    """提交评分：签名 + 有效期 + 会话存在 + 只允许一票。"""
    if not _valid(conv, exp or None, sig or None):
        return JSONResponse({"detail": "评价链接无效或已过期"}, status_code=403)
    if not (_MIN_SCORE <= score <= _MAX_SCORE):
        return JSONResponse({"detail": f"评分必须是 {_MIN_SCORE}-{_MAX_SCORE}"}, status_code=422)
    comment = (comment or "").strip()[:_COMMENT_LIMIT]
    with Session(engine) as session:
        if session.get(Conversation, conv) is None:
            return JSONResponse({"detail": "会话不存在"}, status_code=404)
        if _existing_rating(session, conv) is not None:
            return JSONResponse({"detail": "该会话已评价过"}, status_code=409)
        session.add(SessionRating(conversation_id=conv, rating=score,
                                  comment=comment, source="survey_link"))
        session.commit()
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        return _page("已收到评价", '<p class="done">感谢您的评价，我们已经收到了。</p>')
    return JSONResponse({"ok": True, "rating": score})
