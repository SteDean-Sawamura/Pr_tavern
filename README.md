# 酒馆 — AI 互动文字游戏引擎

一个基于 AI 大语言模型的互动文字冒险游戏引擎。通过 JSON 格式的剧本定义世界观、角色、事件和规则，由 AI 扮演游戏主持人（GM），驱动叙事并与玩家实时互动。

公开仓库仅包含引擎代码和通用测试，不包含剧本正文、运行数据库或剧本回放产物。请把本地剧本和测试数据保存在已忽略的 `data/` 或 `.tmp/` 目录，不要提交到 Git。

### 公共代码与私有数据

推荐把运行数据放在仓库外，通过环境变量指定目录：

```powershell
$env:TAVERN_DATA_DIR = "D:\TavernPrivate\data"
python app.py
```

主游戏、RPG 剧本加载和向量记忆共用该目录，默认数据库位于其中的 `tavern.db`。
`TAVERN_DB_PATH` 可单独覆盖数据库位置；未设置 `TAVERN_DATA_DIR` 时仍使用仓库内被忽略的 `data/`。
不要公开数据库、真实剧本、存档导出、测试回放、截图、日志、API 配置或旧 Git 备份；测试仅使用合成数据。
公开发布前还需检查暂存内容和提交身份；`.gitignore` 不会移除已经跟踪的文件。

剧本可通过 `visibility_policy` 定义可见性策略，而无需修改引擎：

| 字段 | 未设置时的行为 |
|------|----------------|
| `default_event_visibility` / `default_story_visibility` | `public`；可设置 `world` 隐藏未标注的事件或节点 |
| `require_notification_source` | `false`；开启后丢弃缺失来源或来源数量不匹配的通知 |
| `npc_public_profiles_only` | `false`；开启后只展示 NPC 的公开档案字段 |
| `private_world_properties` | 空列表；按属性 ID 或名称过滤玩家视图 |
| `private_expired_states` | 空列表；过滤指定状态的到期提示 |
| `private_location_descriptions` | 空列表；指定地点只展示 `public_description` |
| `inherit_snapshot_metadata` | `false`；开启后用当前剧本补全旧存档缺失的公开性元数据，不覆盖已有值 |

策略内容和实体标识属于各自剧本，应与私有剧本一起保存，不写入公开源码或测试。

RPG 地点别名通过剧本的 `location_aliases`（旧 ID 到当前 ID 的映射）加载。
人物地图外观通过 NPC 的 `visual_profile` 加载：`map_color` 控制标记配色，`hair_color` / `hair_style` 控制发型，`accessory` 支持 `tie`、`whistle`、`earrings`；未设置时使用通用默认外观。
剧本编辑器保存时会保留这些元数据，不再按具体人物 ID 硬编码外观。

---

## 给测试小白的快速上手指南

只需 3 步，不需要任何编程知识。

### 第 1 步：安装 Python

1. 打开 https://www.python.org/downloads/
2. 点击黄色大按钮 **Download Python 3.x.x** 下载
3. 运行安装程序，**一定要勾选底部的 `Add Python to PATH`**，然后点 Install Now
4. 等待安装完成，关闭安装程序

### 第 2 步：双击启动

打开酒馆文件夹，双击 **`start.bat`**

首次启动会自动：
- 创建运行环境（约 30 秒）
- 下载安装所需组件（约 1-3 分钟，取决于网速）
- 启动服务并自动打开浏览器

> 如果遇到 Windows 安全提示"Windows 已保护你的电脑"，点击 **更多信息** → **仍要运行**

### 第 3 步：配置 AI

浏览器打开后，你会看到游戏主页。在开始游戏前需要先配置 AI：

1. 点击页面上的 **设置**（齿轮图标）
2. 点击 **添加配置**
3. 选择一个预设（推荐选 **DeepSeek** 或 **OpenRouter**）
4. 填入你的 API Key（需要自行注册获取）
5. 点击 **测试连接**，显示成功后点 **激活**

配置好 AI 后就可以开始游戏了！你可以：
- 使用已有剧本直接开始游戏
- 在剧本编辑器中用 AI 辅助创建新剧本

### 常见问题

| 问题 | 解决办法 |
|------|----------|
| 双击 start.bat 闪退 | 右键 start.bat → 用管理员身份运行 |
| 提示"未检测到 Python" | 重新安装 Python，确保勾选了 Add to PATH；安装后重启电脑 |
| pip 安装依赖很慢 | 国内网络可能较慢，耐心等待；或使用科学上网 |
| 浏览器没有自动打开 | 手动打开浏览器，访问 http://127.0.0.1:8000 |
| 关闭游戏 | 直接关闭 start.bat 的黑色命令窗口即可 |
| 再次启动 | 再次双击 start.bat（第二次会很快，不需要重新安装） |

---

## 功能特性

### 核心游戏引擎
- **AI 驱动叙事** — AI 扮演 GM，根据剧本设定生成叙述、对话和选项
- **世界树分支历史** — 完整的树状游戏历史，支持回溯、分支探索和撤销
- **Swipe / 重新生成** — 对 AI 回复不满意可重新生成或左右切换备选版本
- **流式输出** — SSE 流式响应，实现打字机效果的实时输出
- **历史摘要压缩** — 自动分层压缩历史记录，节省 Token 消耗

### 剧本系统
- **JSON 剧本格式** — 结构化定义世界观、NPC、地点、事件、属性等
- **AI 辅助生成** — 通过分步向导让 AI 协助创建完整剧本
- **固定开局选项** — 支持预设开局选择，含确定性和条件性结果
- **剧本验证** — 自动检查剧本结构完整性和交叉引用一致性
- **剧本变量与宏** — 支持 `{{var::name}}` 格式的变量宏替换
- **正则脚本** — 对 AI 输出或玩家输入进行后处理的正则规则
- **触发器** — 基于游戏事件的生命周期钩子，自动执行动作

