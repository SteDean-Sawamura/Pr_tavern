"""Tool schemas shared by session pipeline and agent execution."""

VARIABLE_CHANGES_SCHEMA = {
    "type": "array",
    "description": "剧本变量候选变更，按顺序执行，尚未提交。仅使用当前分支已声明的ID；先consult_rules读取规则。number为整数，支持set/inc/dec并按定义min/max钳制；bool支持set/toggle；string及未声明类型仅支持set。set必填value；inc/dec省略value为1，显式0不变。不要重复提交已接收的变更，也不要代替事件或触发器重复结算。",
    "items": {"type": "object", "properties": {
        "id": {"type": "string"},
        "op": {"type": "string", "enum": ["set", "inc", "dec", "toggle"]},
        "value": {},
    }, "required": ["id", "op"]},
}

LOREBOOK_QUERY_PARAMETERS = {"type": "object", "properties": {
    "keyword": {"type": "string", "description": "全文搜索关键词，可用空格分隔；与 entry_id、entry_ids 三选一"},
    "entry_id": {"type": "string", "description": "读取单条设定的 ID；用返回的 id 配合 offset 定位或按 next_offset 续读"},
    "entry_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3,
                  "uniqueItems": True, "description": "一次读取 1-3 个独立条目 ID；不可与 keyword、entry_id、page 或 offset 混用"},
    "page": {"type": "integer", "minimum": 0, "description": "搜索页码，从 0 开始；按 next_page 翻页时携带顶层 snapshot 和原 keyword"},
    "offset": {"type": "integer", "minimum": 0, "description": "按 entry_id 阅读时的字符位置，默认 0；定位或重读被省略的片段用条目当前 offset，读到该片段后才按 next_offset 续读，均携带条目 snapshot。完整阅读从0开始，定位不代表已读全文"},
    "snapshot": {"type": "string", "description": "初查可省略；翻页/续读必填。搜索用顶层快照，按ID阅读用条目快照。失效时丢弃旧片段并按 restart 从头读取，不拼接不同快照；搜索摘要不等于从头读过正文。"},
}}

# Foreground (narration) tools: retrieval + scene presentation. Mutating tools
# are deliberately excluded -- the background agent owns all state changes.
FOREGROUND_TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "recall_history",
        "description": "搜索历史对话记录，返回相关回合的摘要",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "max_results": {"type": "integer", "description": "最多返回条数", "default": 5},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "query_lorebook",
        "description": "全文搜索世界书并返回带 ID 的摘要；按命中条目的 offset 和 snapshot 定位读取，或按 entry_id 分段续读、用 entry_ids 一次读取最多 3 条。定位片段和 truncated 结果均不代表已读全文。",
        "parameters": LOREBOOK_QUERY_PARAMETERS,
    }},
    {"type": "function", "function": {
        "name": "query_npc_history",
        "description": "查询与特定NPC的互动历史",
        "parameters": {"type": "object", "properties": {
            "npc_name": {"type": "string", "description": "NPC名称"},
            "max_turns": {"type": "integer", "description": "最近几轮", "default": 5},
        }, "required": ["npc_name"]},
    }},
    {"type": "function", "function": {
        "name": "roll_dice",
        "description": "掷骰子进行技能检定，返回d20结果和成功/失败判定",
        "parameters": {"type": "object", "properties": {
            "skill": {"type": "string", "description": "技能名称，如察觉、交涉、潜行"},
            "dc": {"type": "integer", "description": "难度等级(Difficulty Class)，默认10", "default": 10},
        }, "required": ["skill"]},
    }},
    {"type": "function", "function": {
        "name": "check_inventory",
        "description": "检查玩家背包中是否持有指定物品",
        "parameters": {"type": "object", "properties": {
            "item_name": {"type": "string", "description": "物品名称"},
        }, "required": ["item_name"]},
    }},
    {"type": "function", "function": {
        "name": "get_npc_attitude",
        "description": "查询NPC当前对玩家的好感度/态度",
        "parameters": {"type": "object", "properties": {
            "npc_id": {"type": "string", "description": "NPC的ID标识"},
        }, "required": ["npc_id"]},
    }},
    {"type": "function", "function": {
        "name": "get_time",
        "description": "获取当前的游戏内时间",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "set_atmosphere",
        "description": "设置场景氛围效果",
        "parameters": {"type": "object", "properties": {
            "weather": {"type": "string", "description": "天气"},
            "lighting": {"type": "string", "description": "光照"},
            "sounds": {"type": "string", "description": "环境音"},
            "mood": {"type": "string", "description": "整体氛围基调"},
        }},
    }},
    {"type": "function", "function": {
        "name": "set_scene_image",
        "description": "触发场景图片生成",
        "parameters": {"type": "object", "properties": {
            "prompt": {"type": "string", "description": "图片描述（英文）"},
            "style": {"type": "string", "enum": ["realistic", "anime", "pixel"], "description": "风格"},
        }, "required": ["prompt"]},
    }},
    {"type": "function", "function": {
        "name": "review_narrative",
        "description": "审查你刚写的叙事文本，检查认知越界/人称错误/NPC声线偏差/决策越权等问题。在输出最终叙事前调用此工具做自查",
        "parameters": {"type": "object", "properties": {
            "narrative": {"type": "string", "description": "要审查的叙事文本"},
        }, "required": ["narrative"]},
    }},
]

GAME_TOOLS = {
    "roll_dice": {"desc": "\u63b7\u9ab0\u5b50\u68c0\u5b9a\u3002\u53c2\u6570: skill(\u6280\u80fd\u540d), dc(\u96be\u5ea6,\u53ef\u9009)", "example": "roll_dice(\u5bdf\u89c9, 12)"},
    "check_inventory": {"desc": "\u68c0\u67e5\u73a9\u5bb6\u662f\u5426\u6301\u6709\u67d0\u7269\u54c1\u3002\u53c2\u6570: item_name", "example": "check_inventory(\u706b\u628a)"},
    "get_npc_attitude": {"desc": "\u67e5\u8be2NPC\u5bf9\u73a9\u5bb6\u7684\u597d\u611f\u5ea6\u3002\u53c2\u6570: npc_id", "example": "get_npc_attitude(merchant_lin)"},
    "get_time": {"desc": "\u83b7\u53d6\u5f53\u524d\u6e38\u620f\u65f6\u95f4", "example": "get_time()"},
}

