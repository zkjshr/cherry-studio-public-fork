/**
 * 数据市场 / 人设市场的固定分区（对齐能力市场的分区呈现）。分区是设计出来的
 * 分类体系，条目按名称+描述的关键词自动落位；都没命中的进 "more" 兜底分区——
 * 管理员后续补充数据/人设时无需改代码，新条目会自动归入合适分区。
 */
export interface MarketCategoryDef {
  /** i18n key 后缀（market.data.cat.* / market.talent.cat.*）。 */
  key: string
  /** 关键词（小写）；名称命中优先于描述命中，分区按声明顺序匹配。 */
  keywords: string[]
}

export const DATA_MARKET_CATEGORIES: MarketCategoryDef[] = [
  { key: 'standards', keywords: ['规范', '标准', '规定', '强制', '政策', '法规', '技术资料', '图集'] },
  { key: 'design', keywords: ['作品', '项目', '模板', '设计', '评选', '奖项', '海绵'] },
  { key: 'training', keywords: ['培训', '课程', '课堂', '书籍', '学习', '教材', '指南', '指引'] },
  { key: 'resources', keywords: ['云盘', '影像', '内刊', '数据源', '素材', '课件', '云'] }
]

export const TALENT_MARKET_CATEGORIES: MarketCategoryDef[] = [
  {
    key: 'writing',
    keywords: ['写作', '文案', '报告', '周报', '总结', '邮件', '办公', '秘书', '文档', '编辑', '行政']
  },
  {
    key: 'business',
    keywords: ['运营', '营销', '销售', '市场', '产品', '广告', '增长', '客服', '电商', '商业', 'seo', '品牌']
  },
  { key: 'dev', keywords: ['编程', '代码', '开发', '程序', 'python', 'javascript', '前端', '后端', '运维', '工程师'] },
  {
    key: 'engineering',
    keywords: ['设计', '建筑', '结构', '工程', '规划', '勘察', '造价', '审图', 'bim', '项目管理']
  },
  { key: 'analysis', keywords: ['数据', '分析', '财务', '统计', '研究', '咨询'] },
  { key: 'translate', keywords: ['翻译', '英语', '日语', '多语', 'localization'] },
  { key: 'education', keywords: ['教师', '教学', '辅导', '培训', '讲师', '人力资源', 'hr'] },
  { key: 'life', keywords: ['生活', '旅行', '健康', '心理', '美食', '健身', '职业', '导航'] }
]

/** 条目落位：名称命中 > 描述命中 > null（调用方归入 more 兜底分区）。 */
export const matchMarketCategory = (
  name: string,
  description: string | null | undefined,
  categories: MarketCategoryDef[]
): MarketCategoryDef | null => {
  const n = (name || '').toLowerCase()
  const d = (description || '').toLowerCase()
  for (const category of categories) {
    if (category.keywords.some((keyword) => n.includes(keyword))) return category
  }
  for (const category of categories) {
    if (category.keywords.some((keyword) => d.includes(keyword))) return category
  }
  return null
}
