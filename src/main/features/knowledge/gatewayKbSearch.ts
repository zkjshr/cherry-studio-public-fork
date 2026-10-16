/**
 * 网关共享库检索适配器：把 /knowledge/api/kbs 目录物化为伪 KnowledgeBase（不落库、
 * 零迁移），把 /knowledge/api/search 映射为 KnowledgeSearchResult[]，供 knowledgeLookup
 * 按 baseId 分流。鉴权头部语义与 sharedKbs.listSharedKnowledgeBases 完全一致
 * （X-Client-Token + X-Client-User）；授权判断在网关每次调用时执行，这里不做本地
 * 二次过滤（本地过滤只是 UI 体验，不是安全边界）。
 */
import { createHash } from 'node:crypto'

import { and, eq } from 'drizzle-orm'
import { net } from 'electron'

import { application } from '@application'
import { preferenceTable } from '@data/db/schemas/preference'
import { loggerService } from '@logger'
import { loadEnterpriseSettings } from '@main/enterprise/enterpriseSettings'
import {
  GatewaySharedKbsResponseSchema,
  type KnowledgeBase,
  type KnowledgeSearchResult
} from '@shared/data/types/knowledge'

const logger = loggerService.withContext('gatewayKbSearch')

const FETCH_TIMEOUT_MS = 15_000
const PREFERENCE_SCOPE_DEFAULT = 'default'

/**
 * One chunk of the gateway search response (own contract, defined by the
 * gateway's Python side). Fields the mapping can survive without are optional;
 * the mapping falls back at use sites.
 */
interface GatewayChunk {
  id: string
  kb_id: string
  kb_name: string
  title: string
  file_name: string
  updated_at?: string
  score: number | null
  content?: string
  chunk_index?: number
}

export interface GatewaySearchResponse {
  mode: 'ok' | 'no_result' | 'unavailable'
  chunks: GatewayChunk[]
  sources: { id: string; kb_id: string; kb_name: string; title: string }[]
}

export interface GatewayCatalogEntry {
  id: string
  name: string
  description: string
  scope: 'dept' | 'public'
}

/**
 * 宽松校验网关搜索响应：mode 三值枚举（未知值归一为 no_result，绝不把坏载荷
 * 升级成 unavailable——那会让调用方把数据问题当基础设施故障）；chunks/sources
 * 必须是数组否则兜底为空；chunk 字段缺失在映射处兜底。
 */
export function parseGatewaySearchResponse(value: unknown): GatewaySearchResponse {
  const body = (value ?? {}) as Record<string, unknown>
  const mode: GatewaySearchResponse['mode'] =
    body.mode === 'unavailable' ? 'unavailable' : body.mode === 'ok' ? 'ok' : 'no_result'
  const chunks = Array.isArray(body.chunks) ? (body.chunks as GatewayChunk[]) : []
  const sources = Array.isArray(body.sources) ? (body.sources as GatewaySearchResponse['sources']) : []
  return { mode, chunks, sources }
}

export function isGatewayKbId(id: string): boolean {
  return gatewayKbIdCache.has(id)
}

/**
 * 目录 → 伪 KnowledgeBase 列表（网关顺序原样保留，虚拟 ekp 库排第一）。
 * 伪对象只存在于内存，绝不写入 knowledge_base 表；id 用 wire id 原样
 * （如 `ekp`、`weknora:<uuid>`——SharedKnowledgeBaseSchema 契约保证必非
 * uuidv4），因此调用方（knowledgeLookup 分流、knowledgeScope 分区）只把它
 * 当不透明 key，绝不落库。
 */
export async function listGatewayKbPseudoBases(): Promise<KnowledgeBase[]> {
  const kbs = await fetchCatalog()
  return kbs.map((kb) => pseudoBase(kb.id, kb.name))
}

/**
 * 单个共享库检索。no_result → []（正常空结果）；unavailable / 非 2xx → 抛错，
 * 让 searchKnowledge 的"全库失败→{error}"路径与本地向量库故障同型（Task 4
 * 的调用方负责把每个 base 的失败归集到 per-base error）。
 */
export async function gatewayKbSearch(kbId: string, query: string): Promise<KnowledgeSearchResult[]> {
  const settings = loadEnterpriseSettings()
  if (settings.status !== 'enabled') throw new Error('Enterprise gateway is not enabled.')
  const resp = await net.fetch(`${settings.serverUrl}/knowledge/api/search`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Client-Token': settings.token,
      'X-Client-User': readLocalUserName()
    },
    body: JSON.stringify({ kb_ids: [kbId], query }),
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS)
  })
  if (!resp.ok) {
    throw new Error(`Gateway knowledge search failed with status ${resp.status}`)
  }
  const body = parseGatewaySearchResponse(await resp.json())
  if (body.mode !== 'ok') {
    if (body.mode === 'unavailable') throw new Error('Gateway knowledge search unavailable')
    return []
  }
  // chunkId 全局去重（WeKnora hybrid-search 可能跨库返回同一 chunk），名次按
  // 去重后的顺序 1 起编。
  const seen = new Set<string>()
  const results: KnowledgeSearchResult[] = []
  let rank = 1
  for (const chunk of body.chunks) {
    if (seen.has(chunk.id)) continue
    seen.add(chunk.id)
    const itemId = deterministicItemId(chunk.kb_id, chunk.id)
    const content = (chunk.content || '').trim()
    results.push({
      pageContent: content || `${chunk.title}（${chunk.kb_name}）`,
      score: clampScore(chunk.score),
      scoreKind: 'relevance',
      rank,
      metadata: {
        itemId,
        itemType: 'file',
        source: chunk.kb_name || 'ekp数据检索',
        chunkIndex:
          typeof chunk.chunk_index === 'number' && Number.isInteger(chunk.chunk_index) && chunk.chunk_index >= 0
            ? chunk.chunk_index
            : 0,
        // WeKnora 未给 token 数；按 ~1.6 字符/token 估——metadata 契约要求非负整数
        tokenCount: Math.max(1, Math.ceil((chunk.content || chunk.title || '').length / 1.6))
      },
      itemId,
      chunkId: chunk.id,
      title: chunk.title
    })
    rank += 1
  }
  return results
}

