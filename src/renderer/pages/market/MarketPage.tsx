import { useNavigate } from '@tanstack/react-router'
import {
  ArrowLeft,
  BarChart3,
  Briefcase,
  Code2,
  Coffee,
  Compass,
  Database,
  Globe,
  GraduationCap,
  HardDrive,
  Languages,
  Loader2,
  PackageOpen,
  PenLine,
  RefreshCw,
  Scale,
  Search,
  Server,
  Settings2,
  Trash2,
  TrendingUp
} from 'lucide-react'
import type { FC, ReactNode } from 'react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import {
  Badge,
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  EmptyState,
  SegmentedControl,
  Spinner,
  Tooltip
} from '@cherrystudio/ui'
import { Navbar, NavbarCenter } from '@renderer/components/Navbar'
import Scrollbar from '@renderer/components/Scrollbar'
import { useQuery } from '@renderer/data/hooks/useDataApi'
import { useAssistantMutations } from '@renderer/hooks/resourceCatalog'
import {
  toCreateAssistantDtoFromCatalogPreset,
  useAssistantCatalogPresets,
  type AssistantCatalogPreset
} from '@renderer/hooks/useAssistantCatalogPresets'
import { useMcpServers } from '@renderer/hooks/useMcpServer'
import { useSkillCatalog } from '@renderer/hooks/useSkills'
import { toast } from '@renderer/services/toast'
import { readSubscribedKbIds, writeSubscribedKbIds } from '@renderer/utils/dataMarket'
import { cn } from '@renderer/utils/style'
import type { SharedKnowledgeBase } from '@shared/data/types/knowledge'
import type { McpServer } from '@shared/data/types/mcpServer'
import type { MarketCatalogPlugin, MarketCatalogResult, MarketInstalledRecord } from '@shared/types/marketplace'
import { ENTERPRISE_MCP_NAME_PREFIX } from '@shared/utils/enterprise'

import { useSharedKnowledgeBases } from '../knowledge/hooks/useSharedKnowledgeBases'
import { DATA_MARKET_CATEGORIES, matchMarketCategory, TALENT_MARKET_CATEGORIES } from './marketTaxonomy'
import { MarketSearchInput, PluginIcon, StoreSection } from './shared'
import SkillsBlock, { isMarketInstalledSkill, InstalledSkillsList } from './SkillsBlock'

/** 人设市场每个分区一次渲染的行数；「加载更多」逐批放出。 */
const TALENT_PAGE_SIZE = 10

/** 数据市场每个分区一次渲染的行数；「加载更多」逐批放出。 */
const DATA_PAGE_SIZE = 10

/** 数据市场关键词都没命中时的兜底分区 key。 */
const DATA_MORE_KEY = 'more'

/** 人设市场关键词都没命中时的兜底分区 key。 */
const TALENT_MORE_KEY = 'more'

/**
 * 扩展页 = 三类市场：能力市场（公司/外部技能源 + 企业 MCP 服务）、数据市场
 * （可插拔的知识库，来自网关共享库目录）、人设市场（预设智能体）。
 */
type MarketTab = 'plugins' | 'data' | 'talent'

/** 能力市场二级分类：skill 技能货架 / mcp 企业 MCP 服务。 */
type PluginSubTab = 'skill' | 'mcp'

/** 数据市场二级分类：internal 网关企业知识库 / external 外部数据源（待接入）。 */
type DataSubTab = 'internal' | 'external'

/** Page-level navigation: the skills market or the installed-plugins manager. */
type MarketView = { page: 'grid' } | { page: 'manage' }

