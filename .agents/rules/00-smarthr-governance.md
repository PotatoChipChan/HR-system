# SmartHR Governance Rule

Before any non-trivial analysis, code change, review, debugging, or UI work on the SmartHR project:

1. Read and follow `AGENTS.md` for agent workflow rules.
2. Read `ARCHITECTURE.md` for the current system architecture.
3. Read `DECISIONS.md` for confirmed architecture/product decisions.

Treat their confirmed content as mandatory project context. Read only the relevant sections needed for the task.

These are the canonical sources of truth. Do not duplicate their content here.

## Always-On Activation

This rule is designed to be always-on. The Antigravity workspace rules format (`.agents/rules/*.md`) applies rules globally to the workspace. If your Antigravity version requires a manual UI toggle to activate this rule, please enable it via the Antigravity rules settings panel (one-time action).
