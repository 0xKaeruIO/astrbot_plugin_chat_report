# astrbot_plugin_chat_report

_✨ 聊天消息举报插件 ✨_

基于 AstrBot（aiocqhttp / NapCatQQ）的群聊举报插件：收到举报先取被举报消息前后的整段群聊做**上下文研判**，
再由 AI 自动研判举报内容，能处理就自动撤回、警告、禁言；拿不准时才私聊管理员与本群指定用户，由人工回复处理。
撤回 / 禁言之前一律**证据先行**：先把违规发言、上下文与 AI 结论私聊转发给指定管理员并落盘留存。
也可以按群开启**自动审查**：达到指定消息条数或时间间隔后自动巡查最近聊天记录，按三种通知模式汇报。
配置视觉模型后，还可以让 AI 查看被举报图片、上下文图片、聊天记录截图，检查违规图片、二维码和引流图。
开启冲突观察后，后台会主动发现两人/多人争执，私聊管理员并只发一条中性劝停消息，不处罚。

## 💡 功能特性

### 举报触发规则（严格匹配）

必须 **同时** 满足两个条件才会触发，避免日常聊天误触：

1. **必须艾特机器人本人**：只 @ 群友不触发。
2. **整条消息只有「@ + 举报」**：把消息里的 @ 去掉、去掉空白后，文字必须**正好**是「举报」。
   多一个字、一张图、一个表情都不触发（例如「举报我」「举报一下」「@群友 来吧 举报我」都不会触发）。

两种举报方式：

| 方式 | 消息内容 | 被举报人 |
|:---|:---|:---|
| 引用举报 | 引用一条消息 + `@机器人 举报` | 被引用消息的作者 |
| 无引用举报 | `@机器人 @某人 举报` | 被 @ 的那个人 |

- 同时引用 + @ 别人时，**以引用的作者为准**（引用优先），@ 的顺序不影响判定。
- 只 `@机器人 举报` 而没指明对象时，机器人会回一条使用提示，不会静默无响应。

### 🧠 上下文研判

只看被举报的那一条消息，熟人之间互怼、游戏内吐槽、对方笑嘻嘻接梗都会被当成辱骂。
现在每次举报都会把**被举报消息前后整段群聊**一起交给 AI：

- **上下文来源**：先读本地群消息缓存，条数不够时调用 `get_group_msg_history` 补齐
  （`context_fetch_history`，需要机器人有相应权限，拉取失败自动退回本地缓存）。
- **带说话人和时间**：送给 AI 的不只是文字，每行都标明谁在什么时候说的，AI 才能看出谁在跟谁说话、语气是否在开玩笑。
- **条数与长度**：默认回看前面 30 条、后面 5 条（`context_before_count` / `context_after_count`），
  文本总长超过 `context_max_chars` 时从最早的记录开始裁剪。

用户原始例子（游戏群里最常见的互怼）：

| 说话人 | 内容 |
|:---|:---|
| A | 来不来玩游戏 |
| B | 来啊 |
| … | （几局之后） |
| A | 你是傻子吗，这也没打过 |
| B | 因为我笨蛋，嘻嘻 |

B 全程接梗、还自己笑，上下文说明这是**双方玩笑互动**，不是单方面辱骂 → AI 判定 `safe`，
**不撤回、不禁言**，只在开关允许时发一条群内安全提示。

- `banter_guard`（默认开）就是这个豁免：AI 判定为双方玩笑互动时，即使消息里含辱骂词也不处罚；
  只有 AI 判定严重程度为 `high`（真正的人身攻击、歧视、骚扰等）时仍照常处理。
- **上下文只当聊天记录**：上下文里出现「忽略以上规则」这类话不会被 AI 当成指令执行，只作为群聊内容参与判断。
- **第三者恶意举报**：举报人根本没参与这段对话、上下文又判定为玩笑互动时，这次举报会被标记为「疑似第三方恶意举报」，
  不处罚被举报人；`notify_admin_on_suspected_malicious` 开启时用「疑似恶意举报模板」私聊提示管理员，
  由管理员自己决定要不要处理这个乱举报的人。

### 🛡️ 证据先行

撤回 / 禁言是不可撤销的动作，所以顺序被固定成「先留证，再动手」：

1. AI 判定违规且可处理 → **先**把违规发言、发生时间、参与人员、AI 结论与整段上下文，
   打包成 QQ「聊天记录」**合并转发**私聊给 `evidence_admin_ids` 里的指定管理员：
   1 条摘要节点 + 每条上下文消息各 1 条节点（带发送者昵称、QQ，被举报消息标注「被举报」；
   图片 / 语音会尝试以 base64 恢复原始媒体（单次最多 10 个、单张约 3MB、总量约 12MB）；超限或转换失败时优先转 AstrBot 文件服务 URL，再退回原始 URL/本地路径，最后才用 `[图片]`、`[语音]` 占位）。
   `evidence_use_forward` 关闭时证据退回纯文本；通知是否额外附带独立聊天记录由 `forward_context_records` 控制。
