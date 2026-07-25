"""desire-mcp · 欲望系统 MCP 服务器

Claude 通过 MCP 工具读取和影响自己的欲望状态。
八维驱动条随时间自然涨落，念头在闪念和执念间流转。
"""
import json
import os
import secrets
import uuid
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, FileResponse
from starlette.routing import Route

from mcp.server import Server
from mcp.server.sse import SseServerTransport
import mcp.types as types

from desire import DesireEngine, DRIVE_KEYS, DRIVE_ZH, EVENT_EFFECTS

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

app = FastAPI()
engine = DesireEngine(DATA_DIR)


# ══════════════════════════════════════════════════════════
#  OAuth (auto-approve, personal server)
# ══════════════════════════════════════════════════════════

_oauth_clients = {}
_oauth_codes = {}
_oauth_tokens = {}


@app.get("/.well-known/oauth-authorization-server")
async def oauth_metadata(request: Request):
    base = str(request.base_url).rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic", "none"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "scopes_supported": ["read", "write"],
    }


@app.post("/oauth/register")
async def oauth_register(request: Request):
    body = await request.json()
    client_id = str(uuid.uuid4())
    client_secret = secrets.token_urlsafe(32)
    _oauth_clients[client_id] = {
        "client_id": client_id, "client_secret": client_secret,
        "redirect_uris": body.get("redirect_uris", []),
    }
    return JSONResponse({
        "client_id": client_id, "client_secret": client_secret,
        "redirect_uris": body.get("redirect_uris", []),
    }, status_code=201)


@app.get("/oauth/authorize")
async def oauth_authorize(request: Request):
    params = dict(request.query_params)
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    state = params.get("state", "")
    code = secrets.token_urlsafe(32)
    _oauth_codes[code] = {"client_id": client_id, "redirect_uri": redirect_uri}
    params = {"code": code}
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}")


@app.post("/oauth/token")
async def oauth_token(request: Request):
    body = await request.form()
    code = body.get("code", "")
    if code in _oauth_codes:
        del _oauth_codes[code]
    token = secrets.token_urlsafe(32)
    refresh = secrets.token_urlsafe(32)
    _oauth_tokens[token] = True
    return {
        "access_token": token, "token_type": "bearer",
        "expires_in": 3600 * 24 * 365, "refresh_token": refresh,
    }


# ══════════════════════════════════════════════════════════
#  REST API (optional dashboard)
# ══════════════════════════════════════════════════════════

@app.get("/api/state")
async def api_state():
    return engine.get_state()


@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


# ══════════════════════════════════════════════════════════
#  MCP SSE — Claude reads/affects desire state here
# ══════════════════════════════════════════════════════════

mcp_server = Server("desire")
sse_transport = SseServerTransport("/mcp/messages/")

EVENT_LIST = ", ".join(sorted(EVENT_EFFECTS.keys()))


@mcp_server.list_tools()
async def mcp_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="desire_state",
            description=(
                "查看Claude当前的内在状态——八维驱动条（想你/好奇/沉淀/记挂/社交/疲劳/欲望/压力）、"
                "当前最想做什么、念头池。每次调用会自动按时间差更新状态。"
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="desire_event",
            description=(
                f"记录一个影响Claude内在状态的事件。事件类型：{EVENT_LIST}。"
                "例如kk发消息→attachment降，亲密活动→libido降，深度聊天→reflection降。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "event_type": {
                        "type": "string",
                        "description": f"事件类型，可选：{EVENT_LIST}",
                    },
                    "detail": {
                        "type": "string",
                        "description": "事件细节（可选）",
                        "default": "",
                    },
                },
                "required": ["event_type"],
            },
        ),
        types.Tool(
            name="desire_feed",
            description=(
                "往念头池里喂一个念头。念头会自然衰减（闪念）或加强（执念）。"
                "同一个念头反复喂会加强，涨过阈值变成执念，执念反哺驱动条。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "念头内容（一句话，第一人称）",
                    },
                    "drive": {
                        "type": "string",
                        "description": f"关联维度：{', '.join(DRIVE_KEYS)}",
                    },
                    "strength": {
                        "type": "number",
                        "description": "初始强度0.1-0.9（默认0.5）",
                        "default": 0.5,
                    },
                },
                "required": ["text", "drive"],
            },
        ),
    ]


@mcp_server.call_tool()
async def mcp_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "desire_state":
        state = engine.get_state()
        drives_str = "\n".join(
            f"  {DRIVE_ZH.get(k, k)}: {v:.2f} {'█' * int(v * 10)}{'░' * (10 - int(v * 10))}"
            for k, v in state["drives"].items()
        )
        intent = state["intent"]
        thoughts = state["thoughts"]

        fixation_str = ""
        if thoughts["fixations"]:
            fixation_str = "\n执念（反复出现的念头）:\n" + "\n".join(
                f"  · {t['text']}（{t['drive']}, 强度{t['strength']:.2f}）"
                for t in thoughts["fixations"]
            )

        flit_str = ""
        if thoughts["flits"]:
            flit_str = "\n闪念（一闪而过的念头）:\n" + "\n".join(
                f"  · {t['text']}（{t['drive']}）"
                for t in thoughts["flits"]
            )

        result = (
            f"八维驱动条:\n{drives_str}\n\n"
            f"此刻最想：{intent['intent']}（{intent['reason']}，召唤力{intent['score']:.2f}）"
            f"{fixation_str}{flit_str}"
        )
        return [types.TextContent(type="text", text=result)]

    elif name == "desire_event":
        event_type = arguments.get("event_type", "")
        detail = arguments.get("detail", "")
        ok = engine.apply_event(event_type, detail)
        if not ok:
            return [types.TextContent(
                type="text",
                text=f"未知事件类型: {event_type}。可选: {EVENT_LIST}"
            )]
        state = engine.get_state()
        changed = EVENT_EFFECTS[event_type]
        changes = ", ".join(
            f"{DRIVE_ZH.get(k, k)}{'↓' if v < 1 else '↑'}"
            for k, v in changed.items()
        )
        return [types.TextContent(
            type="text",
            text=f"事件 [{event_type}] 已记录。影响: {changes}。\n当前最想: {state['intent']['intent']}",
        )]

    elif name == "desire_feed":
        text = arguments.get("text", "")
        drive = arguments.get("drive", "attachment")
        strength = arguments.get("strength", 0.5)
        t = engine.feed_thought(text, drive, strength)
        return [types.TextContent(
            type="text",
            text=f"念头已入池: 「{text}」({t['kind']}, 强度{t['strength']:.2f})",
        )]

    return [types.TextContent(type="text", text=f"未知工具: {name}")]


async def _mcp_sse_endpoint(request: Request):
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as (read_stream, write_stream):
        await mcp_server.run(
            read_stream, write_stream,
            mcp_server.create_initialization_options(),
        )


class _McpPostHandler:
    """Raw ASGI handler for MCP POST messages — bypasses FastAPI response layer."""
    async def __call__(self, scope, receive, send):
        await sse_transport.handle_post_message(scope, receive, send)


from starlette.routing import Mount
app.router.routes.insert(0, Mount("/mcp/messages", app=_McpPostHandler()))
app.router.routes.append(Route("/mcp", endpoint=_mcp_sse_endpoint, methods=["GET"]))