### 故事树 (Story Tree)
- **分支叙事线** — 类似国策树的多线程叙事结构，支持解锁、前置依赖
- **多节点类型** — auto / timed / choice / quest 四种节点类型
- **选择后果** — choice 节点提供带数值影响、inject_prompt、activate_lore 的分支选项
- **知识图谱联动** — 故事树节点可自动激活 Lorebook 条目

### 骰子系统
- **可配置骰子** — 支持 NdM+K 格式（如 1d100、2d6+3）
- **结果区间** — 骰子结果自动映射到预定义的区间标签和状态变化
- **D100 检定** — 支持大于/小于/等于多种比较方式的百分骰检定
- **持续效果 & 冷却** — 骰子结果可附带持续时间和冷却机制
- **玩家可切换** — 骰子系统可由玩家在游戏中开关

### 状态管理
- **点分路径导航** — 使用 `player.attributes.health` 格式访问嵌套状态
- **智能路径解析** — `player.mood` 自动解析到 `player.attributes.mood`
- **状态变更** — 支持 set / add / remove / append / toggle 等操作
- **过期机制** — 持续状态可设置过期时间自动移除

### NPC 系统
- **三维关系模型** — 信任（trust）/ 好感（affection）/ 畏惧（fear）三轴关系
- **独立对话** — 可与单个 NPC 进行独立对话交互
- **离屏模拟** — NPC 在玩家不在场时也会有自己的活动
- **动态注册** — AI 可在游戏中动态创建新 NPC
- **关系网络** — NPC 之间也存在相互关系，形成完整的社交网络
- **组织归属** — NPC 可隶属于组织，拥有职级 (hierarchy) 体系

### 组织系统
- **组织定义** — 定义组织的 hierarchy（等级结构）、standing（组织声望）
- **NPC-组织关系** — NPC 可在组织中担任特定职级
- **组织间关系** — 组织之间可设置关系（同盟、敌对等）

### 事件系统
- **统一事件引擎** — 整合故事树、事件调度器和状态管理的统一引擎
- **周期事件** — 按天/周/月/小时/分钟的频率循环触发，支持条件和过期
- **一次性事件** — 在指定时间点触发的剧情节点，支持条件门控
- **元事件总线** — 回合后自动评估和分发事件钩子
- **随机项** — 条件触发或始终生效的随机事件，与骰子系统联动

### 世界书 / Lorebook
- **关键词激活** — 当关键词出现在近期对话中时自动注入相关设定
- **主/副关键词** — 支持 AND 逻辑的双层关键词匹配
- **时间可用性** — 可为词条设置 `available_from`（ISO 游戏时间）；未到时间或游戏时间无效时，自动激活、工具检索和 RAG 均不读取该词条
- **递归激活** — 已激活条目可触发关联条目（知识图谱边）
- **注入位置控制** — 可配置在系统提示的不同位置注入
- **自动生成** — 从 NPC 和关系数据自动生成 Lorebook 条目
- **NPC 揭露** — `discoverable.method: "npc_reveal"` 的隐藏词条只保存来源 NPC 在叙事中实际说出的原话到主角线索；提示词和玩家查询会标注来源 NPC（未知身份不暴露姓名），不因一个关键词解锁全文。剧本可设置 `discoverable.reveal_facts: ["关键事实短语1", "关键事实短语2"]`，只有原话累计覆盖全部短语时才将词条标为完整已知；未设置时始终只保存线索。直接事件揭露仍按原机制处理。

### 职业与技能系统
- **职业注册表** — 支持 D&D 5e 和 CoC 职业/技能体系
- **技能解锁** — 游戏中可解锁新技能
- **商店系统** — 基于地点的道具买卖

### 数据银行 (Data Bank)
- **文档上传** — 上传参考文档用于游戏内容补充
- **向量检索** — 基于语义相似度的文档片段检索
- **网页抓取** — 支持 URL 内容抓取并存入数据银行

### 向量记忆 (Vector Memory)
- **长期记忆** — 基于 FastEmbed + ChromaDB 的向量记忆系统
- **可选依赖** — 未安装时自动降级为无操作模式
- **独立安装** — `install_vector_memory.bat` 一键安装

### 素材与搜索
- **网页搜索** — 集成 DuckDuckGo 搜索获取参考素材
- **网页抓取** — 支持 URL 内容抓取和提取
- **素材管理** — 保存、标签分类、检索搜索结果
- **素材注入** — 将素材内容注入到剧本的指定位置
- **AI 变换** — 对素材进行 AI 驱动的内容转换

### 多 AI 提供商
- **OpenAI** — 官方 API（GPT-4o 等）
- **Claude (Anthropic)** — Anthropic 官方 API
- **Ollama** — 本地模型（Llama 3 等）
- **OpenAI 兼容** — 支持任何 OpenAI 兼容接口
- **预设配置** — 内置 Xi-AI、DeepSeek、Moonshot、智谱 GLM、SiliconFlow、OpenRouter 等预设
- **多配置管理** — 可保存多个 AI 配置并随时切换
- **连接测试** — 一键测试 AI 服务连通性

## 项目结构