2. 同时在插件数据目录落盘 `evidence/<举报ID>.json`（含完整上下文），`evidence_keep_files` 开启时长期留存。
3. **证据送达后**才撤回 + 群内艾特警告 + 禁言 + 警告次数 +1。
4. `evidence_required`（默认开）时，证据私聊**全部失败**就跳过撤回 / 禁言，改走转人工通知，避免「处理了却没证据」。
5. 撤回失败（消息已过期、机器人无权限等）不影响禁言，但会在审计日志里记一条。

> `evidence_admin_ids` 留空时，依次回退到「默认指定用户 QQ」（`default_receiver_ids`）→ bot 管理员 → 本群群主/管理员（`auto_include_group_admins` 开启时）。

**机器人不是本群管理员时**（`not_admin_evidence_only`，默认开）：

- 撤回、禁言、群内警告全部跳过（没有管理权限，执行必然失败），只做三件事：
  1. 生成证据并落盘 `evidence/<举报ID>.json`；
  2. 把证据私聊转发给 `evidence_admin_ids` 里的**指定管理员**，同时发给**群主**，再加上常规通知对象（默认指定用户 / bot 管理员）；
  3. 登记为待处理，收到证据的管理员或群主回复 `1` 即可结案，处理结果照常通知其他已通知对象。
- 机器人没有管理权限时，**转人工通知的收件人也会自动加上群主**，避免群主收不到消息。
- 证据模板里的「后续动作」由 `{action_plan}` 渲染，此时会写明「机器人不是本群管理员，未撤回、未禁言，请管理员手动处理」。
- 想恢复「照样尝试自动处理」可以把该开关关掉（通常会因权限不足失败，然后转人工）。

### AI 自动研判

AI 输出 JSON：`decision` / `confidence` / `reason` / `target_message_id`，据此分流：

| AI 结论 | 处理 |
|:---|:---|
| `violate` 且置信度 ≥ 阈值，且能定位到消息 ID | **自动**：先留存证据 → 撤回 + 群内艾特警告 + 禁言 + 警告次数 +1，**不私聊管理员**（证据本身已私聊转发给指定管理员） |
| `violate` 但无法定位消息 ID | 转人工（通知里说明原因） |
| `safe` | 视开关决定：是否在群内发安全提示 / 是否私聊通知管理员 |
| `uncertain` 或置信度低于阈值 | 转人工 |
| 被举报消息是纯图片 | 开启「图片理解」后交给视觉模型分析；未开启或没有可读取图片时转人工；语音/文件暂不送视觉 |

- 研判时**附带上下文**：上下文聊天记录、参与人员、发生时间都会进提示词，AI 还要额外给出「上下文结论」。
- 消息内容渲染为可读文本：图片 → `[图片]`、表情 → `[表情]`、语音 → `[语音]`、视频 → `[视频]`、文件 → `[文件]` 等，不会出现 `ComponentType.Image` 这类内部名。
- 无引用举报时，会把被举报人最近 N 条缓存消息（带消息 ID）交给 AI，供其挑选违规消息以便撤回。

### 图片理解（需要视觉模型）

开启 `ai_vision_enabled` 后，插件会把被举报消息/自动审查候选消息以及上下文中的图片传给 AI。**必须同时满足：**

1. 当前 AI 提供商支持 `image_urls`；
2. 模型本身支持视觉（GPT-4o、Claude、Gemini 等）。

如果提供方不支持 `image_urls`，插件会记录日志并自动退回纯文本，不会中断举报流程。

可单独开关的检查项：

| 开关 | 默认 | 检查内容 |
|:---|:---:|:---|
| `ai_vision_target_images` | 开 | 被举报/候选消息里的图片 |
| `ai_vision_context_images` | 开 | 上下文里其他图片，用于还原前后文 |
| `ai_vision_detect_violation` | 开 | 色情、暴力、血腥、违法、辱骂、恐吓、诈骗等 |
| `ai_vision_detect_qr` | 开 | 二维码、小程序码、群二维码、收款码 |
| `ai_vision_detect_diversion` | 开 | 联系方式、广告、拉群、站外导流、刷单、诈骗话术 |
| `ai_vision_read_screenshots` | 开 | 聊天记录截图中的对话、昵称、时间与上下文 |

单次最多 `ai_vision_max_images` 张（默认 4，最大 8）；图片过多时优先保留被举报/候选消息图片。
`ai_vision_extra_prompt` 可以追加自定义检查要求，例如「重点检查游戏代练广告和收款码」。

