---
name: grok2api-remote-reference
description: Use when working in this Python grok2api repository to inspect, extract, compare, adapt, or decide whether functionality from the actively updated chenyme/grok2api reference project can be brought into the local codebase. Always use it before merging or porting remote features.
---

# Grok2API 远程功能参考与合并判断

## 目标

把远程项目当作**功能和行为参考**，不要把它当作可以直接覆盖本仓库的源码。每次工作必须先提取远程功能，再判断本地是否已有实现、能否适配、是否应暂缓，最后才决定是否修改代码。

## 参考项目

- 仓库：`chenyme/grok2api`
- 地址：`https://github.com/chenyme/grok2api`
- 本地远程名：`chenyme`
- 默认分支：`main`
- 当前工作区确认快照：2026-08-17 15:46:40 +08:00，远程提交 `57746fc70289fcbab4bf33db33c521cf756ebbb9`，提交说明为 `Merge branch 'main' of https://github.com/chenyme/grok2api`
- 远程源码结构：Go 后端 `backend/` + TypeScript 前端 `frontend/`
- 本地源码结构：Python/FastAPI `app/` + Python 测试 `tests/`

远程与本地不是同一条可快进历史，也不是同一套目录结构。默认禁止对本仓库执行 `git merge chenyme/main` 或直接 `git cherry-pick` 远程提交。只能移植已确认的行为、规则、接口契约和测试意图。

每次使用前刷新远程引用；上面的提交只是快照，不是永久最新状态：

```powershell
git fetch --no-tags chenyme main
git show -s --date=iso --format='%H %ad %s' chenyme/main
```

## 确定性规则

对每个结论标记以下状态之一：

- `已确认`：有提交、文件、当前代码或测试结果作为证据。
- `推断`：根据文件和调用关系推导，必须写出依据。
- `未确认`：证据不足；写出最小验证动作。
- `不合并`：当前架构、需求或风险不允许移植。

禁止把“远程有实现”写成“本地可以直接合并”。没有对应的本地入口、数据模型、配置语义和测试，不得给出“能合并”的结论。

## 标准流程

### 1. 保护本地工作区

先检查 `git status --short --branch`、未提交 diff 和未跟踪文件。不要执行 `reset --hard`、覆盖文件、自动 stash 或删除用户改动。当前工作区有未提交功能改动时，先把它们列入基线，再比较远程。

### 2. 提取远程功能

按合并提交和功能提交检查，不只看提交标题：

```powershell
git log chenyme/main --first-parent --date=iso --pretty=format:'%h %ad %s' -40
git show --stat --summary <commit>
git diff-tree -m --no-commit-id --name-only -r <commit>
```

每个功能至少提取以下内容：

1. 用户可见能力和适用入口。
2. 请求字段、响应字段、模型名、配置项和默认值。
3. 账号选择、配额扣减、重试、超时、状态转换和持久化规则。
4. 上游失败、代理失败、挑战失败和重复请求的处理方式。
5. 相关测试验证的边界，而不是只复制实现代码。
6. 前端页面是否改变流程；不得把表单、列表和大量无关功能堆到同一页面。

### 3. 映射到本地架构

优先查找以下本地对应位置：

- 模型与别名：`app/control/model/`、`app/products/openai/router.py`
- 账号和配额：`app/control/account/`、`app/dataplane/account/`
- 代理与 Clearance：`app/control/proxy/`、`app/dataplane/proxy/`
- 上游协议：`app/dataplane/reverse/protocol/`、`app/dataplane/reverse/transport/`
- OpenAI 兼容入口：`app/products/openai/`
- 媒体缓存和文件安全：`app/platform/storage/`、`app/products/openai/video.py`、`app/dataplane/reverse/transport/asset_upload.py`
- 测试：`tests/`

先判断本地是否已经有同一行为。若已有实现，不要重复添加；比较远程边界条件，补缺失测试即可。

### 4. 给出合并分类

为每项功能建立矩阵，使用以下决策：

| 决策 | 含义 |
| --- | --- |
| `已有本地实现` | 当前工作区已有对应代码；不重复移植，只补差异和测试。 |
| `行为可移植` | 业务规则有本地对应层；重写为 Python 实现，不复制 Go/TypeScript 文件。 |
| `需验证后移植` | 规则可能有价值，但本地契约、配置或状态模型尚未对齐。 |
| `暂不合并` | 没有本地需求、架构对应物或风险高；保留参考，不改代码。 |
| `未知` | 远程行为或本地影响尚未查清；明确列出最小验证动作。 |

不能使用“全部合并”“基本没问题”这类无证据结论。
## 当前已提取的功能与初步判断

以下判断基于 2026-08-17 15:46:40 +08:00 抓取的 `chenyme/main`（`57746fc70289fcbab4bf33db33c521cf756ebbb9`），后续 AI 必须先刷新远程并重新核对。