```
酒馆/
├── app.py                      # FastAPI 应用入口
├── config.py                   # 应用配置（路径、端口、认证）
├── requirements.txt            # Python 依赖
├── requirements-vector.txt     # 向量记忆可选依赖
├── start.bat                   # Windows 一键启动
├── install_vector_memory.bat   # 向量记忆安装脚本
├── ai/                         # AI 提供商
│   ├── base.py                 # 抽象基类、重试装饰器、think 标签处理
│   ├── openai_provider.py      # OpenAI / OpenAI 兼容提供商
│   ├── claude_provider.py      # Anthropic Claude 提供商
│   ├── ollama_provider.py      # Ollama 本地模型提供商
│   └── response_parser.py      # AI 响应解析（JSON 提取与容错修复）
├── api/                        # API 路由
│   ├── game_routes.py          # 游戏流程 API
│   ├── script_routes.py        # 剧本管理 API
│   ├── save_routes.py          # 存档管理 API
│   ├── config_routes.py        # AI 配置 API
│   ├── search_routes.py        # 搜索与素材 API
│   └── databank_routes.py      # 数据银行 API
├── db/                         # 数据库
│   ├── database.py             # SQLite 异步连接池
│   └── models.py               # Pydantic 数据模型
├── engine/                     # 游戏引擎核心
│   ├── game_session.py         # 游戏会话（核心协调器）
│   ├── prompt_builder.py       # AI 提示词构建
│   ├── script_loader.py        # 剧本加载与验证
│   ├── world_tree.py           # 世界树（分支历史）
│   ├── state_manager.py        # 状态管理器
│   ├── dice.py                 # 骰子系统
│   ├── event_scheduler.py      # 事件调度器（时间触发）
│   ├── event_engine.py         # 统一事件引擎
│   ├── meta_events.py          # 元事件总线（回合后钩子）
│   ├── story_tree.py           # 故事树（分支叙事控制）
│   ├── history_summarizer.py   # 历史摘要压缩
│   ├── lorebook.py             # 世界书 / Lorebook
│   ├── script_variables.py     # 剧本变量与宏替换
│   ├── triggers.py             # 触发器引擎
│   ├── regex_scripts.py        # 正则脚本后处理
│   ├── class_system.py         # 职业与技能注册表
│   ├── data_bank.py            # 数据银行（文档上传与检索）
│   └── vector_memory.py        # 向量长期记忆（可选）
├── search/                     # 搜索模块
│   ├── search_engine.py        # 搜索引擎（DuckDuckGo）
│   ├── web_scraper.py          # 网页抓取
│   ├── ai_knowledge.py         # AI 知识处理
│   └── material_manager.py     # 素材管理
├── storage/                    # 存储扩展（预留）
├── static/                     # 前端静态文件
│   ├── index.html
│   ├── css/style.css
│   └── js/
│       ├── app.js              # 主应用逻辑
│       ├── npc_presence.js     # 地图与人物面板共用的日程和对话可用性
│       ├── game_ui.js          # 游戏界面
│       ├── script_builder.js   # 剧本编辑器
│       ├── search_ui.js        # 搜索界面
│       ├── world_tree_view.js  # 世界树可视化
│       ├── story_tree_view.js  # 故事树可视化
│       └── story_tree_editor.js # 故事树编辑器
├── experiments/                # 测试与基准实验
├── data/                       # 数据目录（运行时生成）
│   ├── tavern.db               # SQLite 数据库
│   └── saves/                  # 存档文件
└── tests/                      # 测试
    ├── test_dice.py
    ├── test_event_scheduler.py
    ├── test_response_parser.py
    └── test_state_manager.py
```

## 快速开始（开发者）

> 普通用户请直接双击 `start.bat`，以下为手动安装步骤。

### 环境要求

- Python 3.10+

### 安装

```bash
# 克隆项目
git clone <repo-url>
cd 酒馆

# 创建虚拟环境
python -m venv bar
source bar/bin/activate  # Linux/macOS
# 或 bar\Scripts\activate  # Windows

# 安装依赖
pip install -r requirements.txt

# （可选）安装向量记忆依赖
pip install -r requirements-vector.txt
```

### 运行

```bash
python app.py
```

服务默认启动在 `http://127.0.0.1:8000`，打开浏览器即可使用。

新游戏可选「叙事」或「大地图」初始视图；两个视图使用同一个主游戏存档和推演接口，选择不改变后端模式。游戏中也可在工具栏切换，浏览器按存档记住视图偏好。地图展示已发现地点、连接及玩家当前位置的已知 NPC 人数，单向连接以箭头标示；不将远处的实时日程当作玩家已知情报，人物面板也不显示远处 NPC 的精确位置和日程活动。窄屏使用可点击的地点列表，避免缩小地图文字。地图视图将原行动输入和提交控件移入地图底部，切回叙事后原位恢复，不复制行动接口；草稿在切换视图时保留。选择地点会标亮从当前位置出发、仅经过已发现地点的已知路线；若输入框已有玩家草稿，只查看路线、不覆盖草稿，否则填写前往行动。路线不保证实际可达，需再点「确认行动」提交。地图在回合结算或道具、现场互动成功持久化后按状态刷新，推演过程中则显示最近的 Agent 活动，不会将未结算的结果提前画到地图上，也不会在玩家不行动时自行推进世界时间。流式行动和重生成可在当前回合插话或停止，控制请求带回合标识，过期回合的控制请求不会作用于后续回合。原 `/rpg` 仍是独立的旧推演界面及会话，尚未迁移到主游戏存档。

地图画布按已发现地点和标签自动取景，保留边缘留白；顶部展示地点数及当前位置、所选路线、旧情报的图例。主游戏内容区宽度不超过 980px 时，地图和叙事改为上下排列；不超过 560px 时使用地点列表，并保留适合触屏点击的现场互动按钮。

手机屏幕（宽度不超过 600px）可从游戏工具栏的「状态」打开角色、世界及 Agent 等原有面板；顶部保留时间和天气，AI 配置与推演模式收进「推演设置」。导航、状态和推演设置互斥打开，点击遮罩或按 Escape 可关闭；切回桌面会清除手机遮罩，不改变桌面状态栏的折叠偏好。

数字键 1–4 仅在主游戏画面可见、未推演且没有弹窗或手机抽屉时选择当前选项；编辑输入、组合键与长按不会触发行动。

开局预设角色卡支持 Tab 聚焦、Enter 或空格选择，并以边框、文字及辅助技术可读状态标明当前选择；确认自建角色后清除预设选择。角色身份与关键属性分层显示，手机单列铺满可用宽度。

职业卡采用相同的键盘选择方式与响应式布局，完整显示职业描述。切换剧本会清除旧角色、职业、熟练技能和 NPC 编辑选择；剧本与职业加载完成前禁止开局，失败可在原位重试。剧本、职业及技能列表的过期响应不会覆盖当前选择。

