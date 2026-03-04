# Current Architecture vs Azure MCP Server — Analysis & Planning

> Generated: 2026-03-04
> Context: Architectural decision on whether to adopt Azure MCP Server tools and whether to add a Gen AI chat capability to the RuriSkry dashboard.

---

## Question 1: Should we replace our direct SDK calls with Azure MCP Server tools?

### What we currently call

| Azure Service | Module | How | Latency |
|---|---|---|---|
| Resource Graph (KQL) | `azure_tools.py`, `resource_graph.py` | Direct SDK (`azure.mgmt.resourcegraph`) | ~100–300ms |
| Monitor Metrics | `azure_tools.py` | Direct SDK (`azure.monitor.query`) | ~200–500ms |
| Monitor Logs | `azure_tools.py` | Direct SDK (`azure.monitor.query`) | ~200–400ms |
| Retail Prices | `cost_lookup.py` | Public REST (`httpx`) | ~200–400ms, 0ms cached |
| AI Search | `search_client.py` | Direct SDK (`azure.search.documents`) | ~100–200ms |
| Cosmos DB | `cosmos_client.py` | Direct SDK (`azure.cosmos`) | ~50–150ms |
| Azure OpenAI | Agent Framework | `openai.AsyncAzureOpenAI` | ~500–2000ms |

### What Azure MCP Server provides for those same services

MCP has tools for: `monitor` (logs + metrics), `pricing`, `search`, `cosmos`, `compute` (VM details), `policy`, `group`, `subscription` — so there's overlap with almost everything we call.

### The honest comparison

| Dimension | Direct SDK (current) | Azure MCP Server |
|---|---|---|
| **Latency per call** | 100–500ms | 150–600ms (adds ~50–100ms MCP protocol overhead per call) |
| **Latency if LLM-routed** | N/A — our queries are hardcoded | 650–2600ms (LLM decides which MCP tool → MCP protocol → Azure API) |
| **Concurrent queries** | `asyncio.gather(4 KQL queries)` = ~300ms total | MCP tools are sequential unless you run 4 MCP calls in parallel yourself — more complexity |
| **Caching** | Full control — module-level `_cache` dict, shared sync/async, TTL-able | None built in — you'd build your own on top |
| **Error handling** | Full control — `RuntimeError` on failure, graceful mock fallback, retry with exponential backoff | MCP surfaces errors as tool results — less granular control |
| **Testability** | Mock at SDK level, well-tested (505 tests) | Harder — MCP server is an external process; must mock the whole MCP transport |
| **Query precision** | Exact OData filters, exact KQL, exact params | MCP abstracts the query — you lose control over filter specifics |
| **Setup cost** | Already done, working in production | New dependency: MCP server process, MCP client library, configuration |
| **Infrastructure** | Zero — SDKs run in-process | Requires running MCP server (process spawn or persistent sidecar) |

### Verdict: Keep direct SDK calls for the governance pipeline.

Here's why. Our governance engine has a critical property: **every query is deterministic and known at build time**. When `blast_radius_agent` evaluates an action, it knows exactly which 4 KQL queries to run. When `cost_lookup` prices a SKU, it knows the exact OData filter. There's no ambiguity, no natural language interpretation needed.

MCP's value proposition is: *"let an AI decide which Azure tool to call based on a natural language question."* That's powerful — but it's solving a problem we don't have in the evaluation pipeline. Our pipeline doesn't need an AI to decide what API to call. It already knows.

Adding MCP to the pipeline would:
- Add ~50–100ms latency per call for zero benefit
- Remove our caching layer (the `_cache` dict that makes repeat SKU lookups instant)
- Break our `asyncio.gather(4 concurrent KQL)` pattern — the thing that makes topology enrichment fast
- Add an external process dependency
- Make testing harder

**MCP is the wrong tool for deterministic pipelines. It's the right tool for conversational interfaces.**

---

## Question 2: Should we add a Gen AI chat capability to the dashboard?

### What the dashboard currently supports

The dashboard is a **command-and-control UI**: 18 REST endpoints, 9 React components, all button-driven. Users can trigger scans, view verdicts, drill into explanations, stream real-time events. But there is no way to ask a question in natural language.

### What a chat interface would enable

Things a user currently **cannot do** with the dashboard:

