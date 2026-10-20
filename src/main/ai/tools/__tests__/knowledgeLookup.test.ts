import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@logger', () => ({
  loggerService: { withContext: () => ({ debug: vi.fn(), warn: vi.fn(), info: vi.fn(), error: vi.fn() }) }
}))

const { knowledgeSearchMock, addItemsMock, listBasesForDiscoveryMock, listRootItemsMock } = vi.hoisted(() => ({
  knowledgeSearchMock: vi.fn<() => Promise<unknown[]>>(),
  addItemsMock: vi.fn<() => Promise<unknown>>(),
  listBasesForDiscoveryMock: vi.fn<() => { items: unknown[]; total: number; nextCursor?: string }>(),
  listRootItemsMock: vi.fn<() => unknown[]>()
}))
vi.mock('@application', () => ({
  application: {
    get: (name: string) => {
      if (name === 'KnowledgeService')
        return {
          search: knowledgeSearchMock,
          addItems: addItemsMock,
          listBasesForDiscovery: listBasesForDiscoveryMock,
          listRootItems: listRootItemsMock
        }
      throw new Error(`Unexpected application.get(${name})`)
    }
  }
}))

const { gatewayKbSearchMock, gatewayKbSearchManyMock, listGatewayKbPseudoBasesMock } = vi.hoisted(() => ({
  gatewayKbSearchMock: vi.fn<() => Promise<unknown[]>>(),
  gatewayKbSearchManyMock: vi.fn<() => Promise<{ result: unknown; baseId: string }[]>>(),
  // Default: no shared bases — searchKnowledge consults the catalog on every call,
  // so local-only cases must see an empty catalog without per-test setup.
  listGatewayKbPseudoBasesMock: vi.fn(async () => [] as unknown[])
}))
vi.mock('@main/features/knowledge', () => ({
  isGatewayKbId: vi.fn((id: string) => id === 'ekp' || id === 'kb-202'),
  gatewayKbSearch: gatewayKbSearchMock,
  gatewayKbSearchMany: gatewayKbSearchManyMock,
  listGatewayKbPseudoBases: listGatewayKbPseudoBasesMock
}))

import {
  KNOWLEDGE_UNREADABLE_OUTPUT_NOTE,
  knowledgeListModelOutput,
  knowledgeManageModelOutput,
  knowledgeReadModelOutput,
  listOrOutlineKnowledge,
  manageKnowledge,
  readOrGrepConcept,
  searchKnowledge
} from '../knowledgeLookup'

const CITE_ID = /^[0-9a-f]{8}-\d+$/

afterEach(() => {
  vi.restoreAllMocks()
  // searchKnowledge / listKnowledgeBases consult the (mocked) gateway catalog on
  // every call; default it to "no shared bases" so local-only cases are unaffected.
  listGatewayKbPseudoBasesMock.mockResolvedValue([])
})

describe('searchKnowledge', () => {
  it('assigns prefixed sequential citation ids across merged base results', async () => {
    knowledgeSearchMock.mockResolvedValueOnce([
      { pageContent: 'chunk one', score: 0.9, conceptId: 'c1', title: 'Doc 1', metadata: { itemType: 'file' } },
      { pageContent: 'chunk two', score: 0.5, conceptId: 'c2', title: 'Doc 2', metadata: { itemType: 'file' } }
    ])
    const output = await searchKnowledge('query', ['base1'], [])
    expect(Array.isArray(output)).toBe(true)
    const ids = (output as Array<{ id: string }>).map((r) => r.id)
    ids.forEach((id) => expect(id).toMatch(CITE_ID))
    expect(ids.map((id) => id.split('-')[1])).toEqual(['1', '2'])
  })

  it('tags each hit with the base it came from', async () => {
    // conceptId is base-relative, so without baseId two bases' `README.md` are
    // indistinguishable downstream — and kb_read needs the baseId to follow a hit up.
    knowledgeSearchMock
      .mockResolvedValueOnce([
        { pageContent: 'from A', score: 0.9, conceptId: 'README.md', title: 'README', metadata: { itemType: 'file' } }
      ])
      .mockResolvedValueOnce([
        { pageContent: 'from B', score: 0.8, conceptId: 'README.md', title: 'README', metadata: { itemType: 'file' } }
      ])

    const output = (await searchKnowledge('query', ['base1', 'base2'], [])) as Array<{
      baseId: string
      conceptId: string
    }>

    expect(output.map((r) => [r.baseId, r.conceptId])).toEqual([
      ['base1', 'README.md'],
      ['base2', 'README.md']
    ])
  })
})

