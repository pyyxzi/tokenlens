# TokenLens 1.2.6 全面审计报告

- **审计对象**：`F:\A_test\token统计\all\源码\kjuan_new`（源码提交 `fbbde08`，版本 1.2.6）
- **审计日期**：2026-11-12
- **审计范围**：backend Python（`tokenlens/` 31 个模块 + `service.py`）、frontend（HTML/CSS/JS）、src-tauri（Rust 宿主）、构建与测试脚本
- **审计方式**：逐模块代码审查 + 隔离环境动态复现（`TOKENLENS_HOME` 指向临时目录，不触碰用户真实索引库）+ 真实日志只读样本验证
- **基线**：仓库自带 101 个单元测试全部通过（`python -m unittest discover -s tests`）

---

## 0. 结论摘要

| 严重度 | 数量 | 说明 |
| --- | ---: | --- |
| High | 6 | 显著数据错误 / 核心功能不可用 / 隐私外泄，常规使用即可触发 |
| Medium | 25 | 特定条件下数据错误、接口 500、状态竞争或体验缺陷 |
| Low | 20 | 边缘场景、防御性不足、UI 细节 |
| 已修复 | 1 | Codex `.jsonl.zst` 全量漏扫（本次会话修复并验证） |

### 最需要优先处理的 6 个问题

1. **Cline 用量虚增 41%**：`tokensIn` 已包含 `cacheWrites`，适配器只减了 `cacheReads`（真实数据复现）。
2. **Gemini CLI 整个会话归零**：JSONL 中的 `$set.messages` 快照会清空已计费消息（真实数据 35,652 token → 0 条）。
3. **Codex quick/full 模式切换永久双计**：聚合行与明细行叠加，切回后仍无法恢复（110 → 610 token）。
4. **备份无法恢复自身导出**：`sqlite_stat1` 被 `_stage()` 拒绝，恢复功能对已执行过 `PRAGMA optimize` 的库完全不可用。
5. **PromptLab 保存永久失败**：检测登记用解析后的协议，保存用 `"auto"` 计算指纹，两者永不相等。
6. **AI 助手把本地绝对路径发给外部模型**：过滤条件里的 `project_paths` 未剥离，与函数自身"不发送源路径"的契约冲突。

---

## 1. 审计方法与证据说明

- **动态复现**：每个 High/Medium 问题都在临时目录中构造最小夹具或用本机真实日志的只读副本复现；文末“验证”字段标注 `已复现` 的均可在当前代码上重复。
- **代码审查**：少数并发/时序类问题无法稳定复现，标注 `代码路径确认`，并给出触发条件。
- **真实数据样本**（只读）：
  - Cline：`%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\tasks\1777691501725\ui_messages.json`
  - Gemini：`~/.gemini/tmp/gemini/chats/session-2026-08-31T18-14-f082b5b9.jsonl`
  - Codex：`~/.codex/sessions/**` 与 `~/.codex/state_5.sqlite`
- **未修改任何业务代码**。仅新增了 `problems.md`、Codex `.jsonl.zst` 修复及其测试/文档。

---

# 2. 问题清单

## 2.1 数据采集与统计

### H-01 [High] Cline / Roo / Kilo 把缓存写入重复计入输入，用量虚增 41%

- **位置**：`backend/tokenlens/adapters.py:2314`（`ClineFamilyAdapter.parse`）
- **问题**：Cline 的 `tokensIn` 是**包含缓存**的提示总量（真实数据中 `tokensIn == cacheWrites + cacheReads + 未缓存输入` 精确成立）。适配器只做了 `inp = max(0, inp - cr)`，没有减去 `cacheWrites`，导致 `cache_write` 同时以输入价和缓存写价计费。
- **复现**：解析本机真实任务 `ui_messages.json`（6 次调用）：
  - 源数据：`tokensIn=93,567`、`cacheWrites=38,911`、`cacheReads=54,656`、`tokensOut=832`
  - 适配器输出：`input=38,911 + cache_write=38,911 + cache_read=54,656 + output=832 = 133,310`
  - 正确总量应为 `93,567 + 832 = 94,399`，**虚增 41.2%**
- **预期**：`input = max(0, tokensIn - cacheReads - cacheWrites)`
- **实际**：缓存写被双计，成本同步虚高
- **影响**：所有 Cline/Roo/Kilo 用户的 token 与费用统计系统性偏高
- **修复建议**：同时减去两个缓存桶；为真实 Cline 样本补回归测试（`tokensIn == cacheW/cacheR/未缓存` 之和）
- **验证**：已复现（真实数据）

---

### H-02 [High] Gemini CLI 的 `$set.messages` 快照会清空已计费消息，整个会话归零

- **位置**：`backend/tokenlens/adapters.py:870-873`（`GeminiCliAdapter.parse`）
- **问题**：
  ```python
  if "messages" in updates:
      messages.clear()
      for message in updates["messages"]:
          keep(message)
  ```
  Gemini CLI 会周期性写入只含初始用户消息的 `$set.messages` 快照。代码用快照**整体替换**内存中的消息表，之前已带 `tokens` 的 gemini 消息被清除；最终只统计快照里剩下的消息。
- **复现**：解析本机真实文件 `session-2026-08-31T18-14-f082b5b9.jsonl`：
  - 第 6、11 行含 gemini 消息，tokens 合计 8,906 + 9,070 = 17,976（输入+输出 35,652）
  - 第 18、23、28 行 `$set.messages` 只包含 1 条初始用户消息
  - `GeminiCliAdapter.parse()` 返回 **0 条记录**
- **预期**：已发生的调用用量应保留（append-only 账本），快照只影响展示状态
- **实际**：会话级用量全部丢失；`$rewindTo` / `$patch.removeIds` 同样会删除已计费用量
- **影响**：Gemini CLI 部分会话完全不计入；用户会看到"用了很多但统计为 0"
- **修复建议**：把用量记录与消息状态分离——维护按消息 ID 的 usage 账本，重放/回滚只影响内容状态；或至少保留"曾出现过的 tokens"不被后续快照删除
- **验证**：已复现（真实数据）

---

### H-03 [High] Codex quick/full 模式切换后聚合行与明细行永久叠加

- **位置**：
  - `backend/tokenlens/adapters.py:518-521`（quick 模式只枚举 `state_*.sqlite`）
  - `backend/tokenlens/scanner.py:186-204`（"discovery omitted an indexed file" 判定）
  - `backend/tokenlens/scanner.py:392-393`（有错误时跳过 `drop_missing_files_for_tool`）
- **问题**：quick 模式读取 `state_*.sqlite` 的 `threads.tokens_used` 生成聚合行；之前全量扫描写入的 rollout 明细行不会被清理。scanner 发现"已索引的 rollout 文件没有被枚举"，把它当作异常并**放弃 prune**，两种口径同时保留。切回全量模式时，聚合行同样不会被清除。
- **复现**：临时 Codex 目录 + 1 个 110 token 的 rollout + `state_5.sqlite` 中 500 token 的线程：
  1. 全量扫描：`records=1, tokens=110`
  2. 开启 `codex_quick` 再扫：`records=2, tokens=610`，来源状态变为 `partial`，错误 `discovery omitted an indexed file; history was retained`
  3. 关闭 quick 再扫：仍为 `610`，错误持续存在
- **预期**：切换口径应替换而不是叠加；聚合与明细互斥
- **实际**：永久双计，且来源卡片一直显示错误
- **影响**：切换过快速扫描的用户 Codex 统计接近翻倍，且无法通过常规重扫恢复
- **修复建议**：模式切换时清理另一口径的文件/记录（按 `files.tool` + 模式标记），或让两种模式各自维护独立的文件命名空间并在枚举层声明"本模式不包含另一模式的路径"，不要把合法省略当成文件丢失
- **验证**：已复现

---

### H-04 [High] 应用自己导出的备份无法恢复（`sqlite_stat1` 被拒绝）

- **位置**：
  - `backend/tokenlens/store.py:563`（`Store.close()` 执行 `PRAGMA optimize`）
  - `backend/tokenlens/backup_service.py:292`
- **问题**：`PRAGMA optimize` / `ANALYZE` 会在数据库中生成 `sqlite_stat1`（部分构建还有 `sqlite_stat4`）。导出使用 SQLite 备份 API，会把它一起复制。恢复校验只允许 `data_tables | FTS_TABLES | {sqlite_sequence}`，遇到 `sqlite_stat1` 直接抛 `备份数据库包含不允许的数据表`。
- **复现**（隔离 Store）：
  1. 写入 1 条记录，`store.close()` → 库里出现 `sqlite_stat1`
  2. `BackupService.export()` 导出 ZIP
  3. `BackupService.preview(base64)` → `BackupError: 备份数据库包含不允许的数据表`
  4. 删除 `sqlite_stat1` 后同一份数据可正常预览
