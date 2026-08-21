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

# B4 routes (always available)
from ib_platform.backend.routes import chat as chat_router
app.include_router(chat_router.router)

# Try to load A4 routes
a4_loaded = False
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
except Exception as e:
    print(f"[WARNING] A4 admin routes not loaded: {e}")
    print("[WARNING] Using fallback /api/agents for B4 testing.")
    
    # Fallback: /api/agents from agent_runner (so frontend can still show agent switcher)
    from ib_platform.backend.services.agent_runner import list_available_agents
    
    fallback_agents = APIRouter(prefix="/api/agents", tags=["agents"])
    
    @fallback_agents.get("")
    def get_agents_fallback():
        agents = list_available_agents()
        return {
            "agents": [
                {
                    "agent_id": f"{a['name']}_agent",
                    "label": a["description"],
                    "type": a["type"],
                    "status": a["status"]
                }
                for a in agents
            ]
        }
    
    app.include_router(fallback_agents)

@app.get("/health")
def health_check():
    routes = ["/api/chat"]
    if a4_loaded:
        routes.extend(["/api/agents", "/api/tools", "/api/hitl", "/api/tickets", "/api/rag_docs"])
    else:
        routes.append("/api/agents (fallback)")
    return {
        "status": "ok",
        "service": "ironbridge-platform",
        "a4_loaded": a4_loaded,
        "routes": routes
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)