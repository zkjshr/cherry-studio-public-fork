import { application } from '@application'
import { loggerService } from '@logger'
import { listGatewayKbPseudoBases, listSharedKnowledgeBases } from '@main/features/knowledge'
import { ErrorCode, isDataApiError } from '@shared/data/api/errors'
import { IpcError } from '@shared/ipc/errors/IpcError'
import { knowledgeErrorCodes } from '@shared/ipc/errors/knowledge'
import type { knowledgeRequestSchemas } from '@shared/ipc/schemas/knowledge'
import type { IpcHandlersFor } from '@shared/ipc/types'

const logger = loggerService.withContext('knowledgeHandlers')

/**
 * Thin adapters for the knowledge request routes: each one translates a parsed route
 * call into a `KnowledgeService` method (business logic + resource lifecycle stay in
 * that service). These routes act on shared business data, not the caller's window, so
 * they ignore `IpcContext` — there is no `senderId` addressing here (contrast window.ts).
 *
 * Void-output routes use a block body so the arrow resolves `undefined`, matching the
 * route's `z.void()` output (see selection.ts hide_toolbar).
 */
export const knowledgeHandlers: IpcHandlersFor<typeof knowledgeRequestSchemas> = {
  'knowledge.create_base': async ({ base }) => application.get('KnowledgeService').createBase(base),
  'knowledge.restore_base': async (dto) => application.get('KnowledgeService').restoreBase(dto),
  'knowledge.delete_base': async ({ baseId }) => {
    await application.get('KnowledgeService').deleteBase(baseId)
  },
  'knowledge.add_items': async ({ baseId, items, conflictStrategy }) =>
    application.get('KnowledgeService').addItems(baseId, items, conflictStrategy),
  'knowledge.delete_items': async ({ baseId, itemIds }) => {
    await application.get('KnowledgeService').deleteItems(baseId, itemIds)
  },
  'knowledge.reindex_items': async ({ baseId, itemIds }) => {
    await application.get('KnowledgeService').reindexItems(baseId, itemIds)
  },
  'knowledge.enable_embedding_model': async ({ baseId, patch }) =>
    application.get('KnowledgeService').enableEmbeddingModel(baseId, patch),
  'knowledge.search': async ({ baseId, query }) => application.get('KnowledgeService').search(baseId, query),
  'knowledge.get_file_path': async ({ itemId }) => {
    try {
      return application.get('KnowledgeService').getFilePath(itemId)
    } catch (error) {
      if (isDataApiError(error) && (error.code === ErrorCode.NOT_FOUND || error.code === ErrorCode.INVALID_OPERATION)) {
        throw new IpcError(knowledgeErrorCodes.SOURCE_PATH_UNAVAILABLE, 'Knowledge source path is unavailable', {
          cause: error.code
        })
      }
      throw error
    }
  },
  'knowledge.list_item_chunks': async ({ baseId, itemId }) =>
    application.get('KnowledgeService').listItemChunks(baseId, itemId),
  'knowledge.list_shared_kbs': async () => listSharedKnowledgeBases(),
  // 网关共享库伪 KnowledgeBase 列表（含虚拟「ekp数据检索」）：composer 选择器与
  // agent scope 的数据源。与 list_shared_kbs 的区别：这里产出可直接合并进
  // allKnowledgeBases 的 KnowledgeBase 形状。目录拉取失败（网关未启用/不可达）
  // 降级为 []，让 composer 与 scope 在企业环境之外照常工作。
  'knowledge.list_gateway_pseudo_bases': async () => {
    try {
      return await listGatewayKbPseudoBases()
    } catch (error) {
      logger.warn('Failed to list gateway pseudo knowledge bases; degrading to empty list', { error })
      return []
    }
  }
}
