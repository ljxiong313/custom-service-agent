// MessageList：附件渲染（image→<img>、file→<a>，链接用接口给的签名 url）、
// Markdown 渲染路径的 XSS 防护、来源徽标与引用来源。
//
// 隔离手段：这个组件是纯 props 展示组件（不碰 pinia/router/fetch），
// 所以只挂 Element Plus 提供 el-tag；marked / dompurify 都用真实现，
// 这样「消毒是否真的发生在组件这一层」才是被测对象本身。
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { marked } from 'marked'

import MessageList from '@/components/MessageList.vue'
import type { ChatMessage } from '@/stores/console'

// 后端 app/utils/media_url.py::sign_url 出来的形状：过期时间 + 签名
const IMG_URL = '/api/files/8ac5d3e1.png?exp=1790000000&sig=3f3d9c1e77aa55bb'
const FILE_URL = '/api/files/%E8%AE%A2%E5%8D%95.pdf?exp=1790000000&sig=11dd22cc33bb44aa'

function assistant(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return { id: 1, role: 'assistant', content: '在的，帮您看下订单~', ...overrides }
}

function mountList(messages: ChatMessage[]) {
  return mount(MessageList, { props: { messages }, global: { plugins: [ElementPlus] } })
}

describe('MessageList：附件渲染', () => {
  it('type=image 渲染成 <img>，src 就是接口给的签名 url，alt 用文件名', () => {
    const wrapper = mountList([
      assistant({ attachments: [{ url: IMG_URL, name: '聊天截图.png', type: 'image' }] }),
    ])

    const img = wrapper.find('img.attach-img')
    expect(img.exists()).toBe(true)
    expect(img.attributes('src')).toBe(IMG_URL)
    expect(img.attributes('alt')).toBe('聊天截图.png')
    // 签名参数不能被前端改写/丢弃，否则 /api/files 的 ?sig= 校验过不去
    expect(img.attributes('src')).toContain('sig=')
    expect(img.attributes('src')).toContain('exp=')
    expect(wrapper.find('a.attach-file').exists()).toBe(false)
    wrapper.unmount()
  })

  it('type=file 渲染成 <a>，href 用签名 url 且 target=_blank 配 rel=noopener', () => {
    const wrapper = mountList([
      assistant({ attachments: [{ url: FILE_URL, name: '订单.pdf', type: 'file' }] }),
    ])

    const link = wrapper.find('a.attach-file')
    expect(link.exists()).toBe(true)
    expect(link.attributes('href')).toBe(FILE_URL)
    expect(link.attributes('target')).toBe('_blank')
    expect(link.attributes('rel')).toBe('noopener')
    expect(link.text()).toContain('订单.pdf')
    expect(wrapper.find('img.attach-img').exists()).toBe(false)
    wrapper.unmount()
  })

  it('同一消息里的图片与文件各按类型渲染（顺序按附件数组）', () => {
    const wrapper = mountList([
      assistant({
        attachments: [
          { url: IMG_URL, name: '图.png', type: 'image' },
          { url: FILE_URL, name: '文.pdf', type: 'file' },
        ],
      }),
    ])

    expect(wrapper.findAll('.attach-list > *')).toHaveLength(2)
    expect(wrapper.findAll('img.attach-img')).toHaveLength(1)
    expect(wrapper.findAll('a.attach-file')).toHaveLength(1)
    wrapper.unmount()
  })

  it('买家（role=user）消息的附件同样渲染，且不会因为纯文本消息产生空容器', () => {
    const withAttach = mountList([{ id: 2, role: 'user', content: '看下这个', attachments: [{ url: IMG_URL, name: 'a.png', type: 'image' }] }])
    expect(withAttach.find('img.attach-img').exists()).toBe(true)
    withAttach.unmount()

    const without = mountList([{ id: 3, role: 'user', content: '只有文字' }])
    expect(without.find('.attach-list').exists()).toBe(false)
    expect(without.find('.user-text').text()).toBe('只有文字')
    without.unmount()
  })
})

describe('MessageList：附件链接的协议不做校验（已知缺陷，见交付报告）', () => {
  it('现状记录：attachments 的 url 不经消毒直接进 href/src', () => {
    // 对比：正文走 DOMPurify（下面 Markdown 那组用例证明了），但附件链接是 Vue 属性绑定，
    // 没有协议白名单。后端 app/api/v1/console.py::Attachment 只收 str，
    // media_url.bare_url() 对不含 /api/files/ 的链接原样返回，
    // 于是 javascript: / data: 一路走到这里被渲染成可点击链接（点击即在本站 origin 执行）。
    // 这条断言是「把缺陷钉在测试里」，不是给它背书：修好之后它会变红，
    // 届时请把期望改成「非 /api/files/ 或 http(s) 的 url 被丢弃/不渲染」。
    const EVIL = 'javascript:window.__xss_attach=1'
    const wrapper = mountList([
      assistant({ content: '', attachments: [{ url: EVIL, name: 'a.pdf', type: 'file' }] }),
    ])

    expect(wrapper.find('a.attach-file').attributes('href')).toBe(EVIL)
    // 没点击就不执行，所以这里只能断言「可执行入口被保留了」
    expect((window as unknown as { __xss_attach?: number }).__xss_attach).toBeUndefined()
    wrapper.unmount()
  })

  it('现状记录：图片附件的 src 同样可以是任意外链（data: URI）', () => {
    const DATA = 'data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg=='
    const wrapper = mountList([
      assistant({ content: '', attachments: [{ url: DATA, name: 'x.png', type: 'image' }] }),
    ])

    expect(wrapper.find('img.attach-img').attributes('src')).toBe(DATA)
    wrapper.unmount()
  })
})