GAME_TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "roll_dice",
        "description": "\u63b7\u9ab0\u5b50\u8fdb\u884c\u6280\u80fd\u68c0\u5b9a\uff0c\u8fd4\u56ded20\u7ed3\u679c\u548c\u6210\u529f/\u5931\u8d25\u5224\u5b9a",
        "parameters": {"type": "object", "properties": {
            "skill": {"type": "string", "description": "\u6280\u80fd\u540d\u79f0\uff0c\u5982\u5bdf\u89c9\u3001\u4ea4\u6d89\u3001\u6f5c\u884c"},
            "dc": {"type": "integer", "description": "\u96be\u5ea6\u7b49\u7ea7(Difficulty Class)\uff0c\u9ed8\u8ba410", "default": 10},
        }, "required": ["skill"]},
    }},
    {"type": "function", "function": {
        "name": "check_inventory",
        "description": "\u68c0\u67e5\u73a9\u5bb6\u80cc\u5305\u4e2d\u662f\u5426\u6301\u6709\u6307\u5b9a\u7269\u54c1",
        "parameters": {"type": "object", "properties": {
            "item_name": {"type": "string", "description": "\u7269\u54c1\u540d\u79f0"},
        }, "required": ["item_name"]},
    }},
    {"type": "function", "function": {
        "name": "get_npc_attitude",
        "description": "\u67e5\u8be2NPC\u5f53\u524d\u5bf9\u73a9\u5bb6\u7684\u597d\u611f\u5ea6/\u6001\u5ea6",
        "parameters": {"type": "object", "properties": {
            "npc_id": {"type": "string", "description": "NPC\u7684ID\u6807\u8bc6"},
        }, "required": ["npc_id"]},
    }},
    {"type": "function", "function": {
        "name": "get_time",
        "description": "\u83b7\u53d6\u5f53\u524d\u7684\u6e38\u620f\u5185\u65f6\u95f4",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "modify_stat",
        "description": "\u4fee\u6539\u73a9\u5bb6\u7684\u5c5e\u6027\u503c\uff08\u5982\u751f\u547d\u503c\u3001\u91d1\u5e01\u7b49\uff09",
        "parameters": {"type": "object", "properties": {
            "stat": {"type": "string", "description": "\u5c5e\u6027\u540d\u79f0\uff0c\u5982hp\u3001gold\u3001mana"},
            "delta": {"type": "integer", "description": "\u53d8\u5316\u91cf\uff0c\u6b63\u6570\u589e\u52a0\uff0c\u8d1f\u6570\u51cf\u5c11"},
        }, "required": ["stat", "delta"]},
    }},
    {"type": "function", "function": {
        "name": "query_lore",
        "description": "\u67e5\u8be2\u77e5\u8bc6\u5e93\u4e2d\u4e0e\u5173\u952e\u8bcd\u76f8\u5173\u7684\u6761\u76ee\uff08\u4e16\u754c\u89c2/\u4eba\u7269/\u5730\u70b9\u4fe1\u606f\uff09",
        "parameters": {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "\u67e5\u8be2\u5173\u952e\u8bcd"},
        }, "required": ["keyword"]},
    }},
    {"type": "function", "function": {
        "name": "change_location",
        "description": "\u5c06\u73a9\u5bb6\u79fb\u52a8\u5230\u6307\u5b9a\u5730\u70b9",
        "parameters": {"type": "object", "properties": {
            "location_id": {"type": "string", "description": "\u76ee\u6807\u5730\u70b9ID"},
        }, "required": ["location_id"]},
    }},
    {"type": "function", "function": {
        "name": "set_variable",
        "description": "\u8bbe\u7f6e\u6216\u4fee\u6539\u811a\u672c\u53d8\u91cf\u7684\u503c",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "\u53d8\u91cf\u540d\u79f0"},
            "value": {"type": "string", "description": "\u53d8\u91cf\u503c"},
        }, "required": ["name", "value"]},
    }},
    # --- P1 \u4e0a\u4e0b\u6587 Pull \u5de5\u5177 ---
    {"type": "function", "function": {
        "name": "recall_history",
        "description": "\u641c\u7d22\u5386\u53f2\u5bf9\u8bdd\u8bb0\u5f55\uff0c\u8fd4\u56de\u76f8\u5173\u56de\u5408\u7684\u6458\u8981",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "\u641c\u7d22\u5173\u952e\u8bcd"},
            "max_results": {"type": "integer", "description": "\u6700\u591a\u8fd4\u56de\u6761\u6570", "default": 5},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "query_lorebook",
        "description": "全文搜索世界书并返回带 ID 的摘要；按命中条目的 offset 和 snapshot 定位读取，或按 entry_id 分段续读、用 entry_ids 一次读取最多 3 条。定位片段和 truncated 结果均不代表已读全文。",
        "parameters": LOREBOOK_QUERY_PARAMETERS,
    }},
    {"type": "function", "function": {
        "name": "query_npc_history",
        "description": "\u67e5\u8be2\u4e0e\u7279\u5b9aNPC\u7684\u4e92\u52a8\u5386\u53f2",
        "parameters": {"type": "object", "properties": {
            "npc_name": {"type": "string", "description": "NPC\u540d\u79f0"},
            "max_turns": {"type": "integer", "description": "\u6700\u8fd1\u51e0\u8f6e", "default": 5},
        }, "required": ["npc_name"]},
    }},
]

# P2: 叙事生成工具 schema（Stage 2+3 合并后的辅助工具调用）
NARRATIVE_TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "set_atmosphere",
        "description": "设置场景氛围效果",
        "parameters": {"type": "object", "properties": {
            "weather": {"type": "string", "description": "天气"},
            "lighting": {"type": "string", "description": "光照"},
            "sounds": {"type": "string", "description": "环境音"},
            "mood": {"type": "string", "description": "整体氛围基调"},
        }},
    }},
    {"type": "function", "function": {
        "name": "set_scene_image",
        "description": "触发场景图片生成",
        "parameters": {"type": "object", "properties": {
            "prompt": {"type": "string", "description": "图片描述（英文）"},
            "style": {"type": "string", "enum": ["realistic", "anime", "pixel"], "description": "风格"},
        }, "required": ["prompt"]},
    }},
]

