# Adapters

The core is LLM-agnostic: `AGENTS.md` plus `agentic.toml` plus `.agentic/`. These adapters are
optional conveniences for specific tools. None of them changes what the gates check.

| Tool | What reads the rules | Adapter |
|---|---|---|
| OpenAI Codex CLI | `AGENTS.md` natively | none needed |
| Claude Code | `CLAUDE.md`, which is `@AGENTS.md` | `claude-code/settings.example.json` adds a Stop hook that blocks "done" while a gate fails |
| Gemini CLI | `GEMINI.md`, which is `@AGENTS.md` | none needed |
| Cursor | `.cursor/rules/*.mdc` | `cursor/.cursor/rules/agentic.mdc` points at `AGENTS.md` |
| Anything else | pipe `AGENTS.md` into the system prompt | `loop.py` already does this via `[loop].agent_command` |

Keep the pointer files one line long. Duplicating rules across `CLAUDE.md`, `GEMINI.md` and
`AGENTS.md` is how they drift.