开局请求期间锁定新游戏设置并显示等待提示，避免重复创建；存档导航和其他页面仍可使用。开局失败原位显示原因，保留角色选择与创作指令供重试；切换页面、剧本或存档后，旧开局结果不会抢回界面，已创建的存档可从侧栏继续。初始视图在提交时固定，确认自建角色后修改的表单、职业和技能会在开局时重新读取。

工具栏的「NPC对话」群聊请求期间显示「对话中…」，禁用行动、等待、休息和选项按钮，阻止重复提交并保留输入草稿；成功后将新对话滚入可视区域，失败时显示接口返回的原因并恢复输入和原本可用的按钮。切换存档或重新载入分支后，旧回复和旧错误不会污染当前画面，也不会解除新请求的等待状态。未配置 AI 时提示先前往设置配置模型。

地图的「现场」栏只列出当前地点的可交互物和已认识、已接触且在场的人物；可交互物沿用主游戏互动接口，对话沿用人物对话入口。没有现场互动时仍可自由输入行动，推演或切换存档时旧入口不会继续提交。NPC 对话会推进游戏时间；回复成功后，叙事历史、当前节点、状态面板和地图同步更新，切档后的旧回复不会覆盖新存档。对话请求期间前端阻止同存档其他行动；切档释放旧界面锁，已确认保存的回复不会因流末尾断开而提示重复发送。


世界树节点详情只返回玩家界面所需字段，节点与 Swipe 变体中的幕后推演字段不对外返回；快照、事件、状态变化和 Agent trace 仍按现有玩家态规则裁剪。内部世界树节点保持原样，这不涵盖历史、`world_properties` 等其他接口的完整情报隔离。

RPG 推演界面位于 `/rpg`。其标签页标识保存在 `sessionStorage`，页面重载可继续当前进程内的会话；复制标签页时会通过 `BroadcastChannel` 在首次请求前尝试检测仍在运行的原标签页并分配新标识。RPG 会话本身仍在服务端内存中，关闭服务后需要从存档恢复。缓存只淘汰没有游戏、构建器或进行中请求的空闲标签页，不会因达到 50 个标签页而丢弃未存档进度；大量活跃标签页仍会占用内存。浏览器不支持 `BroadcastChannel` 或探测超时时，不能保证复制标签页隔离。

RPG 开场白只作为场景展示，不会当作玩家行动自动推进时间；玩家输入第一步后才开始推演。开场时也可以直接保存进度。