- **预期**：应用自身导出的备份必须可以恢复
- **实际**：只要数据库被分析过（关闭服务即可能触发），备份恢复功能完全不可用
- **影响**：灾难恢复路径失效；用户以为有备份，实际无法还原。本机当前 166 MB 索引库尚未生成 `sqlite_stat1`，但服务正常关闭后可能生成
- **修复建议**：在快照阶段忽略/删除 `sqlite_stat1`、`sqlite_stat4`（或在 `_stage()` 白名单中加入并跳过），并补一条"close 后导出→预览→恢复"的回归测试
- **验证**：已复现

---

### M-01 [Medium] 全量重解析会用旧成本快照覆盖新的 actual/free 元数据

- **位置**：`backend/tokenlens/store.py:727-760`
- **问题**：替换文件时，只要旧行能按事件身份匹配到，就把 `cost/cost_kind/cost_source/price_version/currency` 五列**无条件**复制旧值，完全不看新记录的分类。
- **复现**：
  1. 旧行 `cost=5.0, cost_kind='actual'`
  2. 以同一事件、`cost=0.0, cost_kind='free'` 重新解析
  3. 查询结果仍是 `cost=5.0, cost_kind='actual'`，违反模型"free/unknown 必须为 0"的约束
  4. 反向场景（旧 `estimated 1.0` → 新 `actual 9.9`）同样保留旧估算
- **预期**：新的 actual/free 分类优先；只有新记录仍是 estimated/unknown 时才继承旧值
- **实际**：解析器升级或上游修正账单后，修正永远不生效
- **影响**：成本长期错误；免费本地推理被显示为已计费
- **修复建议**：仅在 `new.cost_kind in ('estimated','unknown')` 时继承旧成本；新记录为 `actual/free` 时直接采用新值
- **验证**：已复现

---

### M-02 [Medium] Kimi 未标注会话被误判为"累计镜像"，少计约 44%

- **位置**：`backend/tokenlens/adapters.py:1833-1846`
- **问题**：未带 `turnId`/`scope` 的记录只要满足"≥3 条、单调不减、末值 ≥ 首值×2"就判定为累计镜像并只保留末值。正常的上下文增长会话（每轮输入递增）完全符合该特征。
- **复现**：3 条未标注轮次，total 分别为 1,200 / 2,300 / 4,400：
  - 预期：3 条记录，合计 7,900
  - 实际：1 条记录，合计 4,400（输入 1,000+2,000+4,000 被压成 4,000）
- **预期**：逐轮计费；只有能证明"末值≈轮次总和"或存在显式 session scope 时才按累计处理
- **实际**：增长型会话被腰斩
- **影响**：Kimi 未标注会话用量系统性偏低
- **修复建议**：要求更强的累计证据（如与已解析轮次总量对齐、存在 `scope=session` 标记、或多会话共享同一累计序列），否则保留逐条记录
- **验证**：已复现

---

### M-03 [Medium] Aider 所有记录使用文件 mtime，日期归属错误

- **位置**：`backend/tokenlens/adapters.py:2910`
- **问题**：标题行里的日期时间只用于剥离模型名前缀，随后被丢弃；每条记录统一用 `os.path.getmtime(path)`。
- **复现**：历史文件含 `#### 2026-01-02 10:00 gpt-4o` 与 `#### 2026-03-04 10:00 gpt-4o`，把 mtime 设为 2030-01-01：
  - 预期：2026-01-02 / 2026-03-04
  - 实际：两条都是 2030-01-01
- **预期**：优先使用标题行时间，缺失时才回退 mtime
- **实际**：所有历史塌缩到文件最后修改日
- **影响**：日/月趋势、活跃时间、周期对比全部失真
- **修复建议**：解析标题行时间并写入 `ts`（当前正则已能匹配，只需保留解析结果）
- **验证**：已复现

---

### M-04 [Medium] 导入记录的事件 ID 跨文件碰撞，合法记录被静默丢弃

- **位置**：`backend/tokenlens/source_import.py:290-291`
- **问题**：CSV/API 导入的 identity 为 `[tool, session, explicit_id]`，不包含时间、模型、用量或导入文件摘要。两个文件使用各自从 1 开始的 `id` 列（或不同账号复用同一请求 ID）时，第二次导入会被判为重复。
- **复现**：
  1. 导入 CSV A：`1,gpt-4o,100,10,2026-01-02T00:00:00Z,alpha` → 成功，入库 100 input
  2. 导入 CSV B：`1,gpt-4o,200,20,2026-02-02T00:00:00Z,alpha` → preview 显示 1 条/210 token，`commit` 返回 `{"imported":0,"duplicate":true}`
  3. 最终库中只有 100 input，B 的 200 input 丢失
- **预期**：不同文件/不同时间的同 ID 记录应可共存；同一文件内 ID 重复且用量不同应报错
- **实际**：静默丢弃，只返回 duplicate
- **影响**：批量导入数据不完整，用户难以发现
- **修复建议**：identity 加入导入文件摘要（或 ts/model/usage），并对同文件内 `id` 重复但会计字段不同的情况直接拒绝预览
- **验证**：已复现

---

### M-05 [Medium] 模型筛选/成本重算与分组使用的归一化规则不一致

- **位置**：`backend/tokenlens/aggregator.py:326-330`（`_model_clause`）、`store.py:1047-1053`（`recalc_costs`）
- **问题**：分组使用 Python `str.strip().lower()`（`tokenlens_model_key`，Unicode 感知、去除所有空白），筛选却生成 `lower(trim(model)) = ?`（SQLite 只去空格、`lower` 只处理 ASCII）。
- **复现**：写入模型 `"m"`（10 token）与 `"m\t"`（100 token）：
  - `by_model("all")` 返回 `[('m', 110)]`
  - `records(model="m")` 只返回 1 条 10 token
  - `model="未知模型"` 过滤返回 0 条，而 `by_model` 中"未知模型"确有汇总
- **预期**：按 UI 展示的模型名筛选应命中同一组
- **实际**：非空格空白/非 ASCII 大小写导致漏选；成本重算同样漏行
- **影响**：下钻结果与汇总对不上，重算价格不完整
- **修复建议**：筛选与重算统一使用已注册的 `tokenlens_model_key(model)`，包括 `未知模型` 分支
- **验证**：已复现

---

### M-06 [Medium] 会话详情跨工具/项目混合统计，模型分组重复

- **位置**：`backend/tokenlens/aggregator.py:1159-1185`
- **问题**：
  1. `session_detail` 只按 `session_id` 过滤，`tool`/`project` 是可选参数；`head` 查询在 `SUM/COUNT` 旁直接选裸 `tool/project` 且无 `GROUP BY`，SQLite 会返回任意一行。
  2. 模型分组写的是 `COALESCE(NULLIF(model,''),'未知模型') AS model ... GROUP BY model`，SQLite 把 `model` 解析为源列而非别名，导致 `NULL`、`''`、字面量"未知模型"分成多组；大小写变体也不合并。
- **复现**：
  - 同一 `session="shared"` 分别属于 `codex/p1`（100 token）与 `claude-code/p2`（7 token），`session_detail("shared")` 返回 `tool=codex, project=p1, total=107`，两个工具/项目的模型与轮次被混在一起。
  - 同一会话插入 `model=''`(1)、`NULL`(4)、`model-a`(8)、`Model-A`(2)，`session_detail` 返回两条"未知模型"（4 和 1）、两条大小写不同的 model-a，而 `by_model` 已合并为 `model-a=10`、`未知模型=5`。
- **预期**：详情按 `(tool, project, session)` 身份过滤；模型按统一归一化键分组
- **实际**：标签与总量不一致，抽屉中的数据不可信
- **影响**：会话详情页归属错误、占比/缓存率/成本与排行不一致
- **修复建议**：`session_detail` 强制/推导 `tool`+`project` 身份并按身份过滤；模型分组 `GROUP BY COALESCE(NULLIF(tokenlens_model_key(model),''),'未知模型')`
- **验证**：已复现

---

### M-07 [Medium] 通用数据库适配器把缓存列误当成输入列

- **位置**：`backend/tokenlens/adapters.py:3277-3290`（`_VendorScanAdapter._tok_cols`）
- **问题**：匹配条件使用 `k in low` 子串判断，`cache_read_input_tokens`、`cached_input_tokens` 都包含 `input_tokens`。若缓存列在表结构中先于 `prompt_tokens`，它会被选为输入列，真正的 prompt 列因"首个命中"逻辑被跳过。
- **复现**：表 `(id, cache_read_input_tokens, prompt_tokens, completion_tokens)`，两行 `(900, 1000, 50)`：
  - 预期：input=2000、output=100（或 input=200、cache_read=1800）
  - 实际：`input=1800, output=100, cache_read=0`