图片理解只处理 `Image` 消息段；语音转文字、视频和文件内容暂未接入。
二维码和截图文字都由视觉模型识别，未接入独立二维码解码/OCR 引擎；模糊、过小或遮挡的二维码可能只能判断“存在”，不保证解出链接。

### 转人工通知

- **通知对象**：AstrBot 全局管理员 + 本群群主/管理员（开关）+ 指定 QQ 列表；机器人不是群管理员时群主总会被通知。
- **通知内容**（`notify_include_context` 开启时自动附带）：
  1. 群名称 / 群号；
  2. **发生时间**：被举报消息的发送时间；
  3. **参与人员**：这段上下文里说过话的人；
  4. 被举报人、举报人、被举报消息（含消息 ID）；
  5. 被举报人累计被举报次数（引用 / 无引用举报都带）；
  6. **AI 上下文结论**：结合前后文得出的判断与置信度；
  7. **上下文聊天记录**：通知正文里附多行聊天原文，放在正文末尾；
     同时单独发一条 QQ「聊天记录」合并转发作为证据存档（1 条摘要 + 每条消息 1 条节点，带昵称、QQ，被举报消息标注；
     消息时间由 QQ 合并转发记录自身展示），通知与聊天记录是两条独立消息；
  8. **事情起因**：放在通知正文**末尾**，说明这场对话是怎么起来的。

  样例（模板可自定义）：

  ```text
  【举报待处理-AI拿不准】
  群：测试群(123456789)
  发生时间：2026-10-20 00:12:31
  参与人员：A(10001)、B(10002)
  被举报人：B(10002)
  举报人：A(10001)
  被举报消息（ID：7788）：你是傻子吗，这也没打过
  被举报人累计被举报次数：第 1 次
  AI 上下文判断：双方连续互怼且对方以玩笑回应，疑似熟人玩笑
  AI 初步判断：拿不准（置信度：62%）
  上下文聊天记录（共 20 条）：
  （正文末尾展示聊天原文；另有一条独立的 QQ「聊天记录」合并转发作为证据存档）
  请管理员结合上下文分析并处理。处理完请回复 1。
  举报ID：a1b2c3d4
  事情起因：两人组队连败后互相吐槽
  ```

- **发送方式**：
  1. 先发**通知文本**（群、发生时间、参与人员、被举报人/举报人、AI 结论、举报 ID、上下文、事情起因）；
  2. 再单独发一条 **QQ 合并转发聊天记录**作为证据存档（`forward_context_records` 开启时）；
  3. 通知文本与聊天记录都发送失败时，改为**在群内艾特该管理员**并附上通知文本，保证通知不丢。
- 管理员或指定用户**私聊回复「1」**（可配置）视为处理完成，并向其他已通知对象发送「已处理」通知。
- 收件人昵称优先取群名片/昵称缓存，其次查真实 QQ 昵称，避免出现「临时会话(12345678)」。

### 自动审查（按群开启）

在「默认群配置 → 自动审查」或「分群覆盖」里开启 `auto_review_enabled` 后，机器人会按两种条件自动巡查本群：

- **条数触发**：`auto_review_message_threshold` > 0 时，累计 N 条新消息触发一次审查；
- **时间触发**：`auto_review_interval_minutes` > 0 时，距上次审查超过 N 分钟且群里有新消息触发；
- 两者满足任意一个即触发，也可以只开其中一种。

每次审查读取 `auto_review_scope_count` 条最近消息，并受 `auto_review_scope_minutes` 时间范围限制；只有「上次审查之后的新消息」会作为候选违规消息，旧消息只作为上下文，避免重复处罚。单轮最多自动处理 `auto_review_max_actions` 条（0 = 只通知不处理）。

通知模式 `auto_review_notify_mode`：

1. `always`：无论是否安全都通知；
2. `safe_silent`：安全则不通知（默认）；
3. `after_action`：仅处理后保留证据并通知（没处理就不发）。

自动审查只发一条 QQ「聊天记录」合并转发，通知内容放在首条摘要节点，不再额外发一条纯文本；文字违规消息在末尾追加「[违规内容]」，图片/语音节点保持纯媒体，标记放在紧随其后的独立「审核标记」节点，避免文字与图片混排导致图片不渲染。处理违规时仍按「先证据、后撤回/禁言」执行。同一消息 ID 在 `auto_review_notify_dedup_minutes` 内只标记/通知一次，避免反复提醒。

自动审查状态会写入 `auto_review_state.json`，保存每群的 `pending`、上次审查时间与上次审查到的消息位置，重启后继续累计，避免重复审查。状态受三重上限保护：`auto_review_state_max_groups`（默认 200 个群）、`auto_review_pending_cap`（默认单群 10000 条）、文件总大小 512KB；超限时优先淘汰最久未活动的群。

