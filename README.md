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
| POST | `/{save_id}/regenerate/stream` | 流式重生成；完整重生成保留节点 ID 和备选回复，保存前失败回滚并反馈错误 |
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
| GET | `/{save_id}/story-tree` | 获取故事树状态及来源回合 `base_node_id` |
| GET | `/{save_id}/story-tree/active-choices` | 获取地图可操作的剧情选择及来源回合 `base_node_id`（不含锁定分支或事件详情） |
| POST | `/{save_id}/story-tree/choice` | 提交故事树选择；可传 `expected_node_id` 校验来源回合，地图附 `summary_only=true` 仅返回成功状态 |
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

流式重生成进入 SSE 后，`all`、`state`、`choices` 阶段在准备、生成或保存失败且尚未持久化时，保留原回合及备选版本并释放控制与存档锁。未预期异常通过 SSE `error` 返回通用提示和 `discard_partial: true`，不向玩家暴露内部异常文本，也不发送成功 `final` / `[DONE]`；既有结算校验提示保留。未生效的插话和停止请求会明确反馈。客户端恢复原回合和选项，地图提示重生成失败而非把保存错误误报为校验失败。取消或关闭流仍走原有回滚，不转成服务器错误；成功持久化后的传输故障不冒充未保存。

`tests/test_regenerate_stream_errors.py` 及相关前端测试使用假模型、合成数据、临时 SQLite 和 DOM 单元环境验证错误反馈、回滚、控制清理及重试，不代表真实模型、已运行服务或浏览器体验验收。

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
明确写出“等到明天/次日/翌日 8 点”等数字钟点时，等待按次日时间估算；没有次日标记的已过钟点不会自动推断到次日。明确等待超过 12 小时可结算到玩家请求的时刻，普通行动仍受原有跳时上限约束。`peek_upcoming_events` 每次预览 1–24 小时，可通过 `start_after_hours` 查看后续窗口；行动准备仍将整个预估等待窗口内的候选事件交给 Agent 推演，候选事件不等于已发生或角色已知。

工具批次执行期间收到新插话时，尚未执行的旧调用（包括结算）暂缓，模型先读取插话和已执行工具结果，再决定后续动作；不会强行取消正在执行的写工具，也不会自动撤销已完成的候选修改。最后一轮仍可在共享的额外三轮预算内重新推演，预算用尽则不执行过时结算。工具启动事件让出执行权后，也会再次检查停止请求，避免已接受停止却仍开始调用工具。

终结工具仍在等待叙事审查时，插话会中断旧审查并交回 Agent 重新推演；已开始的调用记为失败，而不是从工具记录中抹去。只有模型实际读取补充后才发送 `user_inject`，补充内容同时进入新稿审查，旧稿不能沿用旧审查通过状态直接结算。普通状态写入和历史查询不会被插话强制取消。终结工具正式接收后关闭插话入口，后续 `/inject` 返回 409，停止入口仍保留；避免在已开始结算时接受无法处理的新指令。

公开版的合成 harness 验收使用本地假模型和临时 SQLite，不读取私有剧本，也不调用外部模型。测试连接真实的流式行动路由、计划答复与插话、工具检索、事件结算、保存和重载，并检查同一行动重放不会重复结算、公开进度不会携带工具参数或结果。它证明控制与持久化契约，不代表真实模型的叙事质量或浏览器交互已验收。

旧式 `event/action/params` 触发器转换为统一事件节点时，也保留 `reveal_lore`、`schedule_override`、`activate_persistent_state` 和 `deactivate_persistent_state` 的效果；静态声明与事件动态添加的声明走同一机制。这里的旧式 `reveal_lore` 保留原有“启用词条”语义，不自动写入玩家已发现词条。合成验收覆盖动态声明经真实 `after_ai` 生命周期结算后影响 NPC 日程和持续状态、保存重载、分支往返与行动重放不重复执行。

一次性与周期事件的 `effects` 也可直接声明 `type: "schedule_override"`，使用 `npc_id`、`time_range`、`location`，以及可选的 `activity`、`priority`、`expires`、`condition`。定时事件的候选推演与正式结算复用通用触发器的日程覆盖执行器；未到时间、事件触发条件未满足或缺少必需字段时不追加覆盖。每次实际触发按声明追加，查询沿用既有优先级与到期规则，显式 NPC 位置仍优先于日程，不把日程声明当作已发生的移动或玩家已知情报。

事件或触发器自身的 `condition` 决定何时追加覆盖；覆盖效果中的 `condition`（触发器写在 `params.condition`）保留表达式原文，在每次查询时按所查询快照求值。覆盖条件不满足时回退到其他有效日程，恢复满足后可再次生效，无需重新触发事件；不填写或空字符串保持无条件语义。`tests/test_conditional_schedule_overrides.py` 与接触持久化回归使用合成声明、假模型和隔离 SQLite，覆盖条件切换、候选撤回、到期、失败回滚、重载、分支及请求重放，不代表真实模型或浏览器体验验收。

`tests/test_scheduled_npc_overrides.py` 使用两类事件、不同声明门槛、假模型和隔离 SQLite，验证线索读取、完整事件规则查询、准备与等待、候选词条读取、正式日程效果、保存失败回滚及重试、重载、分支往返与请求重放。重复候选查询不污染正式状态；已触发事件重放不重复追加覆盖，周期事件在各自触发时刻正常追加。规则及词条正文标记不进入公开 SSE 或保存的 trace；这些测试不代表真实模型的自主行动、NPC 交互质量或在线体验已验收。

候选事件清单、词条查询、接触预测与剧情树候选的定时部分共用正式 `_settle_timed_events`，在隔离会话副本中按事件时刻应用声明效果及触发器，不再分别手写简化效果或事后重放词条变更。因此，词条启用、动态新增等效果可在同一时刻或后续时刻满足连锁事件条件，Agent 读取的候选资料、NPC 日程和接触结果与该定时结算范围一致。候选词条视图读取副本产生的内容覆盖、删除记录与启用状态；不写回正式存档、不更新真实向量索引。

`tests/test_timed_candidate_consistency.py` 覆盖事件效果、动态触发器与动态词条三种声明入口、未到时刻和连锁时刻；合成流式 Agent 根据工具回执决定是否交谈，并验证撤销后重查、重新接受时间、保存失败回滚及重试、事件词条重载、回到旧分支和行动重放。候选仍不是完整回合预测：不执行事件掷骰、剧情节点激活、完整生命周期或实际外部模型调用；角色知识仍沿用已提交状态的限制。这些证据不代表真实模型推演质量或浏览器体验验收。

条件式 `condition: event.<name>.fired` 与 `actions` 声明也会响应 `before_generation` 和 `after_ai`，沿用旧触发器的动作执行顺序，不重复执行已由统一事件引擎处理的事件型声明。准备阶段的注入指令在公开计划提交后进入统一 Agent 的私有执行上下文，变量宏按当前分支状态展开。无来源通知仍受 `visibility_policy.require_notification_source` 约束；内部 `narrative_callbacks` 保存在存档中，但不进入玩家状态、公开节点或最终 SSE。

两回合合成验收覆盖事件动态声明、下一回合实际模型请求中的指令与变量值、缓存续玩、SQLite 重载、分支往返、生成失败回滚和重试，以及同一行动重放不重复执行；不以手工触发生命周期代替完整回合验证。

进行中的剧情词条由目标存档的活跃节点及当前剧本声明重建，重载不依赖旧进程内存，切换分支会移除旧分支残留。静态与动态剧情树、独立事件引擎沿用同一词条构造逻辑；恢复时不重跑生命周期、不推进计时、不重复奖励或通知。有效的词条内容覆盖、禁用与删除继续生效，已结束或已移除节点的旧进行中词条覆盖不会复活；向量索引随目标分支同步。

`tests/test_active_story_lore.py` 使用合成声明验证上述边界，并让假 Agent 在真实流式路由中读取重载或分支恢复后的词条，再据此生成行动叙事；覆盖保存失败回滚及重试、请求重放，以及词条正文不进入公开 SSE 或持久化 trace。这不等于真实模型推演质量、外部向量服务或浏览器体验已验收。

从叙事视图切入大地图时，进度列表定位到最新事件，避免停在隐藏期间留下的旧状态；地图已经打开时，不因视图恢复而重置玩家查看历史的位置。

大地图中，长工具日志溢出时，插话输入与停止按钮吸附在日志区域底部，避免操作入口被挤出可视区；此布局仅作用于地图 Agent 面板。

```powershell
python -m pytest tests/test_agent_harness_execution.py -q
node --test tests/main_map_activity_ui.test.cjs tests/turn_overflow_ui.test.cjs
```

事件和剧情树的 `add_lore`、`update_lore`、`remove_lore` 以及事件引擎的词条同步结果，按分支保存在节点快照中；重载、回退和再生时恢复对应版本的正文、关键词和可见性元数据，删除结果不会被动态剧情重新加载覆盖。向量索引随修改和分支恢复顺序更新，查询仍以当前分支词条正文为准；词条覆盖记录与动态剧情原始内容不进入玩家状态或节点响应。

`tests/test_event_lore_persistence.py` 使用合成事件验证静态、动态词条的 SQLite 重载、分支往返、预览隔离、修改顺序和索引同步；流式 harness 另覆盖激活、添加和改写词条的完整工具到存档链路。这些测试不调用外部模型，也不代表真实叙事质量已验收。旧存档在此修复之前未保存的词条变化无法自动还原，不会通过重复触发旧事件补写。

动态变量、触发器和随机项的定义差异通过 `mechanics_overrides` 保存在节点快照中，事件效果与导演更新共用这套通用机制。重载、分支切换、再生和失败回滚恢复对应版本的规则，加载历史日志只恢复定义，不覆盖快照中的变量现值。动态触发器保留追加顺序，更新同步到实际事件执行缓存；覆盖记录不进入玩家状态或节点响应。剧本专属规则应由剧本声明及通用事件效果加载，而非写入引擎的剧本特判。

新快照的机制恢复不依赖导演日志是否被裁剪；旧快照兼容仍保留的规则更新日志，但已被裁剪且从未保存的定义无法自动还原。`tests/test_mechanics_persistence.py` 验证变量边界、规则幂等、生命周期触发、SQLite 重载、分支往返和失败回滚；流式 harness 同时验证工具驱动的事件结算、动态机制保存与恢复。这些证据来自合成数据与假模型，不代表真实模型的剧本利用或长期叙事体验已验收。

统一 Agent 的完整推演上下文只提示当前分支有哪些机制类别及其数量，不自动装入全部规则正文。计划阶段结束后，可通过 `consult_rules(category="variables", query="关键词")` 按需查询变量定义与现值；另支持 `world_properties`（世界属性及现值）、`triggers`（触发器）、`random_items`（随机项）、`one_time_events`（一次性事件）、`cyclic_events`（周期事件）、`persistent_states`（持续状态）、`story_nodes`（剧情节点）、`locations`（地点完整声明）。事件预览只有简短提示，需核对完整条件、效果和规则说明时，可按事件、节点或地点 ID 搜索对应类别，不必重复把规则写进世界书。不传 `category` 时保留原有 `question` 问答检查。

