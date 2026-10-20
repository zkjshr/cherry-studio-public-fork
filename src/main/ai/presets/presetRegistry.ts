/**
 * 本地 preset 注册表 —— TJAD 百事通（意图路由）的装配单元目录。
 *
 * 设计（用户定稿 2026-10-20）：工具集/库集/人设的注册表放**本地**而非网关——
 * 网关发布的内容最终也落到本地，且本地还有用户自建内容，本地才是全集。
 * 百事通按意图从注册表选 preset（或注册新 preset），按 preset 声明的执行位置
 * 完成任务；用户不需要手动勾选启用什么。
 *
 * 存储：代码内置 defaults + 偏好行 `agent.preset_registry` 的用户覆盖/新增
 * （按 id 合并）。主进程内直读，不暴露 IPC（管理 UI 后续再加）。
 */

import { and, eq } from 'drizzle-orm'

import { application } from '@application'
import { preferenceTable } from '@data/db/schemas/preference'
import { loggerService } from '@logger'

const logger = loggerService.withContext('presetRegistry')

const PREFERENCE_SCOPE_DEFAULT = 'default'
const PREFERENCE_KEY = 'agent.preset_registry'

/** preset 的执行位置。 */
export type PresetExecution = 'gateway-rag' | 'local'

/**
 * 一个装配单元。
 * - `gateway-rag`：网关快速问答管线（服务端改写+检索+生成），小模型可用，同步单轮。
 * - `local`：主会话自身按 `persona` 补充指令继续执行（强模型 + 主会话全量工具）。
 */
export interface AgentPreset {
  id: string
  name: string
  description: string
  exec: PresetExecution
  /** gateway-rag 执行时的库范围（目录 wire id，如 ekp / weknora:<uuid>）；省略 = 用户可见目录全量。 */
  kbIds?: string[]
  /** local 执行时叠加到主会话的人设补充指令。 */
  persona?: string
}

const DEFAULT_PRESETS: readonly AgentPreset[] = [
  {
    id: 'universal',
    name: '通用助手',
    description: '默认模式：全量知识检索 + 通用工具，复杂任务自主编排',
    exec: 'local',
    persona:
      '以 TJAD 百事通的身份回答：所内制度/通知/项目资料类问题必须先检索知识库并引用来源；通用问题直接回答；必要时组合文件、代码等工具。'
  },
  {
    id: 'kb-quick-answer',
    name: '知识库快速问答',
    description: 'WeKnora 服务端检索编排（查询改写+双路检索），单轮直答，小模型可用',
    exec: 'gateway-rag'
  },
  {
    id: 'kb-deep-research',
    name: '知识库深度研究',
    description: '多轮检索+综合对比（需强模型），适合跨文档的复杂问题',
    exec: 'local',
    persona:
      '进入深度研究模式：围绕任务多轮检索知识库（每次调整关键词），必要时深读并对比多份文档，最后综合成结构化答案并逐条注明来源文档名。'
  }
]

function readPreferenceRow(key: string): unknown {
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
    logger.warn('Failed to read preset registry preference row', { error: String(error) })
    return undefined
  }
}

/** 用户覆盖/新增项（偏好行 JSON 数组，按 id 合并进 defaults；字段级缺失回落默认值）。 */
function loadUserPresets(): Partial<AgentPreset>[] {
  const raw = readPreferenceRow(PREFERENCE_KEY)
  if (!Array.isArray(raw)) return []
  return raw.filter(
    (item): item is Partial<AgentPreset> =>
      typeof item === 'object' && item !== null && typeof (item as AgentPreset).id === 'string'
  )
}

/** 内置目录 + 用户覆盖/新增，按 id 合并（用户项可整体替换同 id 项，也可注册新 id）。 */
export function listPresets(): AgentPreset[] {
  const merged = new Map<string, AgentPreset>()
  for (const preset of DEFAULT_PRESETS) merged.set(preset.id, { ...preset })
  for (const override of loadUserPresets()) {
    const id = override.id as string
    const base = merged.get(id)
    merged.set(id, {
      id,
      name: override.name ?? base?.name ?? id,
      description: override.description ?? base?.description ?? '',
      exec: override.exec ?? base?.exec ?? 'local',
      ...(override.kbIds !== undefined ? { kbIds: override.kbIds } : base?.kbIds ? { kbIds: base.kbIds } : {}),
      ...(override.persona !== undefined
        ? { persona: override.persona }
        : base?.persona
          ? { persona: base.persona }
          : {})
    })
  }
  return [...merged.values()]
}

export function getPreset(id: string): AgentPreset | undefined {
  return listPresets().find((preset) => preset.id === id)
}