/** 人才市场一行：预设智能体（与「添加助手」同源）+ 雇佣按钮。 */
const TalentCard: FC<{
  preset: AssistantCatalogPreset
  hired: boolean
  hiring: boolean
  onHire: (preset: AssistantCatalogPreset) => void
  onOpen: (preset: AssistantCatalogPreset) => void
}> = ({ preset, hired, hiring, onHire, onOpen }) => {
  const { t } = useTranslation()
  return (
    <div
      role="button"
      tabIndex={0}
      data-ui="market.talent.card"
      className="flex min-w-0 cursor-pointer items-center gap-3 rounded-xl px-2 py-2.5 transition-colors hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
      onClick={() => onOpen(preset)}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onOpen(preset)
        }
      }}>
      <span
        aria-hidden
        className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-muted text-lg select-none">
        {preset.emoji?.trim() || '🤖'}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-1.5">
          <span className="min-w-0 truncate text-sm font-semibold">{preset.name}</span>
        </div>
        {preset.description && (
          <div className="mt-0.5 truncate text-xs text-muted-foreground">{preset.description}</div>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        {hired ? (
          <Badge variant="outline" className="text-[11px] font-normal text-emerald-600">
            {t('market.talent.hired')}
          </Badge>
        ) : (
          <Button
            variant="secondary"
            size="sm"
            className="rounded-full"
            disabled={hiring}
            data-ui="market.talent.hire"
            onClick={(event) => {
              event.stopPropagation()
              onHire(preset)
            }}>
            {hiring ? <Loader2 className="size-3.5 animate-spin" /> : <Briefcase className="size-3.5" />}
            {t('market.talent.hire')}
          </Button>
        )}
      </div>
    </div>
  )
}

/** 数据市场一行：公共知识库（网关共享库目录 scope=public）+ 接入/移出按钮。 */
const DataKbCard: FC<{
  kb: SharedKnowledgeBase
  subscribed: boolean
  onToggle: (kb: SharedKnowledgeBase) => void
}> = ({ kb, subscribed, onToggle }) => {
  const { t } = useTranslation()
  return (
    <div
      role="button"
      tabIndex={0}
      data-ui="market.data.card"
      className="flex min-w-0 cursor-pointer items-center gap-3 rounded-xl px-2 py-2.5 transition-colors hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
      onClick={() => onToggle(kb)}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onToggle(kb)
        }
      }}>
      <span
        aria-hidden
        className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-muted text-muted-foreground">
        <Database className="size-5" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-1.5">
          <span className="min-w-0 truncate text-sm font-semibold">{kb.name}</span>
        </div>
        {kb.description && <div className="mt-0.5 truncate text-xs text-muted-foreground">{kb.description}</div>}
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        {subscribed ? (
          <Badge variant="outline" className="text-[11px] font-normal text-emerald-600">
            {t('market.data.subscribed')}
          </Badge>
        ) : (
          <Button variant="secondary" size="sm" className="rounded-full" data-ui="market.data.add">
            <Database className="size-3.5" />
            {t('market.data.add')}
          </Button>
        )}
      </div>
    </div>
  )
}

/** 企业 MCP 服务一行：名称（去 [企业] 前缀）+ 类型 + 启用状态；只读（企业下发，MCP 设置页管理）。 */
const EnterpriseMcpCard: FC<{ server: McpServer }> = ({ server }) => {
  const { t } = useTranslation()
  const name = server.name.startsWith(ENTERPRISE_MCP_NAME_PREFIX)
    ? server.name.slice(ENTERPRISE_MCP_NAME_PREFIX.length)
    : server.name
  return (
    <div data-ui="market.mcp.card" className="flex min-w-0 cursor-default items-center gap-3 rounded-xl px-2 py-2.5">
      <span
        aria-hidden
        className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-muted text-muted-foreground">
        <Server className="size-5" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-1.5">
          <span className="min-w-0 truncate text-sm font-semibold">{name}</span>
          {server.type && (
            <Badge variant="outline" className="shrink-0 text-[10px] font-normal">
              {server.type}
            </Badge>
          )}
        </div>
      </div>
      {server.isActive ? (
        <Badge variant="outline" className="shrink-0 text-[11px] font-normal text-emerald-600">
          {t('market.mcp.active')}
        </Badge>
      ) : (
        <Badge variant="outline" className="shrink-0 text-[11px] font-normal text-muted-foreground">
          {t('market.mcp.inactive')}
        </Badge>
      )}
    </div>
  )
}

/** 数据市场二级选项：内部数据 / 外部数据。 */
const DataSubTabs: FC<{ value: DataSubTab; onChange: (value: DataSubTab) => void }> = ({ value, onChange }) => {
  const { t } = useTranslation()
  return (
    <SegmentedControl
      value={value}
      onValueChange={onChange}
      options={[
        { value: 'internal', label: t('market.data.sub.internal') },
        { value: 'external', label: t('market.data.sub.external') }
      ]}
      data-ui="market.data.sub"
    />
  )
}

