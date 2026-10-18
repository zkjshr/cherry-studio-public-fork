/**
 * 数据市场（扩展页）的公共知识库订阅表。localStorage 持久化；知识库页的
 * 公共分区只展示已接入的库——"接入"即订阅，"移出"即取消订阅。
 */
export const DATA_MARKET_SUBSCRIBED_KEY = 'market.dataMarket.subscribed'

export const readSubscribedKbIds = (): string[] => {
  try {
    const raw = localStorage.getItem(DATA_MARKET_SUBSCRIBED_KEY)
    const parsed = raw ? JSON.parse(raw) : []
    return Array.isArray(parsed) ? parsed.filter((id): id is string => typeof id === 'string') : []
  } catch {
    return []
  }
}

export const writeSubscribedKbIds = (ids: string[]): void => {
  try {
    localStorage.setItem(DATA_MARKET_SUBSCRIBED_KEY, JSON.stringify(ids))
  } catch {
    // 存储失败只影响持久化，本次会话内的订阅状态仍然生效。
  }
}
