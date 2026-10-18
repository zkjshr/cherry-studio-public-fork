import type { FC } from 'react'

import { Skeleton } from '@cherrystudio/ui'
import { cn } from '@renderer/utils/style'

/**
 * 对话类路由（/app/chat、/app/agents）`beforeLoad` 期间的骨架屏：侧栏 + 消息
 * 区的灰块轮廓。守卫在解析入口会话时有一段无内容可显示的 pending 期——之前是
 * 纯白屏，现在给出与最终布局一致的骨架，观感上是"页面立刻出现了"。
 */
const ConversationEntrySkeleton: FC<{ className?: string }> = ({ className }) => (
  <div data-ui="entry-skeleton" className={cn('flex min-h-0 flex-1 bg-background', className)}>
    <aside className="hidden w-60 shrink-0 flex-col gap-2 border-r p-3 sm:flex">
      <Skeleton className="h-8 w-full rounded-md" />
      <Skeleton className="h-5 w-3/4 rounded-sm" />
      <Skeleton className="h-5 w-2/3 rounded-sm" />
      <Skeleton className="h-5 w-4/5 rounded-sm" />
      <Skeleton className="h-5 w-1/2 rounded-sm" />
      <Skeleton className="mt-auto h-9 w-full rounded-xl" />
    </aside>
    <main className="flex min-w-0 flex-1 flex-col items-center justify-center gap-3 p-6">
      <Skeleton className="h-6 w-48 rounded-md" />
      <Skeleton className="h-4 w-72 rounded-sm" />
      <Skeleton className="h-4 w-60 rounded-sm" />
    </main>
  </div>
)

ConversationEntrySkeleton.displayName = 'ConversationEntrySkeleton'

export default ConversationEntrySkeleton
