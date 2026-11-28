"""Inkwell Books: a shop page with an Evokai assistant in Python (FastAPI).

Routes:
    GET  /                         Shop page (public/index.html)
    GET  /vendor/...               Browser files for @evokai/evokai-ui and @evokai/evokai-client
    GET  /api/books                The book catalogue
    POST /api/agent/token          Mint a session token for the signed-in user
    GET  /api/agent/conversations  The user's past conversations for history
    POST /mcp                      The shop's MCP server: catalogue_search tool
"""

import json
import os
import secrets
from pathlib import Path
from typing import Annotated, Any, Optional

from catalogue import BOOKS, search
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from evokai import AsyncEvokaiServer, EvokaiError

# 1. Load configuration from .env or environment
HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

PORT = int(os.environ.get("PORT", 3000))
EVOKAI_URL = os.environ.get("EVOKAI_URL", "http://localhost:8787/agent")
AGENT = os.environ.get("EVOKAI_AGENT", "bookshop")
MCP_SECRET = os.environ.get("BOOKSHOP_MCP_SECRET", "")
SERVER_KEY = os.environ.get("EVOKAI_SERVER_KEY")

if not SERVER_KEY:
    raise RuntimeError("EVOKAI_SERVER_KEY is not set: run `setup_app.py` first.")

evokai = AsyncEvokaiServer(base_url=EVOKAI_URL, server_key=SERVER_KEY)

# Paths to shared static files and vendor files
BOOKSHOP_APP_DIR = HERE.parent / "bookshop-app"
PUBLIC_DIR = BOOKSHOP_APP_DIR / "public"
CLIENT_DIR = HERE.parent.parent / "clients"
VENDOR_UI = CLIENT_DIR / "ui" / "dist"
VENDOR_CLIENT = CLIENT_DIR / "typescript" / "dist"

app = FastAPI(title="Inkwell Books")


def current_user() -> dict[str, str]:
    """A real application takes user from sign-in. Here every visitor is the same demo reader.

    Returns:
        The current user dict with id.
    """
    return {"id": "reader-1"}


# 2. Page & Catalogue routes
@app.get("/", response_class=HTMLResponse)
async def get_index():
    index_file = PUBLIC_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    html = index_file.read_text(encoding="utf-8")
    html = html.replace("{{EVOKAI_URL}}", EVOKAI_URL).replace("{{EVOKAI_AGENT}}", AGENT)
    return HTMLResponse(content=html)


@app.get("/api/books")
async def get_books():
    return {"items": BOOKS}


# 3. Evokai Token & Conversation routes
class TokenRequest(BaseModel):
    conversationId: Optional[str] = None


@app.post("/api/agent/token")
async def mint_token(
    req: Optional[TokenRequest] = None,
    detailed: Annotated[bool, Query()] = False,
):
    conversation_id = req.conversationId if req else None
    modules = {"tone": {"params": {"style": "detailed"}}} if detailed else None

    async def mint(conv_id: Optional[str]):
        return await evokai.mint_session_token(
            subject=current_user()["id"],
            agent=AGENT,
            conversation_id=conv_id,
            ttl_seconds=900,
            modules=modules,
        )

    try:
        t = await mint(conversation_id)
        return {
            "token": t.token,
            "expiresAt": t.expires_at,
            "conversationId": t.conversation_id,
            "usage": t.usage,
            "suggestions": t.suggestions,
        }
    except EvokaiError as e:
        # If the browser remembers an old/erased conversation, start a fresh one
        if conversation_id and e.status == 404:
            new_t = await mint(None)
            return {
                "token": new_t.token,
                "expiresAt": new_t.expires_at,
                "conversationId": new_t.conversation_id,
                "usage": new_t.usage,
                "suggestions": new_t.suggestions,
                "restarted": True,
            }
        raise HTTPException(
            status_code=e.status or 502,
            detail={"error": e.code, "message": e.message},
        ) from e


@app.get("/api/agent/conversations")
async def get_conversations():
    convs = await evokai.conversations.list(subject=current_user()["id"], limit=30)
    return {"items": convs}


# 4. MCP Server endpoint (Streamable JSON-RPC 2.0 for tools)
SEARCH_TOOL = {
    "name": "catalogue_search",
    "description": (
        "Search the Inkwell Books catalogue by title, author or genre. "
        "Returns id, title, author, genre, price in euros and copies in stock."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Words to look for; empty lists every book.",
            },
            "in_stock_only": {
                "type": "boolean",
                "description": "Only books with copies in stock.",
            },
        },
    },
    "annotations": {"readOnlyHint": True},
}


def is_authorized(authorization: Optional[str]) -> bool:
    if not MCP_SECRET or not authorization:
        return False
    expected = f"Bearer {MCP_SECRET}"
    return secrets.compare_digest(authorization, expected)


@app.post("/mcp")
async def mcp_endpoint(
    request: Request,
    authorization: Annotated[Optional[str], Header()] = None,
):
    if not is_authorized(authorization):
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": "unauthorized"},
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            },
        )

    if not isinstance(body, dict):
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request"},
            },
        )

    msg_id = body.get("id")
    if msg_id is None:
        # JSON-RPC Notification: acknowledge with HTTP 202
        return Response(status_code=status.HTTP_202_ACCEPTED)

    def reply(result: Any):
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    method = body.get("method")
    params = body.get("params") or {}

    if method == "initialize":
        return reply({
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "inkwell-books", "version": "0.1.0"},
        })

    elif method == "tools/list":
        return reply({"tools": [SEARCH_TOOL]})

    elif method == "tools/call":
        tool_name = params.get("name")
        if tool_name != SEARCH_TOOL["name"]:
            return reply({
                "isError": True,
                "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
            })

        args = params.get("arguments") or {}
        found = search(
            query=args.get("query", ""),
            in_stock_only=bool(args.get("in_stock_only", False)),
        )
        return reply({
            "content": [{"type": "text", "text": json.dumps({"books": found})}],
        })

    else:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": "Method not found"},
        }


# 5. Mount static vendor assets & stylesheets
if VENDOR_UI.exists():
    app.mount("/vendor/evokai-ui", StaticFiles(directory=str(VENDOR_UI)), name="vendor-ui")
if VENDOR_CLIENT.exists():
    app.mount(
        "/vendor/evokai-client", StaticFiles(directory=str(VENDOR_CLIENT)), name="vendor-client"
    )
if PUBLIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(PUBLIC_DIR)), name="public")


if __name__ == "__main__":
    import uvicorn

    print(
        f"Inkwell Books starting on http://localhost:{PORT} (Evokai at {EVOKAI_URL}, agent {AGENT})"
    )
    uvicorn.run("server:app", host="0.0.0.0", port=PORT)  # noqa: S104
