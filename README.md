# Foundry Hosted Agents — Isolation Demo

A live, hands-on demo of **per-tenant / per-user / per-chat isolation** for [Microsoft Foundry Hosted Agents](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents).

👉 **Try it:** https://lordlinus.github.io/foundry-agents-isolation-demo/

The UI is a single static page on GitHub Pages; it talks to a FastAPI proxy running on Azure Container Apps that authenticates to Foundry with a **managed identity** and stamps each request with the right isolation keys.

---

## What it teaches

Three nested workspace boundaries, all enforced by Foundry — not by the agent code:

| Boundary | Header | Effect |
|---|---|---|
| **Tenant / group** | `x-ms-user-isolation-key` | Contoso users never see Fabrikam's sandboxes |
| **User** | `x-ms-user-isolation-key` (per user) | Alice and Bob can't read each other's `$HOME` |
| **Chat / thread** | `x-ms-chat-isolation-key` | Same user, different threads → fully separate microVMs |
| **Shared channel** | `x-ms-chat-isolation-key` (group-scoped) | Multiple users in `#project-x` share one sandbox |

The browser visualises every key as you click through the tree. The agent itself is just `docs-helper-agent` — a small MCP-grounded helper. The point of the demo is the **plumbing around the agent**, not the agent.

> "Workspace key" is a UI label this demo uses for readability. On the platform, isolation is driven by the two `x-ms-*-isolation-key` headers below.

---

## How Foundry actually enforces it

A single hosted agent definition (one container image, one set of weights, one set of tools) can serve unlimited isolated workspaces because routing happens at the **platform layer**, before your container runs:

- Every request carries two opaque headers stamped by your trusted proxy:
  - `x-ms-user-isolation-key` — broader bucket (typically tenant / user)
  - `x-ms-chat-isolation-key` — narrower bucket inside it (typically thread / channel)
- Foundry uses `(agent, user-key, chat-key)` to **select or create a session**. Each session is a VM-isolated sandbox with a persistent `$HOME` filesystem (and `/files` mount) that's automatically restored on resume.
- Sessions hibernate after 15 minutes idle and persist for up to 30 days. Disk survives idle; memory does not.
- The agent container **never sees the isolation key headers** — Foundry consumes them and just hands the container the right `$HOME`. A buggy agent cannot path-traverse into another workspace because the mount namespace doesn't expose it.

**You have to trust two things:**
1. Foundry honours the headers (platform-level guarantee — same trust class as Azure RBAC).
2. Your proxy stamps headers derived from a **trusted user identity** — never a value the browser controls. In this demo, since access is open, each browser gets its own random `vid-<hex>` so no visitor can target another's slice.

### Reference docs

