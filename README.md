# Foundry Hosted Agents — Isolation Demo

> 👋 **New to Microsoft Foundry Hosted Agents?** This is the friendly tour. One small agent, many isolated workspaces — see it work in your browser in 30 seconds.

[![Try the demo](https://img.shields.io/badge/▶_Try_the_live_demo-5b8cff?style=for-the-badge)](https://lordlinus.github.io/foundry-agents-isolation-demo/)

---

## 🤔 What's the problem this solves?

You built an AI agent. Great. Now imagine you want **two different customers** using it — Contoso and Fabrikam. Or two different users inside Contoso — Alice and Bob. Or the same user across two chat threads.

You need each conversation to have its **own private scratch space**:
- Files Alice asks the agent to save shouldn't appear in Bob's session
- Notes pinned in "Thread 1" shouldn't leak into "Thread 2"
- Contoso's team channel should be a shared workspace, but Fabrikam can't see it

Doing this by hand (writing your own router, mounting per-user folders, managing session lifetimes) is a lot of code, a lot of bugs, and a real security risk if you get it wrong.

**Microsoft Foundry Hosted Agents** does this for you, at the platform layer. You stamp two headers on each request; the platform routes to the right sandbox. Your agent code stays simple — it just sees its own `$HOME`.

This demo lets you click around and **watch it happen**.

---

## ✨ What you'll see

Open the [live demo](https://lordlinus.github.io/foundry-agents-isolation-demo/) and you'll get a tree of groups → users → chats:

```
Contoso Ltd.                       Fabrikam Inc.
├── Alice                          └── Carol
│   ├── 🔒 Thread 1                    ├── 🔒 Thread 1
│   ├── 🔒 Thread 2                    └── 👥 #project-x (shared)
│   └── 👥 #project-x (shared)
└── Bob
    ├── 🔒 Thread 1
    └── 👥 #project-x (shared)
```

Each tile fires a request when you click it. The UI shows:
- 🔑 The **isolation keys** stamped on the request
- 🎨 A **colour-coded session bubble** for each unique sandbox the platform created
- 🔁 What happens on a second click (resume!) vs a click on a sibling tile (new sandbox)

In a couple of minutes you'll convince yourself that **one agent + two headers = real multi-tenant isolation**, with no per-customer agent deployments.

---

## 🧠 The 5-minute mental model

There's exactly one hosted agent deployed (`docs-helper-agent`). Every request to it carries two opaque strings as HTTP headers:

| Header | Think of it as | In this demo |
|---|---|---|
| `x-ms-user-isolation-key` | "Who is this caller?" | `user-alice`, `user-bob`, `user-carol`, … |
| `x-ms-chat-isolation-key` | "Which conversation?" | a hash of `(tenant, user, thread)` or `(tenant, shared, channel)` |

Foundry hashes the tuple `(agent, user-key, chat-key)` to **pick or create a session**. Each session is a tiny VM with its own `$HOME` directory — files written there persist across turns and survive idle hibernation for up to 30 days. Same tuple next time → same `$HOME` is restored. Different tuple → fresh, empty `$HOME` that the other session can't reach.

That's the whole trick. Your agent code never sees the headers — it just sees its own filesystem. A bug in agent code physically **cannot** read another workspace because the path doesn't exist in its mount namespace.

**Two trust assumptions:**
1. The platform honours the headers (Microsoft's job — same trust level as Azure RBAC).
2. Your proxy stamps headers derived from a **trusted user identity** — never something the browser invented. (Otherwise a bad actor types `?user=ceo` and reads the CEO's notes.)

---

## 🏗️ How this demo is wired

```
       ┌────────────────────────────────────┐
       │  Browser  (GitHub Pages, static)   │
       │  POST /api/chat                    │
       └──────────────┬─────────────────────┘
                      │  CORS-allowed origin
                      ▼
       ┌────────────────────────────────────┐
       │  Proxy on Azure Container Apps     │
       │  (FastAPI — backend/server.py)     │
       │                                    │
       │  1. authenticates the caller       │
       │  2. derives user-key + chat-key    │
       │  3. forwards to Foundry            │
       │  4. streams the response back      │
       └──────────────┬─────────────────────┘
                      │  managed identity → Bearer token
                      │  x-ms-user-isolation-key: …
                      │  x-ms-chat-isolation-key: …
                      ▼
       ┌────────────────────────────────────┐
       │  Foundry Hosted Agent              │
       │  - per-session VM sandbox          │
       │  - persistent $HOME                │
       │  - auto-resume on idle             │
       └────────────────────────────────────┘
```

The proxy is the **trusted boundary**. It's the only place where "who is this user?" is decided. Everything downstream just follows the headers.

---

## ▶️ Try it (no setup)

1. Open the [live demo](https://lordlinus.github.io/foundry-agents-isolation-demo/).
2. Click **Alice → Thread 1**. The agent says something; a coloured session bubble appears at the bottom.
3. Click **Alice → Thread 1** again. Same bubble, same session — `$HOME` was resumed.
4. Click **Alice → Thread 2**. A *different* colour bubble — totally separate `$HOME`.
5. Click **Bob → Thread 1**. Yet another bubble. Same thread *name* as Alice's, but a different user → different sandbox.
6. Click any of the **👥 #project-x** tiles for Contoso. They all share one bubble — shared channel.
7. Click 🔁 **Reset session** in the header any time to start over.

It's a shared free demo, so usage per visitor is bounded — if you see a ⛔ message just click **Reset session** and continue.

---

## 🛠️ Run it locally

Prereqs: Python 3.12+, Azure CLI, an existing Foundry project with a deployed hosted agent.

```bash
git clone https://github.com/lordlinus/foundry-agents-isolation-demo
cd foundry-agents-isolation-demo

az login                              # the proxy uses your token via DefaultAzureCredential
cp backend/.env.example backend/.env  # then edit FOUNDRY_PROJECT_ENDPOINT
./start.sh                            # creates .venv, installs deps, runs uvicorn on :8080
```

Open <http://localhost:8080/>. The FastAPI app serves both the proxy and the UI in one process.

---

## ☁️ Deploy your own copy to Azure

Prereqs: [Azure Developer CLI (`azd`)](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd), an Azure subscription, and an existing Foundry / Azure AI Services account with a deployed hosted agent.

```bash
azd auth login
azd env new my-foundry-demo --subscription <SUB_ID> --location southeastasia

azd env set FOUNDRY_ACCOUNT_RESOURCE_ID "/subscriptions/<SUB>/resourceGroups/<RG>/providers/Microsoft.CognitiveServices/accounts/<NAME>"
azd env set FOUNDRY_PROJECT_ENDPOINT    "https://<NAME>.services.ai.azure.com/api/projects/<PROJECT>"
azd env set FOUNDRY_AGENT_NAME          "docs-helper-agent"

azd up
```

`azd up` provisions a resource group with:
- Azure Container Apps (scale-to-zero, public ingress)
- A user-assigned Managed Identity granted `Foundry User` + `Cognitive Services User` on the Foundry account
- Azure Container Registry (Basic), Log Analytics, Application Insights

When it's done you'll see:
```
- Endpoint: https://ca-foundry-agents-demo.<hash>.<region>.azurecontainerapps.io/
```

Re-deploy after code changes:
```bash
azd deploy api
```

Tear everything down:
```bash
azd down --purge
```

### Wire your fork to GitHub Pages

The included `.github/workflows/pages.yml` publishes `ui/` to Pages on every push:

1. **Settings → Pages** → Source = **GitHub Actions**.
2. **Settings → Secrets and variables → Actions → Variables** → add `API_BASE` = your Container App URL.
3. Push to `main`. The workflow stamps `API_BASE` into `ui/config.js` and deploys.
4. Allow your Pages origin on the backend:
   ```bash
   azd env set ALLOWED_ORIGINS "https://<you>.github.io,http://localhost:8080"
   azd deploy api
   ```

---

## ⚙️ Core config

The proxy reads everything from environment variables on the Container App:

| Variable | Default | Purpose |
|---|---|---|
| `FOUNDRY_PROJECT_ENDPOINT` | *(required)* | Foundry project endpoint URL |
| `FOUNDRY_AGENT_NAME` | `docs-helper-agent` | Name of the deployed hosted agent |
| `FOUNDRY_API_VERSION` | `2025-11-15-preview` | Foundry Responses API version |
| `ALLOWED_ORIGINS` | localhost only | CORS allow-list, comma-separated |

Additional operational knobs (rate limits, etc.) live in `backend/server.py` — read the source if you fork.

---

## 📚 Learn more about Foundry Hosted Agents

| | |
|---|---|
| [Overview](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents) | What hosted agents are, when to use them, isolation model |
| [Sessions and conversations](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents#sessions-and-conversations) | The `$HOME` lifecycle: 15-min idle, 30-day retention, auto-resume |
| [Responses protocol](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents#protocols-responses-and-invocations) | OpenAI-compatible endpoint your client calls |
| [Agent identity](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/agent-identity) | The Entra ID assigned to every hosted agent at deploy time |
| [Foundry RBAC](https://learn.microsoft.com/en-us/azure/foundry/concepts/rbac-foundry) | Roles to give the managed identity calling Foundry |
| [Deploy with `azd`](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agents) | Quickstart for building and deploying your own hosted agent |

---

## 🗂️ Repo layout

```
.
├── backend/                 # FastAPI proxy → Foundry
│   ├── server.py            # the whole proxy in one file (~500 lines, readable)
│   ├── Dockerfile
│   └── requirements.txt
├── ui/                      # Static demo, deployed to GitHub Pages
│   ├── index.html           # single-file UI (no build step)
│   ├── config.js            # API_BASE — rewritten by the Pages workflow
│   └── mermaid.min.js
├── infra/                   # Bicep for ACA + ACR + LAW + AppI + role assignments
├── agent/                   # Reference Foundry agent (deploy separately)
├── azure.yaml               # azd config
├── start.sh                 # local dev launcher
└── .github/workflows/pages.yml
```

---

## 🙏 Credits

Inspired by Ankit Sinha's hosted-agents blog series ([part 1](https://ankitbko.github.io/blog/2026/05/hosted-agents-part-1/) · [part 2](https://ankitbko.github.io/blog/2026/05/hosted-agents-part-2/)) and the [VS Code Tunnel sample](https://github.com/ankitbko/hosted-agents-vscode-tunnel).

License: MIT. PRs and issues welcome.