/** 能力市场 MCP 分区：企业配置统一下发的 MCP 服务（只读货架，管理入口在设置页）。 */
const McpMarketBlock: FC = () => {
  const { t } = useTranslation()
  const { mcpServers, isLoading } = useMcpServers()
  const enterpriseMcps = useMemo(
    () => mcpServers.filter((server) => server.name.startsWith(ENTERPRISE_MCP_NAME_PREFIX)),
    [mcpServers]
  )

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20" data-ui="market.mcp.loading">
        <Spinner text={t('common.loading')} className="text-muted-foreground" />
      </div>
    )
  }
  if (enterpriseMcps.length === 0) {
    return (
      <div className="flex items-center justify-center py-16" data-ui="market.mcp.empty">
        <EmptyState
          icon={Server}
          title={t('market.mcp.empty.title')}
          description={t('market.mcp.empty.description')}
          compact
        />
      </div>
    )
  }
  return (
    <div className="grid gap-x-6 gap-y-1 sm:grid-cols-2" data-ui="market.mcp.list">
      {enterpriseMcps.map((server) => (
        <EnterpriseMcpCard key={server.id} server={server} />
      ))}
    </div>
  )
}

/**
 * 数据市场 = 可插拔的知识库货架。内部数据来自网关共享库目录（scope=public，
 * 管理员在 WeKnora 维护），接入状态存本地订阅表；「添加/移出」即插拔，不改
 * WeKnora 本体。外部数据源为预留分区。
 */
