import { cacheService } from '@data/CacheService'
import { dataApiService } from '@data/DataApiService'
import { isDataApiNotFoundError } from '@shared/data/api/errors'

/**
 * Entry-target resolution for the conversation routes (`/app/chat`, `/app/agents`),
 * called from their `beforeLoad` interceptors on a bare entry (no explicit
 * topicId / sessionId in the URL).
 *
 * Resolution order: the cross-window "last focused" id — trusted immediately
 * (the pages keep it fresh; staleness is re-validated in the background), then
 * the globally most-recently-active conversation. `null` means
 * nothing to resume — the route falls through bare and the page decides what to
 * show.
 *
 * `last_used_*` ids are never cleared on delete, so a remembered id may point at
 * a deleted row — that surfaces as NOT_FOUND here and falls through to latest.
 * These are one-shot reads rather than SWR preloads: the routes do not consume
 * the latest keys through hooks, so retaining a preload there would permanently
 * pin the first response.
 */

export async function resolveChatEntryTopicId(): Promise<string | null> {
  const lastUsedTopicId = cacheService.getPersist('ui.chat.last_used_topic_id')
  if (lastUsedTopicId) {
    // 快速路径：缓存的 id 由页面实时维护（HomeTabRuntime 每次切换都会写），直接
    // 返回让 beforeLoad 立刻重定向——tab 切换不再等一次 IPC。失效校验挪到后台：
    // id 指向已删会话时清掉缓存，下次进入自然回退到 latest。
    void dataApiService.get(`/topics/${lastUsedTopicId}`).catch((error) => {
      if (isDataApiNotFoundError(error)) cacheService.setPersist('ui.chat.last_used_topic_id', null)
    })
    return lastUsedTopicId
  }

  const { topic } = await dataApiService.get('/topics/latest')
  return topic?.id ?? null
}

export async function resolveAgentEntrySessionId(): Promise<string | null> {
  const lastUsedSessionId = cacheService.getPersist('ui.agent.last_used_session_id')
  if (lastUsedSessionId) {
    // 同上：AgentTabRuntime 实时维护，快速路径 + 后台校验。
    void dataApiService.get(`/agent-sessions/${lastUsedSessionId}`).catch((error) => {
      if (isDataApiNotFoundError(error)) cacheService.setPersist('ui.agent.last_used_session_id', null)
    })
    return lastUsedSessionId
  }

  const { session } = await dataApiService.get('/agent-sessions/latest')
  return session?.id ?? null
}

/**
 * Entity-scoped variants used by the sidebar entries: resolve the most recent
 * conversation of one specific assistant / agent. They intentionally ignore the
 * global last-focused caches — a pinned entity entry is about that entity, not
 * whatever was last focused elsewhere.
 */
export async function resolveChatEntryTopicIdForAssistant(assistantId: string): Promise<string | null> {
  const { topic } = await dataApiService.get('/topics/latest', { query: { assistantId } })
  return topic?.id ?? null
}

export async function resolveAgentEntrySessionIdForAgent(agentId: string): Promise<string | null> {
  const { session } = await dataApiService.get('/agent-sessions/latest', { query: { agentId } })
  return session?.id ?? null
}
