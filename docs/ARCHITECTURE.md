# Architecture

> Full architecture diagram: see `architecture.png` in project root.

## System Overview

SentinelLayer implements a **governance pipeline** pattern:

```
Operational Agent → MCP Interception → 4 SRI™ Agents (parallel) → Decision Engine → Verdict
```

## Key Design Decisions

1. **MCP as interception layer** — Standard protocol, any agent can integrate
2. **Parallel agent evaluation** — All 4 SRI™ dimensions scored simultaneously
3. **Branded scoring (SRI™)** — Memorable, consistent, enterprise-ready
4. **Immutable audit trail** — Every decision logged for compliance
5. **Configurable thresholds** — SRI thresholds tunable per organization

## Data Flow

See the project plan for detailed data flow diagrams and agent specifications.