| 远程提交/功能 | 本地证据 | 当前判断 |
| --- | --- | --- |
| `d1f4bd21`：Grok 4.6 与 `xhigh` reasoning/model catalog | 当前工作区的 `app/control/model/registry.py`、`app/products/openai/router.py`、`tests/test_model_aliases.py`、`tests/test_console_model_payload.py` 已出现对应改动 | `已有本地实现`；对照远程模型目录字段和默认 effort，补测试，不直接 cherry-pick。 |
| `18f0c27f`：on-demand Clearance | `app/control/proxy/__init__.py`、`app/control/proxy/providers/flaresolverr.py`、`config.defaults.toml`、`tests/test_proxy_clearance.py` 已出现对应实现 | `已有本地实现`；重点验证首次请求、挑战后刷新、scheduler 不误刷新和失败回退。 |
| `b93ccc3d`、`50a2f79a`：媒体输入接收、图片历史上下文和外部资源安全 | `app/dataplane/reverse/transport/asset_upload.py`、`app/platform/storage/media_cache.py`、`app/products/openai/images.py`、`tests/test_video_safety.py` | `已有本地实现/需验证`；只补 SSRF、MIME、大小、重定向和缓存清理差异。 |
| `5b1c8ec5`、`4395691c`、`31ccf8be`、`0c541e26`、`82df1120`：视频最大尝试次数、多参考图、播放地址、文件名和入场限制 | `app/products/openai/video.py`、`app/products/openai/router.py`、`config.defaults.toml`、`tests/test_api_compatibility.py`、`tests/test_video_safety.py` | `已有本地实现/需验证`；重点验证幂等键、重试边界、引用数量、下载安全和任务状态转换。 |
| `cecd717a`：Grok Build 1.0.4 的 console 请求与 reasoning summary 兼容 | `app/dataplane/reverse/protocol/xai_console_chat.py`、`app/products/openai/console_chat.py`、`app/products/openai/console_responses.py`、`tests/test_console_model_payload.py` | `行为可移植且已有本地实现`；比较 payload 字段和流式摘要，不复制远程 provider 层。 |
| `736c093e`、`369de6fd`：Imagine 配额同步与媒体配额组 | `app/dataplane/reverse/protocol/xai_usage.py`、`app/control/account/refresh.py`、`app/dataplane/account/` 已接入 `quota_info` 五字段响应，并持久化 `image_pro/image_edit/video/video_720p` | `已迁移第一阶段`；远程 `total` 仍按 0 处理，耗尽恢复依赖下一次权威刷新。 |
| `369de6fd`：媒体 usage/audit 展示语义 | 远程证据在 `backend/internal/domain/audit/audit.go`、`backend/internal/repository/audit.go`、`backend/internal/transport/http/audit/handler.go`；本地已在 `app/platform/usage_audit.py`、`app/platform/request_logging.py` 和 Admin API 建立 JSONL 审计快照与摘要 | `行为可移植且已迁移第一阶段`；本地只记录可确认的请求/用量/媒体/耗时，成本仍明确不可用，不复制远程关系库或 React 页面。 |
| `18f0c27f`、`e8eb8346`、`3593eec9`、`ba38f41b`：按需代理、订阅代理、Clash/tunnel 解析 | 本地已有 `app/control/proxy/` 和 FlareSolverr，但远程使用不同的 egress/tunnel 模型 | `行为可移植`；只提取解析、校验、ALPN 和失败反馈规则，必须在本地代理模型中重写。 |
| `5b0c5cd3`：流式工具调用期间保持 semantic idle timeout | `app/dataplane/reverse/transport/semantic_idle.py`、`tests/test_semantic_idle.py` 已实现并覆盖工具事件边界 | `已有本地实现`；后续只需随远程协议变化补差异测试。 |
| `0cc20278`、`6c2cb39b`：quality guard、双探针、降级账号和审计页面 | 本地有 proxy feedback/cooldown，但没有远程同构的 quality-guard/degrade 数据模型和页面 | `暂不合并`；只有明确需要该运营能力时，才提取状态机和探针规则，禁止整包移植。 |
| `feb65ed3`：SSO 到 Build 的设备流转换 | 远程对应 `backend/internal/infra/provider/web/sso_build.go`；当前本地没有确认到同等认证流程 | `未知`；先查本地认证入口和 token 生命周期，确认契约后再决定。 |
| `f06d6fe7`：A-tier 图片编辑、分层路由和 quota fencing | `app/control/model/registry.py`、`app/dataplane/account/`、`app/products/openai/images.py`、`tests/test_image_quota.py` 已实现 Basic→`imagePro`、Super/Heavy→`imageEdit` | `已迁移第一阶段`；只移植行为规则，不复制远程 selector、gateway 或关系存储。 |
| `120f53a1`、当前 `backend/internal/infra/provider/console/catalog.go`：Console `grok-4.5`、`grok-4.5-console` 和 `low/medium/high` 别名 | `app/control/model/registry.py`、`app/dataplane/reverse/protocol/xai_console_chat.py`、`tests/test_console_model_payload.py`、`tests/test_model_aliases.py` | `已迁移`；全部落到现有 Console Responses 路由，固定别名只发送对应 effort。 |
| 当前 `backend/internal/infra/provider/web/catalog.go`：`grok-chat-fast/auto/expert/heavy` 及 Web tier 规则 | `app/control/model/registry.py`、`app/products/openai/chat.py`、`tests/test_api_compatibility.py` | `已迁移`；按 `fast/basic`、`auto|expert/super`、`heavy/heavy` 注册，复用本地 modeId 和账号池，不复制远程 Web provider。 |
| 当前远程 Build `/models`/OAuth session contract 的 `grok-composer-2.5-fast` | `app/control/model/registry.py` 已纳入目录，但本地没有 Build OAuth provider、动态能力同步或 Build Responses gateway | `模型名已合并，接口暂不支持`；保持 `supported_in_api=false`，最小后续动作是先建立 Build provider 与账号能力契约。 |
| 当前 `backend/internal/infra/provider/console/catalog.go`：Console 原始公开 ID `grok-4.3`、`grok-build-0.1` | `app/control/model/registry.py`、`app/dataplane/reverse/protocol/xai_console_chat.py`、`app/products/openai/router.py`、`tests/test_console_model_payload.py` | `已迁移`；复用已有 Console 路由；`grok-4.3` 保留 `none/low/medium/high`，`grok-build-0.1` 固定为无可配置 reasoning，未伪造媒体或音频入口。 |
| `d1f4bd21`、当前 `backend/internal/transport/http/inference/codex_models.go`：模型目录的上下文窗口、描述和 reasoning levels | `app/products/openai/router.py`、`tests/test_console_model_payload.py` | `已迁移第一阶段`；只补本地 Codex `client_version` 目录字段，不引入远程动态模型数据库。 |
| `b6351901`、`03db0b59`：加密 reasoning item 的内部首 token SSE 标记与 `thinking_content` 识别 | 本地 Console Responses 已过滤 `encrypted_content`，但本地 audit 没有同构的首 token/TPS 计算入口 | `需验证后移植`；先确认本地是否需要首 token 指标，最小动作是为对应入口建立流式计时测试；不向客户端泄露内部 marker。 |
| `05528aca`、`c93cabbf`、`ca7e41e5`：Imagine 2.0、Video 1.5、TTS/STT/Realtime 模型和接口 | `app/control/model/registry.py`、`app/dataplane/reverse/protocol/xai_console_dpop.py`、`app/dataplane/reverse/protocol/xai_console_media.py`、`app/dataplane/reverse/transport/console_media.py`、`app/products/openai/audio.py`、`app/products/openai/router.py`、`tests/test_console_media_protocol.py` | `已迁移第一阶段`；全部远程媒体/音频模型 ID 的 `supported_in_api=true`，图片、视频创建/查询/缓存、TTS、TTS voice 查询、STT 和 Realtime 已有本地入口；真实 Console 账号联调仍未确认。 |
| `ab84ffef`、当前 Responses handler：`prompt_cache_key`、`previous_response_id`、response compact/get/delete | 本地 Responses 只生成进程内结果，没有持久化 response ledger、缓存路由或 compact 服务 | `暂不合并`；先设计本地存储和生命周期契约，再决定是否增加接口，不能只接受字段而宣称支持。 |
| 当前 `backend/internal/infra/provider/conversation/messages_request.go`：Anthropic `redacted_thinking`、`output_config.effort`、MCP/document/tool_reference | 本地 Anthropic 转换器只支持已实现的 text/image/document/tool 基础分支，不能保留加密 reasoning 状态 | `需验证后移植`；先补输入转换单元测试并确定本地上游能否接受 Responses reasoning item。 |

