// 时间显示统一入口：后端所有时间戳是 **naive UTC** 的 ISO 串（形如 2026-10-23T03:28:29，
// 没有 Z、没有时区偏移），直接 new Date(iso) 会被 JS 按本地时间解析，东八区会整整差 8 小时。
// 所以这里统一先补 Z 再解析，最后按浏览器本地时区显示；页面不得再各自实现格式化。
const FALLBACK = '—'
const TZ_RE = /(?:Z|[+-]\d{2}:?\d{2})$/

/**
 * iso 额外接受 number：/mcp/pending 的 created_at 是后端 time.time() 的秒级浮点，不是 ISO 串。
 */
function parse(iso: string | number | null | undefined): Date | null {
  if (iso === null || iso === undefined) return null
  if (typeof iso === 'number') {
    if (!Number.isFinite(iso)) return null
    const d = new Date(iso * 1000)
    return Number.isNaN(d.getTime()) ? null : d
  }
  const raw = String(iso).trim()
  if (!raw) return null
  const d = new Date(TZ_RE.test(raw) ? raw : `${raw}Z`)
  return Number.isNaN(d.getTime()) ? null : d
}

function pad(n: number) { return String(n).padStart(2, '0') }

/** 完整时间：本地时区的日期 + 时间；非法或空值返回「—」。 */
export function fmtDateTime(iso: string | number | null | undefined): string {
  const d = parse(iso)
  return d ? d.toLocaleString() : FALLBACK
}

/** 紧凑时间：MM/DD HH:mm（会话列表/表格这类窄列用）；非法或空值返回「—」。 */
export function fmtShort(iso: string | number | null | undefined): string {
  const d = parse(iso)
  if (!d) return FALLBACK
  return `${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}
