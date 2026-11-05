# 远程 Fork 增量合并计划

更新时间：2026-11-05

## 本轮远程快照

- `chenyme/main`：`e2fd7ab4`（2026-10-22），较上次核对的 `7c889a96` 新增 24 个功能提交；与当前 Python 主线没有共同祖先，只做逐提交移植。
- `upstream-jiujiu/main`：`ef7fb4f2`（2026-08-26），是当前 Python 主线的祖先；没有发现比本地基线更新且尚未吸收的 Jiujiu 提交。
- 当前本地 `main`：已领先 `origin/main` 4 个阶段提交；不执行整分支 merge。

## 范围

- `chenyme/grok2api`：当前远程主线 `7c889a96`，以 Go Gateway / Build OAuth 为主。
- `jiujiu532/grok2api`：当前远程主线 `ef7fb4f2`，仍停留在 Python Console 架构；当前本地基线已包含它的历史变更。
- 当前本地分支：Python 架构，不能直接合并 `chenyme/main`，也不执行整分支 merge。

## 已完成批次

| 批次 | 本地提交 | 内容 | 结论 |
| --- | --- | --- | --- |
| 模型目录 | `a42172af` | `grok-4.7` 作为 Build catalog-only 模型；修正 Console reasoning 菜单 | 已移植。没有 Build OAuth 网关，因此不开放调用 |
| 工具 Schema | `1c1fc2b4` | 工具参数根层 `anyOf`/`oneOf` 安全过滤 | 已移植。只处理根层，不改嵌套联合类型 |
| 流式稳定性 | `31a06ad5` | 下游背压期间暂停语义空闲计时 | 已移植并通过专项测试 |
| 空响应识别 | `45639aa2`、`f3c01f4b`、`3aeb69a9` | 区分文本、工具/推理/媒体活动和纯生命周期流；纯生命周期空流复用换账号重试 | 已改造。非文本活动不判空，避免误伤工具调用 |

## 分阶段执行

### 阶段 A：模型与协议兼容

- 保持 `grok-4.7` catalog-only，直到本地有 Build OAuth 请求链路和可验证上游响应。
- 对 reasoning alias、`minimal`/`max` 映射、固定 reasoning 模型逐项加回归测试。
- 对工具根 Schema 只做安全过滤；Build grammar flatten、深层 `$ref` 展开暂不移植。

### 阶段 B：流式与空响应可靠性

- 已完成语义空闲计时的下游背压修复。
- 已完成 Console 文本、工具/推理/媒体活动和纯生命周期事件分类。
- 已完成纯生命周期空流的非流式/流式换账号重试；工具、推理、媒体活动仍不判为空。
- 仍不对工具/图片结果做账号惩罚，等待真实上游样本和对应输出转换链路。

### 阶段 C：账号冷却与 Retry-After

- 先确认现有 random 策略的固定冷却：basic 24 小时、super/heavy 2 小时、Console 4 小时。
- `15bf810a` 暂缓：Go 有独立 `routing.cooldownMax`，Python 当前没有等价配置；直接套用模型 fallback 上限会改变账号策略。
- `3116c15b` 暂缓：Python 当前没有 provider-wide 失败与 account-scoped 失败的同等分类。
- `d7fca59d` 部分改造：已移植纯生命周期空响应识别和换账号重试；账号冷却/惩罚仍不移植。

### 本轮逐项结论

| 远程提交 | 处理结果 | 原因 |
| --- | --- | --- |
| `7f3f3d3c` | 跳过 | Go `quality_retry` 质量扫描器、可见 token 统计和账号质量隔离在 Python 中不存在；不能按文件名硬移植。 |
| `6db9f67f`、`df4dde39` | 跳过 | Python Console payload 当前由模型能力静态生成，不转发客户端 `tools/tool_choice`；没有对应的空工具对。 |
| `7d1b4246`、`ca392e68`、`8641a782`、`3de758e7` | 跳过 | 依赖 Go Build Responses 历史、reasoning replay 和多轮工具上下文。 |
| `22ac653a`、`e5285ebe` | 已改造 | 已由 `1c1fc2b4` 只处理 Python 工具提示中的根层联合类型。 |
| `72a3a347`、`5d19ccff` | 暂缓 | 深层 `$ref` 展开和 grammar flatten 只属于 Build 网关；当前没有可验证的本地上游链路。 |
| `7ebc46c7` | 跳过 | Go relational `ModelRoute` 手工路由与 Python 运行时 alias 不是同一数据模型。 |
| `15bf810a` | 暂缓 | Python 现有回归测试明确要求 `Retry-After` 可覆盖模型冷却上限；远程改动针对 Go 账号 selector 的独立 `cooldownMax`，直接改会改变既有语义。 |
| `155ebba7` | 跳过 | `view_image` 是 Build 原生工具别名；本地 `grok-4.7` 仍是 catalog-only。 |
| `9dda42ae` | 已改造 | 已移植可验证的模型目录和 reasoning 元数据；Build headers、OAuth、live catalog 不移植。 |