// `toModelOutput` re-runs over every stored tool part on each turn, so a part whose output no longer
// matches a known shape must render a note. Throwing there fails the whole request — the reported
// symptom was a knowledge-base conversation that could not be continued until the base was removed.
describe('model output formatters on unreadable stored output', () => {
  const listInput = { query: null, groupId: null, baseId: null, cursor: null }

  it.each([
    ['undefined', undefined],
    ['null', null],
    ['a rendered marker string', '<persisted-output>…</persisted-output>'],
    ['an MCP result envelope', { content: [{ type: 'text', text: 'kb_list output' }] }],
    ['an object with neither items nor nodes', {}],
    ['nodes without the array', { baseId: 'base1', nodes: undefined }],
    ['items without the array', { total: 3, items: undefined }]
  ])('kb_list renders the note for %s instead of throwing', (_label, output) => {
    expect(knowledgeListModelOutput(output as never, listInput)).toEqual({
      type: 'text',
      value: KNOWLEDGE_UNREADABLE_OUTPUT_NOTE
    })
  })

  it('kb_list still renders its known shapes', () => {
    expect(knowledgeListModelOutput({ items: [{ id: 'b1' }], total: 1 } as never, listInput)).toEqual({
      type: 'json',
      value: { items: [{ id: 'b1' }], total: 1 }
    })
    expect(
      knowledgeListModelOutput(
        { baseId: 'b1', totalItems: 0, truncated: false, nodes: [] },
        {
          ...listInput,
          baseId: 'b1'
        }
      )
    ).toEqual({ type: 'text', value: 'Knowledge base "b1" has no items yet.' })
  })

  it('kb_read and kb_manage render the note for a non-object output', () => {
    expect(knowledgeReadModelOutput(undefined as never)).toEqual({
      type: 'text',
      value: KNOWLEDGE_UNREADABLE_OUTPUT_NOTE
    })
    expect(knowledgeManageModelOutput('marker' as never)).toEqual({
      type: 'text',
      value: KNOWLEDGE_UNREADABLE_OUTPUT_NOTE
    })
  })
})

// kb_manage add with `type: file` stores the absolute path as the row's source
// identifier, not the basename — reindex re-acquires from source, so a
// basename-only source silently breaks every refresh (issue #19954).
describe('manageKnowledge add for file type', () => {
  it('persists the absolute path as source so kb_manage refresh can find it (REGRESSION #19954)', async () => {
    addItemsMock.mockResolvedValueOnce({ status: 'added' })

    const result = await manageKnowledge(
      { action: 'add', baseId: 'base-1', type: 'file', path: 'D:\\AI_Model_tmp\\test.md' },
      []
    )

    expect(result).toEqual({ action: 'add', added: ['D:\\AI_Model_tmp\\test.md'] })
    expect(addItemsMock).toHaveBeenCalledWith('base-1', [
      expect.objectContaining({
        type: 'file',
        data: expect.objectContaining({
          source: 'D:\\AI_Model_tmp\\test.md',
          path: 'D:\\AI_Model_tmp\\test.md'
        })
      })
    ])
  })
})