### 本次迁移结果

- 图片编辑模型 `grok-imagine-image-edit` 已改为 `basic`；Basic 账号使用 `imagePro` 配额，Super/Heavy 使用 `imageEdit` 配额。
- 视频生成已按远程媒体配额组接入：空值或 `720p` 使用 `video720p`，其他分辨率使用 `video`；成功、失败和 429 都沿用本地反馈/刷新链路。
- 新增运行时配额列只存在于 dataplane，持久化仍使用 `AccountRecord.ext.imagine_quota`，没有扩展数据库 schema。
- 未同步的媒体配额保持 `remaining=-1`，只作为兜底；已知耗尽账号不会被媒体选池使用。
- Console 新增 `grok-4.5` 兼容族：`grok-4.5`、`grok-4.5-console`、`grok-4.5-low`、`grok-4.5-medium`、`grok-4.5-high`；固定后缀不会被请求体中的另一个 `reasoning_effort` 覆盖。
- Console 原始目录 ID `grok-4.3` 与 `grok-build-0.1` 已补为可调用模型；Build 模型不接受可配置 reasoning，Codex 目录只报告 `none`。
- 远程 Console catalog 的 29 个公开 ID（含兼容别名）以及远程 Web catalog 的 4 个 `grok-chat-*` ID 已与本地 registry 对齐；同名的 Web/Console 媒体产品沿用本地单一公开模型名和已有路由。
- Codex 目录对 `grok-4.5` 报告 `500000` 上下文窗口和 `low/medium/high` 三档；远程未确认支持 `none`，因此本地不伪造 `none`。
- 验证记录：`python -m compileall -q app tests` 通过；本次改动文件 Ruff 检查通过；全量测试 `136 passed`、`15 subtests passed`，测试状态为 `通过`。全仓 Ruff 仍有既有未改文件告警，不能写成全仓通过。