- **预期**：列名精确匹配优先，且 `_TOKEN_IN` 应排除包含 `cache/cached` 的列
- **实际**：缓存数量被当作输入重复统计
- **影响**：任何列顺序如此的三方数据库来源都会被严重错计
- **修复建议**：先精确匹配 `prompt_tokens/input_tokens`，再退回 `endswith`，并显式排除 `cache*`/`cached*`
- **验证**：已复现

---

### M-08 [Medium] ApiLog 遇到"以 `[` 开头但不是 JSON"的日志行会整文件失败

- **位置**：`backend/tokenlens/adapters.py:3017`
- **问题**：`if ... or line.lstrip().startswith(("{", "[")):` 之后直接 `json.loads(line)`。常见网关日志 `[2026-01-02 03:04:05] POST /v1/chat usage: {...}` 以 `[` 开头但不是 JSON，抛 `JSONDecodeError`，整个文件被 scanner 记为失败并保留旧快照。
- **复现**：`gateway.log` 写入上述单行 → `ApiLogAdapter.parse()` 抛 `JSONDecodeError`，0 条记录
- **预期**：JSON 解析失败时回退到文本正则提取（该行明明含 `usage`）
- **实际**：整文件被拒绝，可能长期不再更新
- **影响**：带时间戳前缀的网关日志完全无法采集
- **修复建议**：`try: json.loads` 失败后走 `_from_text`；只对真正以 `{` 开头的行做强 JSON 校验
- **验证**：已复现

---

### M-09 [Medium] OpenCode / Cline 的 `cost: 0` 被当成"真实账单"，永不复价

- **位置**：`backend/tokenlens/adapters.py:1344-1350`（OpenCode）、`adapters.py:2313`（Cline）
- **问题**：`actual = r.get("cost") is not None`，只要字段存在就标记 `cost_kind="actual"`。上游客户端在无法定价时通常写 `0`，于是这些记录被当作"真实费用 0"，后续 `_priced_rows` 不会用价格表重算。
- **复现**：
  - 本机 Cline 真实任务 6 条记录全部 `cost=0`，适配器 `cost_kind` 全部为 `actual`，费用显示 $0
  - 本机 OpenCode 库中 `grok-4.6` 会话 `cost=0.0`，但价格表估算约 $0.12
- **预期**：0 值应视为"未定价"，回退价格表估算或标记 unknown
- **实际**：全部显示为 $0 的真实账单
- **影响**：成本、预算、报表系统性偏低
- **修复建议**：仅非零来源成本视为 actual；或将这些客户端字段统一归类为 `estimated` 并允许重算
- **验证**：已复现（真实数据）

---

### M-10 [Medium] Markdown 报告在 `repriced_cost` / `historical_cost` 口径下预算少算

- **位置**：`backend/tokenlens/server.py:105-106`、`backend/tokenlens/finance.py:79-97`、`backend/tokenlens/report.py:272-276`
- **问题**：`report_aggregator` 为重新定价创建 `TEMP VIEW records`，只包含"上一等长周期 + 当前周期"的行；`report.render()` 里的 `_budget()` 调用 `agg.budget()`，后者按**自然月**统计，却命中了这个被裁剪的临时视图。
- **复现**：本月 1 日写入 100,000、本月 2 日写入 900,000、今天写入 1,000，预算 10 亿：
  - `cost_basis=total_cost` → 预算已用 1,001,000（正确）
  - `cost_basis=repriced_cost` → 预算已用 **1,000**（少算 1000 倍）
- **预期**：预算始终反映完整预算周期
- **实际**：导出报告中预算严重偏低，可能把"已超支"显示成"远未用完"
- **影响**：仅影响 Markdown 导出（仪表盘正常），但会误导基于报告的决策
- **修复建议**：预算查询使用独立连接/独立快照，或把临时视图窗口扩大到预算周期起点；注意当前 `TEMP VIEW` 会遮蔽所有后续 `records` 查询
- **验证**：已复现

---

### M-11 [Medium] 旧版价格文件损坏会导致整个服务无法启动

- **位置**：`backend/tokenlens/server.py:237-266`（`_migrate_legacy_prices`）、`backend/service.py:32`
- **问题**：迁移逻辑直接 `json.load()` 并 `raise ValueError`，异常一路逃出 `App.__init__`；`service.py` 没有兜底，进程以退出码 1 结束，Rust 宿主只会看到"本地服务启动超时"并无限重试。
- **复现**：在 `TOKENLENS_HOME` 写入 `custom_prices.json` 内容 `{not valid json`，启动 `service.py`：
  - 没有 ready 握手输出
  - 进程退出码 1，stderr `TokenLens analytics service could not start`
- **预期**：损坏的旧文件应被忽略并继续启动（`Config.load` 与 `pricing.load_custom_prices` 都是这样做的）
- **实际**：应用完全不可用，用户只能手动删文件
- **影响**：一次异常断电/手工编辑即可让桌面端永久无法启动
- **修复建议**：捕获 `OSError/JSONDecodeError/ValueError/TypeError`，记录警告并写入迁移标记；迁移失败不得阻塞启动
- **验证**：已复现

---

### L-01 [Low] FTS "已就绪"标记从不校验，搜索会静默漏行

- **位置**：`backend/tokenlens/store.py:414-427`、`:439-450`
- **问题**：`_setup_fts()` 只要看到 `kv.fts_ready='1'` 就直接信任；若期间某次运行 FTS5 不可用（旧 SQLite 构建/瞬时失败），写入会跳过索引维护而标记仍然保留；下次在可用构建上启动也不会重建。
- **复现**：
  1. 正常写入 `model-1`，关闭
  2. 模拟 `fts_supported=False` 打开并写入 `model-2`，关闭（标记仍为 1）
  3. 正常打开：`records_fts MATCH "model-2"` = 0，`LIKE '%model-2%'` = 1；`model-1` 的旧索引仍指向已删除行
- **预期**：启动时校验/重建索引，或 FTS 不可用时清除标记
- **实际**：搜索走 FTS 时静默返回"无结果"
- **影响**：升级 SQLite、索引文件损坏后搜索结果缺失
- **修复建议**：FTS 不可用时删除标记；标记里记录索引版本/行数并在启动时校验，不一致即重建
- **验证**：已复现

---

### L-02 [Low] 纯数字日期 `20260102` 被当作 epoch 秒，落到 1970 年

- **位置**：`backend/tokenlens/models.py:51-52`
- **问题**：`if s.isdigit(): return to_epoch(int(s))` 在 ISO 解析之前执行；`YYYYMMDD` 只有 8 位，低于毫秒降级阈值，被原样当成秒。
- **复现**：`to_epoch("20260102") == 20260102` → `day_of` = `1970-08-23`；`to_epoch(20260102)` 相同。导入校验只要求 `ts > 0`，因此可以成功入库。
- **预期**：识别为 2026-01-02
- **实际**：1970-08-23，数据在仪表盘中几乎不可见
- **影响**：CSV/API 导入中常见的 `date=20260102` 列全部错位
- **修复建议**：在 epoch 分支前识别 `^\d{8}$` / `^\d{14}$`，或要求 epoch 秒至少 10 位
- **验证**：已复现

---

### L-03 [Low] Kimi 默认模型 `kimi-for-coding` 不在价格表，费用恒为 0

- **位置**：`backend/tokenlens/adapters.py:1804-1805`、`pricing.py:219-247`
- **问题**：无 model 字段的 Kimi wire 记录默认使用 `kimi-for-coding`，但 `pricing.is_known()` 只做精确 ID/别名/日期后缀匹配，该名称未收录，`estimate_cost` 返回 0。
- **复现**：`is_known("kimi-for-coding") == False`；构造一条无 model 的 wire 记录，得到 `cost=0`、ledger 显示未定价
- **预期**：命中 `kimi` 价格
- **实际**：全部无 model 的 Kimi 用量显示 $0
- **影响**：Kimi 成本长期缺失
- **修复建议**：把 `kimi-for-coding` 加入价格表/别名，或在适配器里把默认模型归一为 `kimi`
- **验证**：已复现

---

### L-04 [Low] Codex `total_token_usage` 回弹时按差值计数，分项错误

- **位置**：`backend/tokenlens/adapters.py:760-773`
- **问题**：当累计值先下降再回升时（压缩/重启），代码只看"当前是否逐项 ≥ 上一次"，回升后会按 `current - previous` 计数，忽略同时给出的 `last_token_usage`。
- **复现**：构造 totals `164,992 → 142,905 → 195,326`，第三次 `last={input:30,000, output:334}`：
  - 预期：30,000 / 334
  - 实际：52,421 / 0