世界属性以剧本 `world_properties` 的精确 ID 声明为准；工具反馈、候选更新与最终结算均允许已声明的物件机制，或仅在 ID 中包含物件词的属性。未声明的物件属性仍被过滤，已有宏观属性更新方式保留。`consult_rules(category="world_properties")` 返回完整定义及当前分支已提交值，支持搜索、分页和长正文续读，不把候选更新当作已提交。世界属性更新引起的 `conditional_connections` 使用对应的候选或结算快照重新计算，不提前改动正式地图。`tests/test_world_property_declarations.py` 使用合成声明和假模型验证规则读取、条件事件、地图连接、流式回合、SQLite 重载、分支往返、请求重放和公开 trace 裁剪，不代表实际模型体验已验收。

持续状态以剧本 `persistent_states` 的精确 ID 声明为准；名称包含天气、时段词不会阻止激活。工具反馈、候选状态与最终结算共用同一过滤规则，未声明的环境状态仍受保护。若剧本显式声明了内置天气状态的 ID，这些 ID 也由剧本规则控制，内置天气维护不再自动增删它们；未声明时保留原有天气行为。

同一回合多次调用状态工具时，持续状态按已接受工具调用的先后顺序结算，停用后再激活不会被较早的停用覆盖，结果与候选投影一致。单次调用仍先处理 `activate_states` 再处理 `deactivate_states`；失败、撤销、延后和被替代的统一 Agent 记录不参与最终结算。顺序仅保存在内部结算数据中，不改变剧本或存档格式，也不进入公开流和 trace。`tests/test_persistent_state_order.py` 覆盖重复开关、交错状态、旧解析结果及过滤边界；`tests/test_persistent_state_declarations.py` 以合成声明和假模型验证先停用再激活后的条件事件、完整流式回合、SQLite 重载、分支往返和请求重放，不代表真实模型或线上体验已经验收。

普通 `state_changes` 的候选更新与正式结算共用逐项计算路径：连续 `set` 按前一步应用且经过属性范围约束后的值计算限幅，单次工具内和多次工具间保持一致。单项数值变更的既有 30 点限制、`add/subtract` 的 `reason` 要求及剧本属性 `min/max` 仍有效；直接执行的剧本效果不新增这层 Agent 限幅。`tests/test_resource_state_order.py` 覆盖顺序、正负值、小数、属性边界、路径别名、无效记录、旧解析结果及重生成结算；两组完整合成流式回合由假模型读取词条中的操作值和事件声明，验证候选资料解锁、最终条件事件、SQLite 重载、分支往返、请求重放及公开 SSE/trace 脱敏，不代表真实模型自主推演或在线体验已验收。

`npc_location_changes` 与 `room_changes`（含玩家房间）的候选投影和正式结算复用空间状态应用逻辑，普通属性路径显式覆盖 NPC 位置或房间时也同步清理日程派生标记。跨工具累计变更保持正式结算的类别顺序：普通属性、NPC 地点、房间；不是按每次调用逐步推进世界时间。非法地点、未知 NPC 和空房间更新仍按原规则忽略，不创建新实体。`tests/test_spatial_state_projection.py` 覆盖单次/分次更新、混合属性路径、派生位置、被排除记录、实际撤销后的资料重新锁定及重生成结算；两组完整合成流式回合读取声明中的地点和房间，验证候选资料解锁、正式事件、SQLite 重载、分支、重放和正文脱敏。离场 NPC 的空间信息仍不向玩家公开。本轮不扩展接触状态、场外 NPC 更新或日程推演；合成测试不代表真实模型或在线体验已验收。

玩家 `location_change` 的候选投影与正式结算也复用移动逻辑：按正在结算的状态检查地点声明中的 `access_condition`，拒绝进入时保留原地点和房间；跨地点成功移动才重设 `default_room`，同地点保留房间，成功后清除旧的通行阻止信息。多次移动取最后一个通过地点发现校验的目的地，不把中间地点当作已经到达；既有的先揭示后移动校验顺序不变。变量、普通属性、持续状态和世界属性先于通行检查，专用 `inventory_changes` 仍在通行检查之后，显式 `room_changes` 最后应用。重生成和副本结算使用传入状态，不借用当前分支的门槛值。`tests/test_player_location_access.py` 用合成声明验证允许与拒绝进入、默认房间、累计更新、实际撤销、重生成结算，以及两组完整流式回合中的规则读取、候选资料、正式事件、SQLite 重载、分支往返、行动重放和正文脱敏；不代表真实模型、在线服务或完整重生成流式接口已验收。

玩家移动或房间更新被接受后，工具反馈候选地点、房间和 `movement_status`：`blocked` 表示本次通行受阻，`ready` 仅表示候选可通行、不是已经抵达，`none` 表示无待处理移动，`not_applied` 表示请求未应用。反馈直接读取同一投影的有效目的地和结果，不另做一套通行模拟，不把基线中的旧拒绝信息当成新阻止。后续变量、普通属性、持续状态或世界属性更新会重算原待移动，无需再次提交 `location_change`；撤销和上下文压缩保留最新候选，不保留被撤销的成功或失败。候选不包含尚未结算的事件效果，也不等于主角已知。

`consult_rules(category="locations", query="地点ID")` 可读取完整 `access_condition`、`access_blocked_reason`、`default_room` 和自定义声明，即使地点没有描述或尚未被玩家发现。查询只用于 Agent 核对规则，不揭示地点、不移动玩家、不结算效果，也不新增地点的 `runtime` 或 `include_candidates` 视图。长声明复用规则分页、字段读取和快照失效机制。空间反馈附带 `rule_query`，原因过长时用 `reason_truncated=true` 标记；可按条目快照和 `result_path=["definition", "access_blocked_reason"]` 完整续读。工具结果裁剪保留完整空间 JSON，必要时省略原因并标记；极小预算放不下身份字段时用 `feedback_omitted=true` 明确说明不可据此判断通行成功，而不是留下半截 JSON。

`tests/test_player_spatial_feedback.py` 和 `tests/test_player_access_recovery.py` 使用合成声明与假模型，验证候选反馈、累计补条件、撤销、裁剪及规则续读，并以变量和世界属性两种门槛跑通“尝试受阻 → 根据反馈查询地点完整声明 → 读取声明指定的操作词条 → 补条件 → 原待移动可行 → 候选资料解锁 → 正式结算”。完整流式回合另检查 SQLite 重载、分支往返、请求重放与公开 SSE/trace 正文脱敏。已正式结算的拒绝原因仍可作为既有玩家提示，本轮不改变这一行为；合成测试不代表真实模型自主纠错、叙事质量或在线体验已验收。

`story_nodes` 直接读取当前剧情树运行时已接受的定义，覆盖静态节点、当前分支动态节点及兼容转换的系统节点；不会旁路读取已被拒绝、未规范化或已离开分支的声明。每条 `definition` 含 `tree`（除节点列表外的树元数据）与 `node`（完整节点），保留前置、条件、选项、效果及作者自定义字段，不重复装入兄弟节点正文。这是现有引擎规则的只读视图，不是额外执行一次系统触发器或剧情效果的授权。

查询结果标记 `source="current_branch"`，反映运行时定义及变量、世界属性现值，不模拟本轮尚未提交的 `update_state` 或未结算事件，也不代表规则已执行、事件已发生或角色知情。每页最多 5 条，携带顶层 `snapshot` 和原 `query`，按顶层 `next_offset` 翻页；每条 `content` 是含 `definition` 和可选 `current_value` 的 JSON 文本，最多返回 2000 字符，长内容用该条的 `rule_index`、`snapshot` 和 `next_offset` 续读后拼接解析。指定 `rule_index` 时，即使 `offset=0` 也必须携带条目快照；列表与条目的快照不可混用。

上下文压缩省略规则正文时，保留类别、条目索引、快照及阅读位置，并明确提示携带条目 `snapshot` 从 `offset=0` 重新完整读取，避免只保留尾段而漏掉前置约束。`tests/test_mechanics_retrieval.py` 覆盖七类规则、搜索分页、长定义与长变量值续读、旧式无 ID 触发器、参数边界、分支恢复和压缩后的重读指引。流式 harness 验证规则内容真正进入后续模型请求、待结算机制不会提前出现在结果中，以及内部规则标记不进入 SSE 或公开 trace；这些仍是合成数据和假模型证据，不是实际模型叙事质量验收。

同批多条规则查询超过 Agent 上下文预算时，硬限长也保留合法 JSON、类别、全部返回条目的索引与快照、列表分页与正文阅读坐标；省略正文的结果标记 `truncated=true`，条目不再提供 `content`，并要求携带条目快照从 `offset=0` 重读。若元数据本身也超出预算，返回 `metadata_omitted=true`，明确要求按原查询重新取列表，不保留不能安全使用的半套坐标。未超预算的结果不变。`tests/test_agent_rule_context.py` 使用八类合成规则验证转义字符下的实际消息长度、重复压缩后的坐标保留，以及真实工具 schema 和 Agent 预算下假模型重新完整读取声明、据声明提交变量更新的链路；隔离 SQLite 的完整流式回合另验证保存重载、请求重放不重复执行，以及规则正文不进入 SSE 或保存的公开 trace。这些仍是合成数据和假模型证据，不代表真实模型叙事质量或在线服务已验收。

世界书 `query_lorebook` 初查可省略 `snapshot`；旧调用方须为 `page>0` 的搜索翻页携带顶层快照和原关键词，为 `offset>0` 的正文续读携带该条目的快照。搜索、批量结果与精确条目快照不可混用；搜索/批量结果中的条目快照可用于该 ID 的精确读取，但搜索摘要不等于从头读过正文。快照绑定当前会话世界树、节点、时间、读取视图、词条内容和可用关联；候选视图另绑定已接受的状态更新，玩家查询与内部读取不共用快照。搜索快照还绑定原查询及完整匹配集合；无关词条的正文变化不使精确读取失效。数据库重载会重建世界树身份，因此旧读取快照不能跨重载使用；本功能不改变存档格式。

续读未带快照返回 `snapshot_required`，内容、可用性或作用域不匹配返回 `snapshot_expired`；两者均带 `restart_required=true` 和从头重查的 `restart`。调用方必须丢弃旧片段，重新读取后再拼接同一快照的内容，不能把失效解释为词条不存在。超预算时同样返回合法 JSON：`content_omitted=true` 保留可容纳的 ID、快照、可见性、正文坐标、关联 ID、搜索分页、近似匹配与候选投影；这些坐标不代表已读。只需当前相关段落时，按条目 `id`、当前 `offset` 和条目 `snapshot` 重读，不直接跳到 `next_offset`；需要完整阅读时仍从 `offset=0` 开始。元数据或错误恢复参数也过长时返回 `metadata_omitted=true`，要求按原查询从头重查、不传旧快照，不伪造空结果；重复压缩保留搜索进度、快照失效及重查提示。

