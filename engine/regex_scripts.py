"""Regex script engine: post-processing rules applied to AI output or user input.

Supports PUA-token layer separation so that display-only / session-only
replacements never leak across layers.
"""

import re
from typing import Iterator

# ---------------------------------------------------------------------------
# PUA (Private Use Area) token helpers
# ---------------------------------------------------------------------------
_PUA_BASE = 0xE000
_PUA_LIMIT = 0xF8FF  # end of BMP PUA block


def _pua_tokens() -> Iterator[str]:
    """Yield single-char PUA tokens sequentially."""
    code = _PUA_BASE
    while code <= _PUA_LIMIT:
        yield chr(code)
        code += 1


class RegexScriptEngine:
    """Applies regex find/replace rules based on placement."""

    def __init__(self, scripts: list[dict]):
        self.scripts = [s for s in scripts if s.get("enabled", True)]

    # ---- original single-layer API (unchanged for callers) ----------------

    def apply(self, text: str, placement: str) -> str:
        """Apply all matching regex scripts to text."""
        for script in self.scripts:
            script_placement = script.get("placement", "ai_output")
            if isinstance(script_placement, list):
                if placement not in script_placement:
                    continue
            elif placement != script_placement:
                continue
            find = script.get("find", "")
            if not find:
                continue
            try:
                flags = 0
                if script.get("ignore_case"):
                    flags |= re.IGNORECASE
                if script.get("dotall"):
                    flags |= re.DOTALL
                text = re.sub(find, script.get("replace", ""), text, flags=flags)
            except re.error:
                continue
        return text

    # ---- three-layer API (PUA token isolation) ----------------------------

    def apply_layered(self, text: str, rules: list[dict]) -> dict:
        """Three-layer regex processing with PUA token isolation.

        Each rule dict may contain:
            find          -- regex pattern (required)
            replace       -- replacement string
            display_only  -- if True, only affects the display layer
            session_only  -- if True, only affects the session (LLM) layer
            ignore_case   -- bool
            dotall        -- bool

        Returns ``{"source": ..., "session": ..., "display": ...}`` where
        *source* is the untouched original, *session* is what the LLM sees,
        and *display* is what the user sees.
        """
        token_gen = _pua_tokens()
        pua_store: dict[str, str] = {}  # PUA char -> replacement text

        source_text = text
        session_text = text
        display_text = text

        for rule in rules:
            pattern = rule.get("find", "")
            if not pattern:
                continue
            replacement = rule.get("replace", "")
            is_display_only = rule.get("display_only", False)
            is_session_only = rule.get("session_only", False)

            flags = 0
            if rule.get("ignore_case"):
                flags |= re.IGNORECASE
            if rule.get("dotall"):
                flags |= re.DOTALL

            try:
                if is_display_only:
                    display_text = re.sub(pattern, replacement, display_text, flags=flags)
                elif is_session_only:
                    session_text = re.sub(pattern, replacement, session_text, flags=flags)
                else:
                    # Both layers -- use PUA tokens so the two substitutions
                    # stay independent of each other.
                    token = next(token_gen, None)
                    if token is not None:
                        pua_store[token] = replacement
                        session_text = re.sub(pattern, token, session_text, flags=flags)
                        display_text = re.sub(pattern, token, display_text, flags=flags)
                    else:
                        # PUA range exhausted; fall back to direct replacement
                        session_text = re.sub(pattern, replacement, session_text, flags=flags)
                        display_text = re.sub(pattern, replacement, display_text, flags=flags)
            except re.error:
                continue

        # Resolve PUA tokens back to real replacement text
        for token, real in pua_store.items():
            session_text = session_text.replace(token, real)
            display_text = display_text.replace(token, real)

        return {
            "source": source_text,
            "session": session_text,
            "display": display_text,
        }