也可以直接双击 `start.bat`（Windows），会自动完成环境创建、依赖安装、启动服务并打开浏览器。

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TAVERN_HOST` | `127.0.0.1` | 服务监听地址 |
| `TAVERN_PORT` | `8000` | 服务监听端口 |
| `TAVERN_API_TOKEN` | _(空)_ | API 认证 Token，留空则不启用认证 |

### 配置 AI

启动后在设置页面配置 AI 提供商：

1. 选择一个预设（如 DeepSeek、OpenRouter）或手动配置
2. 填入 API Key
3. 点击"测试连接"验证
4. 激活该配置

## 剧本格式

剧本使用 JSON 格式，包含以下主要字段：

```json
{
  "script_id": "my_script",
  "script_name": "剧本名称",
  "version": "1.0",
  "start_time": "2025-09-01T07:00:00",
  "world_background": "世界观描述...",

  "settings": {
    "dice_check": { "default_enabled": true, "player_can_toggle": true },
    "fixed_opening": { "default_enabled": true, "player_can_toggle": true }
  },

  "opening": {
    "text": "开场白文本...",
    "choices": [
      {
        "id": "open_0",
        "text": "选项描述",
        "result": {
          "type": "deterministic",
          "description": "结果描述",
          "state_changes": [
            { "target": "player.mood", "op": "add", "value": 5 }
          ]
        }
      }
    ]
  },

  "player_character": {
    "id": "player_id",
    "bio": "角色简介",
    "initial_location": "初始位置",
    "long_term_goal": "长期目标",
    "attributes": {
      "health": { "value": 80, "min": 0, "max": 100, "rule": "规则说明" }
    },
    "relationships": {
      "npc_id": { "value": 60, "min": 0, "max": 100, "rule": "规则说明" }
    }
  },

  "player_presets": [
    {
      "id": "preset_id",
      "name": "预设名称",
      "description": "预设描述",
      "attributes": {},
      "initial_location": "location_id"
    }
  ],

  "npcs": [
    {
      "id": "npc_id",
      "name": "NPC名称",
      "bio": "NPC背景",
      "public_profile": {"bio": "玩家可知的简介", "title": "公开职务", "organizations": [{"org_id": "公开组织"}]},
      "personality": "性格描述",
      "capabilities": "能力描述",
      "attitude_toward_player": 50,
      "organizations": [
        { "org_id": "org_id", "rank": "member" }
      ],
      "related_lore": ["lorebook_id"]
    }
  ],

  "organizations": [
    {
      "id": "org_id",
      "name": "组织名称",
      "description": "组织描述",
      "hierarchy": ["leader", "officer", "member"],
      "standing": { "value": 50 }
    }
  ],

  "locations": [
    {
      "id": "loc_id",
      "name": "地点名称",
      "description": "地点描述",
      "initially_visible": true,
      "connections": ["other_loc_id"]
    }
  ],

  "world_properties": [
    { "id": "prop_id", "name": "属性名", "value": "属性值", "rule": "规则" }
  ],

  "variables": [
    { "id": "var_id", "name": "变量名", "value": 0 }
  ],

  "triggers": [
    {
      "id": "trigger_id",
      "event": "on_turn_end",
      "condition": "player.health < 20",
      "actions": [
        { "type": "notify", "message": "你的生命值过低！" }
      ]
    }
  ],

  "random_items": [
    {
      "id": "item_id",
      "description": "描述",
      "trigger": "供阅读的场景说明",
      "trigger_type": "conditional",
      "trigger_condition": {
        "location_ids": ["market"],
        "action_keywords": ["逛", "交易"]
      },
      "dice": { "count": 1, "faces": 100, "modifier": 0 },
      "ranges": [
        { "min": 1, "max": 50, "label": "结果标签", "state_changes": [] }
      ]
    }
  ],

  "persistent_states": [
    {
      "id": "state_id",
      "description": "持续状态描述",
      "initially_active": false,
      "expires_at": null
    }
  ],

  "cyclic_events": [
    {
      "id": "event_id",
      "description": "事件描述",
      "frequency_value": 1,
      "frequency_unit": "week",
      "first_trigger": "2025-09-04T15:30:00",
      "condition": "status.state_id == 'active'",
      "expires_at": null
    }
  ],

  "one_time_events": [
    {
      "id": "event_id",
      "description": "事件描述",
      "narrative_claims": ["明确声称事件已经发生的短语"],
      "trigger_time": "2025-09-01T08:30:00",
      "condition": "status.state_id == 'active'"
    }
  ],

  "story_tree": {
    "trees": [
      {
        "id": "tree_id",
        "name": "故事线名称",
        "description": "描述",
        "icon": "skull",
        "nodes": [
          {
            "id": "node_id",
            "name": "节点名称",
            "description": "描述",
            "type": "choice",
            "requires": ["prerequisite_node_id"],
            "on_complete_unlock": ["next_node_id"],
            "effects": {
              "inject_prompt": "注入的叙事提示",
              "notify": "通知消息",
              "set_var": [
                { "target": "player.stat", "op": "add", "value": 5 }
              ],
              "activate_lore": ["lorebook_id"],
              "unlock_locations": ["location_id"]
            },
            "choices": [
              {
                "id": "choice_id",
                "label": "选项标签",
                "description": "选项描述",
                "effects": {},
                "unlock": ["next_node_id"]
              }
            ]
          }
        ]
      }
    ]
  },

  "npc_relationships": [
    {
      "from": "npc_id_1",
      "to": "npc_id_2",
      "trust": 60,
      "description": "内部关系描述",
      "public_description": "玩家初始可知的关系描述",
      "public_type": "合作"
    }
  ],

  "org_relationships": [
    {
      "from": "org_id_1",
      "to": "org_id_2",
      "type": "allied",
      "description": "关系描述"
    }
  ],

  "lorebook": [
    {
      "id": "lore_id",
      "keys": ["关键词1", "关键词2"],
      "secondary_keys": ["次级关键词"],
      "available_from": "1979-10-27T04:00",
      "content": "知识条目内容...",
      "priority": 50,
      "scan_depth": 5,
      "constant": false
    }
  ],

  "regex_scripts": [
    {
      "id": "regex_id",
      "pattern": "正则表达式",
      "replacement": "替换文本",
      "target": "output"
    }
  ]
}
```

随机项的 `trigger` 仅供阅读，不参与判断。`always` 每回合生效；`conditional` 需要 `trigger_condition`，可使用状态比较表达式，或组合 `location_ids`、`action_keywords`、`night`、`destination_changed` 等结构化条件。对象内条件同时满足，`any` 列表中的对象满足其一即可；地点和目的地使用剧本中的地点 ID，目的地识别当前要求行动文本含目标地点的完整名称或 ID。

`narrative_claims` 可选：当叙事逐字命中这些短语时，即使 Agent 未填写 `event_ids`，也会按该事件的实际结算时间校验；它不是泛化的语义剧透审核。数据库中已有的同 ID 剧本需要重新导入才会使用文件里的新增短语。

## API 参考

所有 API 路由前缀为 `/api`。若设置了 `TAVERN_API_TOKEN`，需在请求头中携带 `Authorization: Bearer <token>`。

### 游戏 `/api/game`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/classes/{system}` | 获取职业列表 |
| GET | `/classes/{system}/{class_id}` | 获取职业详情 |
| GET | `/skills/{system}` | 获取技能列表 |
| GET | `/opening-variants/{script_id}` | 获取开局变体 |
| POST | `/new` | 开始新游戏 |
| POST | `/{save_id}/action` | 提交玩家行动；可传 `expected_node_id` 阻止在存档已推进后重复提交旧行动 |
| POST | `/{save_id}/action/stream` | 提交行动（流式响应）；可传 `expected_node_id`，断线重试应沿用原回合 ID，冲突返回 409 |
| | | 行动可传 `action_id`；已结算的相同 ID 在同一存档内返回 409，重试须沿用原 ID |
| POST | `/{save_id}/abort` | 停止当前流式回合；无运行中的流时返回 409 |
| POST | `/{save_id}/inject` | 向当前流式回合插话（`stream_id`）；回应 `agent_plan` 时同时传该事件的 `plan_id`，失效、重复或已有插话排队的计划回复返回 409；断流后未处理的插话不会带入下一回合 |