- **预期**：当差值与 `last` 不一致时以 `last` 为准
- **实际**：分项（未缓存/缓存/输出）错位，总量大致接近
- **影响**：逐日/逐会话分项不准
- **修复建议**：比较 `delta` 与 `last`，不一致时把本次视为新段并使用 `last`
- **验证**：已复现

---

### L-05 [Low] 数据源盘符离线被当作"文件删除"，历史被清理

- **位置**：`backend/tokenlens/scanner.py:183-204`、`:392-393`
- **问题**：`enumerate()` 对不可访问的根目录返回空，`os.path.exists(已索引路径)` 为 False，`os.stat` 抛 `FileNotFoundError` 后 `continue`；`complete` 仍为真，随后 `drop_missing_files_for_tool` 删除该来源的全部记录。
- **复现**（代码路径）：索引 `Z:\logs` 下文件 → 断开 Z: → 扫描：文件被判定删除，记录被清理；重新挂载并扫描后才会恢复
- **预期**：源根不可用时应保留历史并报告来源错误
- **实际**：临时离线被当作永久删除
- **影响**：移动硬盘/网络盘用户的统计数据会突然消失
- **修复建议**：枚举前校验源根/卷是否可用（如 `os.path.isdir(root)` 失败则将该来源标记为 incomplete，跳过 prune）
- **验证**：代码路径确认（未用真实可移动盘复现）

---

### L-06 [Low] Claude Desktop 发布阶段的取消被吞，扫描状态显示 done

- **位置**：`backend/tokenlens/scanner.py:371-381`
- **问题**：dependent publication 的 `except Exception` 会捕获 `_ScanCancelled` / `InterruptedError` / `StoreWriteCancelled`，转成"dependent source update failed"，随后继续保存报告并把 `phase` 设为 `done`。
- **复现**（代码路径）：在 Claude Desktop JSONL plan 消费过程中取消 → `phase=done`、`cancel_requested=True`、records=0、error 含取消信息
- **预期**：`phase=cancelled`
- **实际**：界面显示扫描完成但实际未完成
- **影响**：取消语义不可靠
- **修复建议**：在通用 `except` 前重新抛出取消类异常，或在 handler 中检查 `st.cancel_requested`
- **验证**：代码路径确认

---

## 2.2 API 与服务

### M-12 [Medium] 短 API Key 会把响应 JSON 的键名替换成 `[redacted]`

- **位置**：`backend/tokenlens/credential_safety.py:53-55`（`sanitize_public` 对 dict key 也做替换）、`backend/tokenlens/server.py:575`
- **问题**：`sanitize_public` 递归处理 dict 时对**键名**执行同样的字符串替换。请求体里的 `api_key` 会被注册为响应敏感词；如果 Key 是很短的字符串（如 `k`、`ok`、`model` 的子串），响应中的结构键会被改写。
- **复现**：`POST /api/prompt/save`，body `{"model":"m","base_url":"http://127.0.0.1:1","api_key":"k"}`：
  - 实际响应：`{"o[redacted]": false, "error": "..."}`（`ok` 键被改坏）
- **预期**：只脱敏值，不改结构键名
- **实际**：前端拿不到 `ok` 字段，错误分支/成功分支判断失效
- **影响**：使用短 Key 时多个接口的响应契约被破坏；短 Key 也会误伤普通文本
- **修复建议**：不要对 dict key 做脱敏；对长度小于阈值的 secret 只做整值匹配
- **验证**：已复现

---

### M-13 [Medium] 请求体含孤立 UTF-16 代理项时连接被直接断开

- **位置**：`backend/tokenlens/credential_safety.py:16`（`secret.encode('utf-8')`）、`backend/tokenlens/server.py:575`
- **问题**：`json.loads` 会接受 `"\ud800"` 这样的孤立代理项；`_forms()` 在 `encode('utf-8')` 时抛 `UnicodeEncodeError`。`do_POST` 把它当 `ValueError` 捕获，但错误响应的 `json_out` 又用同一组 secret 脱敏，再次抛异常并逃逸到 `socketserver.handle_error`。
- **复现**：原始请求 `POST /api/prompt/save` body `{"model":"m","base_url":"...","api_key":"\\ud800"}`：
  - 实际：服务端日志 `UnicodeEncodeError`，客户端收到 `RemoteDisconnected`（零字节响应）
- **预期**：400 `invalid JSON`
- **实际**：连接无响应，可能污染日志/触发重试
- **影响**：未认证本地客户端可制造异常；错误的 Key 输入导致 UI 显示网络错误
- **修复建议**：输入阶段拒绝不可编码字符串；`_forms` 用 `errors='replace'` 或捕获 `UnicodeError`；`json_out` 使用 surrogate-safe 编码
- **验证**：已复现

---

### M-14 [Medium] 超大 JSON 整数传入 `/api/prices/set` 返回 500

- **位置**：`backend/tokenlens/server.py:1329-1334`
- **问题**：`set_custom_price` 内部 `float(v)` 对超大整数抛 `OverflowError`，而 handler 只捕获 `(ValueError, TypeError)`。
- **复现**：`POST /api/prices/set` body `{"model":"m","price":[1<400 个 0>,2,3,4]}` → `500 {"error":"local service request failed","code":"internal_error"}`
- **预期**：400 字段错误
- **实际**：500
- **影响**：前端显示"本地服务请求失败"，难以定位；日志噪声
- **修复建议**：捕获 `OverflowError` 映射为 400，或在 `validate_prices` 前做数值边界校验
- **验证**：已复现

---

### M-15 [Medium] `/api/reset` 与运行中的扫描竞争

- **位置**：`backend/tokenlens/server.py:1442-1444`
- **问题**：`/api/reset` 直接调用 `store.reset()`，既不检查 `scanner.state.running`，也不取消/等待扫描；正在解析的 worker 会在 reset 之后继续发布记录。相比之下 `/api/sources/import/commit` 会检查 `state.running`。
- **复现**（代码路径）：`POST /api/scan` 后立即 `POST /api/reset` → 索引被清空，随后扫描 worker 把部分数据写回，前端 `startScan(true)` 还可能因为扫描仍在运行而拿到 `started:false`
- **预期**：重置前取消并等待扫描，或直接拒绝
- **实际**：数据部分复活，重置结果不确定
- **影响**：用户执行"清空索引"后仍看到旧数据
- **修复建议**：获取 scanner 锁、取消并等待 `scanner.wait()`，或在 reset 期间拒绝并发写入
- **验证**：代码路径确认（时序类，未稳定复现）

---

### L-07 [Low] `/api/ledger` 的 offset 未做上限校验，溢出返回 500

- **位置**：`backend/tokenlens/server.py:908-917`
- **问题**：`offset=int(q.get('offset',0))` 只检查负数；`LIMIT ? OFFSET ?` 绑定超过 2^63 的整数时 sqlite3 抛 `OverflowError`，`do_GET` 未映射。
- **复现**：`GET /api/ledger?offset=99999999999999999999999` → 500 `internal_error`
- **预期**：400 或夹紧
- **实际**：500
- **影响**：手工 URL/脚本触发 500，日志噪声
- **修复建议**：与 `/api/records` 一样夹紧 offset
- **验证**：已复现

---

### L-08 [Low] 非 ASCII `X-Kjuan-Secret` 返回 500 而不是 403

- **位置**：`backend/service.py:38-44`
- **问题**：HTTP 头按 Latin-1 解码后，`secrets.compare_digest(str, str)` 遇到非 ASCII 抛 `TypeError`。
- **复现**：原始 socket 发送 `X-Kjuan-Secret: \xff\xfe` → `HTTP/1.1 500 Internal Server Error`，日志 `TypeError`
- **预期**：403
- **实际**：500
- **影响**：无鉴权绕过，但拒绝路径错误，可制造日志噪声
- **修复建议**：双方都编码为 bytes 后再 `compare_digest`，或捕获 `TypeError` 视为拒绝
- **验证**：已复现

---

### L-09 [Low] 未知 `source_id` 的扫描请求返回 `started: true`

- **位置**：`backend/tokenlens/server.py:1294-1305`、`backend/tokenlens/scanner.py:157-160`
- **问题**：来源合法性在后台线程里才校验，接口同步返回"已启动"。
- **复现**：`POST /api/scan {"source_id":"nope"}` → 200 `{"started":true}`；随后状态变为 `phase=error`，错误 `unknown or excluded source: nope`
- **预期**：同步 400 或 `started:false`
- **实际**：前端误以为已开始，随后才看到错误
- **影响**：UI 状态与操作反馈不一致
- **修复建议**：在 `Scanner.start` 内、启动线程前同步校验 `source_id`
- **验证**：已复现

---

### L-10 [Low] `project_paths` 过滤值被 4096 字符上限拒绝