// Gateway shared-KB routing: ids known to the gateway catalog (the virtual `ekp`
// base, WeKnora-backed `kb-*` ids) must go to gatewayKbSearch, not the local
// KnowledgeService — and read/manage/outline must steer to kb_search.
describe('searchKnowledge routes gateway kb ids', () => {
  it('routes a gateway id to the batched gatewayKbSearchMany, not KnowledgeService', async () => {
    gatewayKbSearchManyMock.mockResolvedValueOnce([
      {
        result: {
          pageContent: '正文',
          score: 0.9,
          scoreKind: 'relevance',
          rank: 1,
          chunkId: 'k1',
          itemId: '01900000-0000-7000-8000-000000000001',
          metadata: {
            itemId: '01900000-0000-7000-8000-000000000001',
            itemType: 'file',
            source: '202-建筑一院',
            chunkIndex: 0,
            tokenCount: 10
          },
          title: '通知A'
        },
        baseId: 'ekp'
      }
    ])
    const output = await searchKnowledge('资质', ['ekp'], [])
    expect(gatewayKbSearchManyMock).toHaveBeenCalledWith(['ekp'], '资质')
    expect(knowledgeSearchMock).not.toHaveBeenCalled()
    expect(Array.isArray(output)).toBe(true)
    expect((output as Array<{ baseId: string }>)[0].baseId).toBe('ekp')
  })

  it('mixed local+gateway ids: one batched gateway call + per-base local, merged and sorted by score', async () => {
    gatewayKbSearchManyMock.mockResolvedValueOnce([
      {
        result: {
          pageContent: 'gw',
          score: 0.5,
          scoreKind: 'relevance',
          rank: 1,
          chunkId: 'k2',
          itemId: '01900000-0000-7000-8000-000000000002',
          metadata: {
            itemId: '01900000-0000-7000-8000-000000000002',
            itemType: 'file',
            source: 's',
            chunkIndex: 0,
            tokenCount: 4
          },
          title: 'T2'
        },
        baseId: 'ekp'
      }
    ])
    knowledgeSearchMock.mockResolvedValueOnce([
      { pageContent: 'local', score: 0.9, conceptId: 'c1', title: 'Doc', metadata: { itemType: 'file' } }
    ])
    const output = (await searchKnowledge('q', ['base1', 'ekp'], [])) as Array<{ baseId: string; score: number }>
    expect(output).toHaveLength(2)
    expect(gatewayKbSearchManyMock).toHaveBeenCalledTimes(1)
    expect(output[0].score).toBeGreaterThanOrEqual(output[output.length - 1].score)
    expect(output.map((r) => r.baseId)).toContain('ekp')
  })

  it('gateway failure alone yields { error } (all targeted bases failed)', async () => {
    gatewayKbSearchManyMock.mockRejectedValueOnce(new Error('unavailable'))
    const output = await searchKnowledge('q', ['ekp'], [])
    expect(output).toHaveProperty('error')
  })

  it('cold start: routes a gateway id even before any catalog fetch has populated the id cache', async () => {
    // The catalog returns the id but the (simulated) isGatewayKbId cache would
    // already know it too; the real cold-start hazard is the reverse — exercise
    // that by making the catalog the ONLY source here via a stale-cache id the
    // static mock doesn't recognize ('kb-202' IS recognized, so use catalog-only id).
    listGatewayKbPseudoBasesMock.mockResolvedValueOnce([{ id: 'kb-777', name: 'shared' } as never])
    gatewayKbSearchManyMock.mockResolvedValueOnce([])
    // 'kb-777' is not in the static isGatewayKbId mock — routing must come from
    // the fetched catalog membership, not isGatewayKbId alone.
    const output = await searchKnowledge('q', ['kb-777'], [])
    expect(gatewayKbSearchManyMock).toHaveBeenCalledWith(['kb-777'], 'q')
    expect(knowledgeSearchMock).not.toHaveBeenCalled()
    expect(output).toEqual([])
  })

  it('omitted baseIds defaults to the whole restricted scope (batched gateway + local)', async () => {
    gatewayKbSearchManyMock.mockResolvedValueOnce([
      {
        result: {
          pageContent: 'gw-scope',
          score: 0.8,
          scoreKind: 'relevance',
          rank: 1,
          chunkId: 'k3',
          itemId: '01900000-0000-7000-8000-000000000003',
          metadata: {
            itemId: '01900000-0000-7000-8000-000000000003',
            itemType: 'file',
            source: 's',
            chunkIndex: 0,
            tokenCount: 8
          },
          title: 'T3'
        },
        baseId: 'ekp'
      }
    ])
    knowledgeSearchMock.mockResolvedValueOnce([
      { pageContent: 'local-scope', score: 0.6, conceptId: 'c3', title: 'Doc3', metadata: { itemType: 'file' } }
    ])
    // allowedIds 是天花板也是默认集：省略 baseIds → 两者都进检索
    const output = (await searchKnowledge('q', [], ['base1', 'ekp'])) as Array<{ baseId: string }>
    expect(gatewayKbSearchManyMock).toHaveBeenCalledWith(['ekp'], 'q')
    expect(knowledgeSearchMock).toHaveBeenCalledWith('base1', 'q')
    expect(output).toHaveLength(2)
  })

  it('omitted baseIds + unrestricted scope falls back to local discovery + full gateway catalog', async () => {
    listBasesForDiscoveryMock.mockReturnValueOnce({
      items: [{ id: 'local-a' }, { id: 'local-b' }],
      total: 2
    })
    // 枚举默认集与路由种子各调一次目录 —— 用持久 mock
    listGatewayKbPseudoBasesMock.mockResolvedValue([
      { id: 'ekp', name: 'ekp数据检索' } as never,
      { id: 'weknora:abc', name: '103-科技质量部' } as never
    ])
    gatewayKbSearchManyMock.mockResolvedValueOnce([])
    knowledgeSearchMock.mockResolvedValue([])
    await searchKnowledge('q', [], [])
    expect(gatewayKbSearchManyMock).toHaveBeenCalledWith(['ekp', 'weknora:abc'], 'q')
    expect(knowledgeSearchMock).toHaveBeenCalledTimes(2)
  })
})

