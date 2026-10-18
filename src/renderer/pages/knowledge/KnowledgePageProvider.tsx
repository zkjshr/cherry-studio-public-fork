import {
  createContext,
  type PropsWithChildren,
  type RefObject,
  use,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState
} from 'react'
import { useTranslation } from 'react-i18next'

import {
  useCreateKnowledgeBase,
  useDeleteKnowledgeBase,
  useKnowledgeBases,
  useRestoreKnowledgeBase,
  useUpdateKnowledgeBase
} from '@renderer/hooks/useKnowledgeBase'
import { EVENT_NAMES, EventEmitter } from '@renderer/services/EventService'
import { toast } from '@renderer/services/toast'
import { readSubscribedKbIds } from '@renderer/utils/dataMarket'
import { formatErrorMessageWithPrefix } from '@renderer/utils/error'
import type { KnowledgeBaseListItem } from '@shared/data/api/schemas/knowledges'
import type { Group } from '@shared/data/types/group'
import type { KnowledgeBase, KnowledgeItemType, SharedKnowledgeBase } from '@shared/data/types/knowledge'

import {
  useCreateKnowledgeGroup,
  useDeleteKnowledgeGroup,
  useKnowledgeGroups,
  useUpdateKnowledgeGroup
} from './hooks/useKnowledgeGroups'
import { useSharedKnowledgeBases } from './hooks/useSharedKnowledgeBases'
import type { KnowledgeRestoreBaseInitialValues } from './panels/ragConfig/RagConfigPanel'
import type { KnowledgeFilePreviewTarget, KnowledgeTabKey } from './types'

type EditableKnowledgeGroup = Pick<Group, 'id' | 'name'>
type EditableKnowledgeBase = Pick<KnowledgeBase, 'id' | 'name'>
type CreateKnowledgeBase = ReturnType<typeof useCreateKnowledgeBase>['createBase']
type RestoreKnowledgeBase = ReturnType<typeof useRestoreKnowledgeBase>['restoreBase']

interface KnowledgePageContextValue {
  bases: KnowledgeBaseListItem[]
  groups: Group[]
  isLoading: boolean
  /** 共享库目录（网关）是否仍在加载——全屏空态必须等它结束，避免企业用户误入"创建第一个库"。 */
  isSharedKbsLoading: boolean
  /** 企业共享知识库只读目录（部门/公共），来自网关，客户端不可增删改。 */
  sharedKbs: SharedKnowledgeBase[]
  /** 第一个有置顶话题的受管助手的话题 id；共享库详情"去对话检索"跳转用（Task 4）。 */
  chatTopicId: string | null
  selectedSharedKbId: string | null
  selectedSharedKb: SharedKnowledgeBase | undefined
  selectSharedKb: (kbId: string) => void
  selectedBase: KnowledgeBase | undefined
  selectedBaseId: string
  selectedItemId: string | null
  /** Which detail view the selected item opens into: its original content or its indexed chunks. */
  selectedItemView: 'content' | 'chunks'
  filePreview: KnowledgeFilePreviewTarget | null
  baseNavigationVersion: number
  activeTab: KnowledgeTabKey
  contentRef: RefObject<HTMLDivElement | null>
  editingBase: EditableKnowledgeBase | null
  editingGroup: EditableKnowledgeGroup | null
  restoringBase: KnowledgeBase | null
  restoreBaseInitialValues: KnowledgeRestoreBaseInitialValues | undefined
  isAddSourceDialogOpen: boolean
  pendingAddSource: KnowledgeItemType | undefined
  pendingAddFiles: File[] | undefined
  isRagConfigDrawerOpen: boolean
  isRecallTestDrawerOpen: boolean
  isCreateBaseDialogOpen: boolean
  isCreateGroupDialogOpen: boolean
  createBaseInitialGroupId: string | undefined
  isCreatingBase: boolean
  isCreatingGroup: boolean
  isUpdatingBase: boolean
  isUpdatingGroup: boolean
  isRestoringBase: boolean
  createBase: CreateKnowledgeBase
  restoreBase: RestoreKnowledgeBase
  selectBase: (baseId: string) => void
  setActiveTab: (tab: KnowledgeTabKey) => void
  openItemChunks: (itemId: string) => void
  openItemContent: (itemId: string) => void
  closeItemChunks: () => void
  openFilePreview: (target: KnowledgeFilePreviewTarget) => void
  closeFilePreview: () => void
  openAddSourceDialog: (source?: KnowledgeItemType, files?: File[]) => void
  openRagConfigDrawer: () => void
  openRecallTestDrawer: () => void
  handleRagConfigDrawerOpenChange: (open: boolean) => void
  handleRecallTestDrawerOpenChange: (open: boolean) => void
  openCreateBaseDialog: (groupId?: string) => void
  openCreateGroupDialog: (baseIdToMove?: string) => void
  openRenameBaseDialog: (base: EditableKnowledgeBase) => void
  openRenameGroupDialog: (group: EditableKnowledgeGroup) => void
  openRestoreBaseDialog: (base: KnowledgeBase, initialValues?: KnowledgeRestoreBaseInitialValues) => void
  handleAddSourceDialogOpenChange: (open: boolean) => void
  handleCreateBaseDialogOpenChange: (open: boolean) => void
  handleCreateGroupDialogOpenChange: (open: boolean) => void
  handleRenameBaseDialogOpenChange: (open: boolean) => void
  handleRenameGroupDialogOpenChange: (open: boolean) => void
  handleRestoreBaseDialogOpenChange: (open: boolean) => void
  handleCreateBaseCreated: (createdBase: { id: string }) => void
  handleRestoreBaseRestored: (restoredBase: { id: string }) => void
  submitCreateGroup: (name: string) => Promise<void>
  submitRenameBase: (name: string) => Promise<void>
  submitRenameGroup: (name: string) => Promise<void>
  moveBase: (baseId: string, groupId: string | null) => Promise<void>
  deleteBase: (baseId: string) => Promise<void>
  deleteGroup: (groupId: string) => Promise<void>
}

