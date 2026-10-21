# TJADKnows Desktop · 内测指南（第一期）

- 服务端：http://10.121.16.58:8787（TJADKnows ZeroTier 网段，办公网/居家均可达，须已加入 ZeroTier 网络；原办公网地址 192.168.66.12:8787 已不再作为客户端接入地址）
- 下载页：http://10.121.16.58:8787/download
- 适用：Windows 10+ / macOS 10.15+（Apple Silicon 优先，Intel 包按需另出）
- 客户端版本：2.1.4-tjad.5（基于开源 Cherry Studio 2.1.4 + TJAD 企业定制，产品名 TJADKnows Desktop；tjad.3 起不再弹官方数据迁移向导，tjad.4 新增插件市场，tjad.5 内置地址切换 ZeroTier 并配套服务端稳定性修复——工具调用被截断、长回答丢失等）

## 安装（约 3 分钟）

1. 打开下载页，对应平台下载安装包：
   - Windows：`TJADKnows-Desktop-2.1.4-tjad.5-x64-setup.exe`（SmartScreen 可能提示"未知发布者"→ 更多信息 → 仍要运行；内测包未签名）
   - macOS：`TJADKnows-Desktop-2.1.4-tjad.5-mac-arm64.dmg`（拖入 Applications；首次打开若提示未公证：右键 → 打开，或终端执行 `xattr -cr "/Applications/TJADKnows Desktop.app"`）
2. 启动应用（无需任何配置——企业配置已内置：服务端地址、访问令牌开箱即连）。
3. 验证同步成功：设置 → 通用设置 → 底部「企业管理」卡片显示 **已同步 v1**，且无红色错误。

## 你会得到什么

| 能力 | 说明 |
|---|---|
| 插件市场 | 侧栏「知识库」下方「市场」：安装管理员上架的插件（技能/助手/MCP/小程序打包），已装可一键卸载 |
| 模型对话 | 模型列表自动出现「千问 3.5 32B / 9B」，流量走所里网关，**无需任何 API Key** |
| 所里知识助手 | 助手列表自动出现：基于 WeKnora 知识库（54 库）检索回答并附来源 |
| 知识库小程序 | 侧边栏「知识库」= WeKnora Web 完整界面（免二次登录沿用 WeKnora 账号体系） |
| WeKnora 检索工具 | 知识助手可自动调用 MCP 检索工具查库 |
| 托管保护 | 企业下发的模型/助手/MCP/小程序带「企业」徽标，不可误删误改 |

## 修改生效方式

管理后台改配置 → 点「发布」→ 各客户端**下次启动**（或设置→通用→企业管理→立即同步）生效。

## 远程使用（不在所里网络时）

服务端走 ZeroTier：加入所里 ZeroTier 网络后，办公网内与居家远程均用 **http://10.121.16.58:8787**，无需任何区分。
tjad.5 安装包已内置该地址，装完即用；tjad.4 及更早安装包内置的是旧办公网地址，装完后执行一次：

- macOS/Linux：`mkdir -p ~/.cherrystudio/config && printf '{"enabled":true,"serverUrl":"http://10.121.16.58:8787","token":"tjad-beta-2026"}\n' > ~/.cherrystudio/config/enterprise.json`
- Windows：把同样内容写入 `%USERPROFILE%\.cherrystudio\config\enterprise.json`（JSON 一行）

## 已知限制（内测期）

- **语义检索暂缓**：WeKnora 向量检索正被文档回填任务占用 GPU，知识助手暂以"最近更新"模式工作；回填完成后自动恢复完整语义检索，无需升级客户端。
- 客户端自动更新已关闭（防止升级到社区版丢失企业功能）；新版本由管理员在下载页统一下发。
- 全员共用一个访问令牌（内测期设计），请勿外传安装包。
- 话题命名/翻译等辅助模型默认用 9B 小模型（成本考虑）。

## 故障排查

| 现象 | 处理 |
|---|---|
| 企业管理卡片显示"server unreachable" | 确认已加入 ZeroTier 网络；浏览器开 http://10.121.16.58:8787/api/health 应返回 `{"status":"ok"}` |
| 知识助手回答"知识库暂不可达" | WeKnora（10.137.200.58:8091）服务波动，稍后重试；持续出现报给管理员 |
| 模型回复很慢 | 4090 上 ollama 正被其他任务共用；错峰或报管理员 |
| macOS 打不开 | 见安装节 xattr 命令 |

问题反馈：直接找管理员（内测期收集表随后建）。