| User intent | Today | With chat |
|---|---|---|
| "Why was vm-dr-01 denied?" | Click table → drilldown → read explanation | Just ask |
| "What resources are costing more than $50/month?" | Run cost scan → scan all results → calculate manually | Just ask |
| "What would happen if I deleted the storage account?" | Nothing — no what-if capability | Chat runs a hypothetical evaluation |
| "Show me all denied actions this week" | GET /api/evaluations → filter manually | Just ask |
| "Are there any resources missing lifecycle tags?" | Run deploy scan → wait → read proposals | Just ask |
| "Draft an exception request for vm-dr-01" | No capability | Chat generates a document |
| "Evaluate scaling vm-web-01 to Standard_D4s_v3" | No ad-hoc evaluation trigger | Chat calls the pipeline |

### Is it worth it?

**For a hackathon: absolutely yes.** A chat interface would be the single most impressive feature you can demo. Judges can type questions and see the governance engine respond in real time. It transforms RuriSkry from "a dashboard you look at" into "a governance advisor you talk to."

**For production: also yes**, but for different reasons:
- **Executives/auditors** won't navigate a technical dashboard — they'll ask questions
- **Incident responders** need rapid ad-hoc investigation — typing is faster than clicking through 5 screens
- **Compliance reviews** need "show me all policy violations for resource X in the last 30 days" — that's a natural language query

### Architecture: How to build it (and where MCP fits)

**This is where MCP could shine** — not in the governance pipeline, but in the chat interface:

```
┌─────────────────────────────────────────────────────┐
│  Dashboard Chat Input                               │
│  User: "Why was vm-dr-01 denied last Tuesday?"      │
└──────────────────────┬──────────────────────────────┘
                       ↓
              ┌────────────────┐
              │   GPT-4.1 /    │
              │   Claude        │
              │  (function      │
              │   calling)      │
              └───────┬────────┘
                      ↓
         ┌────────────┴────────────┐
         ↓                         ↓
  ┌──────────────┐        ┌───────────────┐
  │ RuriSkry API │        │ Azure MCP     │
  │ tools        │        │ (optional)    │
  │              │        │               │
  │ • evaluations│        │ • pricing     │
  │ • metrics    │        │ • monitor     │
  │ • scan       │        │ • compute     │
  │ • explain    │        │ • policy      │
  └──────────────┘        └───────────────┘
```

But here's the thing — **you don't need MCP for this**. You can expose your existing 18 API endpoints as function-calling tools directly. The LLM calls `fetch_evaluations(resource_id="vm-dr-01", limit=5)` as a function — no MCP protocol needed.

MCP would only add value if you wanted the chat to query Azure services **beyond** what your existing API covers (ad-hoc KQL queries, pricing for resources you haven't scanned yet, etc.). That's a nice-to-have, not a must-have.

### Recommended approach

**Build chat with function calling against your existing API first. Add MCP later only if users ask questions your API can't answer.**

Here's why:
- Your 18 existing endpoints already cover: evaluations, metrics, agents, scans, explanations
- Function calling is simpler (no MCP server process, no extra dependency)
- You can build it in one phase — a `ChatPanel.jsx` component + one new endpoint `POST /api/chat`
- The LLM (GPT-4.1 or Claude) gets your API endpoints as tools and answers questions by calling them

---

## TL;DR — Three decisions

| Decision | Recommendation | Reason |
|---|---|---|
| **Replace SDK calls with MCP?** | **No** | Our queries are deterministic. MCP adds latency, removes caching, breaks concurrent queries. Zero benefit for the pipeline. |
| **Add chat to dashboard?** | **Yes** | Biggest bang-for-buck feature. Transforms the demo. Accessible to non-technical users. |
| **Use MCP for chat?** | **Not initially** | Function calling against our existing 18 endpoints is simpler and covers 90% of questions. Add MCP later if users need ad-hoc Azure queries the API doesn't cover. |

---

## Next Steps (if proceeding with chat)

1. `POST /api/chat` — new FastAPI endpoint; receives `{message, conversation_history}`; calls GPT-4.1/Claude with our 18 endpoints exposed as function-calling tools; returns `{response, tool_calls_made}`
2. `ChatPanel.jsx` — new React component; text input + message thread; SSE stream for real-time response tokens
3. Tool definitions — wrap the 10 most useful existing endpoints as LLM-callable functions
4. System prompt — give the LLM context about RuriSkry, SRI scoring, governance verdicts

**Phase estimate:** Chat MVP (function calling against existing API, no MCP) = 1 phase.
MCP integration for ad-hoc Azure queries = optional follow-on phase.
