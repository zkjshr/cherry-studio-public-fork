import { loggerService } from '@logger'
import { KnowledgeBaseIdSchema } from '@shared/data/types/knowledge'

const logger = loggerService.withContext('knowledgeScope')

/**
 * Local (persisted) knowledge base ids are uuidv4 — `KnowledgeBaseIdSchema` enforces it at every
 * write path. Gateway shared bases (企业共享库, incl. the virtual「ekp数据检索」) materialize as
 * in-memory pseudo bases whose ids are the gateway wire ids (`ekp`, WeKnora uuids) and are never
 * uuidv4 (see gatewayKbSearch.pseudoBase). So id shape is the reliable local/gateway partition:
 * anything that fails the uuidv4 schema is gateway-shaped.
 */
function isGatewayShapedId(id: string): boolean {
  return !KnowledgeBaseIdSchema.safeParse(id).success
}

/**
 * Resolve the effective knowledge scope used by assistant and Agent runtimes.
 *
 * A static binding is a ceiling, never a floor: main cannot trust a renderer/IPC-supplied id list to
 * stay within the configured bases (the composer UI happens to restrict picks to that set today, but
 * that's a UI nicety, not a security boundary). So a per-turn selection can only ever NARROW a
 * binding — local ids outside it are dropped, and the scope can never be widened into an unbound
 * local base. Within the ceiling the selection wins, so the composer picker means what it shows. An
 * empty (or entirely out-of-scope) selection means "no per-turn narrowing", not "no knowledge" — it
 * falls back to the full binding rather than silently cutting the agent off from its own bases.
 *
 * With no static binding the selection alone defines the scope — the actual gap this resolves:
 * composer-only, ad-hoc knowledge base use.
 *
 * Gateway shared bases follow a union rule instead of the narrowing rule: a gateway id is in scope
 * when the binding lists it OR the user selected it this turn. Rationale: gateway ids can appear in
 * a binding only since the composer learned to surface them (older bindings never contain them, so
 * treating the binding as their ceiling would make every gateway pick from an older-bound agent
 * unusable), and gateway authorization is enforced by the gateway on every search call — the local
 * scope is a UX filter there, not a security boundary (see gatewayKbSearch). A gateway id neither
 * bound nor selected still never appears. This union never touches local-id semantics: the
 * narrowing/ceiling rules above apply to uuidv4 ids exactly as before.
 *
 * The result is canonical (deduplicated, sorted) so callers can compare two scopes by value and
 * hash one into a rebuild signature without re-normalizing.
 */
export function resolveKnowledgeBaseScope(
  configuredIds: readonly string[] | null | undefined,
  selectedIds: readonly string[] | null | undefined
): string[] {
  const configured = configuredIds ?? []
  const selected = selectedIds ?? []

  // Gateway union: bound ∪ selected, no narrowing, no out-of-scope warning.
  const gatewayIds = canonicalize([...configured.filter(isGatewayShapedId), ...selected.filter(isGatewayShapedId)])

  const configuredLocal = configured.filter((id) => !isGatewayShapedId(id))
  const selectedLocal = selected.filter((id) => !isGatewayShapedId(id))

  let localIds: string[]
  if (!configuredLocal.length) {
    localIds = canonicalize(selectedLocal)
  } else if (!selectedLocal.length) {
    localIds = canonicalize(configuredLocal)
  } else {
    const configuredSet = new Set(configuredLocal)
    const narrowed = selectedLocal.filter((id) => configuredSet.has(id))
    if (narrowed.length !== selectedLocal.length) {
      logger.warn('Dropped selected knowledge bases outside the static binding', {
        outOfScopeIds: selectedLocal.filter((id) => !configuredSet.has(id))
      })
    }
    // No valid narrowing is the same as no narrowing at all — a bogus or stale renderer payload
    // must not silently cut a bound assistant off from its own bases.
    localIds = canonicalize(narrowed.length ? narrowed : configuredLocal)
  }

  return canonicalize([...localIds, ...gatewayIds])
}

function canonicalize(ids: readonly string[] | null | undefined): string[] {
  return Array.from(new Set(ids ?? [])).sort()
}