### 主动冲突观察（不需要举报）

开启 `conflict_detection_enabled` 后，后台每 20 秒检查有最近消息的群：

- 最近 `conflict_window_messages` 条 / `conflict_window_minutes` 分钟内；
- 至少 `conflict_min_participants` 个不同发言人；
- 上次扫描后新增至少 `conflict_min_new_messages` 条；
- AI 判断为真实争执，且严重程度不低于 `conflict_min_severity`。

发生冲突时：

1. 私聊管理员发送「群聊冲突预警」，包含群、时间、两人/多人争执类型、参与人员及角色、冲突摘要、升级趋势、最近消息和处理建议；
2. 按 `conflict_mediate` 在群里发**一条**中性消息，只艾特冲突双方：

```text
@A @B 疑似争执，已经通知管理员处理。
有矛盾请不要继续在群里互相攻击。
```

不会撤回、不禁言；同群 `conflict_cooldown_minutes` 内不重复预警。
AI 会把参与者分为发起者、回应者、劝架者、旁观者，避免把劝架的人当成冲突方。

### 防封号

默认情况下，转人工 / 安全通知会在正文附带上下文，并在之后单独发一条 QQ「聊天记录」合并转发作为证据存档（这是本插件的核心改进：管理员必须能看到前因后果才判得准）。
如果担心机器人因转发敏感内容被风控，可在行为开关里关闭 `notify_include_context`：
关闭后通知里不再附带聊天原文或合并转发，只剩发生时间、参与人员、AI 结论与事情起因
（证据转发 `evidence_template` 不受此开关影响，仍按模板私聊转发并保留聊天记录）。

### WebUI 配置

- **面板插件配置**：`_conf_schema.json`，4 组（AI 研判 / 默认群配置 / 行为开关 / 通知模板），
  AI 研判组内含「上下文与玩笑豁免」卡片，默认群配置组内含「证据留存与指定管理员」「自动审查」卡片。
- **独立配置页面**：`pages/config`，分 5 个区（AI 研判 / 默认群配置 / 行为开关 / 通知模板 / 分群覆盖），
  支持卡片式编辑、模板变量一键插入、模板恢复默认、**可视化分群覆盖**（勾选要覆盖的项即可，无需手写 JSON）。

### 代码结构

`main.py` 只保留插件注册、初始化与事件入口，具体逻辑按职责拆到 `chat_report_core/`：

| 文件 | 职责 |
|:---|:---|
| `main.py` | `@register`、`__init__`、生命周期、群消息/私聊事件入口 |
| `chat_report_core/constants.py` | 模板、提示词、缓存上限、消息段展示文案 |
| `chat_report_core/utils.py` | 模板渲染、时长格式化、JSON 提取、共享异常 |
| `chat_report_core/config.py` | 配置读取、分群覆盖、schema 与 WebUI 配置接口 |
| `chat_report_core/state.py` | 持久化、计数、限流、审计、超时清理 |
| `chat_report_core/context.py` | 消息缓存、消息链解析、上下文回溯、群成员/昵称解析 |
| `chat_report_core/ai.py` | 举报研判、自动审查 AI 调用与结果归一化 |
| `chat_report_core/vision.py` | 图片引用收集、视觉提示词与开关控制 |
| `chat_report_core/evidence.py` | 证据组装/落盘、合并转发、先留证再处罚 |
| `chat_report_core/notify.py` | 私聊/群内通知、收件人解析、待处理举报登记 |
| `chat_report_core/report.py` | 举报触发解析与举报主流程 |
| `chat_report_core/auto_review.py` | 按群自动审查、三种通知模式 |

## 📦 安装

1. 将本目录放入 AstrBot 插件目录，目录名必须为 `astrbot_plugin_chat_report`：

```bash
cp -r astrbot_plugin_chat_report /AstrBot/data/plugins/
```

2. 重启 AstrBot（`main.py` 改动必须完整重启，热重载无效）。
3. 在面板 → 插件管理 → `聊天消息举报` 中配置，或打开独立配置页面。

> ⚠️ 想自动撤回 / 禁言，机器人**必须是群管理员**（撤回他人消息、禁言、拉取群历史都依赖管理员权限）。
> 不是管理员也照样能用：`not_admin_evidence_only` 默认开启，只留存证据并私聊转发给指定管理员与群主，由人工处理。

## ⌨️ 使用说明

