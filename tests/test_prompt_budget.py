"""Unit tests for shared/prompt_budget.py — per-agent prompt assembly and budgeting."""
from __future__ import annotations

from types import SimpleNamespace

from shared.config import TGsConfig
from shared.prompt_budget import (
    BLOCK_SEPARATOR,
    BOILERPLATE_DEFINITION,
    BOILERPLATE_LEGACY,
    BOILERPLATE_REORDERED,
    boilerplate_mode,
    effective_budget,
    render,
)


class TestRenderOrdering:
    def test_stable_precedes_variable(self):
        result = render(stable=["INSTRUCTIONS"], variable=["target: a.py"])
        assert result.text == f"INSTRUCTIONS{BLOCK_SEPARATOR}target: a.py"

    def test_stable_block_is_a_shared_prefix(self):
        """The point of the ordering: same stable text = same leading bytes.

        If the variable part came first, two agents of the same dimension would
        diverge at byte one and no provider prefix cache could ever hit.
        """
        stable = "S" * 120
        a = render(stable=[stable], variable=["file: a.py"])
        b = render(stable=[stable], variable=["file: b_with_a_longer_name.py"])
        assert a.text.startswith(stable)
        assert b.text.startswith(stable)

    def test_empty_blocks_dropped(self):
        result = render(stable=["", "   "], variable=["only this"])
        assert result.text == "only this"

    def test_block_order_within_a_group_is_preserved(self):
        result = render(variable=["first", "second", "third"])
        assert result.text.index("first") < result.text.index("second") < result.text.index("third")

    def test_no_budget_is_a_noop(self):
        result = render(stable=["a" * 5000], variable=["b" * 5000], budget=0)
        assert len(result.text) == 5000 + len(BLOCK_SEPARATOR) + 5000
        assert result.compressed is False
        assert result.saved_chars == 0


class TestRenderBudget:
    def test_overflow_is_compressed_within_budget(self):
        variable = "# a comment\n\n\nreal line\n\n" + "z" * 800
        result = render(stable=["KEEP"], variable=[variable], budget=300)
        assert len(result.text) <= 300
        assert result.saved_chars > 0
        assert result.compressed

    def test_never_inflates_a_short_but_over_budget_prompt(self):
        """Regression: fixed-size summary truncation can *grow* short input.

        ContextCompressor's summary layer keeps hardcoded head/tail sizes, so on a
        ~500 char input it emitted ~640 chars. Budgeting must only ever shrink.
        """
        variable = "z" * 500
        result = render(stable=["KEEP"], variable=[variable], budget=200)
        assert len(result.text) <= len(f"KEEP{BLOCK_SEPARATOR}{variable}")

    def test_instructions_are_never_truncated(self):
        """A cap that eats the instructions produces an agent doing the wrong job."""
        stable = "S" * 400
        result = render(stable=[stable], variable=["v" * 400], budget=50)
        assert stable in result.text

    def test_truncation_sentinel_states_the_omission(self):
        result = render(variable=["x" * 4000], budget=1000)
        assert "omitted to fit the prompt budget" in result.text

    def test_accounting_fields(self):
        result = render(stable=["abc"], variable=["de"])
        assert result.stable_chars == 3
        assert result.variable_chars == 2
        assert result.original_chars == len(result.text)


class TestBoilerplateMode:
    def test_legacy_when_flag_off(self):
        config = TGsConfig()
        config.prompt_economy.externalize_boilerplate = False
        assert boilerplate_mode(config, "claude-code") == BOILERPLATE_LEGACY

    def test_definition_for_capable_shell(self):
        assert boilerplate_mode(TGsConfig(), "claude-code") == BOILERPLATE_DEFINITION

    def test_reordered_for_shell_without_definitions(self):
        """Junie has no definition directory, so the text must stay inline."""
        assert boilerplate_mode(TGsConfig(), "junie") == BOILERPLATE_REORDERED

    def test_unknown_shell_does_not_inherit_capability(self):
        """effective_profile() falls back to an advisory profile for unknown shells;
        that fallback must not grant a capability the host does not have."""
        assert boilerplate_mode(TGsConfig(), "some-unknown-shell") == BOILERPLATE_REORDERED

    def test_missing_config_is_legacy(self):
        assert boilerplate_mode(None, "claude-code") == BOILERPLATE_LEGACY

    def test_broken_config_object_is_legacy(self):
        assert boilerplate_mode(SimpleNamespace(), "claude-code") == BOILERPLATE_LEGACY


class TestEffectiveBudget:
    def test_zero_by_default(self):
        assert effective_budget(TGsConfig(), "claude-code") == 0

    def test_global_budget_applies(self):
        config = TGsConfig()
        config.prompt_economy.prompt_char_budget = 12_000
        assert effective_budget(config, "claude-code") == 12_000

    def test_per_shell_budget_wins_over_global(self):
        config = TGsConfig()
        config.prompt_economy.prompt_char_budget = 12_000
        profile = config.routing_policy.effective_profile("claude-code")
        config.routing_policy.shells["claude-code"] = profile.__class__(
            **{**profile.__dict__, "prompt_char_budget": 4_000}
        )
        assert effective_budget(config, "claude-code") == 4_000

    def test_no_config_is_zero(self):
        assert effective_budget(None, "claude-code") == 0
