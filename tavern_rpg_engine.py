"""RPG 推演引擎入口 -- 瘦壳"""
# 保持向后兼容的导入
from rpg.llm_utils import *
from rpg.models import *
from rpg.agents import *
from rpg.session import *
from rpg.tool_system import *

if __name__ == "__main__":
    from rpg.routes import main
    main()
