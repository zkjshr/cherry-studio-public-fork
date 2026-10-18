import { afterEach, describe, expect, it, vi } from 'vitest'

import { DataApiError, ErrorCode } from '@shared/data/api/errors'

const mocks = vi.hoisted(() => ({
  getPersist: vi.fn(),
  setPersist: vi.fn(),
  get: vi.fn()
}))

vi.mock('@data/CacheService', () => ({
  cacheService: { getPersist: mocks.getPersist, setPersist: mocks.setPersist }
}))

vi.mock('@data/DataApiService', () => ({
  dataApiService: { get: mocks.get }
}))

import {
  resolveAgentEntrySessionId,
  resolveAgentEntrySessionIdForAgent,
  resolveChatEntryTopicId,
  resolveChatEntryTopicIdForAssistant
} from '@renderer/utils/conversationEntry'

const notFoundError = () => new DataApiError(ErrorCode.NOT_FOUND, 'not found', 404)

afterEach(() => {
  // reset（而非 clear）：后台校验是 fire-and-forget，遗留的 mockOnce 队列会
  // 泄漏进下一个用例，必须连同实现一起重置。
  vi.resetAllMocks()
})

describe('resolveChatEntryTopicId', () => {
  it('resolves the last-used topic when it still exists', async () => {
    mocks.getPersist.mockReturnValue('topic-last')
    mocks.get.mockResolvedValue({ id: 'topic-last' })

    await expect(resolveChatEntryTopicId()).resolves.toBe('topic-last')
    expect(mocks.getPersist).toHaveBeenCalledWith('ui.chat.last_used_topic_id')
    expect(mocks.get).toHaveBeenCalledWith('/topics/topic-last')
    expect(mocks.get).toHaveBeenCalledTimes(1)
  })

  it('trusts the remembered topic immediately and validates in the background', async () => {
    // 快速路径：缓存的 id 直接返回（tab 切换零等待）；失效校验是后台的——
    // NOT_FOUND 时清掉缓存，下次进入回退到 latest。
    mocks.getPersist.mockReturnValue('topic-deleted')
    mocks.get.mockRejectedValueOnce(notFoundError()).mockResolvedValueOnce({ topic: { id: 'topic-latest' } })

    await expect(resolveChatEntryTopicId()).resolves.toBe('topic-deleted')
    await vi.waitFor(() => {
      expect(mocks.setPersist).toHaveBeenCalledWith('ui.chat.last_used_topic_id', null)
    })
    expect(mocks.get).toHaveBeenCalledWith('/topics/topic-deleted')
    expect(mocks.get).not.toHaveBeenCalledWith('/topics/latest')
  })

  it('asks for the latest topic when nothing is remembered', async () => {
    mocks.getPersist.mockReturnValue(null)
    mocks.get.mockResolvedValue({ topic: { id: 'topic-latest' } })

    await expect(resolveChatEntryTopicId()).resolves.toBe('topic-latest')
    expect(mocks.get).toHaveBeenCalledWith('/topics/latest')
    expect(mocks.get).toHaveBeenCalledTimes(1)
  })

  it('returns null when the library is empty', async () => {
    mocks.getPersist.mockReturnValue(null)
    mocks.get.mockResolvedValue({ topic: null })

    await expect(resolveChatEntryTopicId()).resolves.toBeNull()
  })

  it('performs a fresh latest read on each entry resolution', async () => {
    mocks.getPersist.mockReturnValue(null)
    mocks.get.mockResolvedValueOnce({ topic: { id: 'topic-first' } }).mockResolvedValueOnce({ topic: null })

    await expect(resolveChatEntryTopicId()).resolves.toBe('topic-first')
    await expect(resolveChatEntryTopicId()).resolves.toBeNull()
    expect(mocks.get).toHaveBeenCalledTimes(2)
  })

  it('does not block entry when background validation fails with a non-NOT_FOUND error', async () => {
    // 后台校验的非 NOT_FOUND 错误只被吞掉（保留缓存），绝不阻断进入。
    mocks.getPersist.mockReturnValue('topic-last')
    const serverError = new DataApiError(ErrorCode.INTERNAL_SERVER_ERROR, 'boom', 500)
    mocks.get.mockRejectedValue(serverError)

    await expect(resolveChatEntryTopicId()).resolves.toBe('topic-last')
    await vi.waitFor(() => {
      expect(mocks.get).toHaveBeenCalledTimes(1)
    })
    expect(mocks.setPersist).not.toHaveBeenCalled()
  })
})

