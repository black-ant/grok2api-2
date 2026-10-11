# 远程 Fork 增量合并计划

更新时间：2026-10-11

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

## 分阶段执行

### 阶段 A：模型与协议兼容

- 保持 `grok-4.7` catalog-only，直到本地有 Build OAuth 请求链路和可验证上游响应。
- 对 reasoning alias、`minimal`/`max` 映射、固定 reasoning 模型逐项加回归测试。
- 对工具根 Schema 只做安全过滤；Build grammar flatten、深层 `$ref` 展开暂不移植。

### 阶段 B：流式与空响应可靠性

- 已完成语义空闲计时的下游背压修复。
- 下一步先补“非流式空响应”和“流结束但无可见输出”的最小复现测试。
- 只有确认当前 Python 能区分文本、图片、工具调用和纯生命周期事件后，才考虑账号降级或重试。

### 阶段 C：账号冷却与 Retry-After

- 先确认现有 random 策略的固定冷却：basic 24 小时、super/heavy 2 小时、Console 4 小时。
- `15bf810a` 暂缓：Go 有独立 `routing.cooldownMax`，Python 当前没有等价配置；直接套用模型 fallback 上限会改变账号策略。
- `3116c15b` 暂缓：Python 当前没有 provider-wide 失败与 account-scoped 失败的同等分类。
- `d7fca59d` 暂缓：Python 尚未统一识别非流式空响应和“有无有效输出”的账号惩罚边界。

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

## 每批提交规则

1. 先查看远程 patch 和当前对应模块。
2. 明确标记为：已存在、直接移植、改造后移植、暂缓或跳过。
3. 一个功能批次只改相关文件，并补最小回归测试。
4. 通过专项测试、`compileall`、`git diff --check` 后单独提交。
5. 不执行 `git merge chenyme/main`，不覆盖已有提交。

