"""Backend proxy for the Hosted Agents demo.

Translates browser-friendly ``{customer, user, thread, message}`` requests into
the Foundry Hosted Agents Responses API, attaching multi-customer isolation
keys (Header mode) and an Entra bearer token. Streams SSE back to the browser.

Continuity model: the proxy persists ``{chat_key -> session_id, response_id, turns[]}``
to ``.conversations.json``. Auto-resume looks up both ids when the client
doesn't supply them — ``agent_session_id`` re-binds to the same VM / ``$HOME``,
and ``previous_response_id`` chains the model's conversation history (standard
OpenAI Responses semantics).

Requires ``FOUNDRY_PROJECT_ENDPOINT`` + ``FOUNDRY_AGENT_NAME`` and a working
``DefaultAzureCredential`` (e.g. ``az login``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import AsyncIterator, Optional
from urllib.parse import urlencode

import httpx
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("hosted-agents-proxy")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

FOUNDRY_PROJECT_ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
FOUNDRY_AGENT_NAME = os.environ.get("FOUNDRY_AGENT_NAME", "docs-helper-agent")
FOUNDRY_API_VERSION = os.environ.get("FOUNDRY_API_VERSION", "v1")
FOUNDRY_FEATURES = os.environ.get(
    "FOUNDRY_FEATURES", "HostedAgents=V1Preview,AgentEndpoints=V1Preview"
)
APPINSIGHTS_NAME = os.environ.get("APPINSIGHTS_NAME", "")  # for KQL hint shown in UI
MS_LEARN_MCP_URL = os.environ.get("MS_LEARN_MCP_URL", "https://learn.microsoft.com/api/mcp")

# --- Abuse-mitigation knobs (all tunable via env vars at deploy time) -------
# Comma-separated allow-list of origins. CORS rejects anything else.
ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get(
        "ALLOWED_ORIGINS",
        "https://lordlinus.github.io,http://localhost:8080,http://localhost:8000"
    ).split(",") if o.strip()
]
PUBLIC_DEMO_DISABLED = os.environ.get("PUBLIC_DEMO_DISABLED", "").lower() in {"1", "true", "yes"}
ENABLE_HISTORY_API = os.environ.get("ENABLE_HISTORY_API", "false").lower() in {"1", "true", "yes"}

MAX_MESSAGE_CHARS = int(os.environ.get("MAX_MESSAGE_CHARS", "500"))
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "400"))
MAX_TURNS_PER_CHAT = int(os.environ.get("MAX_TURNS_PER_CHAT", "6"))
MAX_CHATS_PER_IP_PER_HOUR = int(os.environ.get("MAX_CHATS_PER_IP_PER_HOUR", "15"))
MAX_CHATS_PER_VISITOR_PER_HOUR = int(os.environ.get("MAX_CHATS_PER_VISITOR_PER_HOUR", "8"))

# Strict shape validation — visitor/customer/user/thread are all attacker-controlled.
import re
_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,80}$")

if not FOUNDRY_PROJECT_ENDPOINT:
    raise RuntimeError(
        "FOUNDRY_PROJECT_ENDPOINT is not set. Configure backend/.env with the project "
        "endpoint of your deployed Foundry hosted agent."
    )

UI_DIR = os.path.join(os.path.dirname(__file__), "..", "ui")
CONV_STORE = os.path.join(os.path.dirname(__file__), ".conversations.json")


# --------------------------------------------------------------------------- #
# Conversation log (proxy-side persistence keyed by chat_key)
# --------------------------------------------------------------------------- #


def _load_conversations() -> dict:
    try:
        with open(CONV_STORE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_conversations(store: dict) -> None:
    with open(CONV_STORE, "w") as f:
        json.dump(store, f, indent=2, ensure_ascii=False)


def _record_turn(
    chat_key: str,
    user_message: str,
    assistant_text: str,
    session_id: str | None,
    response_id: str | None,
) -> None:
    store = _load_conversations()
    conv = store.setdefault(chat_key, {"session_id": None, "response_id": None, "turns": []})
    if session_id:
        conv["session_id"] = session_id
    if response_id:
        conv["response_id"] = response_id
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conv["turns"].append({"role": "user", "text": user_message, "ts": ts})
    conv["turns"].append({"role": "assistant", "text": assistant_text, "ts": ts})
    _save_conversations(store)


# --------------------------------------------------------------------------- #
# Customer / user roster (hard-coded for the demo)
# --------------------------------------------------------------------------- #

CUSTOMERS = [
    {
        "id": "contoso", "name": "Contoso Ltd.", "color": "#0078D4",
        "users": [
            {"id": "alice", "name": "Alice (Contoso)", "initials": "A", "color": "#0078D4"},
            {"id": "bob",   "name": "Bob (Contoso)",   "initials": "B", "color": "#3DA8FF"},
        ],
    },
    {
        "id": "fabrikam", "name": "Fabrikam Inc.", "color": "#107C10",
        "users": [
            {"id": "carol", "name": "Carol (Fabrikam)", "initials": "C", "color": "#107C10"},
            {"id": "dave",  "name": "Dave (Fabrikam)",  "initials": "D", "color": "#3FBF3F"},
        ],
    },
    {
        "id": "northwind", "name": "Northwind Traders", "color": "#D83B01",
        "users": [
            {"id": "erin",  "name": "Erin (Northwind)",  "initials": "E", "color": "#D83B01"},
            {"id": "frank", "name": "Frank (Northwind)", "initials": "F", "color": "#FF7A40"},
        ],
    },
]
CUSTOMER_IDS = {c["id"] for c in CUSTOMERS}
USERS_BY_ID = {u["id"]: c["id"] for c in CUSTOMERS for u in c["users"]}


def _compute_chat_key(customer: str, user: str, thread: str, share_mode: str) -> str:
    """Derive the per-conversation isolation key.

    private → unique per (tenant, user, thread)
    shared  → unique per (tenant, thread); same across all users in that tenant
    """
    if share_mode == "shared":
        raw = f"{customer}:shared:{thread}"
        return f"{customer}-shared-{hashlib.sha256(raw.encode()).hexdigest()[:24]}"
    raw = f"{customer}:user:{user}:thread:{thread}"
    return f"{customer}-{user}-{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

_credential = DefaultAzureCredential()


def _get_bearer_token() -> str:
    # DefaultAzureCredential caches tokens internally — no manual cache needed.
    return _credential.get_token("https://ai.azure.com/.default").token


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class ChatRequest(BaseModel):
    customer: str
    user: str = Field(..., description="End-user ID inside the customer tenant")
    thread: str = Field(..., description="UI-side thread/chat ID")
    share_mode: str = Field("private", description='"private" or "shared"')
    message: str
    session_id: Optional[str] = Field(None, description="agent_session_id, if continuing one")
    omit_chat_key: bool = Field(False, description="Send no chat-key — defaults to user-key scope on platform")
    visitor_id: Optional[str] = Field(None, description="Per-browser id; used for soft per-visitor rate limit")


class UserOut(BaseModel):
    id: str
    name: str
    initials: str
    color: str


class CustomerOut(BaseModel):
    id: str
    name: str
    color: str
    users: list[UserOut]


@dataclass
class IsolationContext:
    customer_id: str
    user_id: str
    thread_id: str
    share_mode: str
    user_key: str
    chat_key: Optional[str]   # None ⇒ header omitted on the wire
    chat_key_omitted: bool
    correlation_id: str

    @property
    def store_key(self) -> str:
        return self.chat_key or "__default__"


# --------------------------------------------------------------------------- #
# FastAPI app
# --------------------------------------------------------------------------- #

app = FastAPI(title="Hosted Agents Demo Proxy", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Abuse mitigation
# --------------------------------------------------------------------------- #
#
# In-process token bucket. Honest caveat: ACA can scale to 2 replicas, so the
# effective ceiling is ~2x what's configured here, and limits reset on revision
# restart. For a teaching demo with low Foundry quota that's acceptable; for a
# production gate use APIM/Front Door with quota-by-key.

import threading
from collections import defaultdict, deque

_rl_lock = threading.Lock()
_rl_chat_ip: dict[str, deque] = defaultdict(deque)        # ip → timestamps
_rl_chat_visitor: dict[str, deque] = defaultdict(deque)   # visitor_id → timestamps


def _client_ip(request: Request) -> str:
    # ACA ingress puts the client IP at the *rightmost* trusted hop in
    # X-Forwarded-For — but anything before it can be spoofed. We take the
    # last entry, which is what the ACA proxy itself appended.
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[-1].strip() or (request.client.host if request.client else "unknown")
    return request.client.host if request.client else "unknown"


def _enforce_window(bucket: dict[str, deque], key: str, limit: int, window_sec: int) -> tuple[bool, int]:
    """Sliding-window check. Returns (allowed, seconds_until_next_slot)."""
    now = time.time()
    with _rl_lock:
        dq = bucket[key]
        while dq and (now - dq[0]) > window_sec:
            dq.popleft()
        if len(dq) >= limit:
            retry_in = int(window_sec - (now - dq[0])) + 1
            return False, max(retry_in, 1)
        dq.append(now)
        return True, 0


def _validate_id(name: str, value: str) -> None:
    if not _ID_RE.match(value or ""):
        raise HTTPException(400, f"Invalid {name}: must match [A-Za-z0-9_-]{{1,80}}")


def _count_turns(chat_key: str) -> int:
    conv = _load_conversations().get(chat_key)
    if not conv:
        return 0
    # turns list interleaves user/assistant — count user messages as "turns"
    return sum(1 for t in conv.get("turns", []) if t.get("role") == "user")


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "agent_name": FOUNDRY_AGENT_NAME, "endpoint": FOUNDRY_PROJECT_ENDPOINT}


class ResetRequest(BaseModel):
    chat_keys: list[str] = Field(default_factory=list,
                                 description="Explicit chat_keys to delete from the proxy log.")


@app.post("/api/reset")
async def reset(req: ResetRequest) -> dict:
    """Prune entries from the proxy's conversation log.

    The browser sends every chat_key it knows about for the rotating visitor id
    (computed client-side, mirroring `_compute_chat_key`). We delete just those
    keys, so other visitors' history is untouched.
    """
    if not req.chat_keys:
        return {"deleted": 0}
    store = _load_conversations()
    before = len(store)
    for k in req.chat_keys:
        store.pop(k, None)
    _save_conversations(store)
    logger.info("reset: deleted %d/%d requested keys (store had %d, now %d)",
                before - len(store), len(req.chat_keys), before, len(store))
    return {"deleted": before - len(store)}


@app.get("/api/customers", response_model=list[CustomerOut])
async def list_customers() -> list[CustomerOut]:
    return [CustomerOut(**c) for c in CUSTOMERS]


@app.get("/api/config")
async def config() -> dict:
    return {
        "agent_name": FOUNDRY_AGENT_NAME,
        "appinsights_name": APPINSIGHTS_NAME,
        "foundry_endpoint": FOUNDRY_PROJECT_ENDPOINT,
        "api_version": FOUNDRY_API_VERSION,
        "mcp_servers": [
            {"name": "microsoft_learn", "url": MS_LEARN_MCP_URL,
             "auth": "none (public Microsoft MCP server)"},
        ],
    }


def _build_isolation(
    customer: str,
    user: str,
    thread: str,
    share_mode: str = "private",
    omit_chat_key: bool = False,
) -> IsolationContext:
    _validate_id("customer", customer)
    _validate_id("user", user)
    _validate_id("thread", thread)
    if customer not in CUSTOMER_IDS:
        raise HTTPException(400, f"Unknown customer '{customer}'")
    if user not in USERS_BY_ID:
        raise HTTPException(400, f"Unknown user '{user}'")
    if USERS_BY_ID[user] != customer:
        raise HTTPException(400, f"User '{user}' does not belong to customer '{customer}'")
    if share_mode not in {"private", "shared"}:
        raise HTTPException(400, f"Unknown share_mode '{share_mode}'")
    chat_key = None if omit_chat_key else _compute_chat_key(customer, user, thread, share_mode)
    return IsolationContext(
        customer_id=customer, user_id=user, thread_id=thread, share_mode=share_mode,
        user_key=f"user-{user}",
        chat_key=chat_key, chat_key_omitted=omit_chat_key,
        correlation_id=uuid.uuid4().hex,
    )


# --------------------------------------------------------------------------- #
# /api/chat — SSE stream
# --------------------------------------------------------------------------- #


def _sse(event: str, data: dict | str) -> bytes:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


def _deep_find_session_id(obj) -> Optional[str]:
    """Walk a nested JSON object and return the first non-empty
    ``agent_session_id`` / ``session_id`` value."""
    keys = ("agent_session_id", "session_id")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, str) and v:
                return v
        for v in obj.values():
            if (found := _deep_find_session_id(v)):
                return found
    elif isinstance(obj, list):
        for item in obj:
            if (found := _deep_find_session_id(item)):
                return found
    return None


async def _stream_foundry(req: ChatRequest, ctx: IsolationContext) -> AsyncIterator[bytes]:
    # Auto-resume from the proxy's log so continuity survives page refreshes,
    # restarts, and shared-channel joiners. Client-supplied session_id wins.
    # `agent_session_id` gives us the same $HOME / VM session; conversation
    # history (the model's context) is chained via `previous_response_id`,
    # which is standard OpenAI Responses semantics.
    session_id = req.session_id
    prev_response_id: Optional[str] = None
    conv = _load_conversations().get(ctx.store_key)
    if conv:
        if not session_id:
            session_id = conv.get("session_id")
        prev_response_id = conv.get("response_id")

    url = (
        f"{FOUNDRY_PROJECT_ENDPOINT}/agents/{FOUNDRY_AGENT_NAME}"
        f"/endpoint/protocols/openai/responses?api-version={FOUNDRY_API_VERSION}"
    )
    body: dict = {"input": req.message, "stream": True}
    # NOTE: Foundry's Hosted Agents Responses route rejected `max_output_tokens`
    # ("Not allowed" / invalid_payload). Token cap is enforced indirectly via
    # the per-turn / per-visitor / per-IP rate limits + a low Foundry quota.
    if session_id:
        body["agent_session_id"] = session_id
    if prev_response_id:
        body["previous_response_id"] = prev_response_id

    headers = {
        "Authorization": f"Bearer {_get_bearer_token()}",
        "Content-Type": "application/json",
        "Foundry-Features": FOUNDRY_FEATURES,
        "x-ms-user-isolation-key": ctx.user_key,
        "x-ms-correlation-id": ctx.correlation_id,
        "Accept": "text/event-stream",
    }
    if ctx.chat_key is not None:
        headers["x-ms-chat-isolation-key"] = ctx.chat_key

    logger.info(
        "chat customer=%s user=%s thread=%s mode=%s correlation=%s "
        "session_id=%s previous_response_id=%s chat_key=%s",
        ctx.customer_id, ctx.user_id, ctx.thread_id, ctx.share_mode, ctx.correlation_id,
        session_id or "(first turn)", prev_response_id or "(none)",
        ctx.chat_key or "(omitted)",
    )

    yield _sse("meta", {
        "customer": ctx.customer_id, "user": ctx.user_id,
        "thread": ctx.thread_id, "share_mode": ctx.share_mode,
        "user_key": ctx.user_key,
        "chat_key": ctx.chat_key, "chat_key_omitted": ctx.chat_key_omitted,
        "correlation_id": ctx.correlation_id,
        "session_id": session_id,
        "previous_response_id": prev_response_id,
        "agent_name": FOUNDRY_AGENT_NAME,
    })

    timeout = httpx.Timeout(connect=20, read=None, write=20, pool=20)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code >= 400:
                    err_body = (await resp.aread()).decode("utf-8", errors="replace")
                    logger.error("Foundry error %s: %s", resp.status_code, err_body[:500])
                    yield _sse("error", {"status": resp.status_code, "body": err_body[:2000]})
                    return

                resolved_session = None
                resolved_response_id = None
                collected_text: list[str] = []
                async for raw in resp.aiter_lines():
                    if raw is None:
                        continue
                    yield (raw + "\n").encode("utf-8")

                    if not raw.startswith("data:"):
                        continue
                    payload = raw[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue

                    if obj.get("type") == "response.output_text.delta":
                        collected_text.append(obj.get("delta", ""))

                    # Capture response.id for previous_response_id chaining.
                    if obj.get("type") in ("response.completed", "response.created"):
                        rid = (obj.get("response") or {}).get("id") or obj.get("id")
                        if rid:
                            resolved_response_id = rid

                    sid = _deep_find_session_id(obj)
                    if sid and sid != resolved_session:
                        resolved_session = sid
                        logger.info("resolved agent_session_id=%s correlation=%s", sid, ctx.correlation_id)
                        yield b"\n" + _sse("session", {"agent_session_id": sid})

                if resolved_session is None:
                    logger.warning("no session id observed correlation=%s", ctx.correlation_id)
                if resolved_response_id:
                    logger.info("resolved response_id=%s correlation=%s",
                                resolved_response_id, ctx.correlation_id)

                assistant_text = "".join(collected_text)
                if assistant_text:
                    _record_turn(ctx.store_key, req.message, assistant_text,
                                 resolved_session, resolved_response_id)

        except httpx.HTTPError as exc:
            logger.exception("HTTP error talking to Foundry")
            yield _sse("error", {"message": str(exc)})


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request) -> StreamingResponse:
    if PUBLIC_DEMO_DISABLED:
        raise HTTPException(503, "Demo is temporarily disabled. Please try again later.")

    # Input validation — keep abusers from blowing up storage with huge ids/payloads
    if len(req.message) > MAX_MESSAGE_CHARS:
        raise HTTPException(400, f"Message too long ({len(req.message)} chars). Max {MAX_MESSAGE_CHARS}.")
    if req.visitor_id is not None and req.visitor_id != "":
        _validate_id("visitor_id", req.visitor_id)

    # Rate limit — IP first (broader umbrella), then visitor (defense in depth).
    ip = _client_ip(request)
    ok, retry = _enforce_window(_rl_chat_ip, ip, MAX_CHATS_PER_IP_PER_HOUR, 3600)
    if not ok:
        logger.warning("rate-limit IP=%s retry_in=%ds", ip, retry)
        raise HTTPException(
            429, f"Too many requests from your network. Try again in {retry}s.",
            headers={"Retry-After": str(retry)},
        )
    if req.visitor_id:
        ok, retry = _enforce_window(_rl_chat_visitor, req.visitor_id,
                                    MAX_CHATS_PER_VISITOR_PER_HOUR, 3600)
        if not ok:
            logger.warning("rate-limit VID=%s retry_in=%ds", req.visitor_id, retry)
            raise HTTPException(
                429, f"You've hit the per-visitor cap. Try again in {retry}s, "
                     f"or click 'Reset session' to rotate your visitor id.",
                headers={"Retry-After": str(retry)},
            )

    ctx = _build_isolation(req.customer, req.user, req.thread, req.share_mode, req.omit_chat_key)

    # Per-conversation turn cap — stop runaway threads becoming free chatbots.
    if ctx.chat_key and _count_turns(ctx.chat_key) >= MAX_TURNS_PER_CHAT:
        raise HTTPException(
            429, f"This conversation has reached the {MAX_TURNS_PER_CHAT}-turn demo limit. "
                 f"Click 'Reset session' to start a fresh chat.",
        )

    async def safe_stream() -> AsyncIterator[bytes]:
        try:
            async for chunk in _stream_foundry(req, ctx):
                if await request.is_disconnected():
                    logger.info("client disconnected correlation=%s", ctx.correlation_id)
                    break
                yield chunk
        except Exception as exc:
            logger.exception("stream failed")
            yield _sse("error", {"message": str(exc)})

    return StreamingResponse(
        safe_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "x-correlation-id": ctx.correlation_id,
        },
    )


# --------------------------------------------------------------------------- #
# /api/history — proxy-side conversation log (Foundry's GET /responses/{id}
# doesn't expose input or previous_response_id, so a server-side log is the
# practical way to rehydrate the UI on refresh / for new shared-channel joiners).
# --------------------------------------------------------------------------- #


@app.get("/api/history")
async def get_history(
    customer: str, user: str, thread: str,
    share_mode: str = "private", omit_chat_key: bool = False,
) -> JSONResponse:
    if not ENABLE_HISTORY_API:
        raise HTTPException(404, "History API is disabled on this deployment.")
    ctx = _build_isolation(customer, user, thread, share_mode, omit_chat_key)
    conv = _load_conversations().get(ctx.store_key, {"session_id": None, "response_id": None, "turns": []})
    return JSONResponse({
        "chat_key": ctx.chat_key,
        "session_id": conv.get("session_id"),
        "response_id": conv.get("response_id"),
        "turns": conv.get("turns", []),
    })


# --------------------------------------------------------------------------- #
# /api/sessions — Foundry sessions list for a given chat scope
# --------------------------------------------------------------------------- #


@app.get("/api/sessions")
async def list_sessions(
    customer: str, user: str, thread: str,
    share_mode: str = "private", omit_chat_key: bool = False,
) -> JSONResponse:
    if not ENABLE_HISTORY_API:
        raise HTTPException(404, "Sessions API is disabled on this deployment.")
    ctx = _build_isolation(customer, user, thread, share_mode, omit_chat_key)
    qs = urlencode({"api-version": FOUNDRY_API_VERSION})
    url = f"{FOUNDRY_PROJECT_ENDPOINT}/agents/{FOUNDRY_AGENT_NAME}/endpoint/sessions?{qs}"
    headers = {
        "Authorization": f"Bearer {_get_bearer_token()}",
        "Foundry-Features": FOUNDRY_FEATURES,
        "x-ms-user-isolation-key": ctx.user_key,
    }
    if ctx.chat_key is not None:
        headers["x-ms-chat-isolation-key"] = ctx.chat_key

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers)
    try:
        raw = resp.json()
    except Exception:
        raw = {"text": resp.text}
    return JSONResponse(
        status_code=resp.status_code,
        content={"user_key": ctx.user_key, "chat_key": ctx.chat_key, "raw": raw},
    )


# --------------------------------------------------------------------------- #
# Static UI
# --------------------------------------------------------------------------- #


_NO_CACHE_HEADERS = {"Cache-Control": "no-store, max-age=0"}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(UI_DIR, "index.html"), headers=_NO_CACHE_HEADERS)


@app.get("/config.js")
async def ui_config() -> FileResponse:
    return FileResponse(os.path.join(UI_DIR, "config.js"), headers=_NO_CACHE_HEADERS,
                        media_type="application/javascript")


@app.get("/mermaid.min.js")
async def ui_mermaid() -> FileResponse:
    return FileResponse(os.path.join(UI_DIR, "mermaid.min.js"),
                        media_type="application/javascript")


if os.path.isdir(UI_DIR):
    app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    logger.info("Starting proxy on :%s | endpoint=%s | agent=%s",
                port, FOUNDRY_PROJECT_ENDPOINT, FOUNDRY_AGENT_NAME)
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
