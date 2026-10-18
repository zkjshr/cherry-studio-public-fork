import { and, eq } from 'drizzle-orm'
import { net } from 'electron'

import { application } from '@application'
import { preferenceTable } from '@data/db/schemas/preference'
import { loggerService } from '@logger'
import { loadEnterpriseSettings } from '@main/enterprise/enterpriseSettings'
import {
  GatewaySharedKbsResponseSchema,
  type SharedKnowledgeBase,
  type SharedKnowledgeBasesPayload
} from '@shared/data/types/knowledge'
import { ENTERPRISE_MANAGED_ASSISTANT_IDS_KEY, ENTERPRISE_PINNED_TOPICS_KEY } from '@shared/utils/enterprise'

const logger = loggerService.withContext('sharedKbs')

const FETCH_TIMEOUT_MS = 15_000
const PREFERENCE_SCOPE_DEFAULT = 'default'
const EMPTY_PAYLOAD: SharedKnowledgeBasesPayload = { kbs: [], chatTopicId: null }

/**
 * 部门/公共知识库目录 + 置顶知识话题。企业侧共享库来自网关
 * `/knowledge/api/kbs`（分类在网关侧按 ^\d{3}- 命名约定完成）；
 * chatTopicId 取第一个配置了置顶话题的受管助手——详情页"去对话检索"
 * 跳 `/app/chat?topicId=` 用。一切失败静默降级为空 payload。
 */
export async function listSharedKnowledgeBases(): Promise<SharedKnowledgeBasesPayload> {
  const settings = loadEnterpriseSettings()
  if (settings.status !== 'enabled') {
    return EMPTY_PAYLOAD
  }

  let kbs: SharedKnowledgeBase[]
  try {
    const resp = await net.fetch(`${settings.serverUrl}/knowledge/api/kbs`, {
      // X-Client-User：本地用户名（如 51-zhang-kj），网关据此把部门库过滤到
      // 本人所在部门（user_departments 映射）；未登记用户部门分区为空。
      headers: { 'X-Client-Token': settings.token, 'X-Client-User': readLocalUserName() },
      redirect: 'follow',
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS)
    })
    if (!resp.ok) {
      logger.warn('Shared knowledge bases fetch failed', { status: resp.status })
      return EMPTY_PAYLOAD
    }
    // 网关只下发 { kbs }；chatTopicId 由本地解析补齐——用含 chatTopicId 的
    // payload schema 直接 parse 网关响应会永远失败。
    kbs = GatewaySharedKbsResponseSchema.parse(await resp.json()).kbs
  } catch (error) {
    logger.warn('Shared knowledge bases fetch failed', { error: String(error) })
    return EMPTY_PAYLOAD
  }

  return { kbs, chatTopicId: findPinnedManagedTopicId() }
}

/**
 * 第一个有置顶话题的受管助手的话题 id；无受管助手/无置顶话题时 null。
 * 注意：本分支尚无该 preference 行的写入方（applyEnterpriseConfig 的置顶话题
 * 链路落地于后续改动），因此当前恒返回 null，详情页"去对话检索"按钮随之隐藏。
 */
function findPinnedManagedTopicId(): string | null {
  const managedIds = readUndeclaredPreferenceRow(ENTERPRISE_MANAGED_ASSISTANT_IDS_KEY)
  const pinnedTopics = readUndeclaredPreferenceRow(ENTERPRISE_PINNED_TOPICS_KEY)
  if (!Array.isArray(managedIds) || typeof pinnedTopics !== 'object' || pinnedTopics === null) {
    return null
  }
  for (const assistantId of managedIds) {
    if (typeof assistantId !== 'string') continue
    const entry = (pinnedTopics as Record<string, { topicId?: unknown }>)[assistantId]
    if (entry && typeof entry.topicId === 'string') {
      return entry.topicId
    }
  }
  return null
}

/** 本地用户名（app.user.name，如 51-zhang-kj）；读不到时为空串（网关视为未登记）。 */
function readLocalUserName(): string {
  const value = readUndeclaredPreferenceRow('app.user.name')
  return typeof value === 'string' ? value : ''
}

function readUndeclaredPreferenceRow(key: string): unknown {
  try {
    const db = application.get('DbService').getDb()
    const [row] = db
      .select({ value: preferenceTable.value })
      .from(preferenceTable)
      .where(and(eq(preferenceTable.scope, PREFERENCE_SCOPE_DEFAULT), eq(preferenceTable.key, key)))
      .limit(1)
      .all()
    return row?.value
  } catch (error) {
    logger.warn('Failed to read enterprise preference row', { key, error: String(error) })
    return undefined
  }
}