describe('kb_list includes gateway pseudo bases', () => {
  it('appends shared catalog entries to the base list on the last page', async () => {
    listBasesForDiscoveryMock.mockReturnValueOnce({ items: [], total: 0 })
    listGatewayKbPseudoBasesMock.mockResolvedValueOnce([
      {
        id: 'ekp',
        name: 'ekp数据检索',
        groupId: null,
        dimensions: null,
        embeddingModelId: null,
        status: 'completed',
        error: null,
        chunkSize: 1024,
        chunkOverlap: 200,
        chunkStrategy: 'structured',
        chunkSeparator: '\\n\\n'
      } as never
    ])
    const output = await listOrOutlineKnowledge({ limit: 50 }, [])
    if (!('items' in output)) throw new Error('expected list mode')
    expect(output.items.map((i) => i.id)).toContain('ekp')
    expect(output.items.find((i) => i.id === 'ekp')?.itemsUnavailable).toBe(true)
  })

  it('does not append gateway entries on a non-final page (cursor math stays local)', async () => {
    listBasesForDiscoveryMock.mockReturnValueOnce({ items: [], total: 9, nextCursor: 'c2' })
    // No gateway stub needed: a non-final page must not consult the catalog at all.
    const output = await listOrOutlineKnowledge({ limit: 50 }, [])
    if (!('items' in output)) throw new Error('expected list mode')
    expect(output.items.map((i) => i.id)).not.toContain('ekp')
    expect(output.nextCursor).toBe('c2')
  })

  it('degrades to local-only listing when the gateway catalog fails', async () => {
    listBasesForDiscoveryMock.mockReturnValueOnce({ items: [], total: 0 })
    listGatewayKbPseudoBasesMock.mockRejectedValueOnce(new Error('gateway down'))
    const output = await listOrOutlineKnowledge({ limit: 50 }, [])
    if (!('items' in output)) throw new Error('expected list mode')
    expect(output.items).toHaveLength(0)
    expect(output).not.toHaveProperty('error')
  })
})

describe('gateway ids steer read/manage/outline to kb_search', () => {
  const steer = (id: string) =>
    `Knowledge base "${id}" is a shared (gateway) knowledge base — only kb_search is supported. Use kb_search with baseIds: ["${id}"].`

  it('kb_read steers for a gateway baseId', async () => {
    const output = await readOrGrepConcept({ baseId: 'ekp', conceptId: 'c1' }, [])
    expect(output).toEqual({ error: steer('ekp') })
  })

  it('kb_manage steers for a gateway baseId', async () => {
    const output = await manageKnowledge({ action: 'add', baseId: 'ekp', type: 'note', content: 'x' }, [])
    expect(output).toEqual({ error: steer('ekp') })
  })

  it('kb_list outline mode steers for a gateway baseId', async () => {
    const output = await listOrOutlineKnowledge({ baseId: 'ekp', limit: 50 }, [])
    expect(output).toEqual({ error: steer('ekp') })
  })
})