模型实际收到的统一系统提示、共享世界书工具参数和限长反馈使用相同读取约定：`entry_id` 取结果中的 `id`，定位用当前 `offset`，而不是直接跳到下一段；完整阅读才从0开始。定位片段和 `next_offset=null` 都不能证明全文已读。`tests/test_lore_recovery_guidance.py` 核对编译后的系统提示及三套工具说明，并在显式2800字符消息预算下省略合成搜索正文，验证已提交/候选视图、直接恢复/重复压缩四组原生 Agent 循环仅凭当次消息定位读取声明并提交候选变量更新；另覆盖精确续读、批读省略及元数据完全省略后的重查指引。这些是假模型的契约与恢复测试，不证明真实模型会自主选择正确段落，也不替代完整回合或线上验收。

`tests/test_lorebook_snapshots.py` 和 `tests/test_agent_lore_context.py` 使用合成世界书、假模型及隔离 SQLite，覆盖快照边界、视图隔离、上下文预算，以及事件改写资料后拒绝旧坐标、重读关联声明、据新规则结算、保存重载、分支往返与请求重放；公开 SSE/trace 不含内部正文标记或读取快照。流式夹具在请求外拼接正文片段，不能据此宣称真实模型仅凭当前消息就能独立完成全部读取；这些证据也不代表实际模型检索质量或在线体验已验收。

世界书多关键词搜索摘要检查各词在正文中的后续出现，以命中位置前最多80字符为起点，优先返回400字符窗口中完整覆盖更多不同检索词的片段；重复同一词或同一日期的不同写法不增加覆盖数，同等覆盖优先较长词、再取较早位置。日期写法归一化保留，大小写折叠后的命中仍转换为原文坐标。词条排序、分页数量、正文读取上限、Agent 消息预算和轮数不变；没有缓存全文，也不保证语义理解或穷尽全文规则。可用返回的 `id`、`offset` 和条目 `snapshot` 精确读取相关段落，不能把搜索片段当成已读完全文。

上下文压缩的世界书搜索摘要保留各命中条目的 ID、原文定位、条目快照、可见性和可用关联，以及原查询、搜索翻页快照、近似匹配与候选投影标记，不复制正文。定位坐标只用于重读相关段落，不是完整阅读进度；完整阅读仍须从 `offset=0` 开始。条目快照不能用搜索快照替代，压缩后的坐标仍接受既有快照校验；失效后须按原查询从 `page=0` 重查，不携带旧快照。`tests/test_lore_search_compaction.py` 覆盖重复压缩、多页书签、近似匹配、候选视图，以及正文、时间和候选变更或撤销后拒绝旧坐标并重新检索。

`tests/test_lorebook_focus.py` 覆盖中英文长正文的共同主题定位、重复词、日期别名、窗口边界和原文偏移。`tests/test_agent_lore_focus.py` 另用超过实际消息预算两倍的合成词条验证“搜索 → 定位读取 → 从正文发现关联 ID → 读取声明 → 通用变量操作 → 保存重载”；中英文假模型只根据当次消息决定查询坐标、关联 ID 和操作值，不在请求外拼接或积累正文。无压缩两组在既有预算内用7次主 Agent 请求、不到3000字符的工具正文完成回合。压力两组额外批读18条合成噪声词条，触发原生 Agent 上下文压缩，确认原搜索工具消息消失后，仅从当前搜索摘要恢复定位并继续，用8次主 Agent 请求完成回合；压力组的全部工具正文不受上述3000字符描述约束。四组均验证分支往返、请求重放不重复结算及公开 SSE/trace 脱敏。这补充了按需读取和压缩后定位恢复的离线证据，不改变前述旧夹具的证据边界，也不代表真实模型检索质量、完整规则理解或在线体验已验收。

导演新增的一次性和周期事件保存在当前节点的 `dynamic_story_content` 声明集合中，不再像生成日志一样按条数裁剪；重复 ID 不重复保存，也不能覆盖已有事件。重载、分支切换、再生和失败回滚会移除离开分支的动态事件，再恢复目标快照的定义与调度进度，查询和实际调度因此使用同一组规则。现有变量、触发器、随机项的 `mechanics_overrides` 格式保持不变；旧快照中已经被裁剪且未保存的事件定义无法凭空恢复。合成测试覆盖超过旧裁剪阈值的事件、同 ID 的不同分支定义、幂等恢复、调度进度，以及 SQLite 重载和分支往返后的连续流式回合。

动态剧情树同样作为 `dynamic_story_content.trees` 中的有效声明保存，不再按最近 20 棵裁剪。注册时不修改输入，只将去重和规范化后接受的节点同时交给剧情树与事件引擎，再独立保存；同回合新增节点合并到同一棵树，不能覆盖静态树或已有事件。重载、分支切换、再生和失败回滚先清除离开分支的动态树与事件，再恢复目标快照，保留完成状态和计时进度，不误删静态树与系统触发器；旧快照已裁剪且未保存的定义仍无法凭空找回。

后台导演新增的 NPC／组织目标通过 `dynamic_story_content.goal_overrides` 保存完整有效定义，属于当前存档和分支，不受最近 20 条生成日志裁剪影响。目标更新只新增，不覆盖已有 ID；未知实体、非法声明和重复 ID 不生效。重载、切换分支和失败回滚恢复目标快照，角色推演从对应快照读取目标，不被旧 NPC 状态列表遮蔽；原始剧本不被改写，声明也不进入玩家状态。旧版本未写入快照的目标无法自动找回。

GM 可通过 `consult_rules(category="npc_goals" 或 "org_goals", query=关键词)` 按需读取目标。每条 `definition` 包含 `actor_id`、`actor_name` 和完整 `goal`，保留完成条件、组织效果及自定义声明字段；`goal_key` 使用既有 `实体ID:目标ID`，`status` 为 `pending/completed`，`completion` 保留正式完成记录（兼容旧字符串记录）。未完成只表示没有记录，不检查当前条件、不预测候选行动，也不执行效果或推进离屏进度；两个类别均只支持默认定义视图，不支持 `include_candidates=true`。NPC 与组织的完成条件和效果定义不变，完整规则仍是 GM 内部信息，不自动注入角色知识。

目标检索复用搜索、分页、字段下钻、长正文续读、快照校验及压缩后的重读坐标；完成记录或目标定义改变、分支切换后，旧快照不能继续拼接。`tests/test_actor_goal_retrieval.py` 使用合成规则、假模型与临时 SQLite，验证静态和动态 NPC、组织目标及旧完成记录、跨分支重载和长规则读取；连续流式回合由假模型解析查询到的条件阈值来提交通用变量操作。当轮正式结算后即记录完成并执行组织效果，重载后的下一回合模型读到已完成状态，重放不重复结算。查询规则标记不进入 SSE／公开 trace。这不代表真实模型质量、生产部署或浏览器体验已验收。

常规回合保留准备阶段的目标检查，并在状态、定时事件及 `after_ai` 生命周期处理后、最终快照保存前调用同一目标结算入口；未提交的工具候选不触发结算。重生成的 `all/state` 路径在对应版本的状态结算后检查，只有选项重生成不重结算目标。每个入口按 NPC→组织重复扫描，直到一轮没有新增完成记录，让组织奖励触发的后续组织／NPC 目标在本次结算内继续完成。结算期间保留入口已有及新产生的完成记录，按既有实体与目标 ID 去重（兼容旧字符串记录）；目标效果重写完成列表不会让本次已完成目标再次发奖，无起点的循环依赖不会自行启动。组织 `state_change` 的奖励日志进入当前回合／版本，完成记录、回调和效果随分支保存；不新增 NPC 通用奖励语义，不递归重跑其他生命周期、剧情节点或定时事件。

连锁沿用剧本声明的 `condition_met` 与 `on_complete_effects`，不需要新增依赖图语法或具体剧本策略。效果仍按定义顺序即时应用，完成后不因后续条件变化而撤销；对相互撤销条件或冲突写入，不保证调换定义顺序后结果等价。`tests/test_actor_goal_settlement.py` 与 `tests/test_actor_goal_chains.py` 通过合成声明、假模型及临时 SQLite，覆盖正反顺序、跨组织奖励、长链、循环依赖、声望效果、奖励新增 NPC 的离屏完成、完成记录保护、生命周期不重复、流式／非流式行动、保存失败回滚及重试、`all/state` 重生成版本切换和重载；规则正文标记不进入公开结果。测试不调用外部模型或真实服务，不代表真实模型质量或在线体验已验收。

`tests/test_director_goal_persistence.py` 使用合成声明与假模型，实际执行后台调度和完整导演流程，经正常关闭持久化到临时 SQLite，重载及分支往返后由流式 Agent 调用角色推演，检查下一次模型请求确实读到目标。另覆盖事务写入失败的回滚与重试、目标完成条件、批次去重、长批次保存、请求重放及 SSE／公开 trace 不泄漏目标标记。测试未调用外部模型或真实服务，不代表真实模型质量、生产部署或浏览器体验已验收。

动态 `choice` 节点保留声明的选项、条件、效果与解锁关系，不再降级为自动完成节点。导演子任务可生成这类节点；激活后通过现有剧情树和待选接口等待玩家选择，沿用统一条件校验、预演、分支来源校验和存档结算。节点及选项的展示文本应面向玩家；选项集合为空、格式错误或 ID 重复的动态选择节点不注册，也不自动发放节点奖励。只有显式 `auto_resolve: true` 的声明才按条件自动选择，导演的 `complete_nodes` 不得绕过选择结算。旧快照中已被降级并删除的选项不会自动恢复。

`tests/test_dynamic_story_choices.py` 使用假模型调用正式导演子任务，再由真实流式行动激活节点，验证 Agent 读取与只读预演、玩家选择、保存失败回滚及重试、请求重放、SQLite 重载与下一回合继续读取选择结果；同时覆盖缓存和分支恢复。测试不调用外部模型，也不代表后台导演调度、真实模型质量或浏览器界面已验收。

`tests/test_story_rule_retrieval.py` 验证各类型节点的完整声明、运行时规范化、树元数据搜索、跨树分页、长正文续读、分支与失败恢复，以及上下文压缩后的完整重读指引。`tests/test_dynamic_story_persistence.py` 使用合成声明、假模型和临时 SQLite，验证超过旧裁剪阈值的树、同 ID 的兄弟分支定义、树节点与定时事件跨分支复用 ID、规范化一致性及失败清理。连续流式回合中，假模型解析节点查询返回的条件阈值来决定变量操作，而非使用固定操作值；验证节点实际改变变量并激活词条，重载、分支往返或失败回滚后下一次模型请求读到正确规则与已结算效果，重放请求不重复调用模型或结算。内部词条标记、规则正文标记与动态声明不进入 SSE，规则正文标记也不进入保存的公开 trace。上述测试不代表真实模型叙事质量或浏览器体验已验收。

读取规则后，统一 Agent 可通过 `update_state` 或 `finalize_turn` 的 `variable_changes` 提交变量操作，例如 `[{"id":"var_id","op":"inc","value":1}]`。只接受当前分支已声明的变量；`number` 按整数执行 `set/inc/dec` 并遵循定义的 `min/max`，`bool` 支持 `set/toggle`，`string` 或未声明类型只支持 `set`。`set` 必须提供值，`inc/dec` 省略值默认为 1，显式 0 不改变数值。旧式 `state_changes` 中的 `type="variable"` 仍兼容并经过同样的变量校验；同次调用先执行旧字段，再按顺序执行 `variable_changes`。

