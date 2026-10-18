import { useTranslation } from 'react-i18next'

import SharedKnowledgeBaseDetail from './components/SharedKnowledgeBaseDetail'
import { KnowledgePageProvider, useKnowledgePage } from './KnowledgePageProvider'
import KnowledgePageDetailSection from './sections/KnowledgePageDetailSection'
import KnowledgePageDialogSection from './sections/KnowledgePageDialogSection'
import KnowledgePageNavigatorSection from './sections/KnowledgePageNavigatorSection'
import KnowledgePageShell from './sections/KnowledgePageShell'

const KnowledgePageContent = () => {
  const { t } = useTranslation()
  const { filePreview, selectedBase, selectedSharedKb } = useKnowledgePage()

  // 一个库都没有时也不再进"创建第一个知识库"引导页：直接展示两栏外壳，
  // 侧栏分区（个人 + / 部门 / 公共）本身就是创建与浏览入口。
  return (
    <KnowledgePageShell>
      <div className={filePreview ? 'hidden' : 'contents'} hidden={Boolean(filePreview)}>
        <KnowledgePageNavigatorSection />
      </div>
      {selectedSharedKb ? (
        <SharedKnowledgeBaseDetail />
      ) : selectedBase ? (
        <KnowledgePageDetailSection />
      ) : (
        <main
          data-ui="knowledge.content"
          className="flex min-h-0 min-w-0 flex-1 items-center justify-center px-6 text-sm text-muted-foreground">
          {t('common.loading')}
        </main>
      )}
    </KnowledgePageShell>
  )
}

interface KnowledgePageProps {
  baseId?: string
  onBaseIdChange: (baseId?: string) => void
}

const KnowledgePage = ({ baseId, onBaseIdChange }: KnowledgePageProps) => {
  return (
    <KnowledgePageProvider baseId={baseId} onBaseIdChange={onBaseIdChange}>
      <KnowledgePageContent />
      <KnowledgePageDialogSection />
    </KnowledgePageProvider>
  )
}

export default KnowledgePage