### usage/audit 本地统计契约

远程审计模型已确认包含 operation、usage source、token/media usage、status、streaming、duration、error 和 summary 聚合；远程最新快照 `57746fc7` 仅继续修正加密 reasoning 的首 token/TPS 统计，没有改变本地第一阶段审计字段契约。

本地第一阶段按现有按日 JSONL 请求日志重写，不引入远程 Go 的关系数据库模型：

- 规范化记录写入每条请求日志的 `audit` 字段，`schema_version=1`。
- 核心字段：`request_id`、`operation`、`provider`、`model`、`resolved_model`、`status_code`、`success`、`streaming`、`usage_source`、token 四项、媒体输入/输出、`duration_ms`、`error_code` 和受限 `routing`。
- `usage_source` 只允许 `upstream`、`estimated`、`none`；当前响应中可提取但无法证明来自上游的 usage 记为 `estimated`，缺失记为 `none`。
- 管理接口：`GET /admin/api/request-audits` 提供分页和 period/operation/model/status 过滤；`GET /admin/api/request-audits/summary` 提供请求、成功率、token/media、耗时、coverage 和 operation/model 分组。
- 默认统计周期支持 `1h`、`24h`、`7d`、`30d`；实际数据仍受本地请求日志 retention 限制。
- pricing 明确返回 `available=false`；没有官方价格或成本证据时，不生成金额。

已覆盖的入口：OpenAI chat/responses/images/videos、Anthropic messages，以及通过路径兜底识别的兼容入口。失败请求保留审计状态，但失败的媒体输出不计入成功产出。
### 当前未迁移

- 远程媒体 usage/audit UI、关系型 audit ledger、官方定价和质量探针仍未迁移：本地已先完成后端统计契约，但没有足够证据支持成本结算或整包复制页面。
- 远程订阅代理、Clash/tunnel 解析和 quality-guard/degrade 运营页面仍只保留行为参考，未直接移植。
- Console 视频编辑/延长和官方媒体计费仍未迁移；当前已迁移的模型入口不应扩展为未实现的能力。
## 实现约束

- 先写功能矩阵和合并结论，再改代码。
- 每次只移植一个边界清晰的功能，保持提交和测试可回滚。
- 复用本地模型、账号选择器、配置读取、错误类型和媒体缓存，不引入远程的 Go/TypeScript 分层。
- 远程功能涉及安全时，优先验证 SSRF、私网地址、凭据 URL、MIME 欺骗、大小限制、重定向和重复请求。
- 远程功能涉及配额时，明确“预扣、成功扣减、失败回滚、429 冷却、窗口重置”的时序。
- 远程功能涉及流式响应时，验证首事件、工具事件、心跳、空闲超时、上游错误和 SSE 结束顺序。
- 前端改动遵守单页单一职责：一个页面只做列表或表单等 1–2 件事，流程按步骤推进，不添加标题下的废话和无意义留白。

## 必须输出的判断结果

完成分析后，按下面格式输出，不要只给“已合并”或“不能合并”：

```text
参考快照：<远程名、分支、提交、确认时间>

已确认：
- <远程功能及证据>

已有本地实现：
- <功能 -> 本地文件>

行为可移植：
- <功能 -> 本地落点 -> 风险>

暂不合并：
- <功能 -> 原因>

未确认：
- <缺少的证据>
- 最小验证：<命令、测试或代码入口>

下一步：
- <只列一个当前最小动作>
```

测试状态必须明确写成 `未运行`、`通过` 或 `失败`，不能用“应该通过”代替。