// ---------- internals ----------

/**
 * 伪 KnowledgeBase：BM25-only 形态（无 embedding 模型、无维度），满足
 * KnowledgeBaseEntitySchema 的全部必填字段（含 createdAt/updatedAt——伪对象
 * 不落库，时间戳仅用于类型完备）。`as KnowledgeBase` 是有意的：wire id
 * （`ekp` 等）不是 uuidv4，而伪对象从不经过 schema 校验或持久化。
 */
function pseudoBase(id: string, name: string): KnowledgeBase {
  const now = new Date().toISOString()
  return {
    id,
    name,
    groupId: null,
    dimensions: null,
    embeddingModelId: null,
    status: 'completed',
    error: null,
    chunkSize: 1024,
    chunkOverlap: 200,
    chunkStrategy: 'structured',
    chunkSeparator: '\\n\\n',
    createdAt: now,
    updatedAt: now
  } as KnowledgeBase
}

function clampScore(score: number | null): number {
  if (score === null || Number.isNaN(score)) return 0
  return Math.max(0, Math.min(1, score))
}

/**
 * WeKnora chunk id（知识库内稳定）→ 确定性 uuidv7 形态 id。
 * KnowledgeItemIdSchema 要求 uuidv7；同 chunk 跨调用必须得到同一 id（kb_read
 * 提示、引用展示都靠它关联）。v7 布局：48bit 时间戳取固定纪元 + 12bit 随机区
 * 取 sha256(kbId/chunkId) 的切片。
 */
function deterministicItemId(kbId: string, chunkId: string): string {
  const digest = createHash('sha256').update(`${kbId}/${chunkId}`).digest('hex')
  const hex = FIXED_EPOCH_MS.toString(16).padStart(12, '0')
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    `7${digest.slice(0, 3)}`,
    `${'89ab'[parseInt(digest[4], 16) % 4]}${digest.slice(5, 8)}`,
    digest.slice(8, 20)
  ].join('-')
}

// 固定纪元（毫秒），保证 version 位恒为 7。
const FIXED_EPOCH_MS = 0x0190000000000

let catalogCache: { at: number; kbs: GatewayCatalogEntry[] } | null = null
const CATALOG_TTL_MS = 5 * 60_000
const gatewayKbIdCache = new Set<string>()

/** 目录拉取，5 分钟缓存；一切失败静默降级为空列表（与 sharedKbs 同型）。 */
async function fetchCatalog(): Promise<GatewayCatalogEntry[]> {
  if (catalogCache && Date.now() - catalogCache.at < CATALOG_TTL_MS) return catalogCache.kbs
  const settings = loadEnterpriseSettings()
  if (settings.status !== 'enabled') return []
  try {
    const resp = await net.fetch(`${settings.serverUrl}/knowledge/api/kbs`, {
      headers: { 'X-Client-Token': settings.token, 'X-Client-User': readLocalUserName() },
      redirect: 'follow',
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS)
    })
    if (!resp.ok) {
      logger.warn('Gateway KB catalog fetch failed', { status: resp.status })
      return []
    }
    const parsed = GatewaySharedKbsResponseSchema.parse(await resp.json()).kbs
    const kbs: GatewayCatalogEntry[] = parsed.map((kb) => ({
      id: kb.id,
      name: kb.name,
      description: kb.description ?? '',
      scope: kb.scope
    }))
    gatewayKbIdCache.clear()
    for (const kb of kbs) gatewayKbIdCache.add(kb.id)
    catalogCache = { at: Date.now(), kbs }
    return kbs
  } catch (error) {
    logger.warn('Gateway KB catalog fetch failed', { error: String(error) })
    return []
  }
}

/** 本地用户名（app.user.name，如 51-zhang-kj）；读不到时为空串（网关视为未登记）。 */
function readLocalUserName(): string {
  try {
    const db = application.get('DbService').getDb()
    const [row] = db
      .select({ value: preferenceTable.value })
      .from(preferenceTable)
      .where(and(eq(preferenceTable.scope, PREFERENCE_SCOPE_DEFAULT), eq(preferenceTable.key, 'app.user.name')))
      .limit(1)
      .all()
    return typeof row?.value === 'string' ? row.value : ''
  } catch (error) {
    logger.warn('Failed to read local user name', { error: String(error) })
    return ''
  }
}