变量输入和操作在候选副本上先校验，未知 ID、非法操作、值类型错误或当前值不兼容会拒绝整次调用，不能部分接受时间或其他变更。`update_state` 返回累积的候选变量值，尚未提交且不含尚未结算的事件效果；规则查询仍读取已提交状态。后续工具、条件事件预览、最终结算共用现有变量执行路径，撤销恢复调用前的候选操作快照，撤销失败调用不会误删之前成功的变更；最终提交不得重复之前已接收的变量变更，也不能代替事件或触发器重复结算。

长工具链压缩保留本轮已涉及变量和世界属性的最新累计候选值、候选时间和当前已接受的状态更新次数，并保留失败与撤销的执行记录。撤销状态更新或后台结算因叙事修订而回滚后，反馈按恢复后的候选状态重新生成；后续压缩只替换已过时的投影，不把失败或被过滤的变更当作成功，也不以撤销前的值继续推演。撤销新建属性后明确标记未设置，不把缺失值当成字符串或有效空值。

每类候选值反馈正文最多保留 4000 字符，超过时明确省略全文并提供续读入口，不截断成看似完整的值。统一 Agent 和后台结算均可调用 `consult_rules(category="variables"或"world_properties", include_candidates=true)` 分页读回完整候选值；默认查询仍只读已提交状态，`current_value` 始终是已提交值，新增 `candidate_value` 和 `candidate_exists` 描述候选快照，`projection` 标记未提交且不含尚未结算事件效果。仅运行时存在或本轮撤销的宏观属性以 `definition=null`、独立 `id` 返回，不伪造剧本声明。压缩保留候选读取标记及索引快照；续读须保留 `include_candidates=true` 和对应 `snapshot`，状态更新、撤销或切换分支后从 `offset=0` 重查列表，不能拼接不同快照的片段。`tests/test_world_property_feedback.py` 覆盖累计值、失败与过滤、撤销、叙事回滚、长值续读和压缩；验证使用合成数据与假模型，不代表真实模型质量或在线体验已验收。

### 当前剧情节点与选择发现

玩家端的大地图选项、剧情树与选择提交后的树视图共用当前分支读取：已登记的剧情节点以剧情树引擎为准，事件引擎只补充独立节点，陈旧影子状态或定义不覆盖规范节点。激活中的选择使用与实际提交相同的条件求值；缺省或空字符串条件可用，显式 `null`、布尔值、数字、列表、对象及非法表达式不能被当作无条件。未满足条件的选项不生成按钮，已完成节点保留既有选择记录，不因后来条件变化删除历史。

玩家查询只在状态副本上初始化或兼容迁移，不改变当前状态、存档或机制定义，也不推进剧情、事件或节点计时。`tests/test_player_story_choices.py` 使用合成声明与隔离 SQLite 验证两个查询入口、提交响应、双引擎来源、独立事件、重载和分支往返；这些是离线 API 证据，不代表浏览器或真实模型体验验收。正式提交仍重新校验状态和条件；本轮不新增 Agent 自动代选能力，也不改变分支来源校验协议。

统一 Agent 可先调用 `consult_rules(category="story_nodes", view="runtime", node_status="active")` 发现当前分支的节点与选择，不必根据少量剧情摘要猜测 ID，也不必逐个模拟全部声明。省略 `node_status` 查询全部节点；支持 `active`、`completed`、`unlocked`、`locked` 四种存档状态筛选，`query` 在运行态响应内容中检索。列表仍每页最多5条，按顶层 `next_offset` 和 `snapshot` 续读，并保留视图、筛选条件与候选参数；`rule_index` 是原类别索引，不是筛选后的序号。

运行态条目只含 `tree_id`、`node_id`、名称、类型、状态、`chosen_choice_id`、选择列表，以及适用的计时或冷却信息，不重复规则正文。`choices` 提供 `choice_id`、标签、`available` 和 `reason`；可用性遵循现有选择结算的激活检查和条件求值，失败区分 `node_not_active` 与 `condition_not_met`，条件格式错误另附诊断。`unlocked` 不等于已激活或可以提交；冷却已结束也不在查询中重置已完成状态。默认查询不初始化或迁移状态、不触发事件或结算效果；所有视图均不运行剧情树求值、不推进节点计时。

默认仍读取已提交状态。进行中回合可在运行态查询中使用 `include_candidates=true`，与声明式选择预检共用隔离候选基线：累计接受的更新，以及仅在已接受 `end_time` 后、截至该时间的定时事件、附带触发器和剧情解锁。不采用 `new_time` 预估时间。定时部分复用正式结算入口，但仅操作副本；事件骰子只复用本轮已准备的结果，不重新掷骰、执行完整回合生命周期或激活剧情节点；`unlocked` 节点仍返回 `node_not_active`。`projection` 的 `event_ids` 与 `scheduled_through` 说明纳入的事件和时间范围；`staged_event_dice_ids` 标记已消费的缓存骰子，没有缓存参与时 `excludes_event_dice=true`；`excludes_lifecycle` 与 `excludes_unstaged_event_dice` 始终为 true。`excludes_unsettled_events=false` 只表示纳入了该范围的定时部分。无已接受结束时间时仍只有候选更新，变量与世界属性类别维持原来的“只含候选更新”协议。需要规则时用默认 `view="definition"`、`query=node_id` 重新查询，并使用已有字段读取协议读取相关声明；需要后果时调用 `preview_choice_outcome(node_id=..., choice_id=..., include_candidates=true)`。正式选择仍沿既有选择接口重新校验与结算，不能把目录或预检当成已提交结果。

`tests/test_timed_story_projection.py` 和 `tests/test_timed_story_persistence.py` 使用合成声明、假模型与隔离 SQLite，验证时间条件、定时解锁与已激活选择条件、词条和动态机制、候选撤销、快照失效、异常隔离，以及查询结果驱动叙事后的保存失败回滚、重试、重载和分支往返。候选定时变化不是选择差异，玩家流中不包含内部词条正文；这些测试不证明真实模型会自主发掘全部剧情，也不代表在线验收或完整回合预测。

长运行态条目同样支持 `result_path=[]` 查目录，或 `result_path=["choices"]` 分页读取选择。长标签导致选项不完整时，`summary` 仍保留 `choice_id`、`available`、`reason`，并计入本页预算；不把摘要冒充完整 `value`，`complete` 仍为 false。完整标签可用成员 `key` 作为索引，通过 `result_path=["choices",索引,"label"]` 原样读取；定义视图不增加此摘要。每个条目 `read` 保留 `view="runtime"`、候选参数、快照及从0重读坐标。运行态快照绑定分支、回合、状态与节点声明，同一游戏时间内的变化也会使旧快照失效；列表快照另外绑定搜索和状态筛选。切换定义/运行态视图必须重新查列表，不混用快照；`node_status` 不与 `rule_index` 同传。裁剪和历史压缩保留视图与续读坐标，极小预算明确标记元数据省略。目录和预检均是 GM 内部信息，不是角色已知事实，不进入公开 trace。

`tests/test_story_runtime_discovery.py` 使用合成声明与假模型，覆盖超过摘要和单页数量的活跃节点、条件与候选撤销、无查询副作用、长选择列表、压缩续读及快照隔离，并通过隔离流式 API 完成“跨页发现 → 字段读规则 → 预检”，再验证正式选择、SQLite 重载、分支往返和请求重放。长标签用例将唯一可用项放在第二页节点的90个选项末尾，按实际消息预算与20轮上限验证发现链路。假模型只使用当前消息里的查询结果，不在请求外缓存或拼接正文；这些验证不代表真实模型检索策略或在线体验已验收。

重复压缩时，同一快照、同一字段或相同条目列表的旧读取摘要只保留最新一份进度，避免分页与重读让历史摘要持续膨胀；正文仍已省略，不能把最后一页或 `next_offset=null` 当成已掌握全部规则。字段 `read` 始终从0重读，不同字段、视图、候选快照、筛选结果和列表页不合并。这是历史摘要去重，不缓存工具结果、不减少正式查询校验，也不增加模型轮数或消息预算。

### 规则分页快照一致性

`consult_rules` 的八类规则查询统一使用内容快照校验：快照绑定世界树、活动分支、游戏时间、类别、已提交/候选视图和该类别的完整内容。列表快照还绑定搜索条件，条目快照绑定索引；快照不是权限凭据，不会执行机制或提交状态。首次查询列表无需快照，后续列表翻页和条目读取必须原样携带对应标识。

缺少快照返回 `error_code="snapshot_required"`；有效格式的标识与当前读取范围或内容不一致时返回 `error_code="snapshot_expired"`。两者均带 `restart_required=true` 和可直接用于重查列表的 `restart` 参数，不返回正文片段或新的续读快照。调用方必须丢弃旧片段，按 `restart` 获取新列表后完整重读，不能只换新标识继续拼接旧正文。默认已提交视图不会因仅候选值变化而失效。

`tests/test_mechanics_snapshots.py` 覆盖现值、候选值、声明顺序、搜索条件、列表成员、会话与分支变化、撤销及后台叙事回滚，以及快照经限长和重复压缩后的完整重读。`tests/test_world_property_declarations.py` 在隔离 SQLite 与真实流式路由中驱动假模型先遇到过期快照、重新读取，再根据声明提出更新，验证保存重载、分支往返和请求重放，且候选片段和快照不进入公开 SSE/trace。上述证据仍不代表真实模型的自主检索质量或在线体验已验收。

`tests/test_agent_variable_updates.py` 验证字段契约、边界与零增量、类型校验、错误恢复、投影和结算一致性、最终提交修订回滚及分支定义隔离。流式 harness 通过假模型和临时 SQLite 验证规则读取、变量操作、撤销、错误恢复、条件事件、重放去重、保存恢复和分支往返；不调用外部模型、不包含真实剧本，也不代表实际推演流畅性或叙事质量已验收。

### 规则字段按需读取

`consult_rules` 的八类声明支持在 `category`、`rule_index` 和条目 `snapshot` 上增加 `result_path=[]`，读取规范化条目 JSON 的根目录，再按返回的字符串键或非负整数索引下钻。例如剧情节点规则位于 `["definition", "node"]`，变量现值位于 `["current_value"]`；不需要为读取条件或效果先拼接超长描述。字段路径只能访问本次规则条目的 JSON，不访问文件、属性或表达式，缺少身份、非法路径和失效快照均报错，不退回普通问答。

字段响应仍放在外层 `entries` 的对应规则条目中，条目 `content_format="result_section"`；容器提供成员目录，小值附完整 `value`，长值只描述类型、长度和完整性，字符串返回最多2000字符的 `content`，标量保留原类型。容器成员负载按约2000字符分页，并非整个响应的严格长度上限。`offset` 对容器表示成员偏移，对字符串表示字符偏移，标量只接受0；条目 `next_offset=null` 仅表示当前层结束，不表示长子值已读完。条目 `read` 保留当前字段从0重读的完整坐标；压缩后仍须重读省略的字段，不能把目录当成全部规则。候选查询保留 `include_candidates=true`，不把候选值当作已保存或已触发。未传 `result_path` 时保留原始 JSON 字符分页协议。