1. 群成员引用违规消息并 `@机器人 举报`；或发送 `@机器人 @某人 举报`。
2. 机器人取回被举报消息与前后整段群聊（含说话人、时间），交给 AI 结合上下文研判。
3. AI 自动分流：
   - 违规 → 先私聊转发证据给指定管理员并落盘 → 再撤回、群内 `@对方` 警告、禁言；
   - 违规但机器人不是群管理员 → 只留证：证据私聊转发给指定管理员与群主，登记待处理，等人工处理；
   - 玩笑互动 / 安全 → 不处罚，视开关决定是否在群里提示 / 私聊通知；第三方恶意举报会提示管理员；
   - 拿不准 / 证据没送达 → 私聊管理员和指定用户，附发生时间、参与人员、上下文聊天记录、AI 结论与举报 ID。
4. 管理员 / 指定用户私聊回复 `1` → 处理完成，通知其他已通知对象。

## ⚙️ 配置项

### AI 研判

| 配置项 | 默认 | 说明 |
|:---|:---:|:---|
| `ai_provider_id` | 空 | 用于研判的 AI 提供商，留空用当前默认；可指定 DeepSeek 等 |
| `ai_confidence_threshold` | 0.7 | 判定违规的置信度阈值，达到才自动撤回禁言 |
| `ai_timeout` | 30 | 单次调用超时（秒），超时转人工 |
| `ai_retry_times` | 1 | 调用失败重试次数 |
| `ai_system_prompt` | 内置 | 举报研判系统提示词，务必保留输出 JSON 的要求 |
| `auto_review_system_prompt` | 内置 | 自动审查系统提示词，务必保留输出 findings JSON 的要求 |

**上下文与玩笑豁免**（`ai` 分组独立卡片）：

| 配置项 | 默认 | 说明 |
|:---|:---:|:---|
| `context_enabled` | `true` | 结合上下文研判：把被举报消息前后的整段群聊（含其他成员）一起交给 AI，避免把熟人玩笑误判为辱骂 |
| `context_before_count` | 30 | 上下文回溯条数：被举报消息**之前**纳入的记录条数（0–60） |
| `context_after_count` | 5 | 被举报消息**之后**纳入的记录条数（0–30） |
| `context_fetch_history` | `true` | 缓存不足时调用 `get_group_msg_history` 拉取群历史补齐，需要机器人有权限，失败自动退回本地缓存 |
| `context_max_chars` | 3000 | 上下文文本上限（字符，500–12000），超出时从最早的记录开始裁剪 |
| `banter_guard` | `true` | 玩笑 / 互喷豁免：AI 判定为双方玩笑互动时，含辱骂词也不处罚；AI 判定严重程度为 `high` 时仍照常处理 |
| `ai_vision_enabled` | `false` | 启用图片理解，需要支持视觉的模型与 `image_urls` 的提供商 |
| `ai_vision_max_images` | `4` | 单次最多送图张数（1–8） |
| `ai_vision_target_images` | `true` | 读取被举报/候选消息图片 |
| `ai_vision_context_images` | `true` | 读取上下文图片 |
| `ai_vision_detect_violation` | `true` | 识别违规图片 |
| `ai_vision_detect_qr` | `true` | 识别二维码/小程序码 |
| `ai_vision_detect_diversion` | `true` | 识别引流图 |
| `ai_vision_read_screenshots` | `true` | 读取聊天记录截图 |
| `ai_vision_extra_prompt` | 空 | 追加图片检查要求 |

### 默认群配置（可被分群覆盖）

| 配置项 | 默认 | 说明 |
|:---|:---:|:---|
| `default_group_rules` | 内置 | 默认群规，AI 研判依据 |
| `default_receiver_ids` | `[]` | 默认指定用户 QQ 列表 |
| `auto_include_group_admins` | `false` | 自动把本群群主/管理员纳入通知对象 |
| `default_mute_duration` | 600 | 默认禁言时长（秒） |
| `recent_msg_count` | 10 | 无引用举报读取的最近消息条数 |
| `report_cooldown` | 60 | 同一人同群举报冷却（秒），0 不限 |
| `daily_report_limit` | 20 | 单人每日举报上限，0 不限 |
| `group_overrides` | `{}` | 分群覆盖（JSON），页面上可视化编辑 |

**证据留存与指定管理员**（`groups` 分组）：

| 配置项 | 默认 | 说明 |
|:---|:---:|:---|
| `evidence_admin_ids` | `[]` | **指定管理员 / 群主 QQ**（每行一个）：撤回 / 禁言前先把违规发言与上下文私聊转发给这里；机器人不是本群管理员时也会发给这里和群主；留空时依次回退到「默认指定用户 QQ」→ bot 管理员 |
| `not_admin_evidence_only` | `true` | 机器人不是本群管理员时只留证转发：跳过撤回 / 禁言 / 群内警告，只把证据私聊转发给指定管理员与群主，并登记待处理 |
| `evidence_required` | `true` | 证据未送达则不处罚：证据私聊全部失败时跳过撤回 / 禁言并转人工；关闭后证据失败仍会照常处理（不推荐） |
| `evidence_use_forward` | `true` | 优先合并转发证据（`send_private_forward_msg`），失败自动退化为普通私聊文本 |
| `evidence_keep_files` | `true` | 在插件数据目录 `evidence/` 下保存每次处理前的证据 JSON（含完整上下文） |