const DataMarketBlock: FC<{ query: string }> = ({ query }) => {
  const { t } = useTranslation()
  const { payload, isLoading } = useSharedKnowledgeBases()
  const [subscribedIds, setSubscribedIds] = useState<string[]>(readSubscribedKbIds)
  // 二级分类：内部数据（网关企业知识库）/ 外部数据（外部数据源，预留）。
  const [dataSubTab, setDataSubTab] = useState<DataSubTab>('internal')
  // 每个数据分区的独立展示条数（key → count）；初始 DATA_PAGE_SIZE，各自加载更多。
  const [dataSectionVisible, setDataSectionVisible] = useState<Record<string, number>>({})

  const publicKbs = useMemo(() => payload.kbs.filter((kb) => kb.scope === 'public'), [payload.kbs])
  const filtered = useMemo(() => {
    const k = query.trim().toLowerCase()
    if (!k) return publicKbs
    return publicKbs.filter(
      (kb) => kb.name.toLowerCase().includes(k) || (kb.description ?? '').toLowerCase().includes(k)
    )
  }, [publicKbs, query])

  const subscribedSet = useMemo(() => new Set(subscribedIds), [subscribedIds])

  const toggle = useCallback((kb: SharedKnowledgeBase) => {
    setSubscribedIds((prev) => {
      const next = prev.includes(kb.id) ? prev.filter((id) => id !== kb.id) : [...prev, kb.id]
      writeSubscribedKbIds(next)
      return next
    })
  }, [])

  if (isLoading) {
    return (
      <div className="flex flex-col gap-4" data-ui="market.data.loading">
        <DataSubTabs value={dataSubTab} onChange={setDataSubTab} />
        <div className="flex items-center justify-center py-20">
          <Spinner text={t('common.loading')} className="text-muted-foreground" />
        </div>
      </div>
    )
  }

  if (dataSubTab === 'external') {
    return (
      <div className="flex flex-col gap-4" data-ui="market.data.external">
        <DataSubTabs value={dataSubTab} onChange={setDataSubTab} />
        <div className="flex items-center justify-center py-16">
          <EmptyState
            icon={Globe}
            title={t('market.data.external.empty.title')}
            description={t('market.data.external.empty.description')}
            compact
          />
        </div>
      </div>
    )
  }

  if (filtered.length === 0) {
    return (
      <div className="flex items-center justify-center py-16" data-ui="market.data.empty">
        <EmptyState
          icon={Database}
          title={t('market.data.empty.title')}
          description={t('market.data.empty.description')}
          compact
        />
      </div>
    )
  }

  // 分区呈现（对齐能力市场的 StoreSection 风格）：固定分类体系 + 关键词落位，
  // 没命中的进「更多数据」兜底——管理员后续补库不需要改代码。
  const searching = Boolean(query.trim())
  const grouped = new Map<string, SharedKnowledgeBase[]>()
  for (const kb of filtered) {
    const category = matchMarketCategory(kb.name, kb.description, DATA_MARKET_CATEGORIES)
    const key = category?.key ?? DATA_MORE_KEY
    const bucket = grouped.get(key)
    if (bucket) bucket.push(kb)
    else grouped.set(key, [kb])
  }
  const orderedKeys = [
    ...DATA_MARKET_CATEGORIES.map((category) => category.key).filter((key) => grouped.has(key)),
    ...(grouped.has(DATA_MORE_KEY) ? [DATA_MORE_KEY] : [])
  ]

  return (
    <div className="flex flex-col gap-6" data-ui="market.data.list">
      <DataSubTabs value={dataSubTab} onChange={setDataSubTab} />
      {searching && (
        <StoreSection
          title={t('market.data.cat.search')}
          icon={<Search size={14} className="text-muted-foreground" />}
          sectionKey="data-search">
          <div className="grid gap-x-6 gap-y-1 sm:grid-cols-2">
            {filtered.map((kb) => (
              <DataKbCard key={kb.id} kb={kb} subscribed={subscribedSet.has(kb.id)} onToggle={toggle} />
            ))}
          </div>
        </StoreSection>
      )}
      {!searching &&
        orderedKeys.map((key) => {
          const items = grouped.get(key)!
          const visibleCount = dataSectionVisible[key] ?? DATA_PAGE_SIZE
          const taken = items.slice(0, visibleCount)
          const remaining = items.length - taken.length
          const icon =
            key === 'standards' ? (
              <Scale size={14} className="text-muted-foreground" />
            ) : key === 'design' ? (
              <Compass size={14} className="text-muted-foreground" />
            ) : key === 'training' ? (
              <GraduationCap size={14} className="text-muted-foreground" />
            ) : key === 'resources' ? (
              <HardDrive size={14} className="text-muted-foreground" />
            ) : (
              <Database size={14} className="text-muted-foreground" />
            )
          return (
            <StoreSection
              key={key}
              title={`${t(`market.data.cat.${key}`)}（${items.length}）`}
              icon={icon}
              sectionKey={`data-${key}`}>
              <div className="grid gap-x-6 gap-y-1 sm:grid-cols-2">
                {taken.map((kb) => (
                  <DataKbCard key={kb.id} kb={kb} subscribed={subscribedSet.has(kb.id)} onToggle={toggle} />
                ))}
              </div>
              {remaining > 0 && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="mt-1 self-center"
                  onClick={() =>
                    setDataSectionVisible((counts) => ({
                      ...counts,
                      [key]: (counts[key] ?? DATA_PAGE_SIZE) + DATA_PAGE_SIZE
                    }))
                  }
                  data-ui="market.data.section-more">
                  {t('market.data.loadMore', { count: remaining })}
                </Button>
              )}
            </StoreSection>
          )
        })}
    </div>
  )
}

/** Component counts derived from an install record's refs, for the manage rows. */
function installedComponentCounts(record: MarketInstalledRecord): Array<{ count: number; key: string }> {
  return [
    { count: record.refs.skillFolderNames.length, key: 'market.component.skill' },
    { count: record.refs.mcpIds.length, key: 'market.component.mcp_server' },
    { count: record.refs.assistantIds.length, key: 'market.component.assistant' },
    { count: record.refs.minappAppIds.length, key: 'market.component.minapp' }
  ].filter((entry) => entry.count > 0)
}

/** installedAt is a plain ISO string; corrupt values render as '-' instead of Invalid Date. */
function formatInstalledAt(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '-' : date.toLocaleDateString()
}

interface ManageViewProps {
  records: MarketInstalledRecord[]
  catalogById: Map<string, MarketCatalogPlugin>
  onBack: () => void
  onBrowse: () => void
  onOpen: () => void
  onRequestUninstall: (target: { id: string; name: string }) => void
}