`tests/test_rule_inspection.py` 使用合成声明与假模型验证超预算规则读取、字段分页、候选隔离、快照失效、压缩重读和公开 trace 裁剪；隔离流式 API 测试从规则中读取行动后执行通用状态更新，验证 SQLite 保存重载、分支往返及请求重放不重复结算。假模型不在请求外拼接或积累规则正文；这些证据不代表真实模型自主检索质量或线上体验已验收。

### 声明式选择预检

计划提交后，GM 可以先用 `consult_rules(category="story_nodes")` 读取当前分支的节点与选项身份，再调用 `preview_choice_outcome(node_id="test_decision", choice_id="test_open")`。预检保留当前分支的运行时定义，在隔离会话副本中通过与真实选择 API 相同的结算入口检查当前激活节点，应用其选择效果及派发事件；不会按选项文本猜测效果或提交真实选择。`include_candidates=true` 仅在进行中的回合可用，与剧情运行态共用累计已接收更新及已接收结束时间内定时事件的候选基线；事件词条和动态机制也在副本应用。它不会提前激活待决节点，不执行事件掷骰或完整回合生命周期。`state_changes/lore_changes` 仅比较该基线与选择后的副本，不把定时基线变化误报为选择奖励。

结果包括 `available`、未满足原因、完成/解锁的节点ID、直接声明的 `pending_effects`，以及实际副本结算的 `state_changes`、`lore_changes`。差异使用数组路径和 `add/replace/remove`，区分缺失、null、布尔值与数字；词条按ID比较完整字段。`projection.effects_applied=true` 只表示有效选择已在副本应用，`persisted=false` 明确没有保存；基线和结果状态均返回校验哈希。无效选择不返回初始化或迁移差异。影子事件状态与实际提交一样同步，派发事件可读取选择完成和奖励后的状态；不创建模型、存储或索引资源，不覆盖真实会话及撤销记录。

`projection.scope="choice_and_emitted_events"` 限定为选择及其派发事件；`excludes_unsettled_events=true` 表示仍不包含其他待结算事件和完整回合生命周期，不预测 NPC 回应、随机后续或玩家已知信息。入口仅支持 `story_nodes` 的剧情树选择声明，不支持直接预检独立 EventEngine 选择。状态更新、撤销或切换分支后必须重查。超长结果返回差异数量和 `changes_omitted=true`，不能把省略误认为没有效果；`consult_rules` 只能重读声明，实际差异使用预检工具自身的续读接口恢复。

初查不传 `offset` 或 `result_path`，仍返回完整结构化结果，并附带 `snapshot`、`total_chars` 和 `read`。不传路径时保留原始 JSON 分页：使用 `read` 中的节点、选项、候选视图及快照，从 `offset=0` 完整重读；每次返回最多2000字符的 `content`，`content_format="json_fragment"`，按 `next_offset` 续读直至 null，拼接全部片段后再解析 JSON。正文是不包含续读元数据的完整预检结果，不能独立解析单片。历史摘要不保留正文，也不把某个尾页偏移误当完整阅读进度；极小预算连坐标也无法保留时，明确标记 `metadata_omitted=true`，须按原身份和视图重新初查。

大结果优先在 `read` 参数上增加 `result_path=[]`，读取本次模拟结果的根目录，再将返回成员的 `key` 追加到路径按需下钻，例如 `["state_changes", 0, "after"]`。路径仅接受字符串键和非负整数索引，只查询模拟结果 JSON，不访问文件、Python 属性或表达式。每页 `content_format="result_section"` 可独立解析：对象和数组返回分页的 `entries`，小值附完整 `value`，长值只给 `value_type`、`size` 和 `complete=false`；字符串返回最多2000字符的纯文本 `content`；标量保留 null、布尔和数字类型。`offset` 对容器是成员偏移，对字符串是字符偏移，标量只能为0；换字段必须从0开始。`size` 表示当前字段的成员数或字符数，`total_chars` 仍表示完整预检 JSON 的长度。`next_offset=null` 仅表示当前层分页结束；容器中未展开的长值、仅尾页的字符串都不会标成完整，读取部分字段也不代表已知全部后果。裁剪后的 `read` 保留当前字段路径和从0重读坐标，不能把已省略正文当作仍在上下文中。

`tests/test_choice_preview_inspection.py` 使用显著超过真实 Agent 消息预算的合成模拟结果，由假模型仅根据当次实际消息中的目录与差异页找到相关数值后决策，不在模型外累积被压缩的正文，也不拼接整个结果；同时验证字段类型、非法路径、快照隔离、重复压缩、隔离 API 保存重载与分支切换，以及公开 trace 不含内部正文或快照。这证明工具链支持超预算结果的按需读取，不证明真实模型能自主选对全部相关字段，或能整体理解任意长的单个文本字段；没有替代完整回合或线上验收。

任何片段或路径请求（包括 `offset=0` 和 `result_path=[]`）都需要快照。每次读取重新进行隔离模拟，不添加会话缓存；快照基于完整状态、候选操作、存档/分支/回合身份、剧本和运行时定义、基线词条及完整模拟结果计算 SHA-256，并返回128位 URL-safe 标识，不依赖短状态摘要。输入或结果不匹配返回 `snapshot_expired`；未提供快照返回 `snapshot_required`，均要求丢弃旧片段按 `restart` 重新初查。节点或选项消失时先重查 `story_nodes`，不可拼接不同快照的片段。

只提供 `choice_text` 时保留旧式粗略风险估计；节点身份不完整或失效会返回错误，单独提供续读参数也不静默降级为文本猜测。预检是只读工具，结果不应进入玩家 hint。`tests/test_declared_choice_preview.py`、`tests/test_choice_outcome_simulation.py` 和 `tests/test_choice_preview_paging.py` 使用合成声明、假模型及隔离 SQLite，覆盖两组声明门槛、候选/已提交值隔离、撤销、部分执行故障、实例方法重绑定、外部资源隔离、流式工具实际差异、完整 JSON 续读及快照失效、裁剪和重复压缩后的恢复、真实 Agent 循环按坐标读取后决策、API结算与保存重载一致、分支往返、请求重放及公开 SSE/trace 脱敏。静态选择可解锁动态后续节点；临时添加的信号触发器只证明当前会话模拟与结算结果，不证明临时定义能重载。这些离线证据不代表完整回合推演、真实模型自主决策、叙事质量或在线体验已验收。

### 声明式选择实际结算

两个剧情树查询返回 `base_node_id`，指向展示时的世界树回合，不是剧情选择的 `node_id`。提交选择可在 JSON 中传入 `expected_node_id`；与当前活动回合不符时在会话锁内返回 HTTP 409，且不进入选择结算、回滚或持久化。`GET /api/game/{save_id}/state` 也可用同名查询参数约束选择后的状态刷新，成功响应格式保持不变。旧客户端省略或传 `null` 仍兼容，但没有来源回合保护。

大地图和剧情树按钮绑定展示时的存档、来源回合及前端加载序号，不在点击时套用新的上下文；缺少来源或来源不匹配的查询结果不渲染。分支导航发起时递增加载序号，迟到的查询、选择结果和状态刷新不覆盖新导航；失败导航重新加载当前选择。该协议不是完整状态版本锁，不覆盖同一回合内的 Swipe 或其他原地变更；服务端仍重新检查选择条件。

`tests/test_story_choice_origin.py` 和 `tests/story_choice_origin_ui.test.cjs` 使用合成会话、隔离 SQLite 和受控异步响应，覆盖来源校验、锁内重检、分支往返、旧按钮、响应乱序及输入控件归属；不代表真实模型或浏览器体验验收。

`POST /api/game/{save_id}/story-tree/choice` 按节点归属选择结算引擎：剧情树运行时定义中的手动选择由 StoryTreeEngine 处理，独立 EventEngine 选择保留原入口。提交时重新检查当前状态、节点类型和选项条件，不直接信任先前的预检结果。影子事件引擎同步剧情树的完成、激活、解锁、已选选项及进度，不重复执行剧情树奖励；同步发生在应用效果之前和之后，使选项派发的事件既能观察已完成的节点，也能保留事件进一步解锁的节点。独立事件及触发器继续维护自己的状态。

有效选择应用声明中的变量、词条、事件、回调及持久状态效果，并写回当前存档节点；内存快照和校验哈希同步更新。无效、重复和并发重放不会重复发放奖励；展示结果使用状态副本，拒绝选择不保存仅由视图读取引发的旧状态迁移。效果执行或数据库写入失败时恢复状态、词条及词条覆盖跟踪。在此选择入口，词条向量更新先缓冲，数据库提交成功后才调度；这不代表所有推演路径都具备统一的索引事务。

`tests/test_story_choice_settlement.py` 使用合成声明、假索引和隔离 SQLite，覆盖预检与实际选择衔接、两组声明门槛、正常及 `summary_only` 响应、双引擎事件观察顺序、故障回滚、提交前取消与重试、保存重载、分支往返、并发去重和响应正文脱敏。静态选择可解锁动态后续节点；动态导演生成的 `choice` 仍按既有规则转换为 `auto`，不能通过此接口当作手动选择。独立运行时 EventEngine 选择的测试只证明当前会话结算和去重，不保证临时定义可重载。本轮仍不是完整候选结算模拟，也不代表真实模型自主推演、叙事质量或在线体验已验收。

### 选择后的叙事指令

手动剧情树选择和独立 EventEngine 选择中的节点、选项 `effects.inject_prompt`，随成功结算写入当前分支的私有待处理队列。下一次回合准备将其加入既有 `turn_instructions` 和推演历史上下文，交给统一 Agent；仅准备、读取或预检不会消费指令。无效及重复选择不入队，预检只修改隔离副本，选择保存失败时回滚队列。

选择通过 `fire_events` 触发的声明式事件链也会收集实际执行的 `inject_prompt`，沿既有效果执行顺序入队，支持多级信号；条件未满足的事件不入队，即使选择本身没有直接指令，也能保留后续事件指令。选择入口只收集该选择实际执行的指令，不改变原结果中的直接声明效果、预检 `pending_effects` 或定时事件指令回执，不将其他来源的事件统一加入选择队列。

成功生成并结算后，只消费本轮上下文包含的待处理指令；下一回合不重复注入。生成结果保存失败时恢复原队列，重试仍能收到指令。全量及仅状态重生成按该节点已保存的本轮指令维护消费状态，流式全量重生成沿父分支重新执行；返回选择后的分支时恢复待处理指令，返回生成完成的分支时保持已消费。队列不进入玩家状态、选择响应或生成响应；它是 GM 指令，不等于主角知情，也不是玩家通知。旧存档中此前已丢失且未保存的指令不会自动追补。

`tests/test_choice_instructions.py` 使用合成节点、假模型及隔离 SQLite，对直接指令、一层和两层级联分别覆盖真实工具循环、选择及生成保存失败、重生成保存失败、普通与流式重生成、请求重放、重载、分支切换和响应隔离。`tests/test_choice_cascade_instructions.py` 补充条件过滤、兄弟事件顺序、无直接指令的级联及既有结果契约验证。旧单次提示路径只验证指令拼接，替换了其独立的提示构建器；本轮不代表该旧路径完整可用，也不代表真实模型遵循质量或在线体验已验收。

