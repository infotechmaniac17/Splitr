# Splitr

Item-level expense splitting. See `docs/ARCHITECTURE.md` for the full design
and `CLAUDE.md` for build rules and milestone order.

## Getting started (with Claude Code)
1. `docker compose up -d`        # Postgres + Redis
2. `cp .env.example .env`
3. Open Claude Code in this folder: `claude`
4. Run `/agents` to confirm the 6 project subagents loaded.
5. Kick off Milestone 1 (see CLAUDE.md build order).

Subagents live in `.claude/agents/`:
backend-engineer, pdf-extraction-engineer, frontend-engineer,
mobile-engineer, devops-engineer, finance-logic-reviewer (read-only).