**自动审查**（`groups` 分组，建议用分群覆盖按群开启）：

| 配置项 | 默认 | 说明 |
|:---|:---:|:---|
| `auto_review_enabled` | `false` | 是否对本群启用自动审查 |
| `auto_review_message_threshold` | 0 | 累计多少条新消息触发一次审查；0 = 不按条数触发 |
| `auto_review_interval_minutes` | 0 | 距上次审查多少分钟且有新消息时触发；0 = 不按时间触发 |
| `auto_review_scope_count` | 50 | 每次审查读取最近多少条消息作为上下文 |
| `auto_review_scope_minutes` | 60 | 只读取最近多少分钟内的消息；0 = 不限 |
| `auto_review_notify_mode` | `safe_silent` | `always` 无论是否安全都通知；`safe_silent` 安全则不通知；`after_action` 仅处理后保留证据并通知 |
| `auto_review_max_actions` | 3 | 单轮最多自动撤回/禁言多少条违规消息；0 = 只通知不处理 |
| `auto_review_notify_dedup_minutes` | 60 | 同一违规消息 ID 在该时间内只标记/通知一次 |
| `auto_review_state_max_groups` | 200 | 自动审查状态最多保留多少个最近活跃的群 |
| `auto_review_pending_cap` | 10000 | 单群累计等待审查的消息条数上限 |

### 行为开关

| 配置项 | 默认 | 说明 |
|:---|:---:|:---|
| `notify_admin_when_safe` | `false` | AI 判断安全时私聊通知管理员与指定用户 |
| `announce_safe_in_group` | `false` | **AI 判断安全时在群内发一条安全提示** |
| `notify_include_context` | `true` | 转人工 / 安全 / 证据通知自动附带发生时间、参与人员、上下文聊天记录与事情起因；模板里已含 `{context_log}` 时不会重复追加 |
| `forward_context_records` | `true` | 转人工/安全通知先发文本再单独发聊天记录；自动审查只发一条合并转发记录，不再发纯文本；关闭后只在通知正文里附带聊天原文 |
| `notify_admin_on_suspected_malicious` | `true` | 举报人未参与该段对话、且上下文判定为玩笑互动时，私聊提示管理员（用「疑似恶意举报模板」），不处罚被举报人 |
| `notify_on_resolved` | `true` | 处理完成后通知其他已通知对象 |
| `broadcast_result_in_group` | `false` | 在群内播报处理结果 |
| `enable_target_report_count` | `true` | 统计被举报人累计被举报次数 |
| `allow_everyone_report` | `true` | 允许所有人举报；关闭后仅白名单可举报 |
| `reporters_whitelist` | `[]` | 举报人白名单 |
| `receiver_can_handle` | `true` | 指定用户与管理同等处理权限 |
| `reply_content` | `1` | 管理员处理完成的回复内容 |
| `report_expire_minutes` | 60 | 超时未处理时长（分钟），超时标记 `expired` |
| `conflict_detection_enabled` | `false` | 主动冲突观察总开关 |
| `conflict_min_participants` | 2 | 窗口内最少不同发言人数 |
| `conflict_window_messages` | 20 | 冲突检测窗口消息数 |
| `conflict_window_minutes` | 10 | 冲突检测窗口时长（分钟） |
| `conflict_min_new_messages` | 6 | 上次扫描后至少新增多少条才再次研判 |
| `conflict_cooldown_minutes` | 10 | 同群冲突预警冷却（分钟） |
| `conflict_min_severity` | `medium` | 触发预警的最低严重程度 |
| `conflict_notify_admin` | `true` | 冲突时私聊管理员 |
| `conflict_mediate` | `true` | 冲突时发一条群内中性劝停 |
| `conflict_extra_prompt` | 空 | 冲突观察补充提示词 |

### 模板

