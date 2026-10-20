/**
 * `invoke_preset` —— TJAD 百事通意图路由的元工具（cherry-tools 内置 MCP）。
 *
 * 百事通按意图从本地 preset 注册表（presetRegistry）选择装配单元并派发任务：
 * - exec = gateway-rag：同步调用网关快速问答管线（/v1/kb/chat/completions，
 *   服务端改写+检索+生成），小模型可用；库范围省略 = 用户可见目录全量。
 * - exec = local：返回模式切换指令，主会话按 preset 人设继续执行（依赖主会话
 *   自身工具与模型能力，适合强模型）。
 *
 * 工具描述在每次 listing 时从注册表现算——preset 目录（含用户新注册的）随描述
 * 进入模型上下文，这就是"路由可选现成 preset 也可注册新 preset"的入口。
 */

import type { CallToolResult, Tool } from '@modelcontextprotocol/sdk/types.js'
import { net } from 'electron'
import * as z from 'zod'

import { loggerService } from '@logger'
import { getPreset, listPresets, type AgentPreset } from '@main/ai/presets/presetRegistry'
import { loadEnterpriseSettings } from '@main/enterprise/enterpriseSettings'

const logger = loggerService.withContext('McpServer:CherryPresetTools')

export const INVOKE_PRESET_TOOL_NAME = 'invoke_preset'

const FETCH_TIMEOUT_MS = 60_000

const invokePresetInputSchema = z.object({
  preset_id: z.string().min(1).describe('要使用的 preset id（见工具描述中的目录）'),
  task: z.string().min(1).describe('交给该 preset 的完整任务描述（自包含，不依赖上下文代词）')
})

function catalogDescription(): string {
  const lines = listPresets().map(
    (preset) => `- ${preset.id}（${preset.name}）：${preset.description} [执行: ${preset.exec}]`
  )
  return (
    `Invoke a preset assembly (tools + knowledge bases + persona) to complete a task. ` +
    `Use this when the task matches a specialized preset better than the default mode. ` +
    `Available presets:\n${lines.join('\n')}`
  )
}

function toTextResult(text: string): CallToolResult {
  return { content: [{ type: 'text', text }] }
}

/** gateway-rag 默认库范围：用户可见目录全量（ekp + 公共 + 本部门）。 */
async function resolveVisibleCatalogIds(): Promise<string[]> {
  const { listGatewayKbPseudoBases } = await import('@main/features/knowledge')
  const bases = await listGatewayKbPseudoBases().catch(() => [])
  return bases.map((base) => base.id)
}

/** gateway-rag 执行：网关快速问答管线（同步、单轮、小模型可用）。 */
async function runGatewayRag(preset: AgentPreset, task: string): Promise<string> {
  const settings = loadEnterpriseSettings()
  if (settings.status !== 'enabled') {
    return 'Error: 企业网关未启用，gateway-rag preset 不可用。'
  }
  const kbIds = preset.kbIds ?? (await resolveVisibleCatalogIds())
  const resp = await net.fetch(
    `${settings.serverUrl}/v1/kb/chat/completions?kb_ids=${encodeURIComponent(kbIds.join(','))}`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${settings.token}`
      },
      body: JSON.stringify({
        model: `kb-preset:${preset.id}`,
        messages: [{ role: 'user', content: task }],
        stream: false
      }),
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS)
    }
  )
  if (!resp.ok) {
    throw new Error(`gateway-rag 请求失败（HTTP ${resp.status}）`)
  }
  const body = (await resp.json()) as { choices?: { message?: { content?: string } }[] }
  const content = body.choices?.[0]?.message?.content?.trim()
  return content || '企业知识库中未找到相关文件。'
}

export class CherryPresetTools {
  tools(): Tool[] {
    const inputSchema = z.toJSONSchema(invokePresetInputSchema) as Tool['inputSchema']
    delete (inputSchema as { $schema?: unknown }).$schema
    return [
      {
        name: INVOKE_PRESET_TOOL_NAME,
        description: catalogDescription(),
        inputSchema
      }
    ]
  }

  handles(toolName: string): boolean {
    return toolName === INVOKE_PRESET_TOOL_NAME
  }

  async call(toolName: string, args: unknown): Promise<CallToolResult> {
    if (!this.handles(toolName)) {
      return { content: [{ type: 'text', text: `Unknown tool: ${toolName}` }], isError: true }
    }
    try {
      const parsed = invokePresetInputSchema.parse(args ?? {})
      const preset = getPreset(parsed.preset_id)
      if (!preset) {
        return {
          content: [
            {
              type: 'text',
              text: `Error: 未知 preset "${parsed.preset_id}"。可用：${listPresets()
                .map((p) => p.id)
                .join(', ')}`
            }
          ],
          isError: true
        }
      }
      logger.info('invoke_preset dispatched', { preset: preset.id, exec: preset.exec })
      if (preset.exec === 'gateway-rag') {
        const answer = await runGatewayRag(preset, parsed.task)
        return toTextResult(`[preset:${preset.id}｜${preset.name} 执行结果]\n${answer}`)
      }
      // local：主会话按 preset 人设继续执行（工具/能力就是当前会话的全量）
      return toTextResult(
        `[preset:${preset.id}｜${preset.name}] 模式已切换。请按以下人设指令继续完成任务：\n${preset.persona ?? ''}\n\n任务：${parsed.task}`
      )
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      logger.warn('invoke_preset failed', { error: message })
      return { content: [{ type: 'text', text: `Error: ${message}` }], isError: true }
    }
  }
}
