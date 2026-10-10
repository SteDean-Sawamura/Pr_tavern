"""A/B testing framework: compare different prompt strategies."""
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class ABTestRunner:
    def __init__(self):
        self.experiments = []  # historical experiment records

    async def run_comparison(self, session, player_action, variant_a_config, variant_b_config):
        """Run two variants for the same action, return comparison."""
        result_a = await self._run_variant(session, player_action, variant_a_config, "A")
        result_b = await self._run_variant(session, player_action, variant_b_config, "B")

        comparison = {
            "timestamp": datetime.now().isoformat(),
            "action": player_action.get("text", "")[:100],
            "variant_a": {
                "config": variant_a_config,
                "narrative_length": len(result_a.get("narrative", "")),
                "tools_count": result_a.get("tools_count", 0),
                "tokens": result_a.get("tokens", 0),
            },
            "variant_b": {
                "config": variant_b_config,
                "narrative_length": len(result_b.get("narrative", "")),
                "tools_count": result_b.get("tools_count", 0),
                "tokens": result_b.get("tokens", 0),
            },
            "narratives": {
                "a": result_a.get("narrative", "")[:500],
                "b": result_b.get("narrative", "")[:500],
            },
        }

        self.experiments.append(comparison)
        if len(self.experiments) > 20:
            self.experiments.pop(0)
        return comparison

    async def _run_variant(self, session, player_action, config, label):
        """Run a single variant (narrative generation only, no state application)."""
        from engine.session.agentic_mixin import _AgentResult, FOREGROUND_TOOLS_SCHEMA

        system = config.get("system_override", session._build_unified_system())
        max_rounds = config.get("max_rounds", 5)

        holder = _AgentResult()
        async for _ in session._agent_loop(
            [{"role": "user", "content": session._build_unified_context({}, player_action)}],
            system, FOREGROUND_TOOLS_SCHEMA,
            lambda n, a: session._run_tool_native(n, a),
            max_rounds=max_rounds, label=f"AB-{label}",
            result_holder=holder,
        ):
            pass

        return {
            "narrative": holder.text,
            "tools_count": len(holder.records),
            "tokens": holder.prompt_tokens + holder.completion_tokens,
        }

    def get_experiments(self):
        return self.experiments[-20:]