### 回合准备中的事件级联

定时根事件仍在接受的时间窗口中结算，直接叙事指令通过定时回执交给当轮 Agent；其 `fire_events` 信号保留下一轮派发的既有约定。回合准备消费信号后，同步执行它触发的后续事件链，并把实际执行的子事件 `inject_prompt` 加入同轮推演上下文；条件未满足的子事件不执行、不交付指令。相同文本按既有规则去重，原始 EventResult 和定时回执不被改写。

主回合和 NPC 对话使用 `EventEngine.tick(queue_cascades=False)`，由会话接管返回信号的同步派发，避免同一后续信号既立即执行又留到下一轮重复结算。独立调用 `tick` 默认仍排队后续信号；采用同步应用结果的调用者应明确接管派发。NPC 对话在此仅修正重复结算，不改变其既有叙事指令协议；其他生命周期及旧回调也不因此改为新的指令队列。旧存档中已经重复应用的效果不会自动回退。

普通全量、仅状态以及流式仅状态重生成，从父节点快照重建基线时，先通过与 `tick` 共用的 `dispatch_pending` 阶段消费已有信号，并同步恢复其级联效果。该步骤不重跑完整 tick，不额外推进截止时间或其他生命周期；全量重生成同时保留实际执行的级联指令。流式全量重生成仍沿既有父分支完整回放路径执行。失败回滚恢复原状态，成功重生成不会把已消费的信号重新留给下一轮。

`tests/test_event_cascade_dispatch.py` 使用合成声明验证一次性／循环事件、一至三级信号、顶层及字典式效果、条件时刻、兄弟执行顺序、同轮上下文、原结果契约与 NPC 对话去重。`tests/test_cascade_harness_persistence.py` 通过假模型的实际工具循环、进程内 ASGI/SSE 和隔离 SQLite，覆盖根事件延后派发、保存失败回滚与重试、请求重放、普通及流式全量／仅状态重生成、重载后重生成和分支往返。旧单次提示路径仅验证拼接；这些测试不使用真实剧本或存档，不代表真实模型、浏览器或部署验收。

### 声明式条件一致性

布尔变量可在规则中使用 `script_variables.test_ready == true` 或 `!= false`；布尔字面量不区分大小写，并兼容 `1/0`、`yes/no` 和带引号的写法。归一化仅作用于实际布尔值，不会把字符串变量中的 `"true"` 当作布尔值，也不会把无法识别的布尔字面量当作 false。节点完成、里程碑与事件完成条件同样拒绝未知布尔字面量。

生命周期钩子、定时事件附带触发器、候选推演和最终结算共用会话条件求值器，支持 `==`、`!=`、`<`、`<=`、`>`、`>=`，以及 `AND/OR`、`&&/||` 和括号分组；`AND` 优先于 `OR`。例如 `(script_variables.test_ready == true OR script_variables.test_count >= 2) AND world_properties.test_access == open`。带引号的值可包含空格、逻辑运算符和括号；`event.<name>.fired` 可作为布尔条件简写。旧式不带前缀的已声明变量名仍可读取；明确的状态路径优先，推荐使用 `script_variables.` 前缀避免歧义。候选查询针对传入的候选状态求值，不修改已提交状态。

条件先完整解析再求值：缺操作数、多余比较符、未闭合引号或括号、过深嵌套以及非字符串条件均不满足，不能由另一个为真的 `OR` 分支掩盖语法错误。缺省条件或空白字符串仍表示无条件；显式的 `null`、`false`、`0`、数组和对象不是空条件。会话驱动的生命周期、定时调度、剧情树求值、事件解锁及手动选择入口不再因这些值为假而跳过求值。

主行动的候选推演、正式定时结算和重生成定时结算按每个事件的实际 `fire_time` 读取条件中的 `game_time`，不是回合开始或结束时间；循环事件逐次判断，同刻事件仍可在前置效果更新变量后解锁。附带触发器，以及正式/重生成结算中事件直接解锁的剧情节点，也使用该事件时刻。求值只覆盖读取视图中的时钟，不临时改写正式状态；其他条件读取该段已生效的状态。这不等于重演本轮全部行动效果，也不改变普通非定时条件或独立事件引擎旧回调的语义。时间字符串仍遵循现有比较规则，跨格式、跨时区的归一化不在此变更中。

`tests/test_timed_condition_projection.py` 和 `tests/test_timed_condition_persistence.py` 用合成声明、假模型、ASGI 和隔离 SQLite 验证时间窗口、循环触发、同刻解锁、附带触发器、剧情节点、重生成恢复，以及“查询 → 撤销 → 重查 → 叙事 → 结算 → 保存重载”的衔接；覆盖保存失败回滚、重试、幂等重放、分支往返和下一轮 GM 上下文。候选资料保持在内部工具结果中，不因查询而进入玩家 SSE 正文。这些证据不代表真实模型推演质量或运行中服务已验收。

自动分支优先选择首个条件满足的选项，否则只使用真正无条件的默认选项；没有可用选项时不自动完成分支，不再把最后一个未满足或非法条件的选项强行当作兜底。

没有可用选项的自动分支保持激活，在后续已有剧情求值入口重新检查选项，包括同一回合内状态更新后的生命周期结算；重新检查仍受节点条件和前置依赖约束，不额外推进计时节点。选项满足后，节点与选项效果各执行一次并解锁后续节点，不重复发出激活通知。手动选择和任务不会因此自动完成，也不会重复提示。此行为不引入后台轮询或独立实时计时器。

回合结束时，内存世界树快照及其校验哈希同步到生命周期效果结算后的最终状态，供分支切换使用；数据库保存使用同一最终状态。`tests/test_auto_choice_progress.py` 使用合成声明、假模型和隔离 SQLite，覆盖等待后恢复、已提交值与候选值隔离、撤销候选更新、效果执行后故障回滚、内存分支切换、数据库重载、请求重放及公开 SSE/trace 脱敏。影子事件引擎仍不重复应用剧情树效果。这些离线证据不代表真实模型的自主选择质量或在线体验已验收。

作者校验对 `triggers`、`one_time_events`、`cyclic_events` 的非法条件返回结构化的 `target`、`field: condition` 与错误位置；`consult_rules` 查询同类规则时在私有规则正文中附带相同的 `condition_error`，但保留原始定义。错误说明不回显条件原文，规则正文和诊断不进入公开 SSE 或 Agent trace。此处不代表所有规则类别均有作者诊断，也不代表编辑器已能高亮定位这些新增错误。

`tests/test_declarative_conditions.py` 和 `tests/test_condition_grammar.py` 覆盖类型比较、组合语法、错误条件拒绝及派发路径一致性；完整流式回归分别使用简单、分组和错误条件，验证规则读取、地图条件、事件投影、持久化重载、分支恢复和重放幂等。错误条件不会阻止合法的世界属性更新保存，但不得触发受限事件、解锁地图或读取尚未激活的词条。所有验证均使用假模型与隔离数据，不代表真实模型体验或在线服务已验收。

### 候选在场、接触与物品查询

`consult_rules` 不指定 `category` 时仍使用 `question`：问题含 NPC 姓名及“在场/在这”查询在场关系，含物品名及“可用/有没有”检查持有。默认读取已提交状态；进行中回合显式传 `include_candidates=true`，读取累计接收的候选移动、房间、库存等更新。若已有接收的 `end_time`，复用定时事件投影，按候选时间查询声明式日程覆盖及到期状态；不采用尚未接受的预估时间。响应保留 `answers`，附带 `game_time`；候选另附 `projection.through/event_ids/provisional/scope`。

在场判断复用既有房间感知逻辑，区分“在场”“附近（不同房间）”“不在场”；空地点不算在场，显式 NPC 位置、死亡状态和日程优先级仍走已有位置解析。查询只在副本上刷新日程、房间并重算接触，不改变正式状态、不结算或揭示信息，也不占用撤销目标。候选投影不是完整回合模拟；在场不等于获准互动，持有不等于满足使用条件，幕后位置不代表玩家已知。变量、世界属性及剧情节点类别的原有候选协议不变，仍不包含待结算事件或接触更新。

更新、撤销或切换分支后须重新查询，不能沿用旧答案。上下文压缩省略旧在场/持有结果并保留候选边界和重查提示。`tests/test_candidate_presence.py`、`tests/test_agent_undo_retrieval.py` 与 `tests/test_scheduled_npc_overrides.py` 使用合成声明、假模型和隔离 SQLite，覆盖时间与房间、物品增减、撤销重查、工具结果驱动的叙事分支、保存失败回滚与重试、重载、分支往返及请求重放；查询正文不进入公开 SSE/trace。这些证据不代表真实模型的自主互动质量或在线体验已验收。

### 带状态边界的角色推演建议

`invoke_character(npc_id, situation)` 是 GM 的角色独立推演工具，不是玩家直接对话入口；允许推演尚未接触的幕后角色。默认取正式状态，运行时身份信息优先于剧本定义，注入角色位置、房间、情绪、态度、关系和未完成目标的描述，不把目标条件或效果当作角色知识。支持运行时新增角色，未知或已故角色在调用模型前返回错误。

进行中回合可显式传 `include_candidates=true`，读取累计接受的空间和状态更新；仅有已接受的 `end_time` 时纳入定时事件和日程到期投影，不采用 `new_time` 预估值。返回保留 `npc/response`，另含 `npc_id/mode=advisory/game_time/context/knowledge_source` 和候选 `projection`；`context.in_contact` 来自相应视图的接触检查。候选按定时事件、日程和房间处理后的状态重算接触，这是有限的候选状态视图，不是完整回合模拟，不包含候选信息传播。

自动注入的角色知识复用既有 NPC 认知检索，限正式状态中分配给该角色的信息和隐藏词条；定时来源还须经过已触发和知情时间校验。候选事件不自动变成角色知识。调用方仍须确保 `situation` 只包含角色可感知刺激或明确假设，不能把未知秘密塞入情境；自然语言提示不能保证真实模型不会越界。工具建议仅供 GM 内部参考，不等于玩家知情、互动许可或已执行行为，不建立接触、不推进时间、不保存状态，也不占用撤销目标；不缓存建议，更新、撤销或切换分支后重新调用。上下文压缩省略旧建议和角色上下文，保留未提交边界及重调提示。

`tests/test_character_advisory.py`、`tests/test_character_advisory_stream.py` 和撤销回归使用合成角色、假模型与隔离 SQLite，覆盖状态与知识边界、建议驱动叙事选择、撤销重调、保存失败回滚、重载、分支及请求重放。角色建议和工具上下文不直接进入公开 SSE/trace；测试不证明真实模型的语义保密性、自主互动质量或线上体验。

### 玩家直接对话与接触边界

`talk_to_npc` 与流式入口共用确定性检查，在后台工作完成后、调用模型前拒绝未知或明确未接触、已故、位置不明、异地或双方已知房间不同的角色。未设置 `known/met` 的旧存档保持兼容；存活且已知的同伴沿用位置与房间例外。地点仍支持既有 ID、名称及一级父子地点匹配，但不再调用模型猜测两个不匹配的地点是否能对话。拒绝理由不包含远端位置、角色私密信息或隐藏死亡详情。