- **位置**：`backend/tokenlens/server.py:62-72`
- **问题**：`request_filters` 对所有查询值统一限制 4096 字符，但 `Aggregator.filtered` 明确支持 100 条 × 1024 字符，助手快照路径也接受更大值。
- **复现**：100 条长路径 JSON 长度 9,200 → `GET /api/records?project_paths=...` 返回 400 `invalid query filter`
- **预期**：接受合法路径集合
- **实际**：管理了大量项目的用户无法下钻/导出
- **影响**：多项目用户功能受限
- **修复建议**：为 `project_paths` 单独放宽上限并交由 `Aggregator` 校验
- **验证**：已复现

---

### L-11 [Low] Markdown 报告丢失 `tool` 过滤标注

- **位置**：`backend/tokenlens/server.py:1048`
- **问题**：导出时 `report.render(agg, report_range, lang, cfg=app.cfg)` 没有传 `tool=`，虽然 `agg` 已按工具过滤；`report.py` 只有在收到 `tool` 时才输出"数据来源：X"的范围声明。
- **复现**：`GET /api/export?fmt=md&tool=claude-code`：
  - 无 `tool` 参数时报告缺少 `*数据来源：...*` 行（汇总句仍会提到最大来源）
- **预期**：报告明确声明筛选范围
- **实际**：共享报告无法判断是否全量
- **影响**：报告可被误读为全量数据
- **修复建议**：传入 `tool=filters.get("tool")`
- **验证**：已复现

---

### L-12 [Low] 仅用 `to` 收窄 `range=all` 时报告期间标注错误

- **位置**：`backend/tokenlens/server.py:88-97`
- **问题**：`range=all` 时 `start=None`，即使 `to` 已经限定了数据上界，函数直接返回 `rng="all"`，报告标题仍是"全部时间 / — → today"。
- **复现**（代码路径）：`GET /api/export?fmt=md&range=all&to=2026-11-07` → 数据只到 11-07，标题写"全部时间"
- **预期**：标题反映有效区间
- **实际**：标注与数据不一致
- **影响**：报告可信度
- **修复建议**：仅有 `to` 时推导 `最早数据日 → to` 的区间标签
- **验证**：代码路径确认

---

### L-13 [Low] `db_path(cfg)` 忽略传入的 cfg

- **位置**：`backend/tokenlens/config.py:281-282`、`backend/tokenlens/server.py:144`
- **问题**：`db_path` 始终返回 `data_dir()/index.db`，与 `cfg.path` 无关。用自定义配置路径做隔离的调用方仍会读写全局数据库。
- **复现**（代码审查）：`App(Config(path=temp))` 仍打开 `TOKENLENS_HOME/index.db`
- **预期**：数据库路径跟随配置/隔离目录
- **实际**：跨实例共享同一库
- **影响**：测试隔离、便携模式、多配置场景存在数据串用风险
- **修复建议**：由 `cfg` 推导数据库目录，或删除未使用参数并明确契约
- **验证**：代码审查确认

---

## 2.3 AI 助手 / PromptLab / 价格

### H-05 [High] PromptLab 保存永久失败（协议指纹不一致）

- **位置**：`backend/tokenlens/promptlab.py:2470-2478`（`save` 强制 `protocol="auto"`）、`backend/tokenlens/server.py:1348-1356`（检测登记使用解析后的协议）、`promptlab.py:2011-2024`
- **问题**：`/api/prompt/check` 通过 `ai_connection()` 解析出真实协议（`openai`/`anthropic`/`gemini`/`responses`）后用该协议调用 `remember_verified()`；`save()` 却把 `protocol` 强制改为 `"auto"` 再计算指纹。两者哈希永不相等，保存永远返回"请先通过可用性检测"。
- **复现**：
  ```python
  fp("gpt-4o", url, key, "auto")   # fafe753f...
  fp("gpt-4o", url, key, "openai") # 13f9c12e...
  remember_verified(..., "openai")
  save(..., protocol="auto")       # {"ok": False, "error": "请先通过「可用性检测」再保存（检测结果 15 分钟内有效）"}
  ```
- **预期**：检测通过后可以保存/更新配置
- **实际**：Prompt 页的所有保存路径（新建、另存为、更新）全部不可用
- **影响**：Prompt 优化实验室核心功能失效
- **修复建议**：`save()` 与 `check()` 使用同一解析后的协议计算指纹（或在 `check` 时同时登记 `"auto"` 别名）
- **验证**：已复现

---

### H-06 [High] AI 助手把本地绝对项目路径发送给外部模型

- **位置**：`backend/tokenlens/assistant_analytics.py:84-85, 308-330`、`backend/tokenlens/assistant_service.py:421-429`
- **问题**：`build_snapshot` 保留 `project_paths` 过滤值；`model_context()` 的文档字符串声称"Do not send ... source paths"，但它把 `filters` 原样放进模型上下文，evidence 的 `filter`/`previous_filter` 也包含同一字段。
- **复现**：
  ```python
  snapshot["filters"] = {"project_paths": '["C:/Users/alice/secret-client/project"]'}
  ctx = model_context(snapshot)
  "secret-client" in json.dumps(ctx)  # True
  ```
- **预期**：只发送聚合后的展示名/ID，不发送绝对路径
- **实际**：每次助手对话都会把完整路径发给用户配置的第三方端点
- **影响**：泄露用户名、客户名、目录结构等本地隐私，与"本地优先"的产品承诺冲突
- **修复建议**：在 `model_context` 前剥离 `project_paths` 与任何绝对路径；evidence 的 filter 用显示名替代
- **验证**：已复现

---

### M-16 [Medium] 价格核验把模型版本号当成价格证据

- **位置**：`backend/tokenlens/assistant_pricing.py:479-482`
- **问题**：`_has_number` 的正则把 `Claude Sonnet 4.5` 中的 `4.5` 当作独立数值；`_validate` 用它证明"原文确实写了这个单价"。
- **复现**：`_has_number("Claude Sonnet 4.5 pricing: $3 input / $15 output", 4.5) == True`。构造候选 `new_prices={input:4.5,...}`、引用同一句，可全部通过校验并发布。
- **预期**：价格数字必须出现在货币/单位/价格上下文中
- **实际**：与模型版本号相同的价格可以凭空通过核验
- **影响**：错误单价进入账本，成本/预算/报表被污染，且 UI 标注"来源已核验"
- **修复建议**：先剔除模型 ID/版本片段，或要求数字邻近 `$`/`USD`/`per million` 等价格语境
- **验证**：已复现

---

### M-17 [Medium] 可用性检测的 8-token 上限会让推理模型误判失败

- **位置**：`backend/tokenlens/promptlab.py:2369-2396`（探针参数）、`promptlab.py:2210-2227`（结果处理）
- **问题**：`AVAILABILITY_MAX_TOKENS = 8`，而 o 系列/gpt-5/Gemini 2.5 Pro 的隐藏推理会先消耗掉这 8 个 token，流以 `finish_reason=length` 结束；`_stream_terminal` 将其映射为 `truncated`，`check()` 在"用 reasoning 兜底"之前就直接返回错误。
- **复现**（代码路径）：配置 `o3`/`gpt-5`/`gemini-2.5-pro`（且 `/models` 不可用）→ 自检返回 `bad_request`/"请求被拒绝" → `ai_settings.save` 因检查未通过而拒绝保存
- **预期**：HTTP 200 且已产生推理输出应视为连接可用
- **实际**：合法推理模型无法保存
- **影响**：新模型用户无法完成 AI 设置
- **修复建议**：对强制推理模型不设 8-token 上限；把 `truncated` 且收到 reasoning 的情况判为可用；调整错误返回与 reasoning 兜底的顺序
- **验证**：代码路径确认

---

### M-18 [Medium] Prompt 优化对 OpenAI 推理模型发送 `max_tokens`/`temperature` 会被拒绝

- **位置**：`backend/tokenlens/promptlab.py:3164-3168`
- **问题**：OpenAI 推理模型要求 `max_completion_tokens` 且不支持自定义 `temperature`；同一代码库的 `assistant_service.py:190-192` 已经做了正确分支，`promptlab` 没有同步。
- **复现**（代码路径）：连接 `o3`/`gpt-5`，调用 `/api/prompt/optimize` → 上游 400 `Unsupported parameter: 'max_tokens'`
- **预期**：按模型族选择字段并省略 temperature
- **实际**：优化请求被拒
- **影响**：主流 OpenAI 模型下 Prompt 优化不可用
- **修复建议**：复用 `AssistantService.generate` 的字段选择逻辑
- **验证**：代码路径确认

---

### M-19 [Medium] `/models` 返回任意 2xx 就被判定"连接可用"

