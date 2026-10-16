import useSWR from 'swr'

import { ipcApi } from '@renderer/ipc'
import type { GatewayPseudoKnowledgeBase } from '@shared/data/types/knowledge'

export const GATEWAY_PSEUDO_KNOWLEDGE_BASES_CACHE_KEY = 'knowledge.list_gateway_pseudo_bases'

const EMPTY_GATEWAY_PSEUDO_KNOWLEDGE_BASES: GatewayPseudoKnowledgeBase[] = []

/**
 * 网关共享库伪 KnowledgeBase 目录（含虚拟「ekp数据检索」），供 composer 知识库
 * 选择器合并进本地库列表。失败已在主进程降级为 []，这里不再重试；与
 * useSharedKnowledgeBases 相同的姿态禁用焦点/陈旧重验——窗口重新聚焦触发
 * revalidate 会把降级后的空目录刷进缓存，令已选中的网关库失去解析而被剪除。
 */
export const useGatewayKnowledgeBases = (options: { enabled?: boolean } = {}) => {
  const enabled = options.enabled !== false
  const query = useSWR<GatewayPseudoKnowledgeBase[]>(
    enabled ? GATEWAY_PSEUDO_KNOWLEDGE_BASES_CACHE_KEY : null,
    () => ipcApi.request('knowledge.list_gateway_pseudo_bases'),
    { shouldRetryOnError: false, revalidateOnFocus: false, revalidateIfStale: false }
  )

  // 测试或异常载荷可能给非数组；这里归一化，保证调用方拿到的永远是数组。
  const bases = Array.isArray(query.data) ? query.data : EMPTY_GATEWAY_PSEUDO_KNOWLEDGE_BASES
  return { bases, isLoading: query.isLoading }
}
