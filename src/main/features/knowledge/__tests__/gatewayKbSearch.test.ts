import { beforeEach, describe, expect, it, vi } from 'vitest'

import { gatewayKbSearch, listGatewayKbPseudoBases } from '../gatewayKbSearch'

const mockFetch = vi.fn()
const mockGetDb = vi.fn()

vi.mock('electron', () => ({ net: { fetch: (url: string, init?: RequestInit) => mockFetch(url, init) } }))
vi.mock('@logger', () => ({ loggerService: { withContext: () => ({ info: vi.fn(), warn: vi.fn(), error: vi.fn() }) } }))
vi.mock('@main/enterprise/enterpriseSettings', () => ({
  loadEnterpriseSettings: vi.fn()
}))
vi.mock('@application', () => ({ application: { get: () => ({ getDb: mockGetDb }) } }))

import { loadEnterpriseSettings } from '@main/enterprise/enterpriseSettings'

const mockLoadEnterpriseSettings = vi.mocked(loadEnterpriseSettings)

/** 模拟 preference 行查询（app.user.name）：all() 消费队列里的下一批行。 */
function mockPreferenceReads(reads: unknown[][]): void {
  const queue = [...reads]
  mockGetDb.mockImplementation(() => ({
    select: () => ({
      from: () => ({
        where: () => ({
          limit: () => ({
            all: () => queue.shift() ?? []
          })
        })
      })
    })
  }))
}

beforeEach(() => {
  mockFetch.mockReset()
  mockGetDb.mockReset()
  mockLoadEnterpriseSettings.mockReset()
  mockLoadEnterpriseSettings.mockReturnValue({ status: 'enabled', serverUrl: 'http://gw', token: 'tok' })
  mockPreferenceReads([[{ value: '51-zhang-kj' }]])
})

const GW_RESPONSE = {
  mode: 'ok',
  chunks: [
    {
      id: 'k1',
      kb_id: 'kb-202',
      kb_name: '202-建筑一院',
      title: '通知A',
      file_name: 'a.md',
      updated_at: '2026-10-15T00:00:00Z',
      score: 0.9,
      content: '第一条正文',
      chunk_index: 2
    },
    {
      id: 'k1',
      kb_id: 'kb-202',
      kb_name: '202-建筑一院',
      title: '通知A',
      file_name: 'a.md',
      updated_at: '2026-10-15T00:00:00Z',
      score: 0.9,
      content: '第一条正文',
      chunk_index: 2
    }
  ],
  sources: []
}