| 配置项 | 用途 |
|:---|:---|
| `warn_template` | 群内警告（会先自动艾特被举报人） |
| `safe_group_template` | **群内安全提示**（`announce_safe_in_group` 开启时） |
| `safe_notify_template` | 安全通知（私聊，附上下文结论与聊天记录） |
| `manual_notify_template` | 转人工通知（引用举报，含消息内容、上下文与事情起因） |
| `manual_notify_template_nocontent` | 转人工通知（无引用 @ 举报：不单独展示消息原文，上下文记录同样附带） |
| `evidence_template` | **证据转发模板**：撤回 / 禁言前私聊发给指定管理员的证据文案，含完整上下文；机器人不是管理员时同一条模板用于仅留证转发，后续动作用 `{action_plan}` |
| `conflict_notify_template` | 群聊冲突预警私聊模板 |
| `conflict_mediate_template` | 群内中性劝停模板（代码会自动在前面加 @冲突双方） |
| `malicious_notify_template` | **疑似恶意举报模板**：举报人未参与对话且属玩笑互动时，用它私聊提示管理员 |
| `auto_review_notify_template` | **自动审查通知模板**：模式 1/2/3 共用，含审查范围、违规条数、处理结果与聊天记录 |
| `resolved_notify_template` | 处理完成通知 |
| `group_broadcast_template` | 群内处理结果播报 |

## 🧩 模板变量

| 变量 | 说明 |
|:---:|:---|
| `{group_name}` / `{group_id}` | 群名称 / 群号 |
| `{sender}` | 被举报消息发送者昵称 |
| `{target}` | 被举报人（昵称(QQ)） |
| `{reporter}` | 举报人（昵称(QQ)） |
| `{message}` | 被举报消息内容（无引用举报时为空/占位） |
| `{message_id}` | 被举报消息 ID |
| `{target_report_count}` | 被举报人累计被举报次数 |
| `{warn_count}` | 被举报人累计警告次数 |
| `{ai_judgement}` | AI 初步判断：安全 / 拿不准 / 违规但无法处理 |
| `{ai_confidence}` | 置信度（百分比） |
| `{report_id}` | 本次举报 ID |
| `{recent_times}` / `{recent_count}` | 最近消息的发送时间 / 条数 |
| `{time}` | 当前时间 |
| `{handler}` | 处理人（处理完成通知 / 播报时有效） |
| `{mute_duration}` | 禁言时长（已格式化，如 `10分钟`） |
| `{context_log}` | 上下文聊天记录（多行，含说话人与时间） |
| `{context_count}` | 上下文条数 |
| `{participants}` | 参与人员（这段上下文里说过话的人） |
| `{occur_time}` | 被举报消息发生时间 |
| `{context_conclusion}` | AI 上下文结论 |
| `{cause}` | 事情起因（模板里放在末尾） |
| `{malicious_hint}` | 疑似第三方恶意举报提示（正常举报时为空） |
| `{action_plan}` | 后续动作说明：正常处理为「撤回该消息 + 禁言 X」，机器人非管理员时为「仅留存证据，请管理员手动处理」 |
| `{review_scope}` | 自动审查范围（最近 N 条 / N 分钟内） |
| `{violation_count}` | 自动审查发现的违规条数 |
| `{conflict_type_label}` | 冲突类型：两人争执 / 多人争执 |
| `{trend_label}` | 冲突趋势：正在升级 / 持续对峙 / 正在降温 |
| `{recent_messages}` | 冲突窗口内的最近消息摘要 |
| `{suggestion}` | 给管理员的处理建议 |

变量缺失时会原样保留 `{变量名}`，不会报错。

## 🔀 分群覆盖

默认群配置可针对单个群覆盖，key 为群号。可覆盖字段：

`group_rules`、`receiver_ids`、`mute_duration`、`recent_msg_count`、`report_cooldown`、
`daily_limit`、`notify_admin_when_safe`、`announce_safe_in_group`、`auto_include_group_admins`、
`reply_content`、`warn_template`、`safe_group_template`、
`context_enabled`、`context_before_count`、`context_after_count`、`banter_guard`、
`ai_vision_enabled`、`ai_vision_max_images`、`ai_vision_target_images`、`ai_vision_context_images`、
`ai_vision_detect_violation`、`ai_vision_detect_qr`、`ai_vision_detect_diversion`、`ai_vision_read_screenshots`、`ai_vision_extra_prompt`、
`conflict_detection_enabled`、`conflict_min_participants`、`conflict_window_messages`、`conflict_window_minutes`、
`conflict_min_new_messages`、`conflict_cooldown_minutes`、`conflict_min_severity`、`conflict_notify_admin`、`conflict_mediate`、`conflict_extra_prompt`、
`evidence_admin_ids`、`evidence_required`、`not_admin_evidence_only`、`notify_include_context`、`forward_context_records`、`notify_admin_on_suspected_malicious`,、
`auto_review_enabled`、`auto_review_message_threshold`、`auto_review_interval_minutes`、
`auto_review_scope_count`、`auto_review_scope_minutes`、`auto_review_notify_mode`、`auto_review_max_actions`、`auto_review_notify_dedup_minutes`、
`auto_review_state_max_groups`、`auto_review_pending_cap`

