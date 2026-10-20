import { beforeEach, describe, expect, it, vi } from 'vitest'

const prefRows = new Map<string, unknown>()
vi.mock('@application', () => ({
  application: {
    get: () => ({
      getDb: () => ({
        select: () => ({
          from: () => ({
            where: () => ({
              limit: () => ({
                all: () => [...prefRows.entries()].map(([key, value]) => ({ key, value }))
              })
            })
          })
        })
      })
    })
  }
}))
vi.mock('@data/db/schemas/preference', () => ({
  preferenceTable: { scope: 'scope', value: 'value', key: 'key' }
}))
vi.mock('@logger', () => ({ loggerService: { withContext: () => ({ warn: vi.fn(), info: vi.fn() }) } }))

import { getPreset, listPresets } from '../presetRegistry'

describe('presetRegistry', () => {
  beforeEach(() => prefRows.clear())

  it('lists built-in presets (universal / kb-quick-answer / kb-deep-research)', () => {
    const presets = listPresets()
    expect(presets.map((p) => p.id)).toEqual(['universal', 'kb-quick-answer', 'kb-deep-research'])
    expect(getPreset('kb-quick-answer')?.exec).toBe('gateway-rag')
  })

  it('merges user overrides by id and accepts newly registered presets', () => {
    prefRows.set('agent.preset_registry', [
      { id: 'kb-quick-answer', name: '快速问答（定制）', description: '改', exec: 'gateway-rag', kbIds: ['ekp'] },
      { id: 'custom-report', name: '周报生成', description: '用户自建', exec: 'local', persona: '按周报模板输出' }
    ])
    const presets = listPresets()
    expect(presets.map((p) => p.id)).toContain('custom-report')
    const overridden = presets.find((p) => p.id === 'kb-quick-answer')
    expect(overridden?.name).toBe('快速问答（定制）')
    expect(overridden?.kbIds).toEqual(['ekp'])
    expect(getPreset('universal')?.exec).toBe('local')
  })

  it('non-array preference rows degrade to defaults', () => {
    prefRows.set('agent.preset_registry', { broken: true })
    expect(listPresets()).toHaveLength(3)
  })
})