交互计划回复在 `/inject` 确认前先写入存档的待处理行动记录。服务重启后，前端用原 `action_id`、`stream_id` 重提行动；只有行动参数、起始节点、重新生成的计划和问题都一致时才自动带入已确认的回复，否则显示新计划要求重新选择。此机制是重新推演并恢复回复，不保存 Agent 的模型上下文或工具执行进度，也不恢复普通插话。
| GET | `/{save_id}/state` | 获取当前游戏状态 |
| GET | `/{save_id}/tree` | 获取世界树结构 |
| GET | `/{save_id}/tree/{node_id}` | 获取指定节点 |
| POST | `/{save_id}/branch/{node_id}` | 回溯到指定节点 |
| POST | `/{save_id}/toggle-dice` | 切换骰子系统开关 |
| GET | `/{save_id}/history` | 获取游戏历史 |
| GET | `/{save_id}/snapshot` | 在同一会话锁内返回 `state`、`node_id`、`history`，用于一致地加载存档 |
| GET | `/{save_id}/adventure-log` | 获取冒险日志 |
| GET | `/{save_id}/summary` | 获取历史摘要 |
| POST | `/{save_id}/summary/freeze` | 冻结历史摘要 |
| POST | `/{save_id}/use_item` | 使用道具 |
| POST | `/{save_id}/interact` | 交互操作 |
| POST | `/{save_id}/talk/{npc_id}` | 与 NPC 对话 |
| POST | `/{save_id}/talk/{npc_id}/stream` | 与 NPC 对话（流式响应） |
| GET | `/{save_id}/npc-schedules` | 获取 NPC 日程 |
| POST | `/{save_id}/regenerate` | 重新生成 AI 回复 |
| POST | `/{save_id}/regenerate/stream` | 流式重生成；完整重生成保留节点 ID 和备选回复，断流不覆盖存档 |
| POST | `/{save_id}/swipe/{direction}` | 切换备选回复（left/right） |
| GET | `/{save_id}/swipes` | 获取备选回复列表 |
| POST | `/{save_id}/swipe/jump` | 跳转到指定备选回复 |
| POST | `/{save_id}/continue` | 继续生成 |
| POST | `/{save_id}/authors-note` | 设置作者注记 |
| POST | `/{save_id}/negative-prompt` | 设置负面提示 |
| POST | `/{save_id}/logit-bias` | 设置 logit 偏置 |
| POST | `/{save_id}/switch-persona` | 切换角色预设 |
| POST | `/{save_id}/npc-dialogue` | NPC 对话交互 |
| POST | `/{save_id}/undo` | 撤销上一回合 |
| GET | `/{save_id}/story-tree` | 获取故事树状态 |
| GET | `/{save_id}/story-tree/active-choices` | 获取地图可操作的剧情选择（仅含选择名称、ID 和提示，不含锁定分支或事件详情） |
| POST | `/{save_id}/story-tree/choice` | 提交故事树选择；地图附 `summary_only=true` 仅返回成功状态，不返回完整剧情树或事件通知 |
| POST | `/{save_id}/deduce` | AI 推理 |
| GET | `/{save_id}/shop/{location_id}` | 获取商店信息 |
| POST | `/{save_id}/shop/buy` | 购买道具 |
| POST | `/{save_id}/shop/sell` | 出售道具 |
| POST | `/{save_id}/unlock-skill` | 解锁技能 |
| POST | `/{save_id}/newspaper` | 获取新闻报纸 |
| POST | `/{save_id}/newspaper/regenerate` | 重新生成报纸 |

两个 Swipe 写接口可选传入查询参数 `expected_node_id`、`expected_swipe_index`、`expected_total_swipes`；与当前回合不一致时返回 HTTP 409，不修改存档。省略参数的旧客户端仍可使用。

### 数据银行 `/api/game`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/{save_id}/databank/upload` | 上传文档 |
| GET | `/{save_id}/databank` | 列出数据银行文件 |
| DELETE | `/{save_id}/databank/{file_id}` | 删除文件 |
| POST | `/{save_id}/databank/scrape` | 抓取网页存入数据银行 |

### 剧本 `/api/scripts`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 列出所有剧本 |
| GET | `/{script_id}` | 获取剧本内容 |
| POST | `/` | 创建剧本 |
| PUT | `/{script_id}` | 更新剧本 |
| DELETE | `/{script_id}` | 删除剧本 |
| GET | `/{script_id}/export` | 导出剧本文件 |
| POST | `/import` | 导入剧本文件 |
| POST | `/{script_id}/validate` | 验证剧本结构 |
| POST | `/ai-generate` | AI 辅助生成完整剧本 |
| POST | `/ai-generate/tab` | AI 辅助生成剧本单个标签页 |
| POST | `/ai-generate/fields` | AI 辅助生成剧本指定字段 |
| POST | `/ai-generate/fields-batch` | AI 批量生成剧本字段 |
| POST | `/ai-generate/optimize-relations` | AI 优化关系网络 |
| POST | `/ai-generate/optimize-events` | AI 优化事件系统 |

文件剧本首次开局时会注册到数据库；此后同 ID 剧本优先使用数据库版本，若手工修改文件需重新导入或更新数据库中的剧本。

### 存档 `/api/saves`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 列出所有存档 |
| GET | `/{save_id}` | 获取存档详情 |
| DELETE | `/{save_id}` | 删除存档 |
| GET | `/{save_id}/export` | 导出存档文件 |
| POST | `/import` | 导入存档文件 |

新建存档会单独保存开局时生效的剧本配置（角色、NPC 和设置覆盖）；恢复与导出/导入均使用该配置，后续修改原剧本不会改变已有存档。旧存档仍从原剧本恢复。

### AI 配置 `/api/config`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 获取当前活动配置 |
| PUT | `/` | 更新配置（兼容旧格式） |
| POST | `/test` | 测试当前活动配置 |
| GET | `/profiles` | 列出所有配置 |
| POST | `/profiles` | 创建配置 |
| PUT | `/profiles/{id}` | 更新配置 |
| DELETE | `/profiles/{id}` | 删除配置 |
| POST | `/profiles/{id}/activate` | 激活配置 |
| POST | `/profiles/{id}/test` | 测试指定配置 |
| GET | `/presets` | 获取提供商预设列表 |
| GET | `/pipeline-mode` | 读取新游戏默认推演模式；传入 `?save_id=...` 读取指定存档的模式 |
| POST | `/pipeline-mode` | `{"mode":"agentic","save_id":"..."}` 只修改指定存档；省略 `save_id` 只修改新游戏默认值 |

推演模式支持 `workflow`（流程）、`agentic`（双 Agent）和 `agentic_unified`（统一 Agent）。游戏中切换只影响当前存档，立即写入当前节点快照；返回历史分支时恢复该节点记录的模式。推演或后台结算进行中返回 HTTP 409，前端不会显示虚假的切换成功。默认值只在当前服务进程内生效；启动默认值可通过 `PIPELINE_MODE` 环境变量设置。

