# Threnody — Custom Instructions

Threnody generates AI-shell-specific instruction blocks during installation.
The generated block clearly names the shell it applies to and reflects the
current `routing_policy` in `config.yaml`.

## Routing policy

Configure instruction strictness without editing generated instruction files by
hand:

```yaml
routing_policy:
  mode: default # default | strict | advisory | custom
  shells:
    github-copilot-cli:
      mode: advisory
    claude-code:
      mode: strict
```

`mode: default` uses Threnody recommendations:

| Shell | Default behavior |
|---|---|
| `claude-code` | Strict routing, low-tier `execute_subtask`, transparency tables, and Claude edit/write hook guidance |
| `github-copilot-cli` | Advisory routing; direct edits are allowed by default |
| `gemini-cli` | Advisory routing |
| `cursor` | Advisory routing |
| `codex` | Advisory routing |

Use `mode: strict` to make routing mandatory in generated instructions for all
shells. Use `mode: advisory` to make routing non-mandatory for all shells. Use
`mode: custom` with per-shell overrides when you want mixed behavior.

Per-shell profiles may set:

```yaml
routing_policy:
  mode: custom
  shells:
    github-copilot-cli:
      route_task_mandatory: true
      low_tier_execute_subtask: true
      agent_transparency_required: true
      direct_edit_hooks: false
      tier_model_mapping:
        low: gpt-5-mini
        medium: claude-sonnet-4.6
        high: claude-opus-4.6
```

`direct_edit_hooks` is only supported for shells with a real hook surface. Today
that means Claude Code. GitHub Copilot CLI receives advisory instructions by
default and does not receive Claude `PreToolUse` hook language unless a supported
hook surface is added.

## Routing exemptions

Threnody uses an exemption list, not a code-file allowlist. Built-in
exemptions cover Markdown docs (`.md`), Cursor rule docs (`.mdc`), and known AI
assistant instruction files such as `CLAUDE.md`, `GEMINI.md`, `AGENTS.md`,
`copilot-instructions.md`, `.cursorrules`, `.windsurfrules`, and `.clinerules`.
All other filetypes remain routed by default unless explicitly added under
`routing_exceptions` in `config.yaml`.

## Rendering instructions manually

The installer calls the renderer automatically. To inspect or copy a block
manually:

```bash
python3 -m shared.instructions claude-code --config ~/.local/lib/threnody/config.yaml
python3 -m shared.instructions github-copilot-cli --config ~/.local/lib/threnody/config.yaml
python3 -m shared.instructions cursor --config ~/.local/lib/threnody/config.yaml --verbatim
```

The managed block markers remain stable:

| Marker | Shell |
|---|---|
| `<!-- Threnody:claude:start -->` | Claude Code |
| `<!-- Threnody:copilot:start -->` | GitHub Copilot CLI |
| `<!-- Threnody:gemini:start -->` | Gemini CLI |
| `<!-- Threnody:codex:start -->` | OpenAI Codex |
| `<!-- Threnody:junie:start -->` | JetBrains Junie |

Cursor's `.mdc` rule file is written as a standalone generated document instead
of a marked block.

## Legal and provider terms

Threnody is not affiliated with or endorsed by any AI provider. Operators are
responsible for complying with each provider's terms of service. Provider terms,
policies, and enforcement may change at any time without notice; Threnody cannot
guarantee continued compatibility with any provider's rules.

- See [docs/LEGAL.md](docs/LEGAL.md) for routing risk tiers and team guidance
- Cross-routing Claude Pro/Max subscription OAuth from non-Claude hosts carries
  the highest provider-policy risk
- Claude Code → Claude Code routing is blocked by default; explicit opt-in only
- Safer routing examples live in [config.example.yaml](config.example.yaml)
