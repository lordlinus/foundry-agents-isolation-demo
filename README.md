# Foundry Hosted Agents — End-to-End Multi-Customer Demo

A complete, working demo of [Microsoft Foundry Hosted Agents](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents), inspired by Ankit Bansal's blog series ([Part 1](https://ankitbko.github.io/blog/2026/05/hosted-agents-part-1/) · [Part 2](https://ankitbko.github.io/blog/2026/05/hosted-agents-part-2/)) and the [VS Code Tunnel sample](https://github.com/ankitbko/hosted-agents-vscode-tunnel).

What you get:

| Capability | How it's demonstrated |
|---|---|
| **Hosted agent** | `agent/` — Agent Framework + Responses protocol, served by `ResponsesHostServer`. Same shape as the official `03-mcp` sample. |
| **Open Microsoft MCP server** | The agent grounds every answer in the public, no-auth **[Microsoft Learn Docs MCP server](https://learn.microsoft.com/api/mcp)**. |
| **Multi-customer isolation** | Backend proxy switches `x-ms-user-isolation-key` + `x-ms-chat-isolation-key` per customer/thread (Header mode). Same chat-key → shared session; different chat-key → fully isolated `$HOME`. |
| **Built-in observability** | Foundry auto-emits OpenTelemetry traces / metrics / logs to your project's Application Insights. The proxy stamps every request with `x-ms-correlation-id` so you can pivot from the UI straight into a KQL query. |
| **Browser UI** | `ui/index.html` — single-file, no build. Customer picker, multi-thread chat, streaming responses, live isolation-key preview. A second view at `/demo` (`ui/demo.html`) is a side-by-side isolation walkthrough. |

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Browser (ui/index.html)                                                  │
│   - picks customer (contoso/fabrikam/northwind) and thread                │
│   - POST /api/chat   (fetch + ReadableStream → SSE)                       │
└──────────────────────────────┬───────────────────────────────────────────┘
                               │
┌──────────────────────────────▼───────────────────────────────────────────┐
│  Backend proxy (backend/server.py — FastAPI)                              │
│   - maps {customer, thread} → user-key + chat-key                         │
│   - DefaultAzureCredential → Bearer token                                 │
│   - forwards to Foundry Responses API, streams SSE back                   │
│   - logs raw customer/thread + correlation_id (the *trusted* observer)    │
└──────────────────────────────┬───────────────────────────────────────────┘
                               │   x-ms-user-isolation-key
                               │   x-ms-chat-isolation-key
                               │   Authorization: Bearer …
                               ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  Foundry Hosted Agent (agent/main.py)                                     │
│   - microvm per request, persistent $HOME per session                     │
│   - FoundryChatClient + Microsoft Learn MCP tool                          │
│   - Auto OpenTelemetry → Application Insights                             │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Repository layout

```
hosted-agents/
├── agent/                       # Deployed to Foundry
│   ├── main.py                  # The agent — Responses + MCP wiring
│   ├── agent.manifest.yaml      # Used by `azd ai agent init`
│   ├── agent.yaml               # Container agent spec
│   ├── Dockerfile
│   ├── requirements.txt
│   └── .env.example             # Copy to .env to run locally
├── backend/                     # FastAPI proxy (runs anywhere)
│   ├── server.py
│   ├── requirements.txt
│   └── .env.example
├── ui/
│   ├── index.html               # Single-file UI — served by the proxy at /
│   └── demo.html                # Side-by-side isolation demo — served at /demo
├── start.sh                     # One-command bootstrap (venv + deps + .env + run)
├── scenarios.http               # Sample REST calls for the proxy
└── README.md
```

---

## Quick start

The bundled bootstrap script creates a venv, installs deps, copies `.env`, verifies `az login`, and starts the proxy:

```bash
cd hosted-agents
cp backend/.env.example backend/.env   # then edit FOUNDRY_PROJECT_ENDPOINT / FOUNDRY_AGENT_NAME
az login                               # DefaultAzureCredential needs this
./start.sh
# open http://localhost:8000/         # main UI
# open http://localhost:8000/demo     # side-by-side isolation walkthrough
```

Or do it manually:

```bash
cd hosted-agents/backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                   # edit FOUNDRY_PROJECT_ENDPOINT / FOUNDRY_AGENT_NAME
az login
python server.py
# open http://localhost:8000/
```

Try this:

1. Pick **Contoso**, type a question — note the `chat-key` shown in the sidebar.
2. Click **+ New thread** — note the `chat-key` changes (chat-isolated).
3. Switch to **Fabrikam** — same UI thread name, but the `chat-key` is namespaced (`fabrikam-…`), proving cross-tenant safety.
4. Notice every reply shows `customer · thread · session · correlation` underneath.

You need a deployed hosted agent first — see **Full deployment** below.

---

## Full deployment — Foundry mode

### Prerequisites

- Azure subscription + access to Microsoft Foundry
- [Azure Developer CLI (`azd`)](https://learn.microsoft.com/en-us/azure/developer/azure-developer-cli/install-azd) with the AI agents extension:
  ```bash
  azd ext install azure.ai.agents
  azd ext upgrade --all
  ```
- Python 3.12+
- `az login` (so the backend's `DefaultAzureCredential` can fetch tokens)

### 1. Deploy the agent

```bash
cd hosted-agents/agent
azd ai agent init -m ./agent.manifest.yaml      # scaffolds .azure/, infra/, etc.
azd up                                          # provision Foundry + deploy
```

When that finishes, `azd env get-values` will show `FOUNDRY_PROJECT_ENDPOINT` and the agent name.

Quick sanity check (uses your Entra identity — Entra mode by default):

```bash
azd ai agent invoke '{"input": "What is Microsoft Foundry?"}'
```

### 2. Switch the agent to Header isolation mode

The default is **Entra** mode (the platform derives the user key from your token). For multi-customer demos you want **Header** mode so the proxy picks the keys.

```bash
BASE_URL=$(azd env get-value FOUNDRY_PROJECT_ENDPOINT)
AGENT_NAME=docs-helper-agent

az rest --method PATCH \
  --url "${BASE_URL}/agents/${AGENT_NAME}?api-version=v1" \
  --resource "https://ai.azure.com" \
  --headers "Content-Type=application/merge-patch+json" \
            "Foundry-Features=AgentEndpoints=V1Preview" \
  --body '{
    "agent_endpoint": {
      "authorization_schemes": [
        {
          "type": "Entra",
          "isolation_key_source": { "kind": "Header" }
        }
      ]
    }
  }'
```

(See the blog Part 2 § *Two Authorization Schemes* for context.)

### 3. Start the proxy + UI

```bash
cd ../backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

# Edit .env so FOUNDRY_PROJECT_ENDPOINT / FOUNDRY_AGENT_NAME match your azd env.
# Optional: set APPINSIGHTS_NAME for the in-UI hint.

python server.py
# open http://localhost:8000/
```

### 4. Prove isolation works

| Action | Expected |
|---|---|
| Send a message as **Contoso › Thread 1**, then send another | Same `agent_session_id`, conversation continues |
| Click **+ New thread** under Contoso, send a message | New `agent_session_id` — fresh `$HOME` |
| Switch to **Fabrikam › Thread 1**, send a message | Different `agent_session_id` — Fabrikam can never see Contoso's session, even though both are called *Thread 1* (the `chat-key` is namespaced server-side) |

You can confirm with the API directly:

```bash
# Contoso sees its session
curl -sS "http://localhost:8000/api/sessions?customer=contoso&thread=thread-1" | jq

# Fabrikam doesn't
curl -sS "http://localhost:8000/api/sessions?customer=fabrikam&thread=thread-1" | jq
```

### 5. Find your traces in Application Insights

The platform wires OTel automatically — every turn produces a distributed trace. The proxy stamps each request with a correlation ID (shown beneath every UI reply). Pivot in App Insights with:

```kql
union traces, requests, dependencies
| where customDimensions["x-ms-correlation-id"] == "<paste-the-8-char-prefix-shown-in-the-UI>"
| order by timestamp asc
```

Or by session:

```kql
union traces, requests, dependencies
| where customDimensions["agent_session_id"] == "<session-id-from-the-UI>"
| order by timestamp asc
```

---

## Running just the agent locally

For tight iteration on the agent code (without `azd deploy` each time):

```bash
cd agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # set FOUNDRY_PROJECT_ENDPOINT + AZURE_AI_MODEL_DEPLOYMENT_NAME
az login
python main.py        # listens on http://localhost:8088/responses
```

Then point the proxy at it by setting `FOUNDRY_PROJECT_ENDPOINT=http://localhost:8088` — but note: local mode bypasses the platform's session/isolation enforcement. For real multi-customer testing, deploy with `azd up`.

---

## Notes & gotchas

- **`https://learn.microsoft.com/api/mcp` returns `405` to a plain `GET`.** That's expected — MCP is Streamable HTTP and requires `POST` with `Accept: application/json, text/event-stream`. The agent's `FoundryChatClient.get_mcp_tool` handles the protocol correctly.
- **Chat keys are namespaced by customer** (`{customer}-{sha256(...)[:32]}`) so Contoso's *Thread 1* and Fabrikam's *Thread 1* never collide. This is the single most important multi-tenant detail in the demo.
- **The proxy is the trusted observer.** Inside the agent container the platform forwards isolation keys in *obfuscated* form, so logging raw customer identifiers belongs in the proxy. The platform's auto-OTel still gives you per-session traces in the agent.
- **Browser SSE uses `fetch()` + `ReadableStream`**, not `EventSource`, because we POST. Watch out for proxies that buffer responses — the FastAPI server sets `X-Accel-Buffering: no` for nginx and disables Cache-Control.
- Sessions persist 30 days; `$HOME` survives the 15-minute idle/resume cycle. In-memory state does not.

---

## References

- Blog: [Hosted Agents Part 1](https://ankitbko.github.io/blog/2026/05/hosted-agents-part-1/) · [Part 2 (sessions, isolation)](https://ankitbko.github.io/blog/2026/05/hosted-agents-part-2/)
- Sample: [hosted-agents-vscode-tunnel](https://github.com/ankitbko/hosted-agents-vscode-tunnel)
- Samples repo: [microsoft-foundry/foundry-samples — hosted-agents](https://github.com/microsoft-foundry/foundry-samples/tree/main/samples/python/hosted-agents)
- Docs: [Hosted agents overview](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents) · [Manage hosted sessions](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions)
- MCP: [Microsoft Learn Docs MCP server](https://learn.microsoft.com/en-us/training/support/mcp)
