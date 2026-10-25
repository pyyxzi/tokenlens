/* ==========================================================================
   TokenLens · 界面语言层（zh-CN / en-US）
   --------------------------------------------------------------------------
   为什么是「渲染后翻译」而不是在每个渲染点调 t()：

   这个前端有 300 多处把中文直接写进模板字符串，逐个改成 t('...') 意味着
   300 多次改动，而且以后每加一行界面文案都要记得再包一层——漏一个就是一句
   中文孤零零留在英文界面里。

   这里改成在**渲染完成后**对已挂载的 DOM 做精确匹配替换：
     · 渲染函数完全不用动，将来新写的界面文案只要加进字典就自动生效；
     · 中文模式下整层不挂监听、不进 TreeWalker，性能路径零改动；
     · 翻译是幂等的（英文里不含汉字，再翻一次自然是空操作），所以不需要
       "先回滚再翻译"那种每帧 O(n) 的开销；只有切回中文时才做一次全量回滚。
   ========================================================================== */
(() => {
  'use strict';

  /* ---------------- 静态词条：中文 → 英文 ----------------
     键必须是界面上**完整的一个文本节点**，不做子串替换——
     子串替换会把「模型」误伤成「Model」而破坏「模型调用次数」这类短语。 */
  const DICT = {
    '编辑方案': 'Edit filter',
    '用量趋势与本地分析建议': 'Usage trends and local analysis',
    '各编程工具的用量分布': 'Usage by coding tool',
    '模型排行与计费明细': 'Model ranking and pricing details',
    '项目用量、模型流水下钻': 'Project usage and record drill-down',
    '会话排行与调用详情': 'Session ranking and call details',
    '采集状态与来源信息': 'Collection status and source information',
    '项目归档、价格与自身 AI 消费': 'Project organization, pricing and App AI usage',
    '根据当前范围分析用量与费用': 'Analyze usage and cost in the selected range',
    '测试会请求你配置的 API，不使用工具或网页搜索。样例结果仅反映本次任务表现。': 'Tests call your configured API without tools or web search. Results describe this sample task only.',
    '该样例通过': 'Sample passed',
    '该样例未通过': 'Sample not passed',
    '答案未识别': 'Answer not recognized',
    '未通过': 'Not passed',
    '统计数据不上传': 'Usage statistics are not uploaded',
    '用量统计读取本机磁盘上的日志，': 'Usage statistics read logs on the local disk. ',
    '。可选的提示词优化与模型测试会向你配置的 API 发送所输入的内容；只有主动运行时才会请求。共内置': '. Optional prompt editing and model tests send entered content to your configured API only when you start them. Built-in adapters: ',
    /* ---- 概览 KPI 与卡片 ---- */
    '总 Token 消耗': 'Total tokens',
    '日均消耗': 'Daily average',
    '模型调用次数': 'Model calls',
    '估算成本': 'Estimated cost',
    '记录费用': 'Recorded cost',
    '精确': 'Exact',
    '次': 'calls',
    '用量趋势': 'Usage trend',
    '按天统计的 token 消耗': 'Token consumption by day',
    '工具占比': 'Tool share',
    'token 消耗分布': 'Token consumption distribution',
    '活跃热力图': 'Activity heatmap',
    '近一年每日用量强度': 'Daily usage intensity over the past year',
    '少': 'Less',
    '多': 'More',
    '工具排行': 'Tool ranking',
    '按 token 消耗排序': 'Sorted by token consumption',
    '模型排行': 'Model ranking',
    '最常用的模型': 'Most used models',
    '时段分布': 'Hourly distribution',
    '一天中的编程高峰': 'Peak hours of the day',
    '项目排行': 'Project ranking',
    '消耗最多的工程目录': 'Highest-consumption directories',
    '今日分时段用量': 'Today by time of day',
    '按小时统计的 token 消耗': 'Token consumption by hour',
    '按小时': 'Hourly',
    '按半小时': 'Half-hourly',
    '其他工具': 'Other tools',

    /* ---- 时间范围 ---- */
    '今天': 'Today',
    '7 天': '7 days',
    '14 天': '14 days',
    '30 天': '30 days',
    '90 天': '90 days',
    '一年': '1 year',
    '全部': 'All',
    '自定义': 'Custom',
    '自定义时间段': 'Custom date range',
    '本月': 'This month',
    '上月': 'Last month',
    '近 7 天': 'Last 7 days',
    '近 14 天': 'Last 14 days',
    '近 30 天': 'Last 30 days',
    '今年': 'This year',
    '开始日期': 'Start date',
    '结束日期': 'End date',
    '应用': 'Apply',

    /* ---- 趋势图指标 ---- */
    '总量': 'Total',
    '输入 / 输出': 'Input / Output',
    '缓存读写': 'Cache',
    '成本': 'Cost',
    '输入': 'Input',
    '输出': 'Output',
    '缓存读': 'Cache read',
    '缓存写': 'Cache write',
    '合计': 'Total',
    '调用': 'Calls',
    '调用次数': 'Calls',
    'Token 合计': 'Total tokens',
    'Token': 'Tokens',

    /* ---- 洞察 ---- */
    '周期对比': 'Period comparison',
    '本期 vs 上一等长周期': 'This period vs. the previous equal-length period',
    '预算使用': 'Budget usage',
    '可在设置中设定': 'Configure in Settings',
    '用量高峰': 'Usage peaks',
    '消耗最集中的时间段': 'Most intensive time periods',
    '缓存效率': 'Cache efficiency',
    '缓存命中带来的成本节省': 'Cost savings from cache hits',
    '来源': 'Source',
    '本期': 'Current',
    '上期': 'Previous',
    '变化': 'Change',
    '总计': 'Total',
    '新增': 'New',
    '缓存命中率': 'Cache hit rate',
    '缓存读 Token': 'Cache read tokens',
    '未缓存输入': 'Uncached input',
    '估算节省': 'Estimated savings',
    '整体估算成本': 'Overall estimated cost',
    '整体记录费用': 'Overall recorded cost',
    '已用': 'Used',
    '上限': 'Limit',
    '剩余': 'Remaining',
    '暂无数据': 'No data',
    '尚未设置预算': 'No budget set',
    '洞察': 'Insights',

    /* ---- 表格通用表头 ---- */
    '模型': 'Model',
    '项目': 'Project',
    '项目目录': 'Project directory',
    '工具': 'Tool',
    '会话': 'Session',
    '时间': 'Time',
    '来源工具': 'Source tools',
    '最近活跃': 'Last active',
    '起止': 'Start – End',
    '占比': 'Share',

    /* ---- 模型页 / 计费明细 ---- */
    '模型用量明细': 'Model usage detail',
    '含输入/输出/缓存拆分与估算成本': 'Input / output / cache breakdown with estimated cost',
    '含输入/输出/缓存拆分与记录费用': 'Input / output / cache breakdown with recorded cost',
    '估算成本 · 计费明细': 'Estimated cost · billing detail',
    '现价估算 · 计费明细': 'Current price estimate · billing detail',
    '按当前单价 × 用量逐项列出，历史金额与价格版本见成本管理': 'Current unit price × usage; see Cost management for recorded amounts and price versions',
    '按单价 × 用量逐项列出，可核对每一分钱花在哪':
      'Itemized by unit price × usage, so every cent can be verified',
    '未计价': 'Not priced',
    '小计': 'Subtotal',
    '单价 × 用量': 'Unit price × usage',

    /* ---- 项目页 ---- */
    '项目用量明细': 'Project usage detail',
    '按工作目录聚合': 'Aggregated by working directory',

    /* ---- 会话页 ---- */
    '会话排行': 'Session ranking',
    '单次会话消耗最高的记录': 'Sessions with the highest consumption',
    '会话详情': 'Session detail',

    /* ---- 数据源 ---- */
    '已完整采集': 'Fully collected',
    '部分采集': 'Partially collected',
    '已安装未落盘': 'Installed, no data',
    '未安装': 'Not installed',
    '已采集': 'Collected',
    '异常': 'Error',
    '未知': 'Unknown',
    '未知模型': 'Unknown model',
    '采集到 Token': 'Tokens collected',
    '记录条数': 'Records',
    '数据文件': 'Data files',
    '占用空间': 'Disk usage',
    '最近活动': 'Last activity',
    'Composer 会话': 'Composer sessions',
    'Tab 补全采纳行数': 'Tab completions accepted',
    'Chat 应用行数': 'Chat edits applied',
    '加密会话库': 'Encrypted session stores',
    '本地模型': 'Local models',
    '缓存文件': 'Cache files',
    '会话投影缓存': 'Session projections',
    'zstd 事件流': 'zstd event streams',

    /* ---- 明细流水 ---- */
    '用量流水': 'Usage records',
    '每一条模型调用记录': 'Every model call record',
    '按时间': 'By time',
    '按用量': 'By usage',
    '按成本': 'By cost',
    '全部工具': 'All tools',
    '时间筛选': 'Date filter',
    '按日期精修': 'Refine by date',
    '从': 'From',
    '到': 'To',
    '清除': 'Clear',
    '‹ 上一页': '‹ Previous',
    '下一页 ›': 'Next ›',

    /* ---- 侧栏 / 顶栏 ---- */
    '就绪': 'Ready',
    '重新扫描': 'Rescan',
    '取消扫描': 'Cancel scan',
    '关于': 'About',
    '导出': 'Export',
    '导出 Markdown 报告（周报可直接粘贴）': 'Export Markdown report (paste into a weekly update)',
    '导出 CSV（Excel 可直接打开）': 'Export CSV (opens in Excel)',
    '导出 JSON（含汇总与明细）': 'Export JSON (summary and records)',
    '复制汇总到剪贴板': 'Copy summary to clipboard',
    '正在导出 Markdown 报告…': 'Exporting Markdown report…',
    '正在导出 CSV…': 'Exporting CSV…',
    '正在导出 JSON…': 'Exporting JSON…',
    '搜索模型 / 项目 / 会话…': 'Search models, projects, sessions…',
    '流畅': 'Smooth',
    '一般': 'Fair',
    '卡顿': 'Lagging',
    '取消': 'Cancel',
    '退出程序': 'Quit',
    /* ---- 提示词优化 ---- */
    '提示词优化工作区': 'Prompt optimization workspace',
    '根据需求选择清晰润色或专业结构，输出可直接使用的提示词。':
      'Choose clarity editing or professional structure to produce a ready-to-use prompt.',
    '提示词优化方式': 'Optimization mode',
    '清晰润色': 'Clarity editing',
    '专业结构': 'Professional structure',
    '在不改变原意的前提下，改善措辞、语序与逻辑衔接':
      'Improve wording and logic while preserving intent',
    '补充角色、目标、内容范围、约束与输出要求':
      'Add role, goal, scope, constraints and output requirements',
    '原始提示词': 'Original prompt',
    '粘贴需要改写的内容。': 'Paste the prompt to rewrite.',
    '填入示例': 'Use example',
    '请先配置 AI 后开始优化': 'Configure AI before optimizing',
    '将按当前选择的优化方式处理': 'The selected optimization mode will be applied',
    '开始优化': 'Optimize',
    '优化提示词': 'Optimize prompt',
    '优化中…': 'Optimizing…',
    '正在优化': 'Optimizing',
    '已经收到的部分结果': 'Partial result received',
    '当前优化结果': 'Current result',
    '结果将显示在当前选择的优化方式下。':
      'The result follows the selected optimization mode.',
    '模型正在思考…': 'Model is reasoning…',
    '正在连接模型…': 'Connecting to model…',
    '实时思考过程': 'Live reasoning',
    '思考过程': 'Reasoning',
    '公开回答': 'Response',
    '复制优化结果': 'Copy result',
    '优化依据': 'Why this result',
    '本次应用的原则': 'Principles applied',
    '专业结构参考（OpenAI / Anthropic）': 'Professional structure references (OpenAI / Anthropic)',
    '查看': 'View',
    '收起': 'Collapse',
    '展开': 'Expand',
    '复制': 'Copy',
    '历史记录': 'History',
    '历史提示词': 'Prompt history',
    '清空历史': 'Clear history',
    '配置 AI': 'Configure AI',
    '可用性检测': 'Check availability',
    '测试连接': 'Test connection',
    '检测通过后即可保存': 'Available to save once the check passes',
    '保存并使用': 'Save & use',
    '另存为新配置': 'Save as new',
    '删除当前配置': 'Delete profile',
    '清除配置': 'Clear configuration',
    '配置名称': 'Profile name',
    '已保存的 AI 配置': 'Saved AI profiles',
    '新建配置': 'New profile',
    '快速选择': 'Quick start',
    '新建': 'New',
    '删除': 'Delete',
    '保存筛选': 'Save filter',
    '列显示': 'Columns',
    '筛选方案': 'Saved filters',
    '显示列': 'Visible columns',
    '模型': 'Model',
    '项目': 'Project',
    '输入': 'Input',
    '输出': 'Output',
    '缓存读': 'Cache read',
    '缓存写': 'Cache write',
    '合计': 'Total',
    '成本': 'Cost',
    '来源': 'Source',
    '手动填写配置': 'Enter manually',
    '自动识别': 'Detected',
    '优化失败': 'Optimization failed',
    '检测完成': 'Check complete',
    '检测中…': 'Checking…',
    '（可以保存）': ' (ready to save)',
    '检测请求失败': 'Check request failed',
    '正在快速确认模型能否返回内容…': 'Verifying the model can return content…',
    '等待模型响应…': 'Waiting for the model…',
    '响应耗时': 'Response time',
    '首个响应': 'First response',
    '总耗时': 'Total time',
    'HTTP 状态': 'HTTP status',
    '协议': 'Protocol',
    '返回模型': 'Returned model',
    '本次用量': 'Usage',
    '失败': 'failed',
    '请求参数自适应记录：': 'Request adaptation log: ',
    '该模型为推理型模型：已确认服务正常返回内容，判定为可用。':
      'This is a reasoning model. It returned content normally, so it is considered available.',
    '连接验证通过：服务地址可访问、API Key 有效。':
      'Connection verified: the endpoint is reachable and the API Key is valid.',
    '连接验证通过：服务地址可访问、API Key 有效、模型名称在服务列表中。':
      'Connection verified: the endpoint is reachable, the API Key is valid, and the model name appears in the service list.',
    '未配置': 'Not configured',
    '尚未连接 AI': 'No AI connected yet',

    /* ---- AI 检测 ---- */
    '连接模型': 'Connect model',
    '请求不调用工具、不联网；API 地址只用于发送本次测试。':
      'No tools or network access are used; the endpoint only receives this test.',
    '请求不调用工具、不联网；检测不设置应用侧 token 上限，让模型充分发挥。':
      'No tools or browsing. No app-side token cap is applied, so the model can use its full capability.',
    '可以直接开始检测；检测过程中会实时展示模型的思考过程。':
      'Start the test directly; the model reasoning stream is shown live.',
    '可以直接开始检测；点「快速自检」只做一次约 1 秒的连通性确认。':
      'Start the test directly, or run a quick connection check first.',
    '快速自检': 'Quick check',
    '一键测试三题（并发）': 'Run all three (parallel)',
    '逻辑题一': 'Logic test 1',
    '逻辑题二': 'Logic test 2',
    'SVG 创作': 'SVG creation',
    '标准答案 21': 'Expected answer 21',
    '标准答案 20': 'Expected answer 20',
    '生成并渲染动画': 'Generate and render animation',
    '未检测': 'Not tested',
    '未生成': 'Not generated',
    '极端抽取条件下的充分性判断': 'Sufficiency under extreme sampling',
    '条件变化下的答案稳定性判断': 'Answer stability under changed conditions',
    '鹈鹕骑自行车 · SVG 2D 动画': 'Pelican riding a bicycle · SVG 2D animation',
    '开始检测': 'Run test',
    '开始生成': 'Generate',
    '复制 HTML': 'Copy HTML',
    '模型公开回答': 'Model response',
    '判定说明': 'Decision',
    '结果说明': 'Result',
    '等待模型回答': 'Waiting for the model',
    '生成完成后显示模型返回内容': 'Model output appears here once generated',
    '生成完成后显示渲染判定': 'Render verification appears here once generated',
    '检测完成后显示判定依据': 'Decision rationale appears here once the test completes',
    '等待开始检测': 'Waiting to start',
    '等待开始生成': 'Waiting to start',
    '糖果数量统计': 'Candy counts',
    '形状 \\ 口味': 'Shape \\ Flavor',
    '苹果味': 'Apple',
    '桃子味': 'Peach',
    '西瓜味': 'Watermelon',
    '圆形': 'Round',
    '五角星形': 'Star',
    '满足条件': 'Condition',
    '预览': 'Preview',
    '固定画布': 'Fixed canvas',
    '思考强度': 'Reasoning effort',
    '自动': 'Auto',
    '低': 'Low',
    '中': 'Medium',
    '最高': 'Highest',
    '仅本次检测使用': 'Used for this test only',

    /* ---- 设置 ---- */
    '运行与界面偏好': 'Runtime and interface preferences',
    '所有设置仅保存在本机；扫描参数在下一次任务中生效。':
      'Settings are stored on this device only; scan parameters apply on the next run.',
    '外观': 'Appearance',
    '主题、字体与信息密度': 'Theme, font and information density',
    '界面主题': 'Theme',
    '界面字体': 'Font',
    '界面密度': 'Density',
    '主题强调色': 'Accent color',
    '圆角': 'Corner radius',
    '美元兑人民币汇率': 'USD/CNY rate',
    '界面语言': 'Language',
    '扫描与性能': 'Scanning and performance',
    '默认采用增量索引，优先保持界面响应':
      'Incremental indexing keeps the interface responsive',
    '预算提醒': 'Budget alerts',
    '0 表示不设置上限': '0 means no limit',
    '功能开关': 'Feature toggles',
    '关闭后左侧导航栏不再显示对应入口':
      'Hidden entries disappear from the sidebar navigation',
    'AI 提示词优化': 'AI Prompt Lab',
    '关闭后隐藏「提示词优化」工作区（已保存的配置保留）':
      'Hides the Prompt Lab workspace (saved profiles are kept)',
    'AI 智商检测': 'AI Benchmark',
    '关闭后隐藏「AI 智商检测」题板与检测入口':
      'Hides the AI Benchmark panel and its entry',
    '提示词优化': 'Prompt Lab',
    '可选：改写时是否让模型先思考':
      'Optional: let the model reason before rewriting',
    '自定义日志目录': 'Custom log directories',
    '添加 OpenAI / Anthropic 兼容 usage 日志':
      'Add OpenAI / Anthropic-compatible usage logs',
    '自动扫描（分钟）': 'Auto scan (minutes)',
    '解析线程': 'Parser workers',
    '单文件上限（MB）': 'File limit (MB)',
    '明细每页条数': 'Rows per page',
    '列表最多展示': 'Max list rows',
    'Token 预算（本周期）': 'Token budget (period)',
    '成本预算（USD）': 'Cost budget (USD)',
    '预警阈值（百分比）': 'Alert threshold (%)',
    '默认时间范围': 'Default time range',
    '生成上限（tokens）': 'Output limit (tokens)',
    '添加': 'Add',
    '统计口径': 'Methodology',
    '危险操作不会删除原始日志。': 'Destructive actions do not delete original logs.',
    '强制全量重扫': 'Full rescan',
    '清空索引': 'Clear index',
    '保存设置': 'Save settings',
    '数字滚动动画': 'Animated counters',
    '切换时间范围时数值平滑过渡':
      'Smoothly animate values when the time range changes',
    '显示帧率表': 'Show FPS meter',
    '右上角的实时 FPS 指示': 'Live frame-rate indicator in the top-right corner',
    '启动时自动扫描': 'Scan on startup',
    '打开应用后在后台执行一次增量扫描':
      'Run one incremental scan in the background when the app starts',
    'Codex 极速模式': 'Codex fast mode',
    '只读汇总表，跳过逐轮明细': 'Read summary tables and skip turn-level details',
    '大库模式': 'Large-library mode',
    '记录量很大时自动限制明细扫描规模，优先保证界面流畅':
      'Limit detail queries on very large libraries to keep the interface responsive',
    '展示模型思考过程': 'Show model reasoning',
    '关闭后结果区只显示改写结果，界面更清爽（推荐关闭）':
      'When off, only the rewritten prompt is shown (recommended)',

    /* ---- 主题 / 字体 / 密度选项 ---- */
    '紧凑': 'Compact',
    '舒适': 'Comfortable',
    '宽松': 'Spacious',
    '深色': 'Dark',
    '浅色': 'Light',
    '深空': 'Deep space',
    '海蓝': 'Ocean',
    '霓虹': 'Neon',
    '亮色': 'Light',
    '薄荷': 'Mint',
    '日光': 'Daylight',
    '水墨': 'Ink wash',
    '跟随系统': 'System',
    '跟随主题': 'Follow theme',
    '系统无衬线': 'System sans',
    '人文无衬线': 'Humanist sans',
    '中文衬线': 'Chinese serif',
    '等宽字体': 'Monospace',
    '经典宋黑': 'Classic Chinese',
    '圆润无衬线': 'Rounded sans',
    '靛蓝': 'Indigo',
    '紫罗兰': 'Violet',
    '青碧': 'Cyan',
    '翡翠': 'Emerald',
    '琥珀': 'Amber',
    '玫瑰': 'Rose',
    '硬朗': 'Sharp',
    '柔和': 'Soft',
    '圆润': 'Round',
    '简体中文': 'Simplified Chinese',

    /* ---- 占位符 ---- */
    '例如 D:\\logs\\llm-gateway': 'e.g. D:\\logs\\llm-gateway',
    '例如 gpt-4o-mini / deepseek-chat / claude-3-5-sonnet':
      'e.g. gpt-4o-mini / deepseek-chat / claude-3-5-sonnet',
    '例如 https://api.deepseek.com/v1': 'e.g. https://api.deepseek.com/v1',
    '例如：公司 DeepSeek': 'e.g. Company DeepSeek',
    '例如：帮我写一个爬虫': 'e.g. write a web crawler',
    '例如 gpt-4o-mini': 'e.g. gpt-4o-mini',
    '保存后不再回显；已有 Key 可留空':
      'Not shown again after saving; leave blank to keep the existing key',
    '可选择已有配置快速切换；新建时填写三项。协议、网络、证书与生成参数由系统自动处理。':
      'Switch between saved profiles, or fill in the three fields to create one. Protocol, network, certificates and generation parameters are handled automatically.',

    /* ---- 关于 / 帮助 ---- */
    '版本': 'Version',
    '作者': 'Author',
    '运行方式': 'Runtime',
    '数据范围': 'Data scope',
    '本地离线': 'Local, offline',
    '仅本机日志': 'Local logs only',
    '键盘快捷键': 'Keyboard shortcuts',
    '切换左侧视图': 'Switch left-hand view',
    '聚焦搜索框': 'Focus the search box',
    '重新扫描（增量）': 'Rescan (incremental)',
    '切换明暗主题': 'Toggle light / dark theme',
    '导出 CSV': 'Export CSV',
    '关闭抽屉 / 弹层': 'Close drawer or overlay',
    '显示本帮助': 'Show this help',
    '关闭': 'Close',

    /* ---- 空状态与提示 ---- */
    '当前时间范围内还没有可提炼的数据': 'No data to summarize in this range',
    '当前时间范围内没有可计费的调用记录': 'No billable calls in this range',
    '还没有调用记录': 'No records yet',
    '尚未添加自定义目录': 'No custom directory added yet',
    '当前筛选条件下没有记录，可放宽时间或清空搜索词':
      'No records match the current filters. Widen the date range or clear the search.',
    '选择「近 7 / 30 / 90 天」等周期后即可与上一个等长周期对比':
      'Choose a period such as last 7 / 30 / 90 days to compare with the previous one',
    '全部时间（无法对比上一周期）': 'All time (no previous period to compare)',
    '这里会显示使用进度': 'and usage progress appears here',
    '装上后重新扫描即可自动纳入统计':
      'Install it and rescan to include it automatically',
    '（无输入）': '(no input)',
    '（按时间升序）': ' (oldest first)',
    '逐轮明细': 'Turn-level detail',
    '每日用量': 'Daily usage',
    '模型构成': 'Model breakdown',
    '时间范围': 'Time range',
    '缓存命中': 'Cache hit',
    '与上一个等长周期相比': 'Compared with the previous equal-length period',
    '点击查看该会话明细': 'Click to view session detail',
    '点击查看这次会话的明细': 'Click to view this session',
    '点击记录可在原位置展开，再次点击即可收起；当前输入不会被修改':
      'Select a record to view it here; the current input stays unchanged',
    '选择记录后，可在此弹窗查看完整内容；当前输入不会被修改':
      'Select a record to view it here; the current input stays unchanged',

    /* ---- 数据源说明（后端 adapter detail） ---- */
    '读取 ~/.claude/projects/**/*.jsonl 中每条 assistant 消息的 usage 字段（含缓存读写）':
      'Reads the usage field of every assistant message in ~/.claude/projects/**/*.jsonl (including cache read/write)',
    '解析 ~/.codex/sessions/**/rollout-*.jsonl 的 token_count 事件，按轮次累计':
      'Parses token_count events in ~/.codex/sessions/**/rollout-*.jsonl and accumulates them per turn',
    '解析 ~/.gemini/tmp/*/chats/*.jsonl 中 gemini 消息的 tokens 字段':
      'Parses the tokens field of Gemini messages in ~/.gemini/tmp/*/chats/*.jsonl',
    '递归读取 Harness 会话投影缓存中的 tokenUsage（未缓存输入 / 输出 / 缓存读 / 缓存写）；仅统计本机仍保留投影数据的会话，已被 Harness 清理或未投影的云端历史无法从本机恢复':
      'Recursively reads tokenUsage from the Harness session projection cache (uncached input / output / cache read / cache write). Only sessions whose projections still exist locally are counted; cloud history already cleaned up or never projected by Harness cannot be recovered from this machine.',
    '读取 ~/.local/share/opencode 与 mimocode 的 SQLite session 表（含成本字段）':
      'Reads the SQLite session tables of ~/.local/share/opencode and mimocode (including cost fields)',
    '读取 ~/.workbuddy/workbuddy.db 的 session_usage 表，并关联会话模型与项目':
      'Reads the session_usage table in ~/.workbuddy/workbuddy.db and joins it with model and project',
    '读取 VS Code 系 globalStorage 下各任务 ui_messages.json 的 tokensIn/tokensOut/cost':
      'Reads tokensIn/tokensOut/cost from each task ui_messages.json under VS Code globalStorage',
    '读取 ~/.continue/sessions/*.json 中的 usage 记录':
      'Reads usage records from ~/.continue/sessions/*.json',
    '统计 VS Code 聊天会话文件中的 token 用量；早期版本不回写 token 则仅上报会话数':
      'Counts token usage in VS Code chat session files; older versions that do not persist tokens report session counts only',
    '读取本地对话记录中统计到的 token 数（完全离线推理，无 API 费用）':
      'Reads token counts recorded in local conversation history (fully offline inference, no API cost)',
    '本地推理不产生 API 费用；若日志/代理里记录了 prompt_eval_count 与 eval_count，会作为本地推理量采集，成本记为 0':
      'Local inference incurs no API cost; when logs or a proxy record prompt_eval_count and eval_count, they are collected as local inference volume with zero cost',
    '扫描项目中的 .aider.chat.history.md 与 ~/.aider 下的用量记录':
      'Scans .aider.chat.history.md in your projects plus usage records under ~/.aider',
    '本地 state.vscdb 只保存会话结构与补全行数，token 由 Cursor 云端计量，故不纳入本地合计':
      'The local state.vscdb stores only session structure and accepted-completion lines; tokens are metered by Cursor in the cloud, so they are excluded from the local total',
    'ModularData 下的会话库为加密格式，本地无法解出 token；用量以官方账单为准':
      'The session store under ModularData is encrypted and cannot be decoded locally; refer to official billing for usage',
    '检测本地缓存与日志；token 在 Codeium 云端计量，本地不落盘':
      'Inspects local caches and logs; tokens are metered by Codeium in the cloud and never written locally',
    '会话与用量记录保存在 Electron IndexedDB(LevelDB) 中，做只读尽力解析':
      'Conversations and usage are stored in Electron IndexedDB (LevelDB); parsed read-only on a best-effort basis',
    '在插件缓存与本地日志中搜索带 token 字段的会话记录':
      'Searches plugin caches and local logs for session records containing a token field',
    '扫描 CodeBuddy IDE 插件残留的会话与用量文件':
      'Scans session and usage files left behind by the CodeBuddy IDE extension',
    '读取 JetBrains 系 IDE 的 AI 会话统计与日志（只扫配置与日志目录，跳过缓存）':
      'Reads AI session statistics and logs from JetBrains IDEs (config and log directories only, caches skipped)',
    '读取 Zed 的 Agent 线程与助手会话记录':
      'Reads Zed agent threads and assistant session records',
    '读取 Jan 的本地对话线程（多为本地推理，通常无费用）':
      'Reads Jan local conversation threads (mostly local inference, usually no cost)',
    '在 Chatbox 本地库中检索带 token 字段的消息':
      'Searches the local Chatbox database for messages containing a token field',
    '读取 Kiro 的本地会话与用量缓存':
      'Reads Kiro local session and usage caches',
    '读取 Qoder 的本地会话缓存（用量可能仅存于云端）':
      'Reads Qoder local session caches (usage may exist only in the cloud)',
    '读取 Cody 插件的本地对话缓存':
      'Reads the local conversation cache of the Cody extension',
    '读取 Tabnine 本地缓存中的补全与对话统计':
      'Reads completion and conversation statistics from the local Tabnine cache',
    '读取 Warp 终端的 AI 会话记录':
      'Reads AI session records from the Warp terminal',
    '读取 ~/.zcode/cli/db/db.sqlite 的 model_usage 表，按会话关联项目与标题':
      'Reads the model_usage table in ~/.zcode/cli/db/db.sqlite and joins each call '
      + 'with its session project and title',
    '解析 ~/.kimi-code 与 ~/.kimi 下各会话 agents/*/wire.jsonl 的 token 用量（只计轮次记录，避免会话级累计值重复累加）':
      'Parses token usage from each session\'s agents/*/wire.jsonl under ~/.kimi-code and '
      + '~/.kimi (turn-scoped records only, so session-level cumulative totals are not '
      + 'counted twice)',
    '扫描你指定的目录 + 常见网关默认目录，自动识别 OpenAI / Anthropic 兼容格式的 usage 记录（JSONL、日志、JSON、CSV）':
      'Scans the directories you specify plus common gateway defaults, auto-detecting OpenAI / Anthropic-compatible usage records (JSONL, logs, JSON, CSV)',
    '大模型 API 调用日志': 'LLM API call logs',
    '通义灵码': 'Tongyi Lingma',
    'CodeBuddy 插件': 'CodeBuddy Extension',

    /* 说明：这条来自 index.html 的隐私提示，属于静态 HTML，所以放进词条表由翻译层处理。 */
    'API Key 仅保存在本机并由 Windows DPAPI 加密。提交优化时，提示词只会发送到你配置的 AI 服务。':
      'The API Key is stored on this machine only and encrypted with Windows DPAPI. When you submit, '
      + 'the prompt is sent only to the AI service you configured.',

    /* ---- 厂商名与扫描状态（数据源卡片上的附加文案） ---- */
    '腾讯': 'Tencent',
    '腾讯云': 'Tencent Cloud',
    '阿里云': 'Alibaba Cloud',
    '字节跳动': 'ByteDance',
    '智谱 AI': 'Zhipu AI',
    '月之暗面': 'Moonshot AI',
    '开源插件': 'Open source',
    '自定义目录 / 网关': 'Custom directories / gateways',
    '本机未安装': 'Not installed on this device',
    '已定位数据文件但未解析出 token（可能尚未产生用量）':
      'Data files found but no tokens parsed yet (no usage recorded)',
    '未检测到数据目录': 'No data directory detected',
    '加密或无法读取': 'Encrypted or unreadable',
    '未发现可解析的数据': 'No parseable data found',
    '仅检测': 'Detection only',
  };

  /* ---------------- 动态词条：数字/名称混在文本里 ----------------
     顺序敏感：更具体的规则必须排在更宽泛的规则前面，否则
     "1,234 次调用" 会先被 "^([\d,]+) 次$" 之外的规则截走。 */
  const RULES = [
    /* 智商检测题干：题目正文与「满足条件」脚注现在是可翻译的普通文本节点
       （以前关在 <pre> 里被翻译层跳过），所以在这里给出整句英文。 */
    [/^题 ([12])：(.+)$/,
      (m) => `Problem ${m[1]}: A black bag holds candies in three flavors, and each flavor comes in two shapes (round and star, distinguishable by touch). The candies of each flavor and shape are counted in the table below. A participant must decide, before the game starts, how many candies to draw. What is the smallest number of candies to draw that guarantees holding apple and peach candies of different shapes?`],
    [/^同时手中有圆形苹果味匹配五角星桃子味糖果，或者有圆形桃子味匹配五角星苹果味糖果都满足要求。$/,
      () => 'Either a round apple candy together with a star peach candy, or a round peach candy together with a star apple candy, satisfies the requirement.'],
    [/^匹配「(.+?)」共 ([\d,]+)\+? 条记录$/, (m) => `Matching “${m[1]}” · ${m[2]} records`],
    [/^共 ([\d,]+)\+? 条记录$/, (m) => `Total ${m[1]} records`],
    [/^第 ([\d,]+) \/ ([\d,]+) 页$/, (m) => `Page ${m[1]} of ${m[2]}`],
    [/^精确 (.+)$/, (m) => `Exact ${m[1]}`],
    [/^活跃 ([\d,]+) 天$/, (m) => `Active ${m[1]} days`],
    [/^([\d,]+) 个会话$/, (m) => `${m[1]} sessions`],
    [/^([\d,]+) 个模型$/, (m) => `${m[1]} models`],
    [/^([\d,]+) 个工具$/, (m) => `${m[1]} tools`],
    [/^([\d,]+) 个文件$/, (m) => `${m[1]} files`],
    [/^([\d,]+) 个来源$/, (m) => `${m[1]} sources`],
    [/^([\d,]+) 个活跃日）$/, (m) => `${m[1]} active days)`],
    [/^([\d,]+) 个$/, (m) => `${m[1]}`],
    [/^缓存命中 ([\d.]+)%$/, (m) => `Cache hit ${m[1]}%`],
    [/^([\d,]+) 次调用 · ([\d,]+) 会话 · (.+)$/,
      (m) => `${m[1]} calls · ${m[2]} sessions · ${m[3]}`],
    [/^([\d,]+) 个工具 · ([\d,]+) 个会话 · 最近 (.+)$/,
      (m) => `${m[1]} tools · ${m[2]} sessions · last ${m[3]}`],
    [/^([\d,]+) 次 · ([\d,]+) 个工具$/, (m) => `${m[1]} calls · ${m[2]} tools`],
    [/^([\d,]+) tokens · ([\d,]+) 次调用$/, (m) => `${m[1]} tokens · ${m[2]} calls`],
    [/^合计 ([\d,]+) tokens$/, (m) => `Total ${m[1]} tokens`],
    [/^([\d,]+) 次调用$/, (m) => `${m[1]} calls`],
    [/^([\d,]+) 次$/, (m) => `${m[1]} calls`],
    [/^近一年每日用量强度 · 共 (.+)$/, (m) => `Daily usage intensity over the past year · ${m[1]}`],
    [/^(.+?) · 按天统计$/, (m) => `${m[1]} · by day`],
    [/^另有 ([\d,]+) 个较少的模型未显示（可在设置里调整「列表最多展示」）$/,
      (m) => `${m[1]} smaller models hidden (adjust “Max list rows” in Settings)`],
    [/^另有 ([\d,]+) 个项目未显示（可在设置里调整「列表最多展示」）$/,
      (m) => `${m[1]} more projects hidden (adjust “Max list rows” in Settings)`],
    [/^本机未安装的 ([\d,]+) 个数据源$/, (m) => `${m[1]} sources not installed on this device`],
    [/^以下来源的用量无法从本地取得，未计入合计： (.*)。它们的实际消耗请以厂商后台为准。$/,
      (m) => `Usage from these sources cannot be read locally and is excluded from the total: ${m[1]}. Refer to each vendor's dashboard for actual consumption.`],
    [/^其中 ([\d,]+) 个模型不在价目表内/, (m) => `${m[1]} models are not in the price list`],
    [/^将时间范围收窄到 (.+)$/, (m) => `Narrowed to ${m[1]}`],
    [/^未额外筛选，跟随上方时间范围$/, () => 'No extra filter; follows the time range above'],
    [/^切换为 (.+)$/, (m) => `Switch to ${m[1]}`],
    [/^主题已切换为「(.+?)」$/, (m) => `Theme switched to “${m[1]}”`],
    [/^界面密度已切换为「(.+?)」$/, (m) => `Density switched to “${m[1]}”`],
    [/^扫描中 ([\d.]+)% · (.*)$/, (m) => `Scanning ${m[1]}% · ${m[2]}`],
    [/^([\d,]+) 个文件 · 新解析 ([\d,]+) · ([\d.]+)s ?(.*)$/,
      (m) => `${m[1]} files · ${m[2]} newly parsed · ${m[3]}s ${m[4]}`.trim()],
    [/^扫描完成：解析 ([\d,]+) 个文件，共 ([\d,]+) 条新记录$/,
      (m) => `Scan complete: ${m[1]} files parsed, ${m[2]} new records`],
    [/^（([\d,]+) 条提示）$/, (m) => ` (${m[1]} notices)`],
    [/^([\d,]+) 天前$/, (m) => `${m[1]} days ago`],
    [/^([\d,]+) 小时前$/, (m) => `${m[1]} hours ago`],
    [/^([\d,]+) 分钟前$/, (m) => `${m[1]} minutes ago`],
    [/^刚刚$/, () => 'just now'],
    [/^约 ([\d,]+) 分钟$/, (m) => `about ${m[1]} minutes`],
  ];

  const ATTRS = ['placeholder', 'title', 'aria-label'];
  const isChinese = (s) => /[\u4e00-\u9fff]/.test(s);

  let lang = 'zh-CN';
  let pairs = null;          // 仅英文模式下维护，用于切回中文时回滚
  const EXCLUDED = 'script,style,textarea,code,pre,svg,[data-no-translate],#plResult,#plReasoning,#iqQ1Content,#iqQ2Content';
  const metrics = { passes: 0, nodes: 0, totalMs: 0, maxMs: 0 };

  function translate(text) {
    if (!isChinese(text)) return null;
    const key = text.trim();
    if (!key) return null;
    const hit = DICT[key];
    if (hit) return text.replace(key, hit);
    for (let i = 0; i < RULES.length; i++) {
      const m = key.match(RULES[i][0]);
      if (m) return text.replace(key, RULES[i][1](m));
    }
    return null;
  }

  /* 翻译一棵子树里所有中文文本节点与属性。
     幂等：英文里不含汉字，重复调用不会叠加替换。 */
  function apply(root) {
    const host = root || document.body;
    if (!host || lang !== 'en-US') return 0;
    const started = performance.now();
    const parent = host.nodeType === 3 ? host.parentElement : host;
    if (parent && parent.closest && parent.closest(EXCLUDED)) return 0;
    let n = 0;
    if (host.nodeType === 3) {
      const out = translate(host.nodeValue);
      if (out != null) { remember(host, null, host.nodeValue, out); host.nodeValue = out; n++; }
      return n;
    }
    if (host.nodeType !== 1) return 0;
    const tag = host.tagName;
    if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'TEXTAREA' ||
        tag === 'CODE' || tag === 'PRE' || tag === 'SVG') return 0;

    const doc = host.ownerDocument || document;
    // Include elements so FILTER_REJECT prunes entire code/stream/SVG subtrees.
    // A text-only walker still visited every text below excluded elements.
    const walker = doc.createTreeWalker(host, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
      acceptNode(node) { return node.nodeType === 1 && node.matches(EXCLUDED) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT; }
    });
    const attributes = (element) => {
      for (const attr of ATTRS) {
        const current = element.getAttribute(attr);
        if (!current || !isChinese(current)) continue;
        const hit = DICT[current.trim()];
        if (hit) { remember(element, attr, current, hit); element.setAttribute(attr, hit); n++; }
      }
    };
    attributes(host);
    let node = walker.nextNode();
    while (node) {
      metrics.nodes++;
      if (node.nodeType === 1) attributes(node);
      else {
        const out = translate(node.nodeValue);
        if (out != null) { remember(node, null, node.nodeValue, out); node.nodeValue = out; n++; }
      }
      node = walker.nextNode();
    }
    const duration = performance.now() - started;
    metrics.passes++; metrics.totalMs += duration; metrics.maxMs = Math.max(metrics.maxMs, duration);
    return n;
  }

  function remember(node, attr, original, translated) {
    if (!pairs) pairs = [];
    // 上限保护：界面反复重渲染时旧节点会被丢弃，超过阈值先清掉已脱离文档的，
    // 避免长期运行时这个数组无限增长。
    if (pairs.length > 40000) {
      pairs = pairs.filter((p) => p.node && p.node.isConnected);
    }
    pairs.push({ node: node, attr: attr, original: original, translated: translated });
  }

  /* 切回中文：把仍然保留着译文的节点写回原文。
     只在节点**当前值仍等于我们写入的译文**时回滚，否则会把渲染函数
     刚赋的新值覆盖成上一轮的旧文案。 */
  function restore() {
    if (!pairs) return;
    for (let i = 0; i < pairs.length; i++) {
      const p = pairs[i];
      const node = p.node;
      if (!node || !node.isConnected) continue;
      if (p.attr) {
        if (node.getAttribute(p.attr) === p.translated) node.setAttribute(p.attr, p.original);
      } else if (node.nodeValue === p.translated) {
        node.nodeValue = p.original;
      }
    }
    pairs = null;
  }

  /* ---------------- 自动跟随 DOM 变化 ----------------
     英文模式下挂一个 MutationObserver：新写入的 DOM（切视图、翻页、
     实时流式输出、打开弹窗）会在本帧末尾统一翻译一次。这样就不必在
     300 多处渲染点逐一记得调用 apply()——漏一处就是一句中文残留。

     关键细节：观察者必须在 apply() 期间断开。apply 自己会写 DOM，
     若不断开就会触发新一轮回调，形成"翻译→回调→翻译"的死循环。
     断开期间丢失的 mutation 不影响正确性：apply 走的是整棵子树。 */
  let observer = null;
  let scheduled = false;
  let applying = false;
  const dirty = new Set();

  function queue(node) {
    if (!node || !node.isConnected) return;
    if (node.nodeType === 3 && !isChinese(node.nodeValue)) return;
    if (node.nodeType !== 1 && node.nodeType !== 3) return;
    const element = node.nodeType === 3 ? node.parentElement : node;
    if (element && element.closest(EXCLUDED)) return;
    // Coalesce a replaced table/card; do not translate every descendant twice.
    for (const pending of dirty) {
      if (pending === node || pending.contains(node)) return;
      if (node.contains(pending)) dirty.delete(pending);
    }
    dirty.add(node);
  }

  function schedule(mutations) {
    if (applying) return;
    for (const mutation of mutations || []) {
      if (mutation.type === 'characterData') queue(mutation.target);
      else for (const node of mutation.addedNodes) queue(node);
    }
    if (scheduled || !dirty.size) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      if (lang !== 'en-US') { dirty.clear(); return; }
      const started = performance.now();
      pause();
      applying = true;
      try {
        for (const node of dirty) {
          dirty.delete(node);
          if (node.isConnected) apply(node);
          if (performance.now() - started > 4) break;
        }
      } finally { applying = false; resume(); }
      if (dirty.size) schedule();
    });
  }

  function pause() { if (observer) observer.disconnect(); }
  function resume() {
    if (observer) {
      observer.takeRecords();
      observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    }
  }

  function observe(on) {
    if (on && !observer && window.MutationObserver && document.body) {
      observer = new MutationObserver(schedule);
      observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    } else if (!on && observer) {
      observer.disconnect();
      observer = null;
      dirty.clear();
    }
  }

  function setLang(next) {
    const want = next === 'en-US' ? 'en-US' : 'zh-CN';
    if (want === lang) { if (want === 'en-US') apply(document.body); return; }
    lang = want;
    if (want === 'en-US') {
      observe(true);
      apply(document.body);
    } else {
      observe(false);
      restore();
    }
  }

  window.TL_I18N = {
    apply: apply,
    setLang: setLang,
    get lang() { return lang; },
    translate: translate,
    get size() { return Object.keys(DICT).length; },
    stats: () => ({ ...metrics, queuedRoots: dirty.size }),
  };
})();