统一 Agent 的回合流程为「检索剧本 → 提交正文 → 结算 → 持久化 → `final`」。`write_narrative` 提交的正文会先通过 SSE 展示，后续改稿用 `narrative_revised` 替换，地图和正式状态仍以持久化成功的 `final` 为准。`delegate_settlement` 将工作交给结算 Agent，但结算仍在当前请求内等待完成，不会留下跨回合的后台结算任务。停止、断线、模型异常或存档失败时，流式行动回滚未保存的回合，不发送成功的 `final`。提前展示的正文不代表该回合已经保存。
统一 Agent 在正文提交后并行发起一次语义审查，不阻塞首段 SSE；结算前等待审查结果。审查发现明确问题时将违规反馈给 Agent，请它修订正文后再结算；审查不可用时保留原稿，由既有确定性检查兜底。Agent 每轮最多提交三稿审查（包括主动改稿和校验反馈后的纠错），额度耗尽后拒绝未经复审的新稿；第三稿仍未通过明确违规审查则回滚本轮并丢弃流式草稿。结算时还会拒绝与送审稿不一致的正文。NPC 声音修正或委托结算若在此后改写正文，落盘前会额外审查最终稿，明确违规时回滚并丢弃流式草稿；改写稿在额外审查后才通过 SSE 展示，审查不可用时仍由确定性检查兜底。因此每回合可能增加模型调用和结算等待，不能将首段到达视为已通过审查。
统一 Agent 仍先展示计划；玩家行动明确时不为可能的剧情后果反问，计划与首批资料查询可在同一轮模型调用中提交。公开规划仅补入上一轮之前最多两轮的玩家行动与可见叙事片段，不带历史摘要或节点内部状态；提交计划后的推演上下文再带入有界的近期分支历史和已保存摘要。只有缺少执行本轮行动所必需的玩家选择时才暂停等待答复；有问题时同轮的其他工具会延后执行。

同一批工具按顺序执行并逐个报告结果；工具错误返回模型修正，只有成功的终结工具才结束循环，终结后的同批工具不再执行。普通行动的工具轨迹保存在对应新节点的 `_node_context.agent_trace`，随存档加载恢复，不写到前一回合。世界书支持全文搜索、分页摘要、按 `entry_id` 分段续读，以及用 `entry_ids` 一次读取最多 3 条独立资料（不可与搜索或偏移参数混用）；每条仍单独经过当前回合的可用性过滤，禁用条目和其他分支条目不可读。事件预览使用当前存档的调度记录与条件，`status.<id>` 对应持续状态，`event.<id>.fired` / `event_fired.<id>` 对应实际触发记录，而非仅已排程；条件支持 `AND` / `&&`、`OR` / `||`（AND 优先）及数值、时间字符串比较。
统一 Agent 的 trace 还记录 `model_calls`（前台及委托结算调用数）、`semantic_review_calls` / `semantic_review_status`（逻辑审查次数及结果）、`semantic_review_model_calls`（审查模型请求次数；无效响应会用更大预算重试一次）、`plan_waits`（等待玩家回答的次数）、`lore_queries`（世界书工具调用次数）、`lore_hits`（返回条目总数）和 `lore_entry_reads`（成功按 ID 读取的条目数；批量读取逐条计入）。`primary_model_calls` 包含审查请求。这些指标只说明资料送达了模型，不等于模型已在叙事中正确使用资料；旧存档不会回填这些指标。

### 搜索与素材 `/api/search`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/web` | 网页搜索 |
| POST | `/materials` | 保存素材 |
| GET | `/materials` | 列出素材 |
| GET | `/materials/tags` | 获取所有标签 |
| GET | `/materials/{id}` | 获取素材 |
| DELETE | `/materials/{id}` | 删除素材 |
| POST | `/materials/{id}/tags` | 添加标签 |
| DELETE | `/materials/{id}/tags/{tag}` | 删除标签 |
| POST | `/materials/search` | 搜索素材 |
| POST | `/materials/upload` | 上传素材文件 |
| POST | `/inject` | 注入素材到剧本 |
| POST | `/transform` | AI 变换素材内容 |

## 测试

```bash
python -m pip install -r requirements.txt pytest pytest-asyncio
python -m pytest tests/
node --test tests/pipeline_mode.test.cjs
```

推演模式 API 测试使用临时 SQLite 数据库和模拟 AI，不读写玩家存档，也不产生模型调用费用。

流式质量报告还逐回合保留可见的 Agent 计划、问题与自动回复（包含不需要回复的计划）；自动选择首项只用于隔离评测，不代表真实玩家的双工交互质量。








质量回放遇到已知且可见的相邻地点时会提交与地图按钮相同的精确行动文本、`intent_hint=map_travel` 和 `target_id`，验证实际位置结算；无可达目标时保持原地观察，不伪造地图抵达。
脚本模型按计划后的 `<wait_duration>` 结算明确等待，不用固定十分钟覆盖玩家要求；若真实事件打断，仍以引擎结算规则为准。

统一 Agent 的跨轮工具经验从当前分支已结算节点的 `agent_trace` 生成；重载、再生或切换分支时不依赖易失的会话缓存。

`/api/game/{save_id}/performance` 从当前剧情分支已保存的 Agent trace 汇总回合、token、工具及平均耗时；重载存档后仍可查看，切换分支后按新分支计算。页面的 Usage 明细来自当前加载会话的调用跟踪，重载后不会还原历史调用费用，不能与分支总 token 直接比较。

该接口另按有记录的回合汇总 `avg_prepare_ms`、`avg_first_text_active_ms`、`avg_agent_model_ms`（每回合 Agent 模型调用耗时合计）、`avg_semantic_review_ms`（每回合语义复核耗时合计）及 `avg_preflight_retrieval_ms`（准备阶段检索），并在存档性能面板展示可用指标。旧 trace 或非流式回合没有相应数据时返回 `null`，而不是将缺失视为零；各阶段可能并行或重叠，不能相加成总耗时。