# Stage 4: 状态推演工具 schema（单次工具调用替代 5 路并行 LLM）
STATE_TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "update_resources",
        "description": "更新角色属性、物品、持续状态、胜负判定",
        "parameters": {"type": "object", "properties": {
            "state_changes": {
                "type": "array",
                "description": '属性变更。每项: {"target":"player.属性名","op":"add|subtract|set","value":数值,"reason":"原因(add/subtract时必填)"}。也可修改NPC属性: target="npcs.{npc_id}.字段名" op="set"',
                "items": {"type": "object", "properties": {
                    "target": {"type": "string"}, "op": {"type": "string", "enum": ["add", "subtract", "set"]},
                    "value": {}, "reason": {"type": "string"},
                }, "required": ["target", "op", "value"]},
            },
            "inventory_changes": {
                "type": "array",
                "description": '物品变更。每项: {"item":"物品名","action":"add|remove","quantity":1,"description":"可选"}。add限制：只能添加叙事中明确描写获取的物品',
                "items": {"type": "object", "properties": {
                    "item": {"type": "string"}, "action": {"type": "string", "enum": ["add", "remove"]},
                    "quantity": {"type": "integer", "default": 1}, "description": {"type": "string"},
                }, "required": ["item", "action"]},
            },
            "activate_states": {
                "type": "array",
                "description": '激活持续状态，优先使用剧本声明的精确ID；不得另造未声明的天气或时段状态。每项: {"id":"状态ID","name":"中文显示名称（必填）","description":"一句话描述"}',
                "items": {"type": "object", "properties": {
                    "id": {"type": "string"}, "name": {"type": "string"}, "description": {"type": "string"},
                }, "required": ["id", "name"]},
            },
            "deactivate_states": {
                "type": "array", "description": "要移除的状态ID列表",
                "items": {"type": "string"},
            },
            "game_over": {
                "type": "object", "description": '游戏结束时填写: {"reason":"","ending_type":""}',
                "properties": {"reason": {"type": "string"}, "ending_type": {"type": "string"}},
            },
        }},
    }},
    {"type": "function", "function": {
        "name": "update_spatial",
        "description": "更新位置移动、地点发现、NPC位置、房间、场景描写",
        "parameters": {"type": "object", "properties": {
            "location_change": {
                "type": "string",
                "description": "玩家结束位置ID（已发现地点，或本次reveal_locations揭示的地点；未移动则省略）",
            },
            "reveal_locations": {
                "type": "array", "description": '叙事中实际发现的新地点: [{"id":"位置ID","name":"显示名称"}]；发现并移动须同次提交',
                "items": {"type": "object", "properties": {
                    "id": {"type": "string"}, "name": {"type": "string"},
                }, "required": ["id", "name"]},
            },
            "npc_location_changes": {
                "type": "array",
                "description": '本回合在场NPC位置变动: [{"npc_id":"","new_location":"位置ID","reason":"原因"}]',
                "items": {"type": "object", "properties": {
                    "npc_id": {"type": "string"}, "new_location": {"type": "string"}, "reason": {"type": "string"},
                }, "required": ["npc_id", "new_location"]},
            },
            "room_changes": {
                "type": "array",
                "description": '同一建筑内房间级移动: [{"id":"npc_id或player","new_room":"房间名","reason":"原因"}]',
                "items": {"type": "object", "properties": {
                    "id": {"type": "string"}, "new_room": {"type": "string"}, "reason": {"type": "string"},
                }, "required": ["id", "new_room"]},
            },
            "npc_contact_changes": {
                "type": "array",
                "description": '仅在叙事明确写出当面接触或告别时填写: [{"npc_id":"角色ID","in_contact":true/false}]；同区不等于已接触，已接收不等于正式接触成立',
                "items": {"type": "object", "properties": {
                    "npc_id": {"type": "string"}, "in_contact": {"type": "boolean"},
                }, "required": ["npc_id", "in_contact"]},
            },
            "scene_details": {
                "type": "object",
                "description": '场景描写: {"atmosphere":"","sensory":"","key_objects":[],"physical":{"lighting":"","floor":"","spatial_note":""}}',
                "properties": {
                    "atmosphere": {"type": "string"}, "sensory": {"type": "string"},
                    "key_objects": {"type": "array", "items": {"type": "string"}},
                    "physical": {"type": "object", "properties": {
                        "lighting": {"type": "string"}, "floor": {"type": "string"}, "spatial_note": {"type": "string"},
                    }},
                },
            },
        }},
    }},
    {"type": "function", "function": {
        "name": "update_time",
        "description": "更新游戏时间（end_time）",
        "parameters": {"type": "object", "properties": {
            "end_time": {
                "type": "string",
                "description": "叙事文本最后一句描写对应的时刻，ISO 8601格式（如1979-10-26T10:00:00）。必填。严格对齐叙事末尾时间点",
            },
        }, "required": ["end_time"]},
    }},
    {"type": "function", "function": {
        "name": "update_world",
        "description": "更新剧本声明的世界属性或宏观世界级变量；精确声明优先，不另造未声明的物件状态",
        "parameters": {"type": "object", "properties": {
            "world_property_changes": {
                "type": "array",
                "description": '世界属性变更: [{"id":"属性ID","value":"新值"}]。按剧本声明的精确ID和规则更新；未声明的物件状态属于scene_details，只允许另增宏观世界级变量',
                "items": {"type": "object", "properties": {
                    "id": {"type": "string"}, "value": {"type": "string"},
                }, "required": ["id", "value"]},
            },
        }},
    }},
    {"type": "function", "function": {
        "name": "update_extended",
        "description": "更新离场NPC动态、新NPC注册、阵营声望、道德维度、同伴变动",
        "parameters": {"type": "object", "properties": {
            "offscreen_npc_updates": {
                "type": "array",
                "description": '离场NPC动态（最多2-3个）: [{"name":"NPC中文全名","action":"简述行动","location":"当前位置"}]',
                "items": {"type": "object", "properties": {
                    "name": {"type": "string"}, "action": {"type": "string"}, "location": {"type": "string"},
                }, "required": ["name", "action"]},
            },
            "new_npcs": {
                "type": "array",
                "description": '新NPC注册（叙事中有名有姓、有台词或具体互动的非预设角色必须注册）',
                "items": {"type": "object", "properties": {
                    "id": {"type": "string", "description": "英文蛇形ID"},
                    "name": {"type": "string", "description": "完整全名"},
                    "title": {"type": "string"}, "bio": {"type": "string"},
                    "personality": {"type": "string"}, "location": {"type": "string"},
                    "trust": {"type": "integer"}, "affection": {"type": "integer"}, "fear": {"type": "integer"},
                }, "required": ["id", "name", "location"]},
            },
            "faction_reputation_changes": {
                "type": "array",
                "description": '阵营声望变化: [{"faction_id":"","change":±数值,"reason":""}]（±1到±15）',
                "items": {"type": "object", "properties": {
                    "faction_id": {"type": "string"}, "change": {"type": "integer"}, "reason": {"type": "string"},
                }, "required": ["faction_id", "change"]},
            },
            "moral_alignment_changes": {
                "type": "array",
                "description": '道德维度影响: [{"axis":"mercy_vs_cruelty|honesty_vs_deception|order_vs_chaos","change":±数值,"reason":""}]（±1到±15）',
                "items": {"type": "object", "properties": {
                    "axis": {"type": "string"}, "change": {"type": "integer"}, "reason": {"type": "string"},
                }, "required": ["axis", "change"]},
            },
            "recruit_companions": {
                "type": "array", "description": "加入队伍的NPC ID列表",
                "items": {"type": "string"},
            },
            "dismiss_companions": {
                "type": "array", "description": "离队的NPC ID列表",
                "items": {"type": "string"},
            },
            "invalidate_lore": {
                "type": "array", "description": "要废止的知识词条ID列表",
                "items": {"type": "string"},
            },
        }},
    }},
]

