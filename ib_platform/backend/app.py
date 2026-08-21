"""
IronBridge Platform Backend — Central FastAPI App
"""
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from fastapi import FastAPI, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

app = FastAPI(
    title="IronBridge Platform",
    description="Admin + User surfaces for IronBridge Construction AI agents",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# B4 chat routes (always loaded — user surface)
# ---------------------------------------------------------------------------
from ib_platform.backend.routes import chat as chat_router
app.include_router(chat_router.router)

# ---------------------------------------------------------------------------
# A4 admin routes (best-effort load with fallback)
# ---------------------------------------------------------------------------
a4_loaded = False
a4_error = None
a4_routes = []

try:
    from ib_platform.backend.routes import agents as agents_router
    from ib_platform.backend.routes import tools as tools_router
    from ib_platform.backend.routes import hitl as hitl_router
    from ib_platform.backend.routes import tickets as tickets_router
    from ib_platform.backend.routes import rag_docs as rag_docs_router

    app.include_router(agents_router.router)
    app.include_router(tools_router.router)
    app.include_router(hitl_router.router)
    app.include_router(tickets_router.router)
    app.include_router(rag_docs_router.router)

    a4_loaded = True
    a4_routes = ["/api/agents", "/api/tools", "/api/hitl", "/api/tickets", "/api/rag-docs"]
except Exception as e:
    a4_error = str(e)
    print(f"[WARNING] A4 admin routes not loaded: {e}")
    print("[WARNING] Admin panel will show errors for Tools, HITL, Tickets, RAG.")

    # Fallback /api/agents so the admin panel never shows a completely empty roster
    from ib_platform.backend.services.agent_runner import list_available_agents

    fallback = APIRouter(prefix="/api/agents", tags=["agents"])

    @fallback.get("")
    def get_agents_fallback():
        agents = list_available_agents()
        return {
            "agents": [
                {
                    "agent_id": f"{a['name']}_agent",
                    "label": a["description"],
                    "type": a["type"],
                    "status": a["status"],
                    "health": {"state": a["status"], "active_runs": 0, "statuses": []},
                    "entrypoint": f"{a['name']}_agent.py",
                }
                for a in agents
            ],
            "total_count": len(agents),
            "active_state_graphs": len([a for a in agents if a["type"] == "state_graph"]),
            "fallback": True,
            "a4_error": a4_error,
        }

    app.include_router(fallback)
    a4_routes.append("/api/agents (fallback)")

# ---------------------------------------------------------------------------
# Static file serving — mounted INDIVIDUALLY so one missing dir
# does not prevent the other from mounting
# ---------------------------------------------------------------------------
frontend_dir = Path(__file__).resolve().parent.parent / "frontend"

if frontend_dir.exists():
    admin_dir = frontend_dir / "admin"
    user_dir = frontend_dir / "user"

    if admin_dir.exists():
        app.mount("/admin", StaticFiles(directory=str(admin_dir), html=True), name="admin")
        print(f"[StaticFiles] /admin  -> {admin_dir}")
    else:
        print(f"[WARNING] Admin frontend missing: {admin_dir}")
        print("[WARNING] Create it: mkdir -p ib_platform/frontend/admin && copy admin_panel.html there")

    if user_dir.exists():
        app.mount("/user", StaticFiles(directory=str(user_dir), html=True), name="user")
        print(f"[StaticFiles] /user   -> {user_dir}")
    else:
        print(f"[WARNING] User frontend missing: {user_dir}")
else:
    print(f"[WARNING] Frontend directory not found at {frontend_dir}")

# ---------------------------------------------------------------------------
# Health check — useful for verifying which routes loaded
# ---------------------------------------------------------------------------
@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "service": "ironbridge-platform",
        "a4_loaded": a4_loaded,
        "a4_error": a4_error,
        "routes": ["/api/chat"] + a4_routes,
        "static": {
            "admin": "/admin/index.html" if (frontend_dir / "admin").exists() else None,
            "user": "/user/index.html" if (frontend_dir / "user").exists() else None,
        },
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