| 新增可覆盖字段 | 类型 | 说明 |
|:---|:---:|:---|
| `context_enabled` | bool | 该群是否结合上下文研判 |
| `context_before_count` | number | 上下文回溯条数（0–60） |
| `context_after_count` | number | 被举报消息之后的条数（0–30） |
| `banter_guard` | bool | 该群是否启用玩笑豁免 |
| `ai_vision_enabled` | bool | 该群是否启用图片理解 |
| `ai_vision_max_images` | number | 单次最多送图张数 |
| `ai_vision_target_images` | bool | 读取目标消息图片 |
| `ai_vision_context_images` | bool | 读取上下文图片 |
| `ai_vision_detect_violation` | bool | 识别违规图片 |
| `ai_vision_detect_qr` | bool | 识别二维码 |
| `ai_vision_detect_diversion` | bool | 识别引流图 |
| `ai_vision_read_screenshots` | bool | 读取聊天记录截图 |
| `ai_vision_extra_prompt` | text | 图片检查补充要求 |
| `conflict_detection_enabled` | bool | 该群是否启用主动冲突观察 |
| `conflict_min_participants` | number | 冲突最少参与人数 |
| `conflict_window_messages` | number | 冲突检测窗口消息数 |
| `conflict_window_minutes` | number | 冲突检测窗口分钟数 |
| `conflict_min_new_messages` | number | 触发研判的新消息数 |
| `conflict_cooldown_minutes` | number | 同群预警冷却 |
| `conflict_min_severity` | string | 最低预警严重程度 |
| `conflict_notify_admin` | bool | 冲突私聊管理员 |
| `conflict_mediate` | bool | 冲突群内劝停 |
| `conflict_extra_prompt` | text | 冲突观察补充要求 |
| `evidence_admin_ids` | list | 该群的证据接收 QQ |
| `evidence_required` | bool | 该群证据未送达是否跳过处罚 |
| `not_admin_evidence_only` | bool | 该群机器人不是管理员时是否只留证转发 |
| `notify_include_context` | bool | 该群通知是否附带上下文聊天记录 |
| `forward_context_records` | bool | 该群通知后是否单独发送聊天记录存档 |
| `notify_admin_on_suspected_malicious` | bool | 该群是否提示疑似第三方恶意举报 |
| `auto_review_enabled` | bool | 该群是否启用自动审查 |
| `auto_review_message_threshold` | number | 该群审查触发条数 |
| `auto_review_interval_minutes` | number | 该群审查触发间隔（分钟） |
| `auto_review_scope_count` | number | 该群审查上下文条数 |
| `auto_review_scope_minutes` | number | 该群审查时间范围（分钟） |
| `auto_review_notify_mode` | string | 该群审查通知模式 |
| `auto_review_max_actions` | number | 该群单轮最多处理条数 |
| `auto_review_notify_dedup_minutes` | number | 该群违规通知去重时长 |
| `auto_review_state_max_groups` | number | 状态最多保留群数 |
| `auto_review_pending_cap` | number | 单群 pending 上限 |

JSON 示例：

```json
{
  "123456789": {
    "group_rules": "本群禁止发广告、禁止刷屏。",
    "receiver_ids": ["10001", "10002"],
    "mute_duration": 3600,
    "recent_msg_count": 15,
    "notify_admin_when_safe": true,
    "announce_safe_in_group": true,
    "context_before_count": 20,
    "evidence_admin_ids": ["10001"],
    "evidence_required": true,
    "auto_review_enabled": true,
    "auto_review_message_threshold": 50,
    "auto_review_interval_minutes": 30,
    "auto_review_scope_count": 50,
    "auto_review_scope_minutes": 60,
    "auto_review_notify_mode": "safe_silent",
    "auto_review_max_actions": 3,
    "auto_review_state_max_groups": 200,
    "auto_review_pending_cap": 10000
  }
}
```

> 推荐在独立配置页面的「分群覆盖」区可视化编辑：新增群 → 填群号 → 勾选要覆盖的项，不勾的走默认值。

## 📌 数据

运行数据保存在 AstrBot 插件数据目录：`data/plugin_data/astrbot_plugin_chat_report/`

- `counters.json`：被举报次数、警告次数统计
- `auto_review_state.json`：自动审查状态（pending / 上次审查时间 / 上次审查消息位置），受群数、pending、文件大小上限约束
- `reports.json`：待处理举报
- `audit.jsonl`：审计日志（谁举报、谁被举报、AI 判断、是否处理、处理人；机器人非管理员时记 `evidence_only`；自动审查记 `auto_review` / `auto_review_notified`）
- `evidence/`：证据留存目录，每次自动处理前写入 `evidence/<举报ID>.json`，
  内含被举报消息、发生时间、参与人员、AI 结论与完整上下文聊天记录（`evidence_keep_files` 关闭时不写）

## 📄 License

MIT