# Stage 4a: NPC 关系推演工具 schema（工具调用模式替代自由文本 JSON）
NPC_REACTION_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "register_npc",
            "description": "仅注册叙事中出现且尚未登记的具名NPC，不要重复注册已有NPC",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "唯一英文ID"},
                    "name": {"type": "string", "description": "叙事中出现的姓名"},
                    "location": {"type": "string", "description": "当前地点ID"},
                    "bio": {"type": "string", "description": "简短身份"},
                    "personality": {"type": "string", "description": "性格"}
                },
                "required": ["id", "name", "location"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_npc_attitude",
            "description": "更新NPC对玩家的态度变化",
            "parameters": {
                "type": "object",
                "properties": {
                    "npc_id": {"type": "string"},
                    "dimension": {"type": "string", "enum": ["trust", "affection", "fear", "overall"]},
                    "change": {"type": "integer", "description": "变化值，正为增加负为减少"},
                    "reason": {"type": "string"}
                },
                "required": ["npc_id", "dimension", "change", "reason"]
            }
        }
    }
]

# Stage 5: 选项生成工具 schema
CHOICES_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "add_choice",
            "description": "添加一个玩家可选行动",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "选项ID如c1,c2,c3"},
                    "text": {"type": "string", "description": "选项文本"},
                    "hint": {"type": "string", "description": "行动前已知的风险、成本或预计耗时；不得预告 NPC 回应或确定结果"},
                    "time_hint": {"type": "string", "description": "预计耗时如PT30M"},
                    "risk": {"type": "string", "enum": ["safe", "moderate", "risky"]}
                },
                "required": ["id", "text"]
            }
        }
    }
]

# 后台 Agent 实用工具：NPC查找、回合摘要、场景图
SETTLEMENT_UTILITY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_npc",
            "description": "按名字或ID查找已有NPC。注册新NPC前必须先查找，避免重复",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "NPC名字或ID"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_turn_summary",
            "description": "为本轮设置一句简短摘要（用于历史记录和存档列表）",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "一句话摘要，20字以内"}
                },
                "required": ["summary"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_scene_image_prompt",
            "description": "为本轮设置场景图的英文描述（如果叙事有明显的视觉变化）",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "场景图描述（英文）"},
                    "style": {"type": "string", "enum": ["realistic", "anime", "pixel"]}
                },
                "required": ["prompt"]
            }
        }
    },
]

# ── Agentic 路径：合并后的 4 工具（原 10 工具合并，workflow 路径不受影响）──

