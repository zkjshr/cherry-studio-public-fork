import { describe, expect, it, vi } from 'vitest'

const { warnMock } = vi.hoisted(() => ({ warnMock: vi.fn() }))

vi.mock('@logger', () => ({
  loggerService: {
    withContext: () => ({ info: vi.fn(), error: vi.fn(), warn: warnMock, debug: vi.fn(), silly: vi.fn() })
  }
}))

const { resolveKnowledgeBaseScope } = await import('../knowledgeScope')

// Local (persisted) base ids are uuidv4 — `KnowledgeBaseIdSchema` enforces the shape on every write
// path, and `resolveKnowledgeBaseScope` uses that shape to partition local ids from gateway wire
// ids. These fixtures must stay uuidv4-valid or the local-semantics tests below would silently pin
// the gateway union rule instead.
const KB_A = '3f2504e0-4f89-41d3-9a0c-0305e82c3301'
const KB_B = 'b3c0d4a2-1e5f-4a7b-8c9d-0e1f2a3b4c5d'
const KB_C = 'c9bf9f57-6a1e-4d3c-9f2a-7b8c1d2e3f4a'
const KB_PRIVATE = 'd4e5f6a7-b8c9-4d2e-8f1a-2b3c4d5e6f7a'

// Gateway shared bases use the gateway's wire ids (never uuidv4) — the same ids the composer's
// per-turn selection carries for them.
const GW_EKP = 'ekp'
const GW_OTHER = 'kb-202'

describe('resolveKnowledgeBaseScope', () => {
  describe('a static binding is a ceiling, not a floor', () => {
    it('narrows the binding to the selection when the user picked a subset', () => {
      expect(resolveKnowledgeBaseScope([KB_A, KB_B], [KB_A])).toEqual([KB_A])
    })

    it('never widens the binding, and warns about the local ids it dropped', () => {
      warnMock.mockClear()

      expect(resolveKnowledgeBaseScope([KB_A], [KB_A, KB_PRIVATE])).toEqual([KB_A])

      expect(warnMock).toHaveBeenCalledWith(expect.stringContaining('outside the static binding'), {
        outOfScopeIds: [KB_PRIVATE]
      })
    })

    it('keeps the whole binding when the selection is entirely out of scope', () => {
      // No valid narrowing is the same as no narrowing at all — a bogus or stale renderer payload must
      // not silently cut a bound assistant off from its own bases.
      expect(resolveKnowledgeBaseScope([KB_A, KB_B], [KB_PRIVATE])).toEqual([KB_A, KB_B])
    })

    it('keeps the whole binding when the selection is empty', () => {
      expect(resolveKnowledgeBaseScope([KB_A], [])).toEqual([KB_A])
      expect(resolveKnowledgeBaseScope([KB_A], undefined)).toEqual([KB_A])
    })
  })

  it('lets the selection define the scope when there is no binding', () => {
    expect(resolveKnowledgeBaseScope([], [KB_A, KB_B])).toEqual([KB_A, KB_B])
    expect(resolveKnowledgeBaseScope(undefined, [KB_B])).toEqual([KB_B])
  })

  it('returns an empty scope when neither source has ids', () => {
    expect(resolveKnowledgeBaseScope(undefined, undefined)).toEqual([])
    expect(resolveKnowledgeBaseScope([], [])).toEqual([])
  })

  describe('canonical output', () => {
    // Callers compare scopes by value and hash them into rebuild signatures, so equal scopes must
    // serialize identically no matter which branch produced them or how the input was ordered.
    it('deduplicates and sorts the selection branch', () => {
      expect(resolveKnowledgeBaseScope([], [KB_B, KB_A, KB_B])).toEqual([KB_A, KB_B])
    })

    it('deduplicates and sorts the binding branch', () => {
      expect(resolveKnowledgeBaseScope([KB_B, KB_A, KB_B], undefined)).toEqual([KB_A, KB_B])
    })

    it('deduplicates and sorts the narrowed branch', () => {
      expect(resolveKnowledgeBaseScope([KB_A, KB_B, KB_C], [KB_C, KB_A, KB_C])).toEqual([KB_A, KB_C])
    })
  })

  describe('gateway shared bases follow the bound ∪ selected union rule', () => {
    it('admits a gateway id the user selected this turn even though it is unbound', () => {
      // Bindings predate gateway-aware composers, so an existing binding can never contain gateway
      // ids — the local narrowing rule must not swallow a deliberate per-turn gateway pick.
      expect(resolveKnowledgeBaseScope([KB_A], [GW_EKP])).toEqual([KB_A, GW_EKP])
    })

    it('admits a bound gateway id even when the selection does not repeat it', () => {
      expect(resolveKnowledgeBaseScope([GW_EKP, KB_A], undefined)).toEqual([KB_A, GW_EKP])
      expect(resolveKnowledgeBaseScope([GW_EKP, KB_A], [KB_A])).toEqual([KB_A, GW_EKP])
    })

    it('never admits a gateway id that is neither bound nor selected', () => {
      expect(resolveKnowledgeBaseScope([KB_A], [KB_B, GW_EKP])).toEqual([KB_A, GW_EKP])
      // A gateway-only binding grants no local bases, so locally the agent is unbound and the
      // selection defines the local scope (same rule as an agent with an empty binding).
      expect(resolveKnowledgeBaseScope([GW_EKP], [KB_A])).toEqual([KB_A, GW_EKP])
      expect(resolveKnowledgeBaseScope([KB_A, GW_OTHER], [KB_B, GW_EKP])).toEqual([KB_A, GW_EKP, GW_OTHER])
    })

    it('does not warn about gateway picks — they are in scope by rule, not by grace', () => {
      warnMock.mockClear()
      resolveKnowledgeBaseScope([KB_A], [GW_EKP, GW_OTHER])
      expect(warnMock).not.toHaveBeenCalled()
    })

    it('keeps gateway ids canonical in the merged output', () => {
      expect(resolveKnowledgeBaseScope([GW_EKP], [GW_OTHER, GW_EKP])).toEqual([GW_EKP, GW_OTHER])
    })
  })
})