describe('gatewayKbSearch', () => {
  it('maps ok mode to KnowledgeSearchResult[] with deduped ranks and valid uuidv7 itemIds', async () => {
    // 每次调用给全新 Response：Response body 只能消费一次，跨调用复用会崩。
    mockFetch.mockImplementation(() => Promise.resolve(new Response(JSON.stringify(GW_RESPONSE), { status: 200 })))
    const out = await gatewayKbSearch('ekp', '资质')
    expect(out).toHaveLength(1)
    expect(out[0].pageContent).toBe('第一条正文')
    expect(out[0].score).toBe(0.9)
    expect(out[0].scoreKind).toBe('relevance')
    expect(out[0].rank).toBe(1)
    expect(out[0].title).toBe('通知A')
    expect(out[0].chunkId).toBe('k1')
    expect(out[0].metadata.chunkIndex).toBe(2)
    // itemId 必须通过 uuidv7 校验（KnowledgeItemIdSchema）
    expect(out[0].itemId).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i)
    // 同一 chunk 的 itemId 稳定（同文档同 chunk 跨调用可关联）
    const again = await gatewayKbSearch('ekp', '资质')
    expect(again[0].itemId).toBe(out[0].itemId)
  })

  it('gives distinct itemIds to chunks of the same document and keeps both after dedup', async () => {
    // 网关 chunk id 已逐 chunk 唯一；itemId 还纳入 chunk_index——同文档两个 chunk
    // 必须得到不同 uuidv7 itemId，且去重不丢任何一个。
    const twoChunkBody = JSON.stringify({
      mode: 'ok',
      chunks: [
        {
          id: 'k1:0',
          kb_id: 'kb-202',
          kb_name: '202-建筑一院',
          title: '通知A',
          file_name: 'a.md',
          score: 0.9,
          content: '第一段',
          chunk_index: 0
        },
        {
          id: 'k1:1',
          kb_id: 'kb-202',
          kb_name: '202-建筑一院',
          title: '通知A',
          file_name: 'a.md',
          score: 0.8,
          content: '第二段',
          chunk_index: 1
        }
      ],
      sources: []
    })
    // 每次调用给全新 Response：Response body 只能消费一次。
    mockFetch.mockImplementation(() => Promise.resolve(new Response(twoChunkBody, { status: 200 })))
    const out = await gatewayKbSearch('ekp', '资质')
    expect(out).toHaveLength(2)
    expect(out[0].itemId).not.toBe(out[1].itemId)
    for (const r of out) {
      expect(r.itemId).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i)
    }
    // itemId 确定性：同输入再取一次结果一致
    const again = await gatewayKbSearch('ekp', '资质')
    expect(again.map((r) => r.itemId)).toEqual(out.map((r) => r.itemId))
  })

  it('falls back to chunkIndex 0 and title-based content when the chunk has no content', async () => {
    mockFetch.mockResolvedValue(
      new Response(
        JSON.stringify({
          mode: 'ok',
          chunks: [{ id: 'k2', kb_id: 'ekp', kb_name: 'ekp数据检索', title: '通知B', file_name: 'b.md', score: null }],
          sources: []
        }),
        { status: 200 }
      )
    )
    const out = await gatewayKbSearch('ekp', '资质')
    expect(out[0].metadata.chunkIndex).toBe(0)
    expect(out[0].score).toBe(0)
    expect(out[0].pageContent).toBe('通知B（ekp数据检索）')
  })

  it('maps no_result mode to empty array', async () => {
    mockFetch.mockResolvedValue(
      new Response(JSON.stringify({ mode: 'no_result', chunks: [], sources: [] }), { status: 200 })
    )
    await expect(gatewayKbSearch('ekp', '资质')).resolves.toEqual([])
  })

  it('throws on unavailable mode so the caller treats it as infra failure', async () => {
    mockFetch.mockResolvedValue(
      new Response(JSON.stringify({ mode: 'unavailable', chunks: [], sources: [] }), { status: 200 })
    )
    await expect(gatewayKbSearch('ekp', '资质')).rejects.toThrow()
  })

  it('throws on non-2xx so the caller treats it as infra failure', async () => {
    mockFetch.mockResolvedValue(new Response('unauthorized', { status: 401 }))
    await expect(gatewayKbSearch('ekp', '资质')).rejects.toThrow()
  })

  it('sends the shared auth headers and the search body', async () => {
    mockFetch.mockResolvedValue(
      new Response(JSON.stringify({ mode: 'no_result', chunks: [], sources: [] }), { status: 200 })
    )
    await gatewayKbSearch('ekp', '资质')
    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('http://gw/knowledge/api/search')
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>)['X-Client-Token']).toBe('tok')
    expect((init.headers as Record<string, string>)['X-Client-User']).toBe('51-zhang-kj')
    expect(JSON.parse(init.body as string)).toEqual({ kb_ids: ['ekp'], query: '资质' })
  })
})

describe('listGatewayKbPseudoBases', () => {
  it('lists pseudo bases from the shared catalog with the virtual kb first', async () => {
    mockFetch.mockResolvedValue(
      new Response(
        JSON.stringify({
          kbs: [
            { id: 'ekp', name: 'ekp数据检索', description: '', scope: 'public' },
            { id: 'kb-202', name: '202-建筑一院', description: '', scope: 'dept' }
          ]
        }),
        { status: 200 }
      )
    )
    const bases = await listGatewayKbPseudoBases()
    expect(bases.map((b) => b.id)).toEqual(['ekp', 'kb-202'])
    for (const b of bases) {
      expect(b.status).toBe('completed')
      expect(b.error).toBeNull()
      expect(b.embeddingModelId).toBeNull()
      expect(b.dimensions).toBeNull()
      expect(b.createdAt).toBeTruthy()
      expect(b.updatedAt).toBeTruthy()
    }
  })

  it('returns an empty list when enterprise is disabled', async () => {
    // 重置模块以绕过目录缓存：前一个用例已填充 5 分钟 TTL 的 catalogCache。
    vi.resetModules()
    mockLoadEnterpriseSettings.mockReturnValue({ status: 'disabled', reason: 'no config' })
    const fresh = await import('../gatewayKbSearch')
    await expect(fresh.listGatewayKbPseudoBases()).resolves.toEqual([])
    expect(mockFetch).not.toHaveBeenCalled()
  })
})
