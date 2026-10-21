import { beforeEach, describe, expect, it, vi } from 'vitest'

const fetchMock = vi.fn()
vi.mock('electron', () => ({ net: { fetch: (...a: unknown[]) => fetchMock(...(a as [string, RequestInit])) } }))
vi.mock('@main/enterprise/enterpriseSettings', () => ({
  loadEnterpriseSettings: () => ({ status: 'enabled', serverUrl: 'http://gw.test', token: 'tok' })
}))
vi.mock('@main/features/knowledge', () => ({
  listGatewayKbPseudoBases: async () => [
    {
      id: 'ekp',
      name: 'ekp数据检索',
      groupId: null,
      dimensions: null,
      embeddingModelId: null,
      status: 'completed',
      error: null
    }
  ]
}))
vi.mock('@logger', () => ({ loggerService: { withContext: () => ({ warn: vi.fn(), info: vi.fn(), error: vi.fn() }) } }))

import { CherryPresetTools } from '../cherryPresetTools'

const tools = new CherryPresetTools()

describe('CherryPresetTools', () => {
  beforeEach(() => fetchMock.mockReset())

  it('lists invoke_preset with the catalog embedded in the description', () => {
    const [tool] = tools.tools()
    expect(tool.name).toBe('invoke_preset')
    expect(tool.description).toContain('kb-quick-answer')
    expect(tool.description).toContain('kb-deep-research')
  })

  it('gateway-rag exec posts to the gateway RAG endpoint with visible catalog ids and returns the answer', async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ choices: [{ message: { content: '停车申请流程是……' } }] }), { status: 200 })
    )
    const result = await tools.call('invoke_preset', { preset_id: 'kb-quick-answer', task: '如何申请停车' })
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toContain('http://gw.test/v1/kb/chat/completions')
    expect(url).toContain('kb_ids=ekp')
    expect(init.headers).toMatchObject({ Authorization: 'Bearer tok' })
    expect('X-Client-User' in (init.headers as Record<string, unknown>)).toBe(true)
    expect(JSON.parse(String(init.body)).messages[0].content).toBe('如何申请停车')
    expect(result.isError).toBeFalsy()
    expect(JSON.stringify(result)).toContain('停车申请流程是')
  })

  it('local exec returns the mode-switch directive with the persona', async () => {
    const result = await tools.call('invoke_preset', { preset_id: 'kb-deep-research', task: '对比三份规范' })
    expect(result.isError).toBeFalsy()
    const text = JSON.stringify(result)
    expect(text).toContain('深度研究')
    expect(text).toContain('对比三份规范')
  })

  it('unknown preset id errors with the catalog', async () => {
    const result = await tools.call('invoke_preset', { preset_id: 'nope', task: 'x' })
    expect(result.isError).toBe(true)
    expect(JSON.stringify(result)).toContain('kb-quick-answer')
  })
})