- **位置**：`backend/tokenlens/promptlab.py:850-874`、`promptlab.py:2095-2113`
- **问题**：`models_probe` 先设置 `out['ok']=True`，随后 JSON 解析失败也只是 `count=0`；`check()` 在 `ids` 为空时直接返回 `ok`，从未真正调用推理接口。
- **复现**（代码路径）：把 base_url 指向任何对 `/v1/models` 返回 200 HTML 的站点 → `/api/prompt/check` 返回可用并写入 15 分钟验证缓存 → 首次生成才失败
- **预期**：必须解析出模型目录，或回退到流式探针
- **实际**：状态码即结论
- **影响**：误报"已验证"，用户保存不可用连接
- **修复建议**：解析失败/空目录视为未验证，继续走流式探针
- **验证**：代码路径确认

---

### M-20 [Medium] `fill`/`confirm` 可覆盖较新的手动价格

- **位置**：`backend/tokenlens/assistant_pricing.py:1330-1347`、`:1375-1382`、`:1451-1464`、`:1527-1532`
- **问题**：`apply_saved` 会检查"当前价格来源更新"并跳过，但 `fill` 的 reapply 与 `confirm` 的 `estimate_ids` 发布路径没有同样的保护；`_publish_estimate` 只按 `source` 去重，手动版本源不同，会被新的 reference 版本以 `effective_from=today` 覆盖。
- **复现**（代码路径）：助手发布参考价 → 用户手动设置同模型价格 → 再次运行全模型自动定价或"重新应用已确认价格" → `effective_prices()` 返回参考价而非手动价
- **预期**：用户手动价格优先
- **实际**：手动价格被静默回退
- **影响**：成本计算被外部目录覆盖
- **修复建议**：在 `fill`/`confirm` 发布前复用 `apply_saved` 的 `newer-price` 守卫
- **验证**：代码路径确认（未端到端复现）

---

### M-21 [Medium] `effective_to` 被校验和存储，但从未生效

- **位置**：`backend/tokenlens/assistant_pricing.py:770-785`、`finance.py:116-118, 177-199`
- **问题**：候选可带来源声明的结束日期，`confirm` 也会检查本地日期，但 `_publish_estimate` 不传该字段；`price_versions` 表没有结束日期列，`_costs`/`effective_prices` 只判断 `effective_from <= at`。
- **复现**（代码路径）：确认一个"仅有效到明天"的促销价 → 之后所有日期继续使用该价格
- **预期**：过期后停止使用或要求重新确认
- **实际**：永久生效
- **影响**：临时价格被无限期套用
- **修复建议**：为价格版本持久化结束日期，并在所有定价查询中强制 `effective_from <= at <= effective_to`
- **验证**：代码路径确认

---

### L-14 [Low] Prompt 优化缓存淘汰存在竞态，可能丢弃已完成结果

- **位置**：`backend/tokenlens/promptlab.py:3350-3352`、`:3429-3431`
- **问题**：`_opt_cache` 无锁读写；两个并发任务同时判断 `len > 32` 并 `pop(next(iter(...)))`，后一个会抛 `KeyError`。该异常发生在上游生成成功之后、`result` 事件发出之前。
- **复现**（代码路径）：完成 33 次优化后并发发起两个不同 prompt 的流式优化 → 其中一个可能返回 "AI task failed (KeyError)"
- **预期**：淘汰原子化
- **实际**：已付费结果被丢弃
- **影响**：偶发任务失败
- **修复建议**：用锁保护缓存并用 `popitem(last=False)` 原子淘汰
- **验证**：代码路径确认（并发时序）

---

### L-15 [Low] 对冲探针字典在遍历时被其他线程插入，可能抛 RuntimeError

- **位置**：`backend/tokenlens/promptlab.py:2252-2274`、`:2285-2298`
- **问题**：`prog`/`box` 是普通 dict，探针线程写 `prog[idx]=p`、`box[idx]=response`，主线程同时 `sorted(prog)`/`sorted(box)` 并读取 `len(box)`；迭代期间插入键会抛 `RuntimeError: dictionary changed size during iteration`。
- **复现**（代码路径）：启用 `ai_hedge_enabled`，让首个探针变慢以触发对冲
- **预期**：线程安全
- **实际**：偶发 500
- **影响**：可用性检测偶发失败
- **修复建议**：预分配定长列表或加锁
- **验证**：代码路径确认

---

## 2.4 前端与桌面宿主

### M-22 [Medium] 扫描请求的 `started:false` 被丢弃，强制重扫可能静默不执行

- **位置**：`frontend/app.js:5405-5421`（`startScan`）
- **问题**：`await api('/api/scan', ...)` 的返回值从未检查。后端在已有扫描运行时返回 HTTP 200 + `started:false`，前端仍设置 `S.scanRunning=true` 并轮询**正在运行的旧扫描**。
- **复现**（代码路径）：按 R 启动增量扫描，在进度条走动时点击"设置 → 强制全量重扫"或某个来源卡片的"重扫此来源" → 强制/单源扫描从未开始，界面最终显示"扫描完成"
- **预期**：应用请求或提示"已有扫描在进行"
- **实际**：静默忽略
- **影响**：用户以为数据已强制刷新，实际仍是旧索引；`/api/reset` 后接 `startScan(true)` 的组合还可能出现"索引已清空"但未重建
- **修复建议**：检查 `res.started`，为 false 时提示并中止；reset 前先取消/等待扫描
- **验证**：代码审查确认

---

### M-23 [Medium] 会话详情抽屉没有请求序号，慢响应会覆盖后点击的会话

- **位置**：`frontend/app.js:2872-2897`（`openSession`）、`:2907`（`renderSessionDrawer`）
- **问题**：`openSession` 没有 AbortController 或请求序号；先点大会话 A（查询慢）、再点小会话 B，A 的响应晚到后会覆盖 B 的内容；关闭抽屉也不会使在途请求失效。
- **复现**（代码路径）：在"按会话"列表中快速先后点击两个会话
- **预期**：显示最后点击的会话
- **实际**：显示先点击的会话
- **影响**：用户看到错误会话的轮次与费用
- **修复建议**：为 `openSession` 增加递增 token/AbortController，响应回来时校验仍是最新请求
- **验证**：代码审查确认

---

### M-24 [Medium] 首次 dashboard 请求失败后永久停留在骨架屏

- **位置**：`frontend/app.js:4949-4955`（错误只 toast）、`:6057`（`if (!S.data) showSkeleton()`）
- **问题**：加载失败只弹 3 秒 toast，不渲染错误态/重试按钮；`S.data` 仍为 null，后续渲染逻辑不会替换骨架屏。
- **复现**（代码路径）：启动瞬间 sidecar 尚未恢复/返回 500 → KPI 区保持骨架、图表空白，只有一次 toast
- **预期**：显示错误状态与"重试"
- **实际**：主界面不可用，直到切换范围/重启 sidecar
- **影响**：一次瞬时故障导致首页长期空白
- **修复建议**：失败时在 `#kpis` 渲染错误卡片 + 重试按钮，并保留自动重试
- **验证**：代码审查确认

---

### M-25 [Medium] 计费明细加载失败后不标记渲染状态，页面永久空白

- **位置**：`frontend/app.js:1687-1692`、`:4755`
- **问题**：`S.rendered.ledger` 从未被赋值（全文件只有 `delete S.rendered.ledger`），失败分支删除一个不存在的键；而 Models 视图在 `renderView('models')` 返回后就被标记为已渲染，离开再进入不会重新请求。
- **复现**（代码路径）：进入"按模型"，`/api/ledger` 失败一次 → 表格保持空元素，只有 toast；切换视图再回来仍空白
- **预期**：显示错误行与重试
- **实际**：计费明细区一直为空
- **影响**：用户以为该功能不存在
- **修复建议**：失败时 `delete S.rendered.models`（或引入 ledger 独立 epoch），并渲染错误行
- **验证**：代码审查确认（`S.rendered.ledger` 无赋值点已核实）

---

### L-16 [Low] i18n 会把数据中的"模型/项目/成本"等词当 UI 文案翻译

- **位置**：`frontend/i18n.js:682-729`
- **问题**：`apply()` 按精确文本匹配翻译所有文本节点与 `title`；`DICT` 含 `模型/项目/成本/合计/全部` 等通用词，`EXCLUDED` 未覆盖数据单元格。
- **复现**（代码路径）：项目目录名为 `模型` 或 `成本`，切换到英文界面 → 项目名显示为 "Model"/"Cost"
- **预期**：数据原样显示
- **实际**：数据被翻译
- **影响**：英文界面下名称错乱，降低数据可信度
- **修复建议**：为数据节点加 `data-no-translate`，或只翻译带 `data-i18n` 的静态文案
- **验证**：代码审查确认（字典与排除列表已核实）

---

### L-17 [Low] 上游错误文本未转义进入 `innerHTML`