Agent trace 的 `duration_ms` 仅计 Agent 管线；`prepare_ms` 从会话开始处理行动到路由和检索完成，流式 `first_text_ms` 从同一起点到首段非空正文发出（包括等待玩家确认计划的时间）。`preflight_timings_ms` 拆分后台任务、意图预判、上下文构建、路由和检索耗时，其中路由与检索并行，不能直接相加。这些均为服务端计时，不含网络传输和浏览器渲染；非流式回合没有首段文本事件。当前页面的 Agent trace 另显示从点击提交到首段 SSE 正文被处理的「前端首段事件」，以及双帧回调估算的「首段入屏约」；两项不写入存档，后台标签页受浏览器帧节流影响，仅用来区分传输、调度和服务端等待。
隔离质量报告的「SSE 客户端首段接收」从发起请求到测试客户端收到首个非空正文事件，包含传输和测试客户端调度，但不含浏览器渲染；没有独立正文事件时不记为零。可与服务端 `first_text_ms`、`first_text_model_calls` 对照，勿将两项时间相减直接解释为网络耗时。
`retrieval_timings_ms` 在执行相应阶段时记录索引写入等待、历史向量查询和 Data Bank 查询的耗时；Data Bank 与索引等待、历史向量查询并行，不能将三者直接相加。未启用的阶段不出现。可先用此拆分判断首段文本前的等待来自哪里，再决定是否调整索引或检索策略。
统一 Agent 的流式行动另发送 `preflight` 准备期事件（后台任务、意图判断、路由与检索），大地图活动栏可在首段叙事前显示当前阶段；事件只包含固定阶段名，不包含剧本或工具参数。
OpenAI 兼容模型的流式工具回合可在参数生成期间发送 `tool_preparing`，大地图仅显示通用准备状态，不公开参数、工具名或未审核正文；工具执行仍须等待完整响应。若接口不支持 `stream_options` 则退回原有非流式工具调用。这个状态改善等待反馈，不等同于缩短首段正文耗时。
后台任务超过 1 秒时，服务端日志记录任务类型和耗时；等待超时取消时记录任务类型。可结合 trace 的 `preflight_timings_ms.background` 判断具体阻塞来源，日志不包含剧本正文或工具参数。
服务正常关闭时，会先等待缓存中的状态后台任务与待写回任务，把活动节点写入 SQLite，再关闭连接池；缓存淘汰也会等待状态任务完成，不使用行动阶段的 10 秒取消策略。强制结束进程或断电仍无法保证未完成结算落库；关闭时写库失败会记录存档 ID 并报告错误。
普通行动先等待会修改游戏状态的后台任务；纯向量索引写入可与意图判断和路由并行，但语义检索前会等待写入完成。查询、分支切换、撤销及重新生成仍等待全部任务，避免读取未完成的索引或切换后写入旧分支。
索引等待超过 10 秒时，行动本轮跳过向量检索，继续使用关键词资料和 Data Bank；索引写入保留供后续回合使用。分支切换、撤销及重新生成等需要完整索引的操作会继续等待写入结束，而不会取消仍在运行的线程写入。
大地图中原样确认的「前往地点」行动会携带 `intent_hint=map_travel`；服务端仅在目标仍是当前存档可见地点且文本未变时跳过意图分类。同区 NPC 的原样「寻找并尝试接触」草稿携带 `intent_hint=map_npc_search`；服务端仅在当前位置、人物身份和未接触状态均匹配时跳过分类，寻找是否成功仍由完整推演决定。编辑后的行动与查询、重写、撤销仍按原流程分类。
隔离浏览器测试的脚本化 Agent 对成功抵达暂按 30 分钟推进，仅用于避免固定 10 分钟的测试假象，不代表路线的真实耗时，也不是游戏引擎的最低耗时规则。
统一 Agent 的 trace 另记录 `lore_candidate_ids`：玩家行动和路由检索词匹配到的待核实资料 ID，不代表正文已进入上下文；`lore_retrievals` 记录 Agent 实际查询结果。
`peek_upcoming_events` 的 `events` 只包含当前条件满足的计划事件；`conditional_candidates` 单列当前未满足条件但可能在行动中解锁的事件。两者均未发生，也不代表主角知情；工具结果分别给出匹配数和截断标志。
明确写出“等到明天/次日/翌日 8 点”等数字钟点时，等待按次日时间估算；没有次日标记的已过钟点不会自动推断到次日。明确等待超过 12 小时可结算到玩家请求的时刻，普通行动仍受原有跳时上限约束。`peek_upcoming_events` 最多预览 6 小时，但行动准备仍将整个预估等待窗口内的候选事件交给 Agent 推演；候选事件不等于已发生或角色已知。

公开版的合成 harness 验收使用本地假模型和临时 SQLite，不读取私有剧本，也不调用外部模型。测试连接真实的流式行动路由、计划答复与插话、工具检索、事件结算、保存和重载，并检查同一行动重放不会重复结算、公开进度不会携带工具参数或结果。它证明控制与持久化契约，不代表真实模型的叙事质量或浏览器交互已验收。

从叙事视图切入大地图时，进度列表定位到最新事件，避免停在隐藏期间留下的旧状态；地图已经打开时，不因视图恢复而重置玩家查看历史的位置。

```powershell
python -m pytest tests/test_agent_harness_execution.py -k retrieval_settlement_stream_and_reload -q
node --test tests/main_map_activity_ui.test.cjs
```

## 技术栈

- **后端**: FastAPI + Uvicorn + aiosqlite (SQLite WAL 模式)
- **前端**: 原生 HTML/CSS/JavaScript
- **AI**: OpenAI SDK / Anthropic SDK / Ollama (httpx)
- **搜索**: DuckDuckGo + BeautifulSoup
- **向量检索**: FastEmbed + ChromaDB（可选）
