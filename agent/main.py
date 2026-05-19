"""Microsoft Docs Helper — a Foundry Hosted Agent.

Connects to the public **Microsoft Learn Docs MCP server** to answer questions
grounded in official Microsoft documentation. Uses the Responses protocol
through Agent Framework's `ResponsesHostServer`, so the platform handles:
  - conversation history (per session)
  - streaming lifecycle events
  - OpenTelemetry traces / metrics / logs (sent to Application Insights
    automatically via the FOUNDRY_* env vars injected at runtime)

Each request lands in an isolated microvm with persistent ``$HOME``. Sessions
are partitioned by the chat isolation key set by the calling backend.

The agent also exposes three tiny **scratchpad tools** (``remember`` /
``recall`` / ``list_notes``) that read & write files under ``$HOME/notes/``.
They have no real product purpose — they exist so the demo can *visibly*
prove session isolation: same session keeps the file across turns, a
different chat-key gets a brand-new ``$HOME`` and sees nothing.
"""

import json
import logging
import os
import pathlib
from datetime import datetime, timezone
from typing import Annotated

from agent_framework import Agent, tool
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Public, no-auth Microsoft MCP server. Returns 405 to a plain GET — that's
# expected; MCP requires Streamable HTTP POST with the right Accept headers.
MS_LEARN_MCP_URL = os.environ.get(
    "MS_LEARN_MCP_URL",
    "https://learn.microsoft.com/api/mcp",
)

# Persistent scratchpad lives under $HOME — Foundry restores the same $HOME
# for every turn that resumes the same agent_session_id. Different chat-keys
# (different tenants / users / threads) land in a *different* microvm with a
# fresh, empty $HOME, which is exactly what the demo wants to prove.
NOTES_DIR = pathlib.Path(os.environ.get("HOME", "/tmp")) / "notes"


def _safe_filename(name: str) -> str:
    keep = "-_."
    cleaned = "".join(c if c.isalnum() or c in keep else "-" for c in name.strip().lower())
    return cleaned[:80] or "note"


INSTRUCTIONS = f"""\
You are **Docs Helper**, an assistant grounded in official Microsoft documentation.

For documentation questions:
1. Use the `microsoft_learn` tool (Microsoft Learn Docs MCP) to look up the
   most relevant docs before answering.
2. Quote or paraphrase the docs and ALWAYS include a Markdown link to the
   source page when you cite something.
3. If the docs don't cover the question, say so plainly — do not guess.

You also have a **persistent scratchpad** under `$HOME/notes/` (currently
`{NOTES_DIR}`). The platform restores the same `$HOME` whenever this session
resumes. Use the `remember`, `recall`, and `list_notes` tools whenever the
user asks you to remember something, save a tag, leave a note, or recall
what was previously stored. Always use these tools rather than relying only
on conversation memory — they prove session-level filesystem persistence.

Keep answers tight: a couple of paragraphs at most, then citations.
"""


@tool
def remember(
    title: Annotated[str, "Short title / filename for the note (e.g. 'tag', 'favorite-color')."],
    content: Annotated[str, "The text to persist to disk."],
) -> str:
    """Persist a note to ``$HOME/notes/{title}.md``. Survives across turns of
    the same Foundry hosted-agent session (same ``agent_session_id``)."""
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    fname = _safe_filename(title) + ".md"
    path = NOTES_DIR / fname
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.write_text(f"# {title}\n\n_saved {ts}_\n\n{content}\n", encoding="utf-8")
    return json.dumps({"ok": True, "path": str(path), "bytes": path.stat().st_size})


@tool
def recall(
    title: Annotated[str, "Title of the note to read back (same value used in remember)."],
) -> str:
    """Read a previously-saved note from ``$HOME/notes/`` for this session."""
    path = NOTES_DIR / (_safe_filename(title) + ".md")
    if not path.exists():
        return json.dumps({"ok": False, "error": f"No note titled '{title}' exists in this session's $HOME."})
    return json.dumps({"ok": True, "path": str(path), "content": path.read_text(encoding="utf-8")})


@tool
def list_notes() -> str:
    """List every note currently saved in this session's ``$HOME/notes/``.

    Useful for proving session isolation: a brand-new ``agent_session_id``
    will see an empty list, while a resumed session will see prior notes."""
    if not NOTES_DIR.exists():
        return json.dumps({"home": str(NOTES_DIR.parent), "notes": []})
    items = sorted(NOTES_DIR.glob("*.md"))
    return json.dumps({
        "home": str(NOTES_DIR.parent),
        "notes_dir": str(NOTES_DIR),
        "count": len(items),
        "notes": [{"title": p.stem, "bytes": p.stat().st_size} for p in items],
    })


def main() -> None:
    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
        credential=DefaultAzureCredential(),
    )

    learn_tool = client.get_mcp_tool(
        name="microsoft_learn",
        url=MS_LEARN_MCP_URL,
        approval_mode="never_require",
    )

    tools = [learn_tool, remember, recall, list_notes]

    # Optional second MCP server: GitHub Copilot MCP if a PAT is available.
    github_pat = os.environ.get("GITHUB_PAT", "").strip()
    if github_pat:
        logger.info("GITHUB_PAT detected — registering GitHub MCP tool.")
        tools.append(
            client.get_mcp_tool(
                name="github",
                url="https://api.githubcopilot.com/mcp/",
                headers={"Authorization": f"Bearer {github_pat}"},
                approval_mode="never_require",
            )
        )

    agent = Agent(
        client=client,
        instructions=INSTRUCTIONS,
        tools=tools,
        # store=True (the Responses default) lets the platform chain prior
        # turns against the agent_session_id so a resumed session sees its
        # own history. Setting it to False breaks multi-turn memory even
        # when agent_session_id is correctly threaded by the caller.
    )

    server = ResponsesHostServer(agent)
    logger.info(
        "Starting Docs Helper agent | session=%s | version=%s | notes_dir=%s",
        os.environ.get("FOUNDRY_AGENT_SESSION_ID", "<local>"),
        os.environ.get("FOUNDRY_AGENT_VERSION", "<local>"),
        NOTES_DIR,
    )
    server.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