传统模式仍可与满足条件的同地角色直接对话并建立接触，不新增“必须已有 contact”的全局门槛；地图模式仍由前端引导寻找和建立接触，不新增后端模式开关。公开状态中的 `npc_talk_block_reasons` 只为已知角色返回当前阻止理由，空字符串表示通过基础检查；`npc_contacts` 复用后端接触判定，按所请求的状态快照解析日程，不能用当前会话时间误判历史节点。这两项是公开视图，不将可交互性缓存进存档。前端在打开和发送时检查可交互性；地图聊天窗口不能沿用已中断的接触，拒绝发送时保留输入。

对话沿用现有时间结算，随后刷新 NPC 日程位置和房间，再维护接触并创建节点；若期间离场或变为不可对话，不再提供继续聊天的快捷回复。前端消费最终状态，移除过期快捷回复；后端仍会重新检查下一次请求。角色独立推演工具 `invoke_character` 的 GM 建议语义不变，直接对话入口不扩展重放幂等或信息传播规则。

`tests/test_npc_talk_access.py`、`tests/test_npc_talk_persistence.py` 和 `tests/npc_talk_access_ui.test.cjs` 使用合成角色、假模型、隔离 SQLite 与前端单元环境，覆盖入口一致性、同伴与传统模式兼容、接触失效、时间结算、保存拒绝回滚与重试、重载和分支、流式中断以及前端发送拦截。测试不代表真实模型对话质量或已运行服务、浏览器界面经过验收。

### Agent 接触声明与候选反馈

`update_state.npc_contact_changes` 先校验数组、非空角色 ID 和严格布尔值，格式错误时整次调用不接受。合法声明可以暂时不成立：工具反馈中的 `contacts` 分别返回最后的 `requested_in_contact`、候选 `in_contact` 和建立接触的 `block_reason`，`through` 指明查询时间。后续时间、位置、房间和状态更新会重算已接收的声明，无需重复提交；撤销后也重算并替换上下文压缩中的旧反馈。详情过长时明确标记 `feedback_omitted`，不把省略伪装成空接触。

候选互动视图的顺序是：累计已接受更新 → 已接受 `end_time` 内的定时事件 → 日程位置与房间刷新 → 接触维护。未接受结束时间时使用正式时间，不采用行动预估值；不能提前把候选接触设置的 `known/met` 用来触发本轮定时事件。接触建立复用正式结算检查：角色存在且存活、双方地点非空且严格相同、双方房间已知时相同；不沿用在场/直接对话的地点别名、父子地点或同伴例外。允许同地未认识角色通过明确接触建立 `known/met`，解除接触不抹去既有认识；离场或死亡会清理失效接触。

用 `consult_rules(question="角色ID 接触", include_candidates=true)` 可读取候选 `contacts`，省略候选参数读取正式接触；也支持姓名和运行时角色。查询不创建真实接触、不占用撤销目标；`invoke_character` 可使用相同候选接触上下文，但仍是 GM 建议，自动注入的角色知识只来自正式状态。候选不包含完整生命周期或信息传播，不是已提交结果或互动许可，更新、撤销和切换分支后必须重查。

`tests/test_candidate_contacts.py` 和 `tests/test_candidate_contact_persistence.py` 使用合成声明、假模型与隔离 SQLite，覆盖日程离场/到期、房间、累计声明与撤销、压缩及大结果重查；真实工具反馈驱动假 GM 的叙事分支，并验证正式保存、保存拒绝回滚与重试、重载、分支往返和请求重放。候选工具正文不进入公开 SSE/trace。这些测试不代表完整生命周期、真实模型交互质量或线上/浏览器验收。

### 条件日程的状态边界

NPC 日程中的 `condition` 按所查询的状态快照求值，不借用当前会话的变量、事件标记或其他状态。候选更新改变条件，或候选定时事件触发后，位置、日程房间、在场提示与接触反馈会据此重算；历史节点的公开接触和对话检查也只读取对应快照。查询不会临时替换或修改正式会话状态，显式指定的位置与房间仍高于日程推断。

角色对话提示读取同一快照的日程条件及 `npc_schedule_overrides`，沿用既有优先级和到期规则，避免接触检查与对话活动描述使用两套日程。不新增剧本专用判断或字段，也不扩展候选生命周期和角色知识传播。`tests/test_schedule_snapshots.py` 与接触持久化回归使用合成条件、假模型和隔离 SQLite，覆盖相反状态快照、事件触发后的日程、保存失败回滚、重载、分支与请求重放；不代表真实模型或浏览器体验验收。

### 自主行为的位置与恢复边界

NPC 自主行为缓存复用正式位置和日程解析，不再单独按整点推断活动；分钟边界、跨午夜、优先级、条件、临时覆盖、到期、显式位置和死亡状态使用同一套规则。日程活动与实际解析位置不一致时，不把异地活动挂到该角色身上。

首次观察只建立位置基线，不生成“移动”事件；行动预估时间只在缓存副本上生成可能变化，不提前推进正式时间或缓存。提交时按结算状态更新缓存。上下文中的位置变化只是前后位置核对，不代表目击经过或主角知情。读档按保存快照恢复独立缓存，切换分支和撤销回合也恢复对应节点；没有自主快照的旧节点清空缓存，不继承未来分支的位置。

`tests/test_npc_autonomy_schedules.py` 和接触持久化回归使用合成日程、假模型、ASGI 与隔离 SQLite，覆盖预估隔离、结算、保存失败回滚、读档后的下一轮提示、旧根节点恢复、分支往返和请求重放；这些测试不代表真实模型或线上体验验收。

### 世界时钟的历史边界

世界时钟不再把静态日程开始时间直接写成已发生事件。正常结算和重生成按定时事件分段，在每段内检查日程开始/结束、临时覆盖到期和持续状态到期；同一时刻的事件与触发效果全部完成后，再用该时刻的状态解析 NPC 位置和活动。条件、优先级、分钟、跨午夜、`24:00`、`time` 别名、显式位置及死亡判定复用正式解析，不使用回合终态倒推之前的活动。动态角色和仅有覆盖的角色也参与检查。

只有前后均可解析且位置或活动实际改变时，才保存带时间和前后值的 `npc_schedule_change`；死亡或首次出现不冒充移动。旧存档中未经核对的 `npc_schedule_start` 不再作为事实提示。候选投影默认不生成历史，重生成只更新候选状态、不污染正式时钟；保存失败回滚、重试、请求重放、重载和分支沿用存档中的历史。下一轮 GM 上下文消费最近的核对记录，不将其描述为目击经过或主角已知。

`tests/test_world_clock_history.py` 与接触持久化回归使用合成声明、假模型和隔离 SQLite。新增历史字段不进入公开 SSE/trace；完整日程声明遵守下述玩家响应边界。验证范围是上述声明边界和结算检查点，不是连续世界模拟、真实模型的语义保密性或线上体验验收。

### 日程声明的玩家响应边界

完整 `npc_schedule_overrides` 仅保留在引擎、GM 推演和本地存档中。玩家状态不再携带该字段，变更日志也过滤以该字段及其子路径为目标的整条记录，避免 `value`、`old`、`new` 或 `reason` 旁路泄漏。共用过滤覆盖回合结果、状态、历史节点及其重生成版本、分支/版本切换和生成中的快照，不修改内部声明或持久化数据。

前端继续使用 `/npc-schedules` 的观察视图：仅返回当前同地、已知且存活角色的 `location` 和 `activity`，不返回完整时段、条件、优先级、到期声明或未来活动；显式位置与日程位置不一致时不附带异地活动。`tests/test_schedule_visibility.py` 使用合成日程和隔离 SQLite 验证这些出口及重载，接触和 harness 回归继续验证候选反馈、保存失败回滚、重试、重放及下一轮 GM 上下文。生成中快照使用受控假行动；这些测试不代表真实模型的叙事语义保密性、完整接口保密审计或线上/浏览器体验验收。

### NPC 日程声明的按需读取

统一 Agent 可在计划结束后用 `consult_rules(category="npc_schedules", query="角色ID或关键词")` 检索当前分支已注册角色的完整日程。每条 `definition` 包含 `npc_id/npc_name`、基础 `schedule` 和正式状态中的 `overrides`；只列出至少有一种日程声明的角色，支持从存档恢复的动态角色，不把静态角色状态中残留的同名字段覆盖基础定义。类别数量自动进入机制目录，完整正文不会自动塞进回合提示。

查询复用规则列表分页、条目快照及 `result_path` 字段读取，例如 `["definition", "schedule", 0, "condition"]`。保留所有声明字段和已到期覆盖，不判断条件、优先级或到期，不执行日程、不预测位置，也不支持候选或 runtime 视图。需要判断正式或候选在场、接触时，另用既有 `question` 查询；声明不等于正在生效、未来必然发生或角色已知。覆盖、定义、时间或分支变化后，旧快照失效，必须重新查询。

`tests/test_npc_schedule_retrieval.py` 使用合成声明、假模型和隔离 SQLite，覆盖只读边界、完整字段、分页和压缩恢复、日程读取驱动变量与等待提案、撤销重查、正式结算、保存失败回滚与重试、请求重放、重载和分支往返。工具正文不直接进入公开 SSE/trace；这些测试不代表真实模型能自主理解任意日程、叙事语义保密性或线上体验验收。

### 明确等待的事件中断校验

玩家明确等待一段时间时，只有截至拟议结束时间实际触发的公开事件才允许提前结束。时间校验复用正式定时事件结算的只读投影，纳入已接收及本次待接收的状态更新，按各事件触发时刻判断条件和前序事件影响；不再把回合开始的事件预览当作实际发生的依据。隐藏事件本身、未触发事件及已消费的一次性事件不能充当中断依据。

接收结束时间后，后续状态更新也会重新校验该时间。如果更新撤销了提前结束所需的条件，整个更新会被拒绝且保留原候选状态；Agent 可在同次重试中改为完整等待时间并提交修正。普通旅途耗时、系统开场及最大推进时长限制仍然生效。

`tests/test_wait_event_validation.py` 使用合成事件、假模型和隔离 SQLite，覆盖合并/分步提案、条件时钟与前序事件、过期预览、候选更新拒绝、真实统一 Agent 流式入口中的读取与反馈驱动重试、撤销、保存失败回滚、请求重放及分支重载。这不代表真实模型的中断叙事质量或线上体验已验收。

### 事件预览与正式结算一致性

`peek_upcoming_events` 与准备阶段事件提示复用正式定时结算的只读分支，按每次触发时刻检查条件，并应用前序事件的声明式效果。回合内只复用已准备的关联骰子结果和持续/冷却状态，不重新掷骰；同一骰子在循环事件及接续预览窗口中只消费一次。`events` 表示当前分支条件下投影会触发的事件，`conditional_candidates` 表示投影未触发的条件候选，供按声明复核，不能据此断言未来必定发生；两者都不代表已发生或玩家知情，也不包含后续玩家行动、未准备的骰子或非定时生命周期结算。独立查询没有本轮缓存，不预支骰子结果。