/** Page-level installed-plugins manager (ZCode 管理已安装): rows with version, install date, component badges, uninstall. */
const ManageView: FC<ManageViewProps> = ({ records, catalogById, onBack, onBrowse, onOpen, onRequestUninstall }) => {
  const { t } = useTranslation()

  return (
    <div className="flex min-h-0 flex-1 flex-col" data-ui="market.manage">
      <Scrollbar className="min-h-0 flex-1">
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 p-6">
          <div data-ui="market.manage.back-row">
            <Button
              variant="ghost"
              size="sm"
              className="-ml-2 gap-1.5 text-muted-foreground"
              onClick={onBack}
              data-ui="market.manage.back">
              <ArrowLeft size={16} />
              {t('market.manage.back')}
            </Button>
          </div>

          {records.length === 0 ? (
            <div className="flex flex-1 items-center justify-center py-20">
              <EmptyState
                icon={PackageOpen}
                title={t('market.manage.empty.title')}
                description={t('market.manage.empty.description')}
                actionLabel={t('market.manage.browse')}
                onAction={onBrowse}
                compact
              />
            </div>
          ) : (
            <>
              <header className="flex items-center gap-2" data-ui="market.manage.header">
                <h1 className="text-2xl font-semibold tracking-tight">{t('market.manage.title')}</h1>
                <Badge variant="secondary" className="text-[11px] font-normal">
                  {records.length}
                </Badge>
              </header>

              <div className="flex flex-col gap-1" data-ui="market.manage.list">
                {records.map((record) => {
                  const inCatalog = catalogById.has(record.pluginId)
                  // 展示名优先取网关目录的最新字段——本地记录可能是旧版 plugin.json 的快照。
                  const displayName = catalogById.get(record.pluginId)?.name ?? record.name ?? record.pluginId
                  return (
                    <div
                      key={record.pluginId}
                      role={inCatalog ? 'button' : undefined}
                      tabIndex={inCatalog ? 0 : undefined}
                      data-ui="market.manage.row"
                      className={cn(
                        'flex min-w-0 items-center gap-3 rounded-xl px-2 py-3 transition-colors',
                        inCatalog && 'cursor-pointer hover:bg-accent focus-visible:bg-accent focus-visible:outline-none'
                      )}
                      onClick={() => inCatalog && onOpen()}
                      onKeyDown={(event) => {
                        if (!inCatalog) return
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault()
                          onOpen()
                        }
                      }}>
                      <PluginIcon iconUrl={catalogById.get(record.pluginId)?.iconUrl} size={40} className="shrink-0" />
                      <div className="min-w-0 flex-1">
                        <div className="flex min-w-0 items-center gap-1.5">
                          <span className="min-w-0 truncate text-sm font-semibold">{displayName}</span>
                          <Badge variant="secondary" className="shrink-0 text-[11px] font-normal">
                            v{catalogById.get(record.pluginId)?.version ?? record.version ?? '-'}
                          </Badge>
                        </div>
                        <div className="mt-0.5 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
                          <span className="shrink-0 whitespace-nowrap">
                            {t('market.manage.installedAt')}: {formatInstalledAt(record.installedAt)}
                          </span>
                          {installedComponentCounts(record).map((entry) => (
                            <Badge key={entry.key} variant="outline" className="shrink-0 text-[11px] font-normal">
                              {entry.count} {t(entry.key)}
                            </Badge>
                          ))}
                        </div>
                      </div>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="shrink-0 gap-1 text-destructive hover:text-destructive"
                        data-ui="market.manage.uninstall"
                        onClick={(event) => {
                          event.stopPropagation()
                          onRequestUninstall({ id: record.pluginId, name: displayName })
                        }}>
                        <Trash2 className="size-3.5" />
                        {t('market.uninstall')}
                      </Button>
                    </div>
                  )
                })}
              </div>
            </>
          )}
        </div>
      </Scrollbar>
    </div>
  )
}