describe('MessageList：Markdown 与 XSS', () => {
  it('助手消息走 Markdown：粗体/列表被渲染成标签', () => {
    const wrapper = mountList([assistant({ content: '**已发货**\n\n- 顺丰\n- 中通' })])
    const html = wrapper.find('.md-body').html()

    expect(html).toContain('<strong>已发货</strong>')
    expect(html).toContain('<li>顺丰</li>')
    wrapper.unmount()
  })

  it('助手消息里的 <img src=x onerror=...> 不会留下可执行属性', () => {
    const payload = '<img src=x onerror="window.__xss_fired=1">'
    // 对照组：marked 本身不消毒，原始输出确实带 onerror——
    // 说明下面这条断言靠的是组件里的 DOMPurify，而不是「marked 恰好没输出」。
    expect(marked.parse(payload, { async: false }) as string).toContain('onerror')

    const wrapper = mountList([assistant({ content: payload })])
    const md = wrapper.find('.md-body')

    expect(md.html().toLowerCase()).not.toContain('onerror')
    expect(md.element.querySelector('img[onerror]')).toBeNull()
    // 属性不存在，浏览器求值时无从触发
    expect((window as unknown as { __xss_fired?: number }).__xss_fired).toBeUndefined()
    wrapper.unmount()
  })

  it('<script> 与 on* 事件属性、内联 style 表达式都被清掉', () => {
    const payload = [
      '<script>window.__xss_script=1</script>',
      '<p onclick="window.__xss_click=1">点我</p>',
      '<div>正常文字</div>',
    ].join('\n\n')

    const wrapper = mountList([assistant({ content: payload })])
    const html = wrapper.find('.md-body').html().toLowerCase()

    expect(html).not.toContain('<script')
    expect(html).not.toContain('onclick')
    expect(html).toContain('正常文字')
    wrapper.unmount()
  })

  it('javascript: 协议的链接 href 被剥掉，文本保留', () => {
    const wrapper = mountList([assistant({ content: '[点我看物流](javascript:window.__xss_href=1)' })])
    const md = wrapper.find('.md-body')
    const anchor = md.element.querySelector('a')

    expect(anchor).not.toBeNull()
    expect(anchor?.getAttribute('href') ?? '').not.toContain('javascript:')
    expect(md.text()).toContain('点我看物流')
    wrapper.unmount()
  })

  it('存储型 XSS 载荷（模型回复里夹带的远程脚本）不会产出 iframe/object', () => {
    const payload = '<iframe src="https://evil.example/x.js"></iframe><object data="https://evil.example/y"></object>'
    const wrapper = mountList([assistant({ content: payload })])
    const md = wrapper.find('.md-body')

    expect(md.element.querySelector('iframe')).toBeNull()
    expect(md.element.querySelector('object')).toBeNull()
    expect(md.html()).not.toContain('evil.example')
    wrapper.unmount()
  })

  it('买家消息是纯文本插值：HTML 不会被解析成节点', () => {
    const raw = '<img src=x onerror="window.__xss_user=1">'
    const wrapper = mountList([{ id: 9, role: 'user', content: raw }])

    expect(wrapper.find('.md-body').exists()).toBe(false)
    expect(wrapper.find('.user-text').text()).toBe(raw)
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.element.querySelector('[onerror]')).toBeNull()
    wrapper.unmount()
  })
})

describe('MessageList：来源徽标 / 工具 / 引用', () => {
  it('meta.from 命中时显示运营侧徽标，并带上操作人', () => {
    const wrapper = mountList([assistant({ from: 'human_operator', staff: 'ops-a' })])
    const tag = wrapper.find('.src-tag')

    expect(tag.exists()).toBe(true)
    expect(tag.text()).toBe('人工代答 · ops-a')
    wrapper.unmount()
  })

  it('没有 staff 时只显示标签本身；未知 from 不显示徽标', () => {
    const noStaff = mountList([assistant({ from: 'human_agent' })])
    expect(noStaff.find('.src-tag').text()).toBe('人工客服（企微）')
    noStaff.unmount()

    const unknown = mountList([assistant({ from: 'not_a_known_source' })])
    expect(unknown.find('.src-tag').exists()).toBe(false)
    unknown.unmount()

    const none = mountList([assistant()])
    expect(none.find('.src-tag').exists()).toBe(false)
    none.unmount()
  })

  it('工具调用与引用来源按 meta 还原（工具名可读、output 走 title 不内联展开）', () => {
    const wrapper = mountList([
      assistant({
        tools: [{ name: 'search_knowledge', status: 'done', output: '命中 3 条' }],
        citations: [{ index: 1, file_name: '售后政策.md', snippet: '7 天无理由退换' }],
      }),
    ])

    const tool = wrapper.find('.tool-tag')
    expect(tool.text()).toBe('search_knowledge')
    expect(tool.attributes('title')).toBe('命中 3 条')
    expect(wrapper.find('.cites-title').text()).toBe('参考来源')
    expect(wrapper.find('.cite-item').text()).toBe('[1] 售后政策.md：7 天无理由退换')
    wrapper.unmount()
  })
})