- **位置**：`frontend/app.js:3916-3917`（以及 `:3891-3893`）
- **问题**：`r.attempts.map((a) => \`${esc(a.variant)} → ${a.status || a.error || ...}\`)`，`a.error` 来自上游 HTTP/异常文本，直接拼进 `#plResult.innerHTML`；`protoLabel` 也可能回退到原始协议字符串。
- **复现**（代码路径）：把 AI 端点指向可控服务器，让其错误体包含 `<img src=x onerror=...>` → 内容被当作 HTML 解析
- **预期**：纯文本展示
- **实际**：HTML/UI 注入（当前 CSP 阻止脚本执行，但可伪造界面）
- **影响**：钓鱼/界面欺骗；若 CSP 放宽会升级为 XSS
- **修复建议**：对 `a.status`/`a.error`/协议回退统一 `esc()`，或使用 `textContent`
- **验证**：代码审查确认

---

### L-18 [Low] 数据源页"未安装"汇总卡插入到卡片网格中间

- **位置**：`frontend/app.js:2387-2409`、`:4765-4785`
- **问题**：`chunkMount` 分批异步渲染并先清空容器；`renderSources` 在其返回后立即 `host.appendChild(box)`，此时只挂载了第一批卡片。
- **复现**（代码路径）：打开数据源页（已安装来源较多）→ 汇总卡出现在第二张卡片位置，其余卡片在其下方
- **预期**：汇总卡位于所有来源卡之后
- **实际**：阅读顺序错乱
- **影响**：视觉/信息架构问题
- **修复建议**：把汇总卡放进 `chunkMount` 的 `onDone` 回调，或使用独立容器
- **验证**：代码审查确认

---

### L-19 [Low] SVG 预览 iframe 的 `allow-scripts` 实际无法执行脚本

- **位置**：`src-tauri/tauri.conf.json:24`（`script-src 'self'`）、`frontend/index.html:368`（`sandbox="allow-scripts"`）、`frontend/app.js:3341`（`srcdoc`）
- **问题**：`srcdoc` 文档继承父页面 CSP；在 `script-src 'self'` 下，内联 `<script>` 被拒绝，而 sandbox 的 opaque origin 又使 `'self'` 匹配不到任何资源。
- **复现**（代码路径）：让模型返回带 `<script>requestAnimationFrame(...)</script>` 的 SVG 动画 → 预览为静态帧，判定仍是"已生成"
- **预期**：隔离框架内脚本可运行（`allow-scripts` 的意图）
- **实际**：脚本全部被 CSP 阻止且无提示
- **影响**：模型质量判定失真，预览与输出不一致
- **修复建议**：为预览文档提供独立 CSP（如 blob URL + nonce），或在判定中检测脚本被阻止并明确说明仅支持声明式动画
- **验证**：代码审查确认

---

### L-20 [Low] GET `/api/backup/export` 可绕过保存对话框

- **位置**：`src-tauri/src/service.rs:15`（`GET_PATHS` 含 `/api/backup/export`）、`frontend/native.js:39-116`（`fetch` → `service_request` / `service_download`）
- **问题**：`service_download` 会弹出保存对话框并限制导出；但 `service_request` 走同一 GET 白名单，任何页面脚本都可以 `fetch('/api/backup/export')` 把完整备份 ZIP 读进页面。
- **复现**（代码路径）：在 WebView 控制台执行 `await (await fetch('/api/backup/export')).text()` → 得到备份内容，无对话框
- **预期**：批量导出必须由用户手势选择保存位置
- **实际**：任意脚本可读取全量数据
- **影响**：防御纵深不足；一旦出现 XSS 即可全量外泄（API Key 除外）
- **修复建议**：把批量导出端点从 `GET_PATHS` 移除，只保留 `service_download` 通道
- **验证**：代码路径确认

---

## 2.5 已修复（本次会话）

### F-01 [Fixed] Codex `.jsonl.zst` 与 `archived_sessions` 全量漏扫

- **现象**：用户反馈"我的世界"项目在 Codex 中的记录消失。实际 `~/.codex/sessions` 下 734 个 rollout 全部是 `.jsonl.zst`，另有 16 个在 `archived_sessions/`；旧 `enumerate()` 只 glob `rollout-*.jsonl`，一个都匹配不到。
- **修复**：
  - `iter_lines()` 按扩展名透明解压 `.zst/.zstd`（`iter_lines` → `_open_text_source`）
  - `CodexAdapter.enumerate()` 同时扫描 `sessions/**` 与 `archived_sessions/**` 的 `.jsonl/.jsonl.zst/.jsonl.zstd`
  - 支持 `CODEX_HOME`
  - 会话 ID 从文件名推导时剥离 `.zst/.zstd`
  - 移除 `scan_extra()` 中对不存在方法 `_quick_from_db` 的调用
- **验证**：
  - 新增 3 个单元测试，仓库 101 个测试全部通过
  - 真实数据全量扫描：**754 个文件、52,293 条记录、8,260,043,507 token、0 错误、quality=exact**
  - "我的世界"项目成为 token 排行第一：**2,127,468,779 token / 10,126 条 / 80 个会话**（2026-07-13 ~ 2026-10-08）
  - 单会话与 Codex 自身 `threads.tokens_used` 误差 0.08%（去重口径差异）
- **文档/测试**：`backend/tokenlens/SOURCES.md` 增加 Codex 数据源说明；`backend/tests/test_adapter_codex.py` 增加压缩会话/归档目录/会话 ID 测试

---

# 3. 未复现 / 待确认项

以下问题有代码依据但未做稳定端到端复现，列出以便后续确认：

1. **`/api/reset` 与扫描竞争**（M-15）：需要精确时序；代码上确实没有同步。
2. **可移动盘离线导致历史清理**（L-05）：需要真实卷挂载/卸载；代码路径成立。
3. **`fill`/`confirm` 覆盖手动价格**（M-20）：需要完整助手定价流程；`apply_saved` 有守卫而另外两条路径没有。
4. **`effective_to` 过期未生效**（M-21）：表结构确实没有结束日期列。
5. **对冲探针并发 RuntimeError**（L-15）：依赖 `ai_hedge_enabled` 与线程时序。
6. **优化缓存并发 KeyError**（L-14）：依赖恰好 32 条缓存与并发请求。

---

# 4. 验证通过（负面结果）

以下关键安全/正确性路径经审查或测试未发现问题：

- **鉴权与来源校验**：`X-Kjuan-Secret` 常量时间比较（ASCII 输入）、`Host` 仅允许 localhost/127.0.0.1/::1 且端口必须匹配、`Origin`/`Sec-Fetch-Site` 拒绝、POST 需要 `X-TokenLens: 1`。
- **路径/命令注入**：Rust `service_request` 只允许固定 `GET_PATHS`/`POST_PATHS`，拒绝 `\`、`#`、控制字符与重定向；`open_external` 只允许无凭据的 http(s)；`pick-folder`/`sources/reveal` 不把用户路径直接拼命令。
- **增量 JSONL**：写入前对已保存前缀做 SHA-256 校验，校验失败自动全量重解析；发布使用暂存库 + 单事务，取消可回滚；游标只在记录、文件状态、FTS 同事务提交后才持久化。
- **导入安全**：ZIP 条目白名单、大小/解压上限、SHA-256 完整性、拒绝符号链接/加密条目；CSV 公式注入已在导出侧加 `'` 前缀。
- **日志驱动的 XSS**：项目/会话/模型/工具名在主要列表与抽屉中均经过 `esc()`/`textContent`（仅上游 AI 错误文本一处遗漏，见 L-17）。
- **价格表缓存桶**：抽查模型四元组未发现缓存读/写价格倒置。
- **Codex `token_usage_record` 账本**：`input - cached` 与最终累计总量一致；ZCode 的 cache-read 归一化与 reasoning-in-output 处理正确。
- **既有测试**：101 个单元测试全部通过，覆盖账务修复、Codex/Gemini 适配器、定价、模型校验、服务冒烟。

---

# 5. 修复优先级建议

| 优先级 | 问题 | 理由 |
| --- | --- | --- |
| P0（本周） | H-01 Cline 双计、H-02 Gemini 归零、H-03 Codex 模式叠加 | 直接污染核心 token/费用统计，影响所有对应来源用户 |
| P0 | H-04 备份恢复失败、H-05 PromptLab 保存失效 | 核心功能不可用/灾难恢复失效 |
| P1 | H-06 路径外泄、M-11 启动失败、M-12/M-13 API 异常 | 隐私与可用性 |
| P1 | M-01 成本快照、M-02 Kimi、M-05/M-06 分组不一致、M-09 零成本 | 统计正确性 |
| P2 | M-10 报告预算、M-14~M-25 及其余 Low | 体验与边界场景 |