- [Hosted agents — overview](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents) (per-session VM isolation, `$HOME`, `/files`, scale-to-zero with stateful resume)
- [Sessions and conversations](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents#sessions-and-conversations) — lifecycle, 15-min idle, 30-day session lifetime
- [Responses protocol](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents#protocols-responses-and-invocations) — the OpenAI-compatible endpoint this demo calls
- [Agent identity](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/agent-identity) — the per-agent Entra ID that authenticates downstream calls
- [Foundry RBAC](https://learn.microsoft.com/en-us/azure/foundry/concepts/rbac-foundry) — Foundry User / Owner roles assigned to your managed identity
- [Quickstart: deploy a hosted agent with azd](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agents)

---

## Architecture

```
Browser (GitHub Pages, static)
    │  fetch(POST /api/chat, …) — CORS-restricted to lordlinus.github.io
    ▼
Container Apps proxy (FastAPI, this repo)
    │  - validates input, applies rate limits & turn caps
    │  - maps {customer, user, thread, visitor_id} → user-key + chat-key
    │  - DefaultAzureCredential (Managed Identity) → Bearer token
    │  - streams Foundry SSE back to the browser
    ▼
Foundry Hosted Agent
    - micro-VM per (user-key, chat-key)
    - persistent $HOME per session
    - emits OTel → Application Insights
```

Everything in this repo is the **proxy** + **infra** + **UI**. The Foundry agent (`docs-helper-agent`) lives in a separate `agent/` folder (kept here for reference) and is deployed to your Foundry project independently.

---

## Try it (zero setup)

1. Open https://lordlinus.github.io/foundry-agents-isolation-demo/
2. Click any chat tile — Contoso / Alice / Thread 1, etc. Each tile auto-sends its starter prompt.
3. Watch the **workspace key** badges and **session bubbles** appear as Foundry returns its `agent_session_id`.
4. Click the same tile again to send another turn — same session id resumes.
5. Click a different tile (different user or thread) — new session id, completely isolated `$HOME`.
6. Click 🔁 **Reset session** in the header to rotate your visitor id and start fresh.

This is a shared free demo, so usage per visitor is bounded — if you see a ⛔ message just click **Reset session** and continue.

---

## Run the backend locally

Prereqs: Python 3.12+, Azure CLI, an existing Foundry project with a deployed hosted agent.

```bash
git clone https://github.com/lordlinus/foundry-agents-isolation-demo
cd foundry-agents-isolation-demo

az login                                  # DefaultAzureCredential picks this up
cp backend/.env.example backend/.env      # then edit FOUNDRY_PROJECT_ENDPOINT
./start.sh                                # creates .venv, installs deps, runs uvicorn
```

Open http://localhost:8080/ — the FastAPI app serves both the proxy and the UI.

---

## Deploy your own copy to Azure

Prereqs: [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd) (`azd`), a subscription, and the resource id of an existing Foundry / Azure AI Services account.

```bash
azd auth login
azd env new my-foundry-demo --subscription <SUB_ID> --location southeastasia

# Pin the Foundry account / project / agent your demo will call
azd env set FOUNDRY_ACCOUNT_RESOURCE_ID "/subscriptions/<SUB>/resourceGroups/<RG>/providers/Microsoft.CognitiveServices/accounts/<NAME>"
azd env set FOUNDRY_PROJECT_ENDPOINT    "https://<NAME>.services.ai.azure.com/api/projects/<PROJECT>"
azd env set FOUNDRY_AGENT_NAME          "docs-helper-agent"
azd env set FOUNDRY_API_VERSION         "2025-11-15-preview"

azd up
```

`azd up` provisions a resource group with:

- Azure Container Apps environment (scale-to-zero, max 2 replicas)
- User-assigned Managed Identity, granted `Azure AI User` + `Cognitive Services User` on the Foundry account
- Azure Container Registry (Basic), Log Analytics, Application Insights
- The proxy image, built remotely in ACR and deployed to ACA

Output:
```
- Endpoint: https://ca-foundry-agents-demo.<hash>.southeastasia.azurecontainerapps.io/
```

After the first deploy, re-deploy just the app on code changes:
```bash
azd deploy api
```

To tear it all down:
```bash
azd down --purge
```

---

## Wire your fork to GitHub Pages

The repo includes `.github/workflows/pages.yml`. To use it on your own fork:

1. In **Settings → Pages**, set **Source = GitHub Actions**.
2. In **Settings → Secrets and variables → Actions → Variables**, add a repo variable:
   - `API_BASE` = your Container App URL
3. Push to `main` (or run the workflow manually). The workflow stamps `API_BASE` into `ui/config.js` and publishes `ui/` to Pages.
4. Allow your Pages origin on the backend and redeploy:
   ```bash
   azd env set ALLOWED_ORIGINS "https://<you>.github.io,http://localhost:8080"
   azd deploy api
   ```

---

## Configuration reference

Core knobs for `azd env set` / Container App env vars:

| Variable | Default | Purpose |
|---|---|---|
| `FOUNDRY_PROJECT_ENDPOINT` | *(required)* | Foundry project endpoint |
| `FOUNDRY_AGENT_NAME` | `docs-helper-agent` | Hosted agent name |
| `FOUNDRY_API_VERSION` | `2025-11-15-preview` | Foundry Responses API version |
| `ALLOWED_ORIGINS` | localhost only | CORS allow-list (comma-separated) |

Additional operational knobs (rate limits, kill switch, etc.) are defined in `backend/server.py` — read the code if you fork.

---

## Repo layout

```
.
├── backend/                # FastAPI proxy → Foundry
│   ├── server.py           # the whole proxy in one file
│   ├── Dockerfile
│   └── requirements.txt
├── ui/                     # Static demo, deployed to GitHub Pages
│   ├── index.html          # single-file UI (no build step)
│   ├── config.js           # API_BASE — rewritten by the Pages workflow
│   └── mermaid.min.js
├── infra/                  # Bicep for ACA + ACR + LAW + AppI + role assignments
├── agent/                  # Reference Foundry agent (deploy separately)
├── azure.yaml              # azd config (remoteBuild: true)
├── start.sh                # local dev launcher
└── .github/workflows/pages.yml
```

---

## Credits

Inspired by Ankit Sinha's hosted-agents blog series ([part 1](https://ankitbko.github.io/blog/2026/05/hosted-agents-part-1/) · [part 2](https://ankitbko.github.io/blog/2026/05/hosted-agents-part-2/)) and the [VS Code Tunnel sample](https://github.com/ankitbko/hosted-agents-vscode-tunnel).

License: MIT.