const MarketPage: FC = () => {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const [catalog, setCatalog] = useState<MarketCatalogResult | null>(null)
  const [loading, setLoading] = useState(true)
  const [installed, setInstalled] = useState<MarketInstalledRecord[]>([])
  const [view, setView] = useState<MarketView>({ page: 'grid' })
  const [query, setQuery] = useState('')
  const [activeTab, setActiveTab] = useState<MarketTab>('plugins')
  // 能力市场二级分类：Skill 技能货架 / MCP 企业服务货架。
  const [pluginSubTab, setPluginSubTab] = useState<PluginSubTab>('skill')
  // 每个人设分区的独立展示条数（key → count）；初始 TALENT_PAGE_SIZE，各分区
  // 各自「加载更多」。空 = 全部用初始值。
  const [talentSectionVisible, setTalentSectionVisible] = useState<Record<string, number>>({})
  // SkillsBlock 的公司源/外部榜单随此计数变化重新拉取（页面刷新按钮触发）。
  const [skillsRefreshKey, setSkillsRefreshKey] = useState(0)
  const { data: localSkills } = useSkillCatalog()
  const [hiringPresetId, setHiringPresetId] = useState<string | null>(null)
  const [uninstallTarget, setUninstallTarget] = useState<{ id: string; name: string } | null>(null)
  const [uninstalling, setUninstalling] = useState(false)

  const catalogById = useMemo(() => new Map((catalog?.plugins ?? []).map((plugin) => [plugin.id, plugin])), [catalog])
  // 人才市场：与「添加助手」选择器同源的预设目录；已存在同名助手即视为已雇佣。
  const { isLoading: talentLoading, presets: talentPresets } = useAssistantCatalogPresets({
    enabled: activeTab === 'talent'
  })
  const { data: assistantsData } = useQuery('/assistants', {
    query: { limit: 500 },
    enabled: activeTab === 'talent'
  })
  const { createAssistant } = useAssistantMutations()
  const hiredNames = useMemo(
    () => new Set((assistantsData?.items ?? []).map((assistant) => assistant.name)),
    [assistantsData]
  )
  const talentFiltered = useMemo(() => {
    const list = talentPresets ?? []
    if (!query.trim()) return list
    const k = query.trim().toLowerCase()
    return list.filter(
      (preset) => preset.name.toLowerCase().includes(k) || (preset.description ?? '').toLowerCase().includes(k)
    )
  }, [talentPresets, query])

  const refreshInstalled = useCallback(async () => {
    try {
      setInstalled(await window.api.market.getInstalled())
    } catch {
      // The strip is additive — a failed read leaves it empty rather than blocking the page.
      setInstalled([])
    }
  }, [])

  const loadCatalog = useCallback(async () => {
    setLoading(true)
    try {
      setCatalog(await window.api.market.getCatalog())
    } catch {
      setCatalog({ source: 'error', plugins: [], warnings: [], error: 'unreachable' })
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadCatalog()
    void refreshInstalled()
  }, [loadCatalog, refreshInstalled])

  const handleRefresh = useCallback(() => {
    void loadCatalog()
    void refreshInstalled()
    setSkillsRefreshKey((key) => key + 1)
  }, [loadCatalog, refreshInstalled])

  // 已安装的包统一去自带的技能管理面。
  const openInstalledPack = useCallback(() => void navigate({ to: '/settings/skills' }), [navigate])

  const backToGrid = useCallback(() => setView({ page: 'grid' }), [])

  const confirmUninstall = useCallback(async () => {
    if (!uninstallTarget) return
    setUninstalling(true)
    try {
      const result = await window.api.market.uninstall(uninstallTarget.id)
      if (result.ok) {
        toast.success(t('market.uninstallSuccess', { name: uninstallTarget.name }))
        setUninstallTarget(null)
        await refreshInstalled()
      } else {
        // Keep the dialog open with the error visible — the record stays installed.
        const failed = result.results.find((entry) => entry.status === 'failed')
        toast.error(failed?.error ? `${t('market.uninstallFailed')}: ${failed.error}` : t('market.uninstallFailed'))
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : t('market.uninstallFailed'))
    } finally {
      setUninstalling(false)
    }
  }, [refreshInstalled, t, uninstallTarget])

  const handleHire = useCallback(
    async (preset: AssistantCatalogPreset) => {
      setHiringPresetId(preset.id)
      try {
        await createAssistant(toCreateAssistantDtoFromCatalogPreset(preset))
        toast.success(t('market.talent.hireSuccess', { name: preset.name }))
      } catch (error) {
        toast.error(error instanceof Error ? error.message : t('market.talent.hireFailed'))
      } finally {
        setHiringPresetId(null)
      }
    },
    [createAssistant, t]
  )

  const openTalentPreset = useCallback((preset: AssistantCatalogPreset) => {
    toast.info(preset.description || preset.name)
  }, [])

  // 已安装分类 = 市场安装的技能（公司源 + 外部源）+ 已安装的包（齿轮进入管理）。
  // 内置/系统/本地技能不进市场，留在设置页。
  const marketSkillCount = useMemo(() => localSkills.filter(isMarketInstalledSkill).length, [localSkills])
  const showInstalledSection = installed.length > 0 || marketSkillCount > 0

  return (
    <div data-ui="market.view" className="relative flex h-full min-h-0 flex-1 flex-col text-foreground">
      <Navbar>
        <NavbarCenter className="border-r-0">{t('market.title')}</NavbarCenter>
      </Navbar>

      {view.page === 'grid' ? (
        <Scrollbar className="min-h-0 flex-1">
          <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 p-6">
            <div className="flex flex-col gap-2">
              <h1 className="text-2xl font-semibold tracking-tight" data-ui="market.page-title">
                {t('market.pageTitle')}
              </h1>
              <div className="flex flex-wrap items-start justify-between gap-3">
                <p className="min-w-0 flex-1 text-sm leading-6 text-muted-foreground" data-ui="market.subtitle">
                  {t('market.subtitle')}
                </p>
                <Tooltip title={t('market.refresh')}>
                  <Button
                    variant="outline"
                    size="icon"
                    aria-label={t('market.refresh')}
                    data-ui="market.refresh"
                    disabled={loading}
                    onClick={handleRefresh}>
                    <RefreshCw className={cn('size-4', loading && 'animate-spin')} />
                  </Button>
                </Tooltip>
              </div>
            </div>

            <MarketSearchInput value={query} onChange={setQuery} placeholder={t('market.searchPlaceholder')} />

            <SegmentedControl
              value={activeTab}
              onValueChange={setActiveTab}
              options={[
                { value: 'plugins', label: t('market.tab.plugins') },
                { value: 'data', label: t('market.tab.data') },
                { value: 'talent', label: t('market.tab.talent') }
              ]}
              data-ui="market.tab"
            />

            {activeTab === 'plugins' && showInstalledSection && (
              <section className="flex flex-col" data-ui="market.installed">
                <div className="flex items-center justify-between border-b pb-2">
                  <h2 className="text-base font-semibold text-foreground">{t('market.installed.title')}</h2>
                  {installed.length > 0 && (
                    <Tooltip title={t('market.manageInstalled')}>
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        aria-label={t('market.manageInstalled')}
                        data-ui="market.installed-manage"
                        onClick={() => setView({ page: 'manage' })}>
                        <Settings2 className="size-4" />
                      </Button>
                    </Tooltip>
                  )}
                </div>
                <div
                  className="mt-1 flex items-center gap-3 overflow-x-auto px-2 pt-2 pb-1 sm:-mx-2"
                  data-ui="market.installed-strip">
                  <InstalledSkillsList />
                </div>
              </section>
            )}

            {activeTab === 'plugins' && (
              <div className="flex flex-col gap-4" data-ui="market.plugins.sub-wrap">
                <SegmentedControl
                  value={pluginSubTab}
                  onValueChange={setPluginSubTab}
                  options={[
                    { value: 'skill', label: t('market.plugins.sub.skill') },
                    { value: 'mcp', label: t('market.plugins.sub.mcp') }
                  ]}
                  data-ui="market.plugins.sub"
                />
                {pluginSubTab === 'skill' ? (
                  <SkillsBlock query={query} refreshKey={skillsRefreshKey} />
                ) : (
                  <McpMarketBlock />
                )}
              </div>
            )}
            {activeTab === 'data' && <DataMarketBlock query={query} />}
            {activeTab === 'talent' && (
              <div className="flex flex-col gap-2" data-ui="market.talent.list">
                {talentLoading ? (
                  <div className="flex items-center justify-center py-20" data-ui="market.talent.loading">
                    <Spinner text={t('common.loading')} className="text-muted-foreground" />
                  </div>
                ) : talentFiltered.length === 0 ? (
                  <div className="flex items-center justify-center py-16" data-ui="market.talent.empty">
                    <EmptyState
                      icon={Briefcase}
                      title={t('market.talent.empty.title')}
                      description={t('market.talent.empty.description')}
                      compact
                    />
                  </div>
                ) : (
                  <>
                    {/* 分区呈现（对齐能力市场）：人设按关键词落入固定分区，没命中
                        的进「更多人设」兜底。每个分区独立限量展示（talentVisible
                        是分区初始条数），各自「加载更多」——避免某个大分区吃光全局
                        预算导致只看到一个分区。 */}
                    {(() => {
                      const grouped = new Map<string, AssistantCatalogPreset[]>()
                      for (const preset of talentFiltered) {
                        const category = matchMarketCategory(preset.name, preset.description, TALENT_MARKET_CATEGORIES)
                        const key = category?.key ?? TALENT_MORE_KEY
                        const bucket = grouped.get(key)
                        if (bucket) bucket.push(preset)
                        else grouped.set(key, [preset])
                      }
                      const orderedKeys = [
                        ...TALENT_MARKET_CATEGORIES.map((category) => category.key).filter((key) => grouped.has(key)),
                        ...(grouped.has(TALENT_MORE_KEY) ? [TALENT_MORE_KEY] : [])
                      ]
                      const icons: Record<string, ReactNode> = {
                        writing: <PenLine size={14} className="text-muted-foreground" />,
                        business: <TrendingUp size={14} className="text-muted-foreground" />,
                        dev: <Code2 size={14} className="text-muted-foreground" />,
                        engineering: <Compass size={14} className="text-muted-foreground" />,
                        analysis: <BarChart3 size={14} className="text-muted-foreground" />,
                        translate: <Languages size={14} className="text-muted-foreground" />,
                        education: <GraduationCap size={14} className="text-muted-foreground" />,
                        life: <Coffee size={14} className="text-muted-foreground" />,
                        [TALENT_MORE_KEY]: <Briefcase size={14} className="text-muted-foreground" />
                      }
                      return orderedKeys.map((key) => {
                        const items = grouped.get(key)!
                        const visibleCount = talentSectionVisible[key] ?? TALENT_PAGE_SIZE
                        const taken = items.slice(0, visibleCount)
                        const remaining = items.length - taken.length
                        return (
                          <StoreSection
                            key={key}
                            title={`${t(`market.talent.cat.${key}`)}（${items.length}）`}
                            icon={icons[key]}
                            sectionKey={`talent-${key}`}>
                            <div className="grid gap-x-6 gap-y-1 sm:grid-cols-2">
                              {taken.map((preset) => (
                                <TalentCard
                                  key={preset.id}
                                  preset={preset}
                                  hired={hiredNames.has(preset.name)}
                                  hiring={hiringPresetId === preset.id}
                                  onHire={(target) => void handleHire(target)}
                                  onOpen={openTalentPreset}
                                />
                              ))}
                            </div>
                            {remaining > 0 && (
                              <Button
                                variant="ghost"
                                size="sm"
                                className="mt-1 self-center"
                                onClick={() =>
                                  setTalentSectionVisible((counts) => ({
                                    ...counts,
                                    [key]: (counts[key] ?? TALENT_PAGE_SIZE) + TALENT_PAGE_SIZE
                                  }))
                                }
                                data-ui="market.talent.section-more">
                                {t('market.talent.loadMore', { count: remaining })}
                              </Button>
                            )}
                          </StoreSection>
                        )
                      })
                    })()}
                  </>
                )}
              </div>
            )}
          </div>
        </Scrollbar>
      ) : (
        <ManageView
          records={installed}
          catalogById={catalogById}
          onBack={backToGrid}
          onBrowse={backToGrid}
          onOpen={openInstalledPack}
          onRequestUninstall={setUninstallTarget}
        />
      )}

      <Dialog
        open={uninstallTarget !== null}
        onOpenChange={(open) => !open && !uninstalling && setUninstallTarget(null)}>
        <DialogContent size="sm" showCloseButton={false} data-ui="market.uninstall-dialog">
          <DialogHeader>
            <DialogTitle>{t('market.uninstallConfirmTitle')}</DialogTitle>
            <DialogDescription>
              {t('market.uninstallConfirmDescription', { name: uninstallTarget?.name ?? '' })}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" disabled={uninstalling} onClick={() => setUninstallTarget(null)}>
              {t('common.cancel')}
            </Button>
            <Button variant="destructive" disabled={uninstalling} onClick={() => void confirmUninstall()}>
              {uninstalling ? <Loader2 className="mr-1 size-3.5 animate-spin" /> : null}
              {t('market.uninstallConfirm')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

export default MarketPage