describe('resolveAgentEntrySessionId', () => {
  it('resolves the last-used session when it still exists', async () => {
    mocks.getPersist.mockReturnValue('session-last')
    mocks.get.mockResolvedValue({ id: 'session-last' })

    await expect(resolveAgentEntrySessionId()).resolves.toBe('session-last')
    expect(mocks.getPersist).toHaveBeenCalledWith('ui.agent.last_used_session_id')
    expect(mocks.get).toHaveBeenCalledWith('/agent-sessions/session-last')
  })

  it('trusts the remembered session immediately and validates in the background', async () => {
    mocks.getPersist.mockReturnValue('session-deleted')
    mocks.get.mockRejectedValueOnce(notFoundError()).mockResolvedValueOnce({ session: { id: 'session-latest' } })

    await expect(resolveAgentEntrySessionId()).resolves.toBe('session-deleted')
    await vi.waitFor(() => {
      expect(mocks.setPersist).toHaveBeenCalledWith('ui.agent.last_used_session_id', null)
    })
    expect(mocks.get).toHaveBeenCalledWith('/agent-sessions/session-deleted')
    expect(mocks.get).not.toHaveBeenCalledWith('/agent-sessions/latest')
  })

  it('returns null when no sessions exist', async () => {
    mocks.getPersist.mockReturnValue(null)
    mocks.get.mockResolvedValue({ session: null })

    await expect(resolveAgentEntrySessionId()).resolves.toBeNull()
  })
})

describe('resolveChatEntryTopicIdForAssistant', () => {
  it('resolves the latest topic of the given assistant', async () => {
    mocks.get.mockResolvedValue({ topic: { id: 'topic-assistant' } })

    await expect(resolveChatEntryTopicIdForAssistant('assistant-1')).resolves.toBe('topic-assistant')
    expect(mocks.get).toHaveBeenCalledWith('/topics/latest', { query: { assistantId: 'assistant-1' } })
    expect(mocks.get).toHaveBeenCalledTimes(1)
  })

  it('returns null when the assistant has no topics', async () => {
    mocks.get.mockResolvedValue({ topic: null })

    await expect(resolveChatEntryTopicIdForAssistant('assistant-1')).resolves.toBeNull()
  })

  it('never consults the global last-focused topic cache', async () => {
    mocks.getPersist.mockReturnValue('topic-global')
    mocks.get.mockResolvedValue({ topic: { id: 'topic-assistant' } })

    await expect(resolveChatEntryTopicIdForAssistant('assistant-1')).resolves.toBe('topic-assistant')
    expect(mocks.getPersist).not.toHaveBeenCalled()
  })
})

describe('resolveAgentEntrySessionIdForAgent', () => {
  it('resolves the latest session of the given agent', async () => {
    mocks.get.mockResolvedValue({ session: { id: 'session-agent' } })

    await expect(resolveAgentEntrySessionIdForAgent('agent-1')).resolves.toBe('session-agent')
    expect(mocks.get).toHaveBeenCalledWith('/agent-sessions/latest', { query: { agentId: 'agent-1' } })
    expect(mocks.get).toHaveBeenCalledTimes(1)
  })

  it('returns null when the agent has no sessions', async () => {
    mocks.get.mockResolvedValue({ session: null })

    await expect(resolveAgentEntrySessionIdForAgent('agent-1')).resolves.toBeNull()
  })

  it('never consults the global last-focused session cache', async () => {
    mocks.getPersist.mockReturnValue('session-global')
    mocks.get.mockResolvedValue({ session: { id: 'session-agent' } })

    await expect(resolveAgentEntrySessionIdForAgent('agent-1')).resolves.toBe('session-agent')
    expect(mocks.getPersist).not.toHaveBeenCalled()
  })
})