const KnowledgePageContext = createContext<KnowledgePageContextValue | null>(null)

interface KnowledgePageProviderProps extends PropsWithChildren {
  baseId?: string
  onBaseIdChange: (baseId?: string) => void
}

export const KnowledgePageProvider = ({ children, baseId, onBaseIdChange }: KnowledgePageProviderProps) => {
  const { t } = useTranslation()
  const { bases, isLoading, error: basesError } = useKnowledgeBases()
  const { payload: sharedPayload, isLoading: isSharedKbsLoading } = useSharedKnowledgeBases()
  // 公共库只在数据市场（扩展页）接入后出现在知识库页；部门库由网关按登录
  // 用户过滤（X-Client-User + user_departments 映射），本地不再二次过滤。
  const subscribedKbIds = useMemo(() => new Set(readSubscribedKbIds()), [])
  const sharedKbs = useMemo(
    () =>
      sharedPayload.kbs.filter((kb) => {
        if (kb.scope === 'public') return subscribedKbIds.has(kb.id)
        return true
      }),
    [sharedPayload.kbs, subscribedKbIds]
  )
  const chatTopicId = sharedPayload.chatTopicId
  const { groups } = useKnowledgeGroups()
  const { createGroup, isCreating: isCreatingGroup } = useCreateKnowledgeGroup()
  const { createBase, isCreating: isCreatingBase } = useCreateKnowledgeBase()
  const { restoreBase, isRestoring: isRestoringBase } = useRestoreKnowledgeBase()
  const { updateBase, isUpdating: isUpdatingBase } = useUpdateKnowledgeBase()
  const { updateGroup, isUpdating: isUpdatingGroup } = useUpdateKnowledgeGroup()
  const { deleteBase } = useDeleteKnowledgeBase()
  const { deleteGroup } = useDeleteKnowledgeGroup()
  const selectedBaseId = baseId ?? ''
  const [selectedItemId, setSelectedItemId] = useState<string | null>(null)
  const [selectedItemView, setSelectedItemView] = useState<'content' | 'chunks'>('content')
  const [filePreview, setFilePreview] = useState<KnowledgeFilePreviewTarget | null>(null)
  const [previewNavigationVersion, setPreviewNavigationVersion] = useState(0)
  const [baseNavigationVersion, setBaseNavigationVersion] = useState(0)
  const previewNavigationVersionRef = useRef(0)
  const [pendingSelectedBaseId, setPendingSelectedBaseId] = useState<string | null>(null)
  const pendingSelectedBaseListRef = useRef<KnowledgeBase[] | null>(null)
  const [activeTab, setActiveTab] = useState<KnowledgeTabKey>('data')
  const [editingBase, setEditingBase] = useState<EditableKnowledgeBase | null>(null)
  const [editingGroup, setEditingGroup] = useState<EditableKnowledgeGroup | null>(null)
  const [restoringBase, setRestoringBase] = useState<KnowledgeBase | null>(null)
  const [restoreBaseInitialValues, setRestoreBaseInitialValues] = useState<
    KnowledgeRestoreBaseInitialValues | undefined
  >()
  const [isAddSourceDialogOpen, setIsAddSourceDialogOpen] = useState(false)
  const [pendingAddSource, setPendingAddSource] = useState<KnowledgeItemType | undefined>()
  const [pendingAddFiles, setPendingAddFiles] = useState<File[] | undefined>()
  const [isRagConfigDrawerOpen, setIsRagConfigDrawerOpen] = useState(false)
  const [isRecallTestDrawerOpen, setIsRecallTestDrawerOpen] = useState(false)
  const [isCreateBaseDialogOpen, setIsCreateBaseDialogOpen] = useState(false)
  const [createBaseInitialGroupId, setCreateBaseInitialGroupId] = useState<string | undefined>()
  const [isCreateGroupDialogOpen, setIsCreateGroupDialogOpen] = useState(false)
  // Set when the create-group dialog is opened from a base's context menu: the
  // freshly created group immediately adopts that base (create-and-move in one go).
  const [pendingGroupMoveBaseId, setPendingGroupMoveBaseId] = useState<string | null>(null)
  const contentRef = useRef<HTMLDivElement>(null)

  const selectedBase = useMemo(() => {
    return bases.find((base) => base.id === selectedBaseId)
  }, [bases, selectedBaseId])

  const [selectedSharedKbId, setSelectedSharedKbId] = useState<string | null>(null)
  const selectedSharedKb = useMemo(
    () => sharedKbs.find((kb) => kb.id === selectedSharedKbId),
    [sharedKbs, selectedSharedKbId]
  )

  // 陈旧选中清理：共享目录一旦降级为空（网关不可达/响应异常），已选中的共享库不再
  // 解析，而 selectedSharedKbId 仍卡在选中态——自动选中效果被它压制，详情区会永远
  // 停在加载占位。目录不在加载中却解析不到时清掉选中，让自动选中恢复接管。
  useEffect(() => {
    if (isSharedKbsLoading || !selectedSharedKbId || selectedSharedKb) return

    setSelectedSharedKbId(null)
  }, [isSharedKbsLoading, selectedSharedKb, selectedSharedKbId])

  const resetFilePreview = useCallback(() => {
    const nextVersion = previewNavigationVersionRef.current + 1
    previewNavigationVersionRef.current = nextVersion
    setPreviewNavigationVersion(nextVersion)
    setFilePreview(null)
  }, [])

  const resetBaseNavigation = useCallback(() => {
    resetFilePreview()
    setBaseNavigationVersion((version) => version + 1)
  }, [resetFilePreview])

  const openFilePreview = useCallback(
    (target: KnowledgeFilePreviewTarget) => {
      if (previewNavigationVersionRef.current !== previewNavigationVersion) return
      setFilePreview(target)
    },
    [previewNavigationVersion]
  )

  // `selectedSharedKbId` guard: while a shared (dept/public) kb is selected the detail
  // area shows that catalog entry, so the auto-select must not bounce the route back
  // to the first local base.
  useEffect(() => {
    if (isLoading || basesError || selectedSharedKbId) return

    if (pendingSelectedBaseId) {
      if (bases.some((base) => base.id === pendingSelectedBaseId)) {
        setPendingSelectedBaseId(null)
        pendingSelectedBaseListRef.current = null
        return
      }

      if (bases.length === 0 || bases === pendingSelectedBaseListRef.current) {
        return
      }

      setPendingSelectedBaseId(null)
      pendingSelectedBaseListRef.current = null
    }

    if (bases.length === 0) {
      if (selectedBaseId) {
        resetBaseNavigation()
        onBaseIdChange()
      }
      setSelectedItemId(null)
      return
    }

    const hasSelectedBase = bases.some((base) => base.id === selectedBaseId)
    if (!selectedBaseId || !hasSelectedBase) {
      resetBaseNavigation()
      setSelectedItemId(null)
      onBaseIdChange(bases[0].id)
    }
  }, [
    bases,
    basesError,
    isLoading,
    onBaseIdChange,
    pendingSelectedBaseId,
    resetBaseNavigation,
    selectedBaseId,
    selectedSharedKbId
  ])

  const selectBase = useCallback(
    (baseId: string) => {
      resetBaseNavigation()
      setSelectedSharedKbId(null)

      if (bases.some((base) => base.id === baseId)) {
        setPendingSelectedBaseId(null)
        pendingSelectedBaseListRef.current = null
      } else {
        setPendingSelectedBaseId(baseId)
        pendingSelectedBaseListRef.current = bases
      }

      onBaseIdChange(baseId)
      setSelectedItemId(null)
    },
    [bases, resetBaseNavigation, onBaseIdChange]
  )

  const selectSharedKb = useCallback(
    (kbId: string) => {
      resetBaseNavigation()
      setSelectedItemId(null)
      setSelectedSharedKbId(kbId)
      // 释放本地库路由选中：详情区按 selectedSharedKb 分流（Task 4）。
      if (selectedBaseId) {
        onBaseIdChange()
      }
    },
    [resetBaseNavigation, selectedBaseId, onBaseIdChange]
  )

  useEffect(() => {
    resetBaseNavigation()
    setSelectedItemId(null)
  }, [selectedBaseId, resetBaseNavigation])

  useEffect(() => {
    const unsubscribe = EventEmitter.on(EVENT_NAMES.GLOBAL_SEARCH_SELECT_KNOWLEDGE_BASE, (baseId) => {
      if (typeof baseId !== 'string') return
      selectBase(baseId)
    })

    return () => {
      unsubscribe()
    }
  }, [selectBase])

  const handleSetActiveTab = useCallback((tab: KnowledgeTabKey) => {
    setActiveTab(tab)
    setSelectedItemId(null)
  }, [])

  const openItemChunks = useCallback((itemId: string) => {
    setSelectedItemView('chunks')
    setSelectedItemId(itemId)
  }, [])

  const openItemContent = useCallback((itemId: string) => {
    setSelectedItemView('content')
    setSelectedItemId(itemId)
  }, [])

  const closeItemChunks = useCallback(() => {
    setSelectedItemId(null)
  }, [])

  const openCreateBaseDialog = useCallback((groupId?: string) => {
    setCreateBaseInitialGroupId(groupId)
    setIsCreateBaseDialogOpen(true)
  }, [])

  const openAddSourceDialog = useCallback((source?: KnowledgeItemType, files?: File[]) => {
    setPendingAddSource(source)
    setPendingAddFiles(files?.length ? files : undefined)
    setIsAddSourceDialogOpen(true)
  }, [])

  const openRagConfigDrawer = useCallback(() => {
    setIsRagConfigDrawerOpen(true)
  }, [])

  const openRecallTestDrawer = useCallback(() => {
    setIsRecallTestDrawerOpen(true)
  }, [])

  const handleRagConfigDrawerOpenChange = useCallback((open: boolean) => {
    setIsRagConfigDrawerOpen(open)
  }, [])

  const handleRecallTestDrawerOpenChange = useCallback((open: boolean) => {
    setIsRecallTestDrawerOpen(open)
  }, [])

  const openCreateGroupDialog = useCallback((baseIdToMove?: string) => {
    setPendingGroupMoveBaseId(baseIdToMove ?? null)
    setIsCreateGroupDialogOpen(true)
  }, [])

  const openRenameBaseDialog = useCallback((base: EditableKnowledgeBase) => {
    setEditingBase(base)
  }, [])

  const openRenameGroupDialog = useCallback((group: EditableKnowledgeGroup) => {
    setEditingGroup(group)
  }, [])

  const openRestoreBaseDialog = useCallback(
    (base: KnowledgeBase, initialValues?: KnowledgeRestoreBaseInitialValues) => {
      setRestoringBase(base)
      setRestoreBaseInitialValues(initialValues)
    },
    []
  )

  const handleCreateBaseDialogOpenChange = useCallback((open: boolean) => {
    setIsCreateBaseDialogOpen(open)

    if (!open) {
      setCreateBaseInitialGroupId(undefined)
    }
  }, [])

  const handleAddSourceDialogOpenChange = useCallback((open: boolean) => {
    setIsAddSourceDialogOpen(open)
    if (!open) {
      setPendingAddSource(undefined)
      setPendingAddFiles(undefined)
    }
  }, [])

  const handleCreateGroupDialogOpenChange = useCallback((open: boolean) => {
    setIsCreateGroupDialogOpen(open)

    if (!open) {
      setPendingGroupMoveBaseId(null)
    }
  }, [])

  const handleRenameBaseDialogOpenChange = useCallback((open: boolean) => {
    if (!open) {
      setEditingBase(null)
    }
  }, [])

  const handleRenameGroupDialogOpenChange = useCallback((open: boolean) => {
    if (!open) {
      setEditingGroup(null)
    }
  }, [])

  const handleRestoreBaseDialogOpenChange = useCallback((open: boolean) => {
    if (!open) {
      setRestoringBase(null)
      setRestoreBaseInitialValues(undefined)
    }
  }, [])

  const handleCreateBaseCreated = useCallback(
    (createdBase: { id: string }) => {
      selectBase(createdBase.id)
    },
    [selectBase]
  )

  const handleRestoreBaseRestored = useCallback(
    (restoredBase: { id: string }) => {
      setRestoringBase(null)
      setRestoreBaseInitialValues(undefined)
      selectBase(restoredBase.id)
    },
    [selectBase]
  )

  const moveBase = useCallback(
    async (baseId: string, groupId: string | null) => {
      try {
        await updateBase(baseId, { groupId })
      } catch (error) {
        toast.error(formatErrorMessageWithPrefix(error, t('knowledge.error.failed_to_move')))
      }
    },
    [t, updateBase]
  )

  const submitCreateGroup = useCallback(
    async (name: string) => {
      const group = await createGroup(name)
      const baseIdToMove = pendingGroupMoveBaseId
      setIsCreateGroupDialogOpen(false)
      setPendingGroupMoveBaseId(null)

      // Opened from a base's context menu: the new group adopts that base right
      // away. moveBase surfaces its own failure toast and never rejects, so a
      // failed move can't resurrect the already-closed dialog.
      if (baseIdToMove) {
        await moveBase(baseIdToMove, group.id)
      }
    },
    [createGroup, moveBase, pendingGroupMoveBaseId]
  )

  const submitRenameBase = useCallback(
    async (name: string) => {
      if (!editingBase) {
        return
      }

      if (name === editingBase.name.trim()) {
        setEditingBase(null)
        return
      }

      await updateBase(editingBase.id, { name })
      setEditingBase(null)
    },
    [editingBase, updateBase]
  )

  const submitRenameGroup = useCallback(
    async (name: string) => {
      if (!editingGroup) {
        return
      }

      if (name === editingGroup.name.trim()) {
        setEditingGroup(null)
        return
      }

      await updateGroup(editingGroup.id, { name })
      setEditingGroup(null)
    },
    [editingGroup, updateGroup]
  )

  const handleDeleteBase = useCallback(
    async (baseId: string) => {
      try {
        await deleteBase(baseId)
      } catch (error) {
        toast.error(formatErrorMessageWithPrefix(error, t('knowledge.error.failed_to_delete')))
      }
    },
    [deleteBase, t]
  )

  const handleDeleteGroup = useCallback(
    async (groupId: string) => {
      try {
        await deleteGroup(groupId)
      } catch (error) {
        toast.error(formatErrorMessageWithPrefix(error, t('knowledge.groups.error.failed_to_delete')))
      }
    },
    [deleteGroup, t]
  )

  const value = useMemo<KnowledgePageContextValue>(
    () => ({
      bases,
      groups,
      isLoading,
      isSharedKbsLoading,
      sharedKbs,
      chatTopicId,
      selectedSharedKbId,
      selectedSharedKb,
      selectSharedKb,
      selectedBase,
      selectedBaseId,
      selectedItemId,
      selectedItemView,
      filePreview,
      baseNavigationVersion,
      activeTab,
      contentRef,
      editingBase,
      editingGroup,
      restoringBase,
      restoreBaseInitialValues,
      isAddSourceDialogOpen,
      pendingAddSource,
      pendingAddFiles,
      isRagConfigDrawerOpen,
      isRecallTestDrawerOpen,
      isCreateBaseDialogOpen,
      isCreateGroupDialogOpen,
      createBaseInitialGroupId,
      isCreatingBase,
      isCreatingGroup,
      isUpdatingBase,
      isUpdatingGroup,
      isRestoringBase,
      createBase,
      restoreBase,
      selectBase,
      setActiveTab: handleSetActiveTab,
      openItemChunks,
      openItemContent,
      closeItemChunks,
      openFilePreview,
      closeFilePreview: resetFilePreview,
      openAddSourceDialog,
      openRagConfigDrawer,
      openRecallTestDrawer,
      handleRagConfigDrawerOpenChange,
      handleRecallTestDrawerOpenChange,
      openCreateBaseDialog,
      openCreateGroupDialog,
      openRenameBaseDialog,
      openRenameGroupDialog,
      openRestoreBaseDialog,
      handleAddSourceDialogOpenChange,
      handleCreateBaseDialogOpenChange,
      handleCreateGroupDialogOpenChange,
      handleRenameBaseDialogOpenChange,
      handleRenameGroupDialogOpenChange,
      handleRestoreBaseDialogOpenChange,
      handleCreateBaseCreated,
      handleRestoreBaseRestored,
      submitCreateGroup,
      submitRenameBase,
      submitRenameGroup,
      moveBase,
      deleteBase: handleDeleteBase,
      deleteGroup: handleDeleteGroup
    }),
    [
      activeTab,
      baseNavigationVersion,
      bases,
      chatTopicId,
      createBase,
      editingBase,
      editingGroup,
      restoringBase,
      restoreBaseInitialValues,
      groups,
      handleAddSourceDialogOpenChange,
      handleCreateBaseCreated,
      handleCreateBaseDialogOpenChange,
      handleCreateGroupDialogOpenChange,
      handleDeleteBase,
      handleDeleteGroup,
      handleSetActiveTab,
      handleRenameBaseDialogOpenChange,
      handleRenameGroupDialogOpenChange,
      handleRestoreBaseDialogOpenChange,
      handleRestoreBaseRestored,
      isAddSourceDialogOpen,
      pendingAddFiles,
      pendingAddSource,
      isRagConfigDrawerOpen,
      isRecallTestDrawerOpen,
      handleRagConfigDrawerOpenChange,
      handleRecallTestDrawerOpenChange,
      openRagConfigDrawer,
      openRecallTestDrawer,
      isCreateBaseDialogOpen,
      isCreateGroupDialogOpen,
      createBaseInitialGroupId,
      isCreatingBase,
      isCreatingGroup,
      isLoading,
      isSharedKbsLoading,
      isUpdatingBase,
      isUpdatingGroup,
      isRestoringBase,
      filePreview,
      moveBase,
      openAddSourceDialog,
      closeItemChunks,
      openFilePreview,
      openItemChunks,
      openItemContent,
      openCreateBaseDialog,
      openCreateGroupDialog,
      openRenameBaseDialog,
      openRenameGroupDialog,
      openRestoreBaseDialog,
      restoreBase,
      resetFilePreview,
      selectBase,
      selectSharedKb,
      selectedBase,
      selectedBaseId,
      selectedItemId,
      selectedItemView,
      selectedSharedKb,
      selectedSharedKbId,
      sharedKbs,
      submitCreateGroup,
      submitRenameBase,
      submitRenameGroup
    ]
  )

  return <KnowledgePageContext value={value}>{children}</KnowledgePageContext>
}

export const useKnowledgePage = () => {
  const context = use(KnowledgePageContext)

  if (!context) {
    throw new Error('useKnowledgePage must be used within KnowledgePageProvider')
  }

  return context
}