CONSOLIDATED_SETTLEMENT_TOOLS = [
    # 1. update_state — 合并 time + resources + spatial + world
    {"type": "function", "function": {
        "name": "update_state",
        "description": "更新游戏状态（所有字段可选，只填需要变更的）",
        "parameters": {"type": "object", "properties": {
            "end_time": {
                "type": "string",
                "description": "场景结束时间，ISO 8601格式（如1979-10-26T10:00:00）。每轮必填",
            },
            "state_changes": {
                "type": "array",
                "description": '属性变更。每项: {"target":"player.属性名","op":"add|subtract|set","value":数值,"reason":"原因(add/subtract时必填)"}',
                "items": {"type": "object", "properties": {
                    "target": {"type": "string"}, "op": {"type": "string", "enum": ["add", "subtract", "set"]},
                    "value": {}, "reason": {"type": "string"},
                }, "required": ["target", "op", "value"]},
            },
            "variable_changes": VARIABLE_CHANGES_SCHEMA,
            "inventory_changes": {
                "type": "array",
                "description": '物品变更。每项: {"item":"物品名","action":"add|remove","quantity":1}。add限制：只能添加叙事中明确描写获取的物品',
                "items": {"type": "object", "properties": {
                    "item": {"type": "string"}, "action": {"type": "string", "enum": ["add", "remove"]},
                    "quantity": {"type": "integer", "default": 1}, "description": {"type": "string"},
                }, "required": ["item", "action"]},
            },
            "activate_states": {
                "type": "array",
                "description": '激活持续状态，优先使用剧本声明的精确ID；不得另造未声明的天气或时段状态。每项: {"id":"状态ID","name":"中文显示名称（必填）","description":"一句话描述"}',
                "items": {"type": "object", "properties": {
                    "id": {"type": "string"}, "name": {"type": "string"}, "description": {"type": "string"},
                }, "required": ["id", "name"]},
            },
            "deactivate_states": {
                "type": "array", "description": "要移除的状态ID列表",
                "items": {"type": "string"},
            },
            "game_over": {
                "type": "object", "description": '游戏结束时填写: {"reason":"","ending_type":""}',
                "properties": {"reason": {"type": "string"}, "ending_type": {"type": "string"}},
            },
            "location_change": {
                "type": "string",
                "description": "玩家结束位置ID（已发现地点，或本次reveal_locations揭示的地点；未移动则省略）",
            },
            "reveal_locations": {
                "type": "array", "description": '叙事中实际发现的新地点: [{"id":"位置ID","name":"显示名称"}]；发现并移动须同次提交',
                "items": {"type": "object", "properties": {
                    "id": {"type": "string"}, "name": {"type": "string"},
                }, "required": ["id", "name"]},
            },
            "npc_location_changes": {
                "type": "array",
                "description": '本回合在场NPC位置变动: [{"npc_id":"","new_location":"位置ID","reason":"原因"}]',
                "items": {"type": "object", "properties": {
                    "npc_id": {"type": "string"}, "new_location": {"type": "string"}, "reason": {"type": "string"},
                }, "required": ["npc_id", "new_location"]},
            },
            "room_changes": {
                "type": "array",
                "description": '同一建筑内房间级移动: [{"id":"npc_id或player","new_room":"房间名","reason":"原因"}]',
                "items": {"type": "object", "properties": {
                    "id": {"type": "string"}, "new_room": {"type": "string"}, "reason": {"type": "string"},
                }, "required": ["id", "new_room"]},
            },
            "npc_contact_changes": {
                "type": "array",
                "description": '仅在叙事明确写出当面接触或告别时填写: [{"npc_id":"角色ID","in_contact":true/false}]；同区不等于已接触。按工具返回的接触候选值核对，后续更新或撤销会重算；不得把已接收当作正式接触成立',
                "items": {"type": "object", "properties": {
                    "npc_id": {"type": "string"}, "in_contact": {"type": "boolean"},
                }, "required": ["npc_id", "in_contact"]},
            },
            "scene_details": {
                "type": "object",
                "description": '场景描写: {"atmosphere":"","sensory":"","key_objects":[]}',
                "properties": {
                    "atmosphere": {"type": "string"}, "sensory": {"type": "string"},
                    "key_objects": {"type": "array", "items": {"type": "string"}},
                    "physical": {"type": "object", "properties": {
                        "lighting": {"type": "string"}, "floor": {"type": "string"}, "spatial_note": {"type": "string"},
                    }},
                },
            },
            "world_property_changes": {
                "type": "array",
                "description": '世界属性变更: [{"id":"属性ID","value":"新值"}]。先用consult_rules(category="world_properties")读取声明与已提交现值，按精确ID和规则更新；未声明的物件状态属于scene_details，只允许另增宏观世界级变量',
                "items": {"type": "object", "properties": {
                    "id": {"type": "string"}, "value": {"type": "string"},
                }, "required": ["id", "value"]},
            },
        }},
    }},
    # 2. manage_npcs — 合并 lookup + extended + attitude
    {"type": "function", "function": {
        "name": "manage_npcs",
        "description": "NPC管理：查找、注册、更新态度、记录离屏动态、阵营声望。用operations数组按顺序执行",
        "parameters": {"type": "object", "properties": {
            "operations": {
                "type": "array",
                "description": (
                    '操作列表。每个操作的op字段决定类型：\n'
                    '- lookup: {"op":"lookup","name":"NPC名字或ID"} — 注册前必须先查找\n'
                    '- register: {"op":"register","id":"英文蛇形ID","name":"全名","bio":"...","location":"..."} — 注册新NPC\n'
                    '- attitude: {"op":"attitude","npc_id":"","dimension":"trust|affection|fear","change":±数值,"reason":"..."}\n'
                    '- offscreen: {"op":"offscreen","name":"NPC全名","action":"简述行动","location":"当前位置"}\n'
                    '- faction: {"op":"faction","faction":"阵营ID","delta":±数值,"reason":"..."}\n'
                    '- moral: {"op":"moral","axis":"mercy_vs_cruelty|honesty_vs_deception|order_vs_chaos","change":±数值,"reason":"..."}\n'
                    '- recruit: {"op":"recruit","npc_id":"加入队伍的NPC ID"}\n'
                    '- dismiss: {"op":"dismiss","npc_id":"离队的NPC ID"}'
                ),
                "items": {"type": "object"},
            },
        }, "required": ["operations"]},
    }},
    # 3. add_choices — 批量版
    {"type": "function", "function": {
        "name": "add_choices",
        "description": "批量添加玩家选项（必须恰好4个行动选项）",
        "parameters": {"type": "object", "properties": {
            "choices": {
                "type": "array",
                "items": {"type": "object", "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "hint": {"type": "string", "description": "行动前已知的风险、成本或预计耗时；不得预告 NPC 回应或确定结果"},
                    "risk": {"type": "string", "enum": ["safe", "moderate", "risky"]},
                    "time_hint": {"type": "string"},
                }, "required": ["id", "text"]},
            },
        }, "required": ["choices"]},
    }},
    # 4. set_turn_meta — 合并 summary + image
    {"type": "function", "function": {
        "name": "set_turn_meta",
        "description": "设置本轮元数据：摘要（必须）和场景图（可选）",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string", "description": "一句话摘要，20字以内（必须）"},
            "image_prompt": {"type": "string", "description": "场景图英文描述（可选）"},
            "image_style": {"type": "string", "enum": ["realistic", "anime", "pixel"]},
        }, "required": ["summary"]},
    }},
]

# Unified agent tools: info + settlement + output in one tool set.
# Lazily assembled on first access via _unified_tools().
UNIFIED_TOOLS_SCHEMA = None  # type: list[dict] | None