后续时间窗口先推演被跳过时段的前序事件，再筛选窗口内结果；相邻窗口不重复边界事件。保留每类每页 5 个结果、准确总数及循环事件跨调度页读取。候选状态变化、撤销或分支切换后应重新查询，不复用旧预测。

`tests/test_event_forecast_consistency.py` 使用合成事件、假模型和隔离 SQLite，覆盖时钟条件、声明式效果/触发器/资料连锁、同刻因果顺序、跨日窗口、超过千次的循环事件分页，以及正式统一 Agent 入口中读取声明后由预览驱动等待、撤销重查、保存失败回滚、重放与分支重载；不代表真实模型或线上体验验收。

### 定时事件叙事与实际结算

`write_narrative.event_ids` 及剧本定时事件声明的 `narrative_claims` 用于核对明确的已发生声明，不再只检查准备阶段候选的时间。提交按接受的状态更新做正式定时结算投影，纳入已准备的事件骰子结果；条件失效、前序事件取消、窗口外和不存在的事件均要求修订，可沿现有反馈在同轮改写再提交。完整重生成同样使用本轮缓存；已确认发生的历史事件仍可回顾。

当本轮状态更新使新的定时事件满足条件时，`finalize_turn` 在通过既有参数和时间检查后，在副本中准备尚未缓存的关联骰子、持续及冷却状态。首次返回 `event_dice_prepared` 和具体结果，要求 Agent 在原有循环中确认或修订后再提交；即使没有明确的错误事件声明，也要先核对新结果。本次 finalize 的状态参数未接收，须随重试重传；此前成功的 `update_state` 保留。相同骰子在本轮后续修订、查询及实际结算中复用，不靠重复提交重掷；补齐结果会清除工具查询缓存，避免读到旧预测。普通只读查询不准备新骰子，候选结果不是已发生事件或主角知情。

委托结算在已有合法结束时间、且 `update_state` 已接收后，同样补齐新事件骰子，再生成候选投影与叙事校验反馈。返回的 `state_update_accepted=true` 表示本次更新无需重复，不等于已经落库；后台 Agent 可在原有循环内核对或改写。改写仍撤销本阶段此前的结算，保留进入本阶段前已接受的更新、本轮固定骰子，以及新叙事对应的条件事件核对记录；重新提交状态后若候选事件改变，仍须重新核对。未接受时间、非法更新及普通前台状态更新不会因此提前掷骰。

正常落库和完整重生成按实际结算状态再次核验，包含事件关联骰子的影响。统一 Agent 中引用定时事件的正文延迟到实际结算校验通过后发送；预测通过但实际失败时不发送该正文、不保存本轮，由原有失败回滚路径处理。保存事务失败仍可能发生在已通过结算校验的正文发送之后，这不是提交成功通知。

`tests/test_event_narrative_consistency.py` 使用合成声明、确定性骰子、假模型和隔离 SQLite，验证声明/时间/条件校验、改写反馈、SSE 正文边界、保存失败重试、请求重放及分支重载。该校验只检查显式 ID 和剧本声明的原文匹配，不推断任意叙事语义，也不证明主角知情或真实模型体验。

`tests/test_staged_event_dice_projection.py` 验证只读投影复用缓存、状态副本隔离、冷却和禁用边界、事件取消、循环与接续窗口不重复应用；查询不消耗随机数，不修改正式状态或缓存。`tests/test_finalize_event_dice.py` 验证提交时补齐结果、重试不重复应用状态参数或重掷、无冲突时仍先确认、失效查询缓存刷新，以及实际结算复用结果。合成 ASGI/SQLite 验收还覆盖完整重生成、保存失败重试、请求重放和分支重载；故障注入跳过补齐阶段时，最终结算保护仍会拒绝矛盾正文且不保存。

这不是完整生命周期预执行，也不增加回合外自动重跑模型的机制；委托结算或提交后才出现的未预测影响仍由实际结算校验把关。Agent 不修订或耗尽原有预算时仍拒绝提交，不能把测试通过等同于真实模型能稳定完成修订。

`tests/test_delegated_event_dice.py` 使用合成声明、假模型、进程内 ASGI/SSE 和隔离 SQLite，验证委托新骰子反馈、持续/冷却副本、分步时间更新、改写不重复增量与重掷、保留此前更新，以及保存失败重试、请求重放、分支重载和完整重生成。事件矛盾未修订时（包括耗尽后台预算）仍不发送矛盾正文、不保存本轮；这些验证不代表真实模型或线上体验验收。

### 可视化编辑的声明保留

表单保存定时事件时，按事件 ID 保留未提供编辑控件的 `effects`、`narrative_claims` 和 `rule_notes`；图编辑模式原本已保留完整事件定义，仍以图中的当前值为准。NPC／组织目标在两种模式下共用表单，保存时按实体 ID 与目标 ID 保留 `rule_notes`，组织目标另保留 `on_complete_effects`。没有稳定 ID 的目标不继承隐藏声明，不跨实体匹配同名目标。

仅补回缺失的上述字段并深拷贝，不合并整份旧 JSON；不覆盖控件编辑或图中显式清空的声明，不恢复已删除的事件、实体和目标。通过 JSON 更新的空数组／空对象按当前声明保存；重复保存和修改输出不改写原始声明。不新增编辑控件，也不增加 NPC 奖励或其他引擎语义。

`tests/script_declaration_roundtrip.test.cjs` 用合成 DOM 字段替身调用真实构建／保存函数，并在图模式调用实际事件导出函数。`tests/test_script_declaration_roundtrip.py` 将其保存请求送入进程内脚本 API，关闭 SQLite 连接池后重新读取并核对导出，验证表单／图模式保存的列表式和字典式定时事件效果、目标奖励及去重、规则查询和未来事件原文声明校验。这些测试不代表真实浏览器、外部模型或生产部署验收。

### 字典式定时事件效果

一次性和循环事件的 `effects` 可使用列表或已有标准字典格式。列表仍按原顺序执行，保留动态变量、触发器等扩展；字典复用通用事件效果执行器，支持变量／状态、世界书增删改及启停、发现标记、地点／NPC／声望、节点解锁、持续状态、条件日程和叙事回调。裸变量 ID 的 `set_var` 保留类型与上下限处理，点分状态路径沿用状态管理器的运算规则；结算日志记录实际变更前后值，隐藏事件保留来源标记。初始化世界书时，两种格式的一次性事件均参与原有时间门控。

NPC 对话也复用主回合的定时结算入口，避免事件引擎 tick 与旧对话调度器在同一窗口重复应用效果。行动、对话和重生成结算的 `fire_events` 进入原有待处理队列，在后续 tick 消费；候选投影不提前执行这部分后续生命周期。通知仍沿用来源可见性过滤。

`tests/test_dictionary_event_effects.py` 使用合成声明和假模型，验证一次性／循环次数、同时间条件链、世界书查询隔离、状态日志、延迟转发，以及流式／非流式 NPC 对话不重复结算；进程内 ASGI 和隔离 SQLite 验证候选撤销、保存失败回滚重试、请求重放、分支重载及完整重生成。验证不调用外部模型、不使用真实剧本或存档，不等同于真实模型体验或生产部署验收。

### 定时事件的当轮叙事指令

字典效果中的 `inject_prompt` 现在可通过统一 Agent 与委托结算的 `update_state` 反馈进入当轮上下文。指令来自与正式结算相同的定时事件执行结果，携带事件 ID 和具体触发时刻；一次性、循环事件均按当前已接收的候选时间及条件计算，不提前执行后续生命周期，也不修改正式状态。撤销更新后，反馈会明确列出当前仍有效的指令，空列表表示此前候选指令已撤回。

`finalize_turn` 会在新骰子准备后重新核对指令。若本轮尚未反馈某个事件时刻的指令，先返回指令及重试说明，让 Agent 有机会确认或改写叙事；该次提交参数不接收，重试须重传，先前已接收的更新仍保留。已反馈且仍相同的指令不增加确认轮次；只读查询不改变反馈记录。

上下文压缩保留最新候选的指令反馈，包括仅更新时间的调用；重复压缩和撤销更新不会恢复旧候选。失败 `finalize_turn` 的指令回执单独保留，明确标识参数未接收和重试边界，不把历史失败当成已完成结算。预算内指令完整保留；原始工具消息或摘要超预算时以 `instructions_omitted` 明确标记省略，保留指令数量及可容纳的事件 ID／触发时刻，不用空指令列表代替省略。坐标也超预算时标记 `metadata_omitted`，要求重新查询。

多次候选提交失败时，摘要只保留最新一份有效指令反馈；更晚的反馈即使仍在原始工具消息尾部，也会取代摘要中的旧指令。旧失败标为 `[历史提交失败]` 和 `instructions_superseded`，移除旧指令正文、坐标与续读提示，对完全相同的历史回执去重；参数未接收、其他拒绝原因和已准备骰子结果仍保留，不把取代指令误当成接受提交。空间与接触反馈不随旧指令一起删除。无关失败不取代指令；撤回指令后又有不含指令的普通状态回执时，仍单独保留最新的空指令反馈，避免旧指令复活。

续读复用 `consult_rules`：在 `one_time_events`／`cyclic_events` 按事件 ID 初查，取得 `rule_index` 与 `snapshot`，再以 `result_path=["definition","effects","inject_prompt"]`、`offset=0` 读取字段，沿 `next_offset` 拼齐全文；快照失效须丢弃旧片段重新初查。查询只读取声明，不确认候选已发生或主角已知；压缩也不修改游戏状态或指令反馈记录。

这些内容是幕后叙事指导，不是玩家通知、角色知情证据或必须逐字展示的正文。反馈记录仅在当前回合上下文中使用，不保存进游戏状态；本机制只保证指令有反馈机会，不判断模型是否在语义上遵循所有要求，也不为旧工作流或独立 NPC 对话新增提示队列。

`tests/test_scheduled_event_instructions.py` 验证候选与正式结果一致、隐藏事件、循环时刻区分、条件／时间门控、候选撤回、完整长文本以及提交重试不重复应用状态。假模型实际读取工具反馈后改写；进程内 ASGI 和隔离 SQLite 覆盖统一 Agent、委托结算、保存失败回滚、请求重放、重生成与分支重载。这些测试不等同于真实模型、浏览器或部署验收。

`tests/test_event_instruction_context.py` 用合成事件验证重复压缩、超长消息限长、明确省略、规则字段续读与快照失效，并检查空间／接触反馈和骰子失败回执不丢失；连续 16 次候选失败分别覆盖一次压缩和逐轮压缩，核对实际传给假模型的消息预算、最新指令及历史去重，另验证原始消息尾部、撤回后普通更新和真实处理器生成的联合骰子拒绝。受控假模型读取压缩指令后改写并完成工具提交，仅证明进程内 harness 链路，不代表真实模型遵循指令的语义质量。

## 技术栈

- **后端**: FastAPI + Uvicorn + aiosqlite (SQLite WAL 模式)
- **前端**: 原生 HTML/CSS/JavaScript
- **AI**: OpenAI SDK / Anthropic SDK / Ollama (httpx)
- **搜索**: DuckDuckGo + BeautifulSoup
- **向量检索**: FastEmbed + ChromaDB（可选）