下一批优先做完整回归和真实上游样本核对；在证据不足前，不启用工具/图片输出的账号降级、质量惩罚或 Build 专属 replay。

### 阶段 D：Build 专属协议

以下提交只登记，不直接移植：

- `22ac653a`、`72a3a347`、`5d19ccff`、`e5285ebe`：Build function grammar 的根联合清理和深层引用展开。
- `8641a782`、`3de758e7`、`ca392e68`：Build reasoning proof、历史恢复和加密 reasoning replay。
- `155ebba7`、`a608c209`：Build 原生 `view_image` 工具别名与工具声明生命周期。
- `43afb615`、`4793b092`、`e0a82d50`：Build Responses 转换器的 reasoning summary/raw 流合并。
- `9dda42ae`：Build 1.0.40 headers/catalog；当前只移植可验证的模型目录部分，Build header 仍等待本地 Build 网关。

### 阶段 E：Go 数据层、管理端和基础设施

明确跳过直接 cherry-pick，必要时另做 Python 方案：

- relational `ModelRoute`、manual route alias 持久化。
- Go 账号 selector、quality guard、audit retention、egress profiles、HTTP/2 transport。
- Go Console quota reconciliation、Build credential refresh、Build bot-risk、provider gateway。
- Go frontend/admin 页面改动；当前 Python 管理端结构和页面职责不同。

## 2026-11-05 增量复核（`7c889a96..e2fd7ab4`）

共 24 个非合并提交。逐项结论如下，未列出的合并提交均为文档、版本号或依赖锁文件变更。

| 远程提交 | 处理结果 | 原因 |
| --- | --- | --- |
| `8038dca6`、`2f29773b`、`23793f3e` | 待移植（推荐） | Imagine WebSocket 事件按 `requestId` 过滤。本地 `app/dataplane/reverse/transport/imagine_ws.py` 在一条连接上跨轮复用，`_stream_round` 每轮只重建 slot 表、不校验帧归属，上一轮未结束的帧会被当成新一轮数据。移植时采用宽松判定：帧带 `requestId` 且与本轮不符才丢弃，缺字段的帧保持放行，避免畸形帧导致整轮空结果；需补多轮复用回归测试。 |
| `ea563c31`、`0a3efcc4` | 待移植（推荐） | 支持 `地址:端口:用户名:密码` 的老式代理写法。本地 `app/control/proxy/__init__.py` 的 `load()` 直接使用配置文本构造成出站节点，没有规范化步骤，用户按代理服务商给的原始格式填写会直接连接失败。落点应放在节点构建前，只在解析成功时改写，其余情况保留原值。 |
| `abc5c294`、`cfc5865d`、`bedc88fe` | 跳过 | SSO→Build 设备流的 consent token；本地没有 Build OAuth 链路。 |
| `a88cd880` | 跳过 | Build 账号未知 403 冷却；本地无 Build provider 与账号分类。 |
| `31bbe64d` | 跳过 | 视频任务重试绕过客户端密钥作用域；本地没有客户端密钥作用域概念，视频重试始终限定在模型自身的账号池内。 |
| `6c193995`、`b789e846`、`659ee8ca`、`4e220e81` | 暂缓 | 质量重试、自动停用降级账号、瞬时重试计数与请求头；依赖 Go 侧质量守护与 Build 客户端语义，本地无对应状态模型。其中 525/526 不参与账号轮换的边界可单独评估，但当前本地无 Build 上游，证据不足。 |
| `6ea58d3a`、`dbd1a29f` | 暂不合并 | 语音长连接观察请求超时并按确认时长计费；本地没有计费通道，也没有服务端请求超时配置，长连接生命周期由客户端断开驱动。 |
| `6cd06ea5` | 跳过 | 视频任务在 worker 停机后的恢复；本地视频为同步流式生成，没有后台任务表。 |
| `838aca3c` | 跳过 | Console 未绑定账号自动删除、配额刷新最小间隔调整；本地没有 reauth/未绑定状态模型，按需刷新节流已是 300 秒。 |
| `eaca3d0e`、`f18624b1` | 未确认 | Console usage-status 的 `budgetCents`/`usedCents` 边界与溢出处理；本地当前没有该解析入口。最小验证：确认本地是否需要 Console 付费额度同步，再决定是否新增解析器。 |
| `87407fe1` | 跳过 | uTLS Reality 握手的 ECDHE key share；Go 隧道内核专属。 |
| `31e660bc` | 只登记 | Dockerfile 与前后端依赖升级；本地为 Python 镜像，依赖树不同，证书包由基础镜像维护。 |
| `a23e4654` | 跳过 | Go 1.27 的 HTTP/2 测试适配。 |
| `e03cacb9` | 跳过 | README 赞助商条目删除。 |

## 每批提交规则

1. 先查看远程 patch 和当前对应模块。
2. 明确标记为：已存在、直接移植、改造后移植、暂缓或跳过。
3. 一个功能批次只改相关文件，并补最小回归测试。
4. 通过专项测试、`compileall`、`git diff --check` 后单独提交。
5. 不执行 `git merge chenyme/main`，不覆盖已有提交。