建议为每个修复补一条最小回归测试；本次审计使用的最小夹具可直接沉淀到 `backend/tests/`。

---

# 6. 附：审计覆盖范围

| 模块 | 审查方式 | 主要发现 |
| --- | --- | --- |
| `adapters.py`（30+ 来源） | 代码审查 + 真实样本 | H-01/H-02、M-02~M-09、L-02~L-04 |
| `scanner.py` / `incremental_logs.py` | 代码审查 + 夹具 | H-03、L-05、L-06 |
| `store.py` | 代码审查 + 夹具 | H-04、M-01、L-01 |
| `aggregator.py` | 代码审查 + 夹具 | M-05、M-06、M-10 |
| `server.py` / `service.py` | 代码审查 + 启动真实 sidecar | M-10~M-15、L-07~L-13 |
| `backup_service.py` / `source_import.py` | 代码审查 + 端到端 | H-04、M-04 |
| `promptlab.py` / `assistant_*` / 定价 | 代码审查 + 函数级复现 | H-05、H-06、M-16~M-21、L-14~L-15 |
| `frontend/*.js` | 代码审查 | M-22~M-25、L-16~L-19 |
| `src-tauri/*.rs` | 代码审查 | L-20（其余鉴权/监管路径验证通过） |
| 测试套件 | 全量执行 | 101 通过 |

---

*报告生成于 2026-11-12，基于提交 `fbbde08`（1.2.6）。*

---

# 7. 修复状态（2026-11-12）

本报告中的问题已全部在源码中修复，并补充了回归测试；仓库测试从 101 项增加到 139 项，全部通过。
新增测试文件：`backend/tests/test_audit_fixes.py`、`backend/tests/test_server_fixes.py`、`backend/tests/test_assistant_fixes.py`；`backend/tests/test_adapter_gemini.py` 按“用量仅追加”语义更新。

| 编号 | 状态 | 关键修复 |
| --- | --- | --- |
| H-01 | 已修复 | Cline 同时扣除 cacheWrites：`input = max(0, tokensIn - cacheReads - cacheWrites)`；真实任务由 133,310 修正为 94,399 |
| H-02 | 已修复 | Gemini 用量改为仅追加账本，`$set.messages`/`$rewindTo`/`$patch.removeIds` 只清显示状态；真实会话由 0 条恢复为 2 条 |
| H-03 | 已修复 | 新增 `Scanner._reconcile_codex_mode()`：quick/full 切换时 `purge_tool("codex")`，不再叠加 |
| H-04 | 已修复 | `backup_service._stage` 跳过 `sqlite_stat1/4`，自身导出可恢复（含回归测试） |
| H-05 | 已修复 | `PromptLab.save()` 与检测使用同一解析后协议计算指纹；用真实 API 验证保存成功 |
| H-06 | 已修复 | `assistant_analytics.model_context()` 递归剥离 `project_paths` 与证据筛选 |
| M-01 | 已修复 | 成本快照仅在新的 `estimated/unknown` 时继承；新 `actual/free` 生效 |
| M-02 | 已修复 | Kimi 无逐轮证据时不再把增长序列当累计镜像 |
| M-03 | 已修复 | Aider 使用标题日期时间，mtime 仅兜底 |
| M-04 | 已修复 | 导入 identity 纳入用量字段（ts/model/input/output/cache…），同 id 不同用量不再被丢弃；完全相同的行重复导入仍幂等（`tests/test_import_fixes.py` 3 项） |
| M-05 | 已修复 | 模型筛选/重算统一使用 `tokenlens_model_key` |
| M-06 | 已修复 | `session_detail` 解析 `(tool, project)` 身份；模型按归一化键分组 |
| M-07 | 已修复 | 通用数据库适配器排除 `cache*/cached*` 列 |
| M-08 | 已修复 | ApiLog 非 JSON 方括号行回退文本解析 |
| M-09 | 已修复 | OpenCode/Cline 的 `cost=0` 不再标记为 actual |
| M-10 | 已修复 | 报告在创建 TEMP VIEW 前用未投影库计算预算 |
| M-11 | 已修复 | 损坏的旧版价格文件不再阻塞服务启动 |
| M-12 | 已修复 | `sanitize_public` 不再脱敏字典键；短密钥只做整值匹配 |
| M-13 | 已修复 | 代理项安全编码，请求总能收到响应 |
| M-14 | 已修复 | 价格超大整数返回 400 而非 500 |
| M-15 | 已修复 | `/api/reset` 先取消并等待扫描，无法停止则拒绝 |
| M-16 | 已修复 | 价格核验剔除模型版本号后匹配数值 |
| M-17 | 已修复 | 推理模型探针上限 512，截断但有输出视为可用 |
| M-18 | 已修复 | OpenAI 推理模型改用 `max_completion_tokens` 并省略 temperature |
| M-19 | 已修复 | `/models` 需返回可解析目录，否则回退流式探针 |
| M-20 | 已修复 | `fill`/`confirm` 复用较新手动价格守卫 |
| M-21 | 已修复 | `price_versions.effective_to` 迁移、校验并在估算中生效 |
| M-22 | 已修复 | 前端识别 `started:false`，重置前取消扫描 |
| M-23 | 已修复 | 会话抽屉增加请求序号，丢弃过期响应 |
| M-24 | 已修复 | 仪表盘首屏失败显示错误卡片与重试 |
| M-25 | 已修复 | 计费明细失败标记视图未完成并显示重试行 |
| L-01~L-20 | 已修复 | FTS 标记、日历日期、Kimi 价格、Codex 回弹、卷离线、取消语义、offset、非 ASCII 鉴权头、未知来源、project_paths 上限、报告标注、db_path、缓存竞态、i18n、转义、卡片顺序、预览说明、导出白名单 |

## 7.1 本轮修复过程中新发现并修复的问题

| 编号 | 状态 | 说明 |
| --- | --- | --- |
| F-02 | 已修复 | **ZCode 大数据库被 400MB 上限整表跳过**：`~/.zcode/cli/db/db.sqlite` 已增长到 639MB，原扫描器按文本文件上限跳过，导致约 45.8 亿 token 完全缺失。现对声明 `scan_db` 的数据库来源放宽到 4096MB（SQLite 为有界查询，不载入内存）。 |
| F-03 | 已修复 | **活动数据库在解析中被写入导致整文件失败**：应用运行时会持续写入 ZCode/OpenCode 数据库，解析后指纹校验失败即整文件作废并保留旧快照。现对数据库来源容忍解析期间的变化（保留旧指纹，下次扫描重解析，事件 ID 去重保证幂等）。 |
| F-04 | 已修复 | 扫描器把 `unknown/excluded source` 的校验提前到 `start()`，接口不再返回假的 `started:true`。 |

## 7.2 端到端验证证据（2026-11-12）

- 单元/回归测试：139 项全部通过。
- 真实 API（用户提供端点 `https://api.relaycore.cn` / `deepseek-v4.1-flash-1017`）：可用性检测通过、配置保存成功、提示词优化成功、助手生成成功。
- 真实数据全量扫描（隔离库）：1,708 个文件、167,910 条记录、**0 错误**；Codex 8,256,829,233、ZCode 4,578,650,238、DeepSeek Harness 14,009,573,371、Cline 94,399、Gemini 29,186；「我的世界」2,127,468,779 / 10,126 条 / 80 会话。
- 已安装应用（1.2.7，真实数据目录 `%APPDATA%\com.kjuan.desktop`）强制全量重扫后：1.2.7 版本生效，ZCode 16,298 条 / 4,612,206,584 token、Cline 94,399、Codex 52,291 条 / 8,256,829,233、DeepSeek Harness 98,977 条，全部为 ok/aggregate，无错误。

## 7.3 未完成/受限项

- **M-04（导入 ID 跨文件碰撞）**：已修复。explicit id 现与用量字段组合成 identity：CSV/网关复用行号或请求 id 时，只要用量不同就会各保留一条；逐字段完全相同的行重复导入依旧判重，保持幂等（`backend/tests/test_import_fixes.py`）。
- **自动更新签名**：仓库中的 `.tokenlens-updater-key.key` 是加密私钥且密码未知，本次 1.2.7 构建以 `createUpdaterArtifacts:false` 生成安装包，未生成 `latest.json` 签名。应用内“检查更新”仍指向 GitHub 发布地址，需要发布时用原密码签名。
- **构建工具链**：本机没有 MSVC C++ 工具链（BuildTools 未装 VC 组件、无 Windows SDK），1.2.7 使用 MinGW-w64（GNU 目标）构建；由于项目路径与工具链路径含中文会导致 windres/link 失败，构建在纯 ASCII 路径（`D:\tl_src` + `D:\rustup`/`D:\cargo`/`D:\tl_target2`）下完成。
