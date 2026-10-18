import { beforeEach, describe, expect, it, vi } from 'vitest'

import { listSharedKnowledgeBases } from '../sharedKbs'

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

/**
 * 模拟 preference 行查询：实现按 key 各查一次（受管助手 id 在前、置顶话题在后），
 * 每次查询的 all() 消费队列里的下一批行；断言只锁定最终返回值，不锁定中间形态。
 */
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
})

describe('listSharedKnowledgeBases', () => {
  it('returns empty payload when enterprise is disabled', async () => {
    mockLoadEnterpriseSettings.mockReturnValue({ status: 'disabled', reason: 'no config' })
    await expect(listSharedKnowledgeBases()).resolves.toEqual({ kbs: [], chatTopicId: null })
    expect(mockFetch).not.toHaveBeenCalled()
  })

  it('fetches, validates and returns kbs plus the pinned knowledge topic id', async () => {
    mockLoadEnterpriseSettings.mockReturnValue({ status: 'enabled', serverUrl: 'http://gw.test', token: 't' })
    mockFetch.mockResolvedValue(
      new Response(
        JSON.stringify({
          kbs: [
            { id: '1', name: '101-结构所规范库', scope: 'dept' },
            { id: '2', name: '通用设计资料', scope: 'public' }
          ]
        }),
        { status: 200 }
      )
    )
    // 读取顺序：① X-Client-User 用户名（readLocalUserName）→ ② 受管助手 id → ③ 置顶话题
    mockPreferenceReads([
      [{ value: '51-zhang-kj' }],
      [{ value: ['a1', 'a2'] }],
      [{ value: { a2: { topicId: 'topic-9' } } }]
    ])
    await expect(listSharedKnowledgeBases()).resolves.toEqual({
      kbs: [
        { id: '1', name: '101-结构所规范库', scope: 'dept' },
        { id: '2', name: '通用设计资料', scope: 'public' }
      ],
      chatTopicId: 'topic-9'
    })
  })

  it('returns null chatTopicId when no managed assistant has a pinned topic', async () => {
    mockLoadEnterpriseSettings.mockReturnValue({ status: 'enabled', serverUrl: 'http://gw.test', token: 't' })
    mockFetch.mockResolvedValue(new Response(JSON.stringify({ kbs: [] }), { status: 200 }))
    mockPreferenceReads([[], []])
    await expect(listSharedKnowledgeBases()).resolves.toEqual({ kbs: [], chatTopicId: null })
  })

  it('degrades to empty payload on gateway http error', async () => {
    mockLoadEnterpriseSettings.mockReturnValue({ status: 'enabled', serverUrl: 'http://gw.test', token: 't' })
    mockFetch.mockResolvedValue(new Response('boom', { status: 500 }))
    await expect(listSharedKnowledgeBases()).resolves.toEqual({ kbs: [], chatTopicId: null })
  })

  it('degrades to empty payload on invalid payload shape', async () => {
    mockLoadEnterpriseSettings.mockReturnValue({ status: 'enabled', serverUrl: 'http://gw.test', token: 't' })
    mockFetch.mockResolvedValue(new Response(JSON.stringify({ kbs: [{ id: '1' }] }), { status: 200 }))
    await expect(listSharedKnowledgeBases()).resolves.toEqual({ kbs: [], chatTopicId: null })
  })
})