def _unified_tools() -> list[dict]:
    """Build and cache the unified tools schema (12 tools)."""
    global UNIFIED_TOOLS_SCHEMA
    if UNIFIED_TOOLS_SCHEMA is not None:
        return UNIFIED_TOOLS_SCHEMA
    # submit_plan: interactive plan with optional questions/options
    plan_tool = [{"type": "function", "function": {
        "name": "submit_plan",
        "description": "提交本轮行动计划。可以包含需要用户确认的问题和选项。",
        "parameters": {"type": "object", "properties": {
            "plan": {"type": "string", "description": "简短的行动计划说明"},
            "questions": {"type": "array", "items": {"type": "object", "properties": {
                "question": {"type": "string", "description": "问题"},
                "options": {"type": "array", "items": {"type": "string"}, "description": "可选项（2-4个）"},
            }}, "description": "需要用户选择的问题（可选）"},
        }, "required": ["plan"]},
    }}]

    # 5 info tools (from foreground, excluding set_atmosphere/set_scene_image/review_narrative/roll_dice/get_time)
    INFO_NAMES = {"recall_history", "query_lorebook", "query_npc_history", "check_inventory", "get_npc_attitude"}
    info_tools = [t for t in FOREGROUND_TOOLS_SCHEMA if t["function"]["name"] in INFO_NAMES]

    # 2 action tools (from consolidated settlement)
    action_tools = [t for t in CONSOLIDATED_SETTLEMENT_TOOLS if t["function"]["name"] in ("update_state", "manage_npcs")]

    # narrative tool (dedicated creative writing step)
    narrative_tool = [{"type": "function", "function": {
        "name": "write_narrative",
        "description": "提交本轮叙事正文。这是你最重要的创作环节——用第二人称写出完整、高质量的叙事文本。写完叙事后再调用 finalize_turn 提交结构化数据。",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "完整的叙事正文。用第二人称（'你'）描写，注重细节和氛围。"},
            "event_ids": {"type": "array", "items": {"type": "string"},
                          "description": "叙事中明确写到已发生的定时事件 ID；没有则传空数组。提交时按候选状态投影校验，落库前按实际结算复核；不得把预览、条件失效或窗口外事件当成已发生。"},
        }, "required": ["text"]},
    }}]

    # 2 output tools (new)
    output_tools = [
        {"type": "function", "function": {
            "name": "finalize_turn",
            "description": "同步完成本轮结算：提交状态变更+选项+摘要。必须在 write_narrative 之后调用。",
            "parameters": {"type": "object", "properties": {
                "end_time": {"type": "string", "description": "场景结束时间ISO"},
                "state_changes": {
                    "type": "array",
                    "description": '属性变更。每项: {"target":"player.属性名","op":"add|subtract|set","value":数值,"reason":"原因"}',
                    "items": {"type": "object", "properties": {
                        "target": {"type": "string"}, "op": {"type": "string", "enum": ["add", "subtract", "set"]},
                        "value": {}, "reason": {"type": "string"},
                    }, "required": ["target", "op", "value"]},
                },
                "variable_changes": VARIABLE_CHANGES_SCHEMA,
                "inventory_changes": {
                    "type": "array",
                    "description": '物品变更。每项: {"item":"物品名","action":"add|remove","quantity":1}',
                    "items": {"type": "object", "properties": {
                        "item": {"type": "string"}, "action": {"type": "string", "enum": ["add", "remove"]},
                        "quantity": {"type": "integer", "default": 1},
                    }, "required": ["item", "action"]},
                },
                "activate_states": {
                    "type": "array",
                    "description": '激活持续状态，优先使用剧本声明的精确ID；不得另造未声明的天气或时段状态。每项: {"id":"状态ID","name":"中文名称","description":"描述"}',
                    "items": {"type": "object", "properties": {
                        "id": {"type": "string"}, "name": {"type": "string"}, "description": {"type": "string"},
                    }, "required": ["id", "name"]},
                },
                "location_change": {"type": "string", "description": "玩家所在位置ID（已发现或本次揭示，未移动则省略）"},
                "reveal_locations": {
                    "type": "array", "description": "叙事中实际发现的新地点；可与location_change同次提交",
                    "items": {"type": "object", "properties": {
                        "id": {"type": "string"}, "name": {"type": "string"},
                    }, "required": ["id", "name"]},
                },
                "lore_reveals": {
                    "type": "array", "description": "仅在来源NPC当面说出具体线索后提交原话；部分内容只记录线索，不自动解锁词条全文",
                    "items": {"type": "object", "properties": {
                        "entry_id": {"type": "string"},
                        "evidence": {"type": "string", "description": "叙事中来源NPC原话，包含中文弯引号"},
                        "occurred_at": {"type": "string", "description": "仅当本轮跨越词条开放时间时填写，ISO时间；叙事须在原话前明确写出对应的年月日时分"},
                    }, "required": ["entry_id", "evidence"]},
                },
                "event_reports": {
                    "type": "array", "description": "在场NPC当面转述已经发生的一次性事件；仅记录未经核实的原话，不自动视为事件真相",
                    "items": {"type": "object", "properties": {
                        "event_id": {"type": "string"},
                        "npc_id": {"type": "string"},
                        "evidence": {"type": "string", "description": "叙事中来源NPC原话，包含中文弯引号"},
                        "occurred_at": {"type": "string", "description": "若事件在本轮发生，填写获知的ISO时间，并在叙事原话前写出相同年月日时分"},
                    }, "required": ["event_id", "npc_id", "evidence"]},
                },
                "npc_operations": {
                    "type": "array",
                    "description": "manage_npcs 的 operations 格式",
                    "items": {"type": "object"},
                },
                "choices": {
                    "type": "array",
                    "description": "4个行动选项",
                    "items": {"type": "object", "properties": {
                        "id": {"type": "string"}, "text": {"type": "string"},
                        "hint": {"type": "string", "description": "行动前已知的风险、成本或预计耗时；不得预告 NPC 回应或确定结果"}, "risk": {"type": "string", "enum": ["safe", "moderate", "risky"]},
                        "time_hint": {"type": "string"},
                    }, "required": ["id", "text"]},
                },
                "summary": {"type": "string", "description": "一句话摘要（必须）"},
                "image_prompt": {"type": "string", "description": "场景图英文描述"},
                "image_style": {"type": "string", "enum": ["realistic", "anime", "pixel"]},
                "proactive_hint": {"type": "string", "description": "主动提示/建议（如'提醒：xxx 事件即将发生'）"},
            }, "required": ["choices", "summary"]},
        }},
        {"type": "function", "function": {
            "name": "delegate_settlement",
            "description": "委托结算 Agent 处理复杂状态。必须在 write_narrative 之后调用；叙事先展示，结算完成后统一保存本轮状态。",
            "parameters": {"type": "object", "properties": {
                "hint": {"type": "string", "description": "给后台Agent的结算提示"},
                "lore_reveals": {
                    "type": "array", "description": "来源NPC当面说出的具体线索；以最终叙事重新校验，部分内容不解锁全文",
                    "items": {"type": "object", "properties": {
                        "entry_id": {"type": "string"},
                        "evidence": {"type": "string"},
                        "occurred_at": {"type": "string", "description": "跨越词条开放时间时填写ISO时间，叙事原话前须有相同年月日时分"},
                    }, "required": ["entry_id", "evidence"]},
                },
                "event_reports": {
                    "type": "array", "description": "在场NPC当面转述已经发生的一次性事件，保留来源原话与获知时间",
                    "items": {"type": "object", "properties": {
                        "event_id": {"type": "string"},
                        "npc_id": {"type": "string"},
                        "evidence": {"type": "string"},
                        "occurred_at": {"type": "string", "description": "事件在本轮发生时必填，叙事原话前须有相同年月日时分"},
                    }, "required": ["event_id", "npc_id", "evidence"]},
                },
                "choices": {
                    "type": "array",
                    "description": "4个行动选项",
                    "items": {"type": "object", "properties": {
                        "id": {"type": "string"}, "text": {"type": "string"},
                        "hint": {"type": "string", "description": "行动前已知的风险、成本或预计耗时；不得预告 NPC 回应或确定结果"}, "risk": {"type": "string", "enum": ["safe", "moderate", "risky"]},
                        "time_hint": {"type": "string"},
                    }, "required": ["id", "text"]},
                },
                "summary": {"type": "string", "description": "一句话摘要"},
                "proactive_hint": {"type": "string", "description": "主动提示/建议"},
            }, "required": ["choices", "summary"]},
        }},
    ]

    # --- #4/#6 中期进化：图谱遍历 + 规则咨询 ---
    graph_tools = [
        {"type": "function", "function": {
            "name": "traverse_graph",
            "description": "从某个角色/地点/事件出发，查询关联网络（2层深度）",
            "parameters": {"type": "object", "properties": {
                "entity": {"type": "string", "description": "实体名称或ID"},
                "depth": {"type": "integer", "description": "遍历深度", "default": 2},
            }, "required": ["entity"]},
        }},
        {"type": "function", "function": {
            "name": "consult_rules",
            "description": "只读咨询当前分支规则。指定 category 可检索完整声明；locations 包含通行条件、阻止原因和默认房间，不揭示地点或执行移动。story_nodes 默认返回 tree 元数据和完整 node。先用 view=runtime、node_status=active 分页发现当前节点与选项身份/可选性，再用定义视图 query=node_id 查规则，用 preview_choice_outcome 查后果。运行态不激活或结算节点，unlocked 不等于可提交。不指定类别时检查 NPC 在场、接触和物品持有。幕后信息不是角色已知。",
            "parameters": {"type": "object", "properties": {
                "question": {"type": "string", "description": "不指定 category 时，含 NPC 姓名及在场/在这检查房间感知的在场或附近；含 NPC ID或姓名及接触返回 contacts 中的 in_contact 和建立接触的 block_reason，支持运行时角色；含物品名及可用/有没有检查持有。不判断使用条件或互动许可，默认读取已提交状态"},
                "context": {"type": "string", "description": "相关上下文"},
                "category": {"type": "string", "enum": ["variables", "triggers", "random_items", "one_time_events", "cyclic_events", "persistent_states", "world_properties", "story_nodes", "locations", "npc_goals", "org_goals", "npc_schedules"],
                             "description": "机制类别；指定后无需 question。npc_schedules 返回已注册角色的 npc_id/npc_name、完整基础 schedule 和分支 overrides，保留已到期声明，不判断生效或预测位置。npc_goals/org_goals 返回所属 actor_id/actor_name、完整 goal 声明及正式 status/completion；pending 仅指没有完成记录。以上均不执行效果、非角色知情，不支持候选或 runtime 视图。每次最多返回5条，每条 content 最多2000字符"},
                "view": {"type": "string", "enum": ["definition", "runtime"], "default": "definition",
                         "description": "runtime 仅用于 story_nodes，返回紧凑的节点身份、状态、choices 中的 choice_id/available/reason、已选项和计时/冷却信息；无规则正文。available 只检查激活和选项条件，不模拟效果。默认 definition 保持完整规则查询。切换视图须重新查列表，不能复用 snapshot"},
                "node_status": {"type": "string", "enum": ["active", "completed", "unlocked", "locked"],
                                "description": "仅运行态列表：按当前状态筛选；省略则全部。不是激活预测，不与 rule_index 同传；列表翻页保持此参数"},
                "query": {"type": "string", "description": "在所选视图的内容中搜索，不区分大小写；空字符串列出该类别或状态"},
                "include_candidates": {"type": "boolean", "default": False,
                                        "description": "进行中回合可用：variables/world_properties 附加 candidate_value/candidate_exists，current_value 仍是已提交值，不含未结算事件，运行时世界属性 definition=null。story_nodes 的 runtime 视图与选择预检共用候选基线，仅在已接收 end_time 后纳入定时事件及剧情解锁，不含事件掷骰或完整生命周期，不提前激活节点；续读保持此参数、view、snapshot。无 category 时按 question 查询候选在场、接触和物品：已接收更新及显式 end_time 内的定时事件后，刷新日程、房间并重算接触；不含完整生命周期。所有候选均未提交、非角色知情；更新、撤销、切换分支后重查"},
                "rule_index": {"type": "integer", "minimum": 0,
                               "description": "读取某条查询结果的完整定义时，传入其 rule_index 和该条目 snapshot（offset=0 也必须携带）；不可与 query 混用"},
                "offset": {"type": "integer", "minimum": 0, "default": 0,
                           "description": "未指定 rule_index 时按顶层 next_offset/snapshot 翻页；指定时按条目 next_offset/snapshot 续读。无 result_path 时按原始 JSON 字符；指定字段时容器按成员、字符串按字符、标量仅0；换字段从0开始"},
                "result_path": {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "integer", "minimum": 0}]},
                                "description": "须同时提供 category、rule_index、条目 snapshot。[] 读取 JSON 根目录，再按键或索引下钻。只读 JSON，不是文件路径或表达式。条目 content_format=result_section；容器返回 entries，小值附 value，长值仅描述。runtime 的 choices 长选项另附 summary，保留 choice_id/available/reason，不含完整标签；按成员 key 读取 [choices,索引,label]。字符串返回 content，标量返回 value。next_offset=null 不保证完整；条目 read 用于当前字段从0重读，total_chars 仍是整个条目的长度"},
                "snapshot": {"type": "string", "pattern": "^[A-Za-z0-9_-]{22}$",
                             "description": "原样携带返回的快照：列表翻页使用顶层 snapshot 并保持 query，条目读取使用该条目的 snapshot。首次查列表可省略。收到 restart_required 时丢弃旧片段，按 restart 重查；不得换新快照接着拼旧正文"},
            }},
        }},
    ]

    UNIFIED_TOOLS_SCHEMA = plan_tool + info_tools + action_tools + narrative_tool + output_tools + graph_tools + [
        {"type": "function", "function": {
            "name": "peek_upcoming_events",
            "description": "按正式定时结算只读投影预览计划事件和未触发条件候选，纳入触发时刻与前序事件效果（每次 1-24 小时；状态改变后重新查询，不代表已发生或主角知情）",
            "parameters": {"type": "object", "properties": {
                "hours_ahead": {"type": "integer", "description": "向前看几小时（1-24）", "default": 3},
                "start_after_hours": {"type": "integer", "description": "从当前存档时间起跳过几小时，默认 0；等待超过24小时可按24、48……查询后续时间段；相邻窗口不会重复返回边界事件", "default": 0},
                "offset": {"type": "integer", "description": "跳过前几个事件，每页最多 5 个；结果截断时以 5、10……继续读取", "default": 0},
            }},
        }},
        {"type": "function", "function": {
            "name": "undo_my_last_action",
            "description": "撤销本轮上一次非查询工具的可回退结果；中间的只读查询不改变回退点。撤销后应重新查询候选信息，仅支持回退一步。",
            "parameters": {"type": "object", "properties": {
                "reason": {"type": "string", "description": "为什么要撤销"},
            }, "required": ["reason"]},
        }},
        {"type": "function", "function": {
            "name": "trace_causality",
            "description": "追溯某事件的因果链（向上追原因，向下追后果）",
            "parameters": {"type": "object", "properties": {
                "entity": {"type": "string", "description": "事件/实体名称或ID"},
                "direction": {"type": "string", "enum": ["causes", "effects", "both"], "description": "追溯方向", "default": "both"},
                "depth": {"type": "integer", "description": "追溯深度", "default": 3},
            }, "required": ["entity"]},
        }},
        {"type": "function", "function": {
            "name": "preview_choice_outcome",
            "description": "GM 内部只读预检：用 consult_rules 的 story_nodes 声明中的 node_id、choice_id 在隔离副本结算当前激活选择及其派发事件，不保存、不建索引；可基于候选状态，不含事件掷骰或完整回合生命周期。大结果优先按 read 加 result_path=[] 查目录，再把 entries 的 key 追加到路径读相关字段；每页可独立解析，不必拼回整个预检。complete=false 表示值不完整，next_offset=null 仅表示当前层分页结束，不表示所有后果已读。正文省略时按 read 从 offset=0 重读；不传 result_path 时保持原协议，携带 snapshot 按 next_offset 续读，拼接全部 JSON 片段后解析。快照失效则丢弃旧片段按 restart 重查。无身份和续读参数时才按 choice_text 粗估风险。不得把预演后果写入玩家 hint 或预告 NPC 回应。",
            "parameters": {"type": "object", "properties": {
                "node_id": {"type": "string", "description": "当前分支剧情树选择节点ID；与 choice_id 同时提供"},
                "choice_id": {"type": "string", "description": "节点中已声明的选项ID；不是面向玩家的生成选项ID"},
                "include_candidates": {"type": "boolean", "description": "仅声明预检：与 story_nodes 运行态查询共用候选基线，以已接收 update_state 及已接收 end_time 内定时事件为基线，不使用预估时间，不提前激活节点。差异仅含基线后的选择效果，不含事件掷骰或完整生命周期，实际提交须重新检查", "default": False},
                "offset": {"type": "integer", "minimum": 0, "description": "不传 result_path 时为完整预检 JSON 的字符偏移；指定路径时为容器成员偏移或字符串字符偏移，标量仅0。文本每片最多2000字；包括0都须携带 snapshot。初查不传；换路径或正文省略后从0重读"},
                "result_path": {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "integer", "minimum": 0}]}, "description": "仅在本次模拟结果 JSON 内按字符串键/非负整数索引读取；[] 为根目录，须携带 snapshot。容器返回 entries(key/value_type/size/complete；小值附 value)，长值按追加 key 的路径下钻。字符串返回 content 文本片段，标量返回 value。size 是字段成员数或字符数，total_chars 仍是完整预检长度；next_offset=null 不保证整个值 complete。不是文件路径或表达式。"},
                "snapshot": {"type": "string", "description": "原样使用预检返回的快照，保持相同节点、选项与候选视图；输入或结果变化后丢弃旧片段重查"},
                "choice_text": {"type": "string", "description": "无节点ID时用于旧式粗略风险估计的文本；不会执行剧本声明"},
                "context": {"type": "string", "description": "当前场景摘要"},
            }},
        }},
        {"type": "function", "function": {
            "name": "read_director_notes",
            "description": "阅读导演笔记（后台整理的叙事摘要），了解之前的关键剧情、人物关系和线索",
            "parameters": {"type": "object", "properties": {
                "category": {"type": "string", "enum": ["character", "plot", "world", "relationship", "clue", "all"], "description": "笔记类别", "default": "all"},
                "limit": {"type": "integer", "description": "返回条数", "default": 5},
            }},
        }},
        {"type": "function", "function": {
            "name": "invoke_character",
            "description": "为GM推演单个NPC的行为/对话建议，可用于幕后角色；不是玩家对话入口，不建立接触或提交状态。默认读取正式角色状态，进行中回合可显式使用候选时空；自动注入的角色知识仍仅来自正式状态。更新、撤销或切换分支后重新调用，不能把建议当成已发生事件或互动许可。",
            "parameters": {"type": "object", "properties": {
                "npc_id": {"type": "string", "description": "NPC的ID"},
                "situation": {"type": "string", "description": "给NPC看的可感知刺激或明确假设，不要塞入其未知的GM秘密"},
                "include_candidates": {"type": "boolean", "description": "默认false。true仅限进行中回合，投影已接收更新及已接收结束时间内定时事件，随后刷新日程、房间并重算接触；不采用预估时间，不包含完整生命周期或候选知识传播"},
            }, "required": ["npc_id", "situation"]},
        }},
        {"type": "function", "function": {
            "name": "attempt_deduction",
            "description": "将多条线索关联推理。如果推理成立，可能解锁新地点、NPC秘密或里程碑。",
            "parameters": {"type": "object", "properties": {
                "clue_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要关联推理的线索ID列表（至少2条）",
                },
                "reasoning": {"type": "string", "description": "你的推理过程简述"},
            }, "required": ["clue_ids"]},
        }},
    ]
    return UNIFIED_TOOLS_SCHEMA
