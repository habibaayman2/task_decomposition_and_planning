"""
Chat endpoints — user-facing surface.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any, Dict, List, Optional

from mcp_server.db import get_conn
from ib_platform.backend.services.agent_runner import (
    is_state_graph_agent,
    is_legacy_agent,
    run_state_graph_agent,
    run_legacy_agent,
)

router = APIRouter(prefix="/api/chat", tags=["chat"])

AGENT_ID_MAP = {
    "equipment_recovery_agent": "equipment_recovery",
    "change_order_agent": "change_order",
    "safety_incident_agent": "safety_incident",
    "memory_rag_agent": "memory_rag",
    "planning_agent": "planning",
}

REVERSE_AGENT_ID_MAP = {v: k for k, v in AGENT_ID_MAP.items()}

def _to_internal_name(agent_id: str) -> str:
    return AGENT_ID_MAP.get(agent_id, agent_id)

def _to_agent_id(internal_name: str) -> str:
    return REVERSE_AGENT_ID_MAP.get(internal_name, internal_name)

class CreateSessionRequest(BaseModel):
    agent_id: str
    user_id: int
    initial_state: Optional[Dict[str, Any]] = None
    first_message: Optional[str] = None

class SendMessageRequest(BaseModel):
    content: str

class MessageOut(BaseModel):
    message_id: int
    sender: str
    content: str
    message_type: str
    created_at: str

class SessionOut(BaseModel):
    session_id: int
    agent_id: str
    agent_name: str
    status: str
    created_at: str
    updated_at: str

@router.post("/sessions", response_model=Dict[str, Any])
def create_session(req: CreateSessionRequest):
    internal_name = _to_internal_name(req.agent_id)

    if not (is_state_graph_agent(internal_name) or is_legacy_agent(internal_name)):
        raise HTTPException(status_code=404, detail=f"Agent '{req.agent_id}' not found")

    # Step 1: create the session row, then close this connection --
    # before calling the graph, which opens its OWN connection to the
    # same sqlite file to write checkpoints. Keeping this connection
    # open while the graph runs caused "database is locked".
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO ChatSessions (UserID, AgentName, Status) VALUES (?, ?, ?)",
            (req.user_id, internal_name, "active"),
        )
        session_id = cur.lastrowid

    status = "active"

    if is_state_graph_agent(internal_name):
        if req.initial_state is None:
            raise HTTPException(status_code=400, detail=f"Agent '{req.agent_id}' requires initial_state")

        # Step 2: run the graph with NO connection open on our side.
        run_id, status, msg, full_state = run_state_graph_agent(
            agent_name=internal_name,
            initial_state=req.initial_state,
        )

        # Step 3: re-open a fresh connection to record the results.
        with get_conn() as conn:
            conn.execute(
                "UPDATE ChatSessions SET RunID = ?, Status = ? WHERE SessionID = ?",
                (run_id, status, session_id),
            )
            if req.first_message:
                conn.execute(
                    "INSERT INTO ChatMessages (SessionID, Sender, Content) VALUES (?, ?, ?)",
                    (session_id, "user", req.first_message),
                )
            msg_type = "status_completed" if status == "completed" else f"status_{status}"
            conn.execute(
                "INSERT INTO ChatMessages (SessionID, Sender, Content, MessageType) VALUES (?, ?, ?, ?)",
                (session_id, "agent", msg, msg_type),
            )
    else:
        if req.first_message is None:
            raise HTTPException(status_code=400, detail=f"Agent '{req.agent_id}' requires first_message")

        with get_conn() as conn:
            conn.execute(
                "INSERT INTO ChatMessages (SessionID, Sender, Content) VALUES (?, ?, ?)",
                (session_id, "user", req.first_message),
            )

        # legacy agent call also happens with no connection open.
        status, response = run_legacy_agent(internal_name, req.first_message)

        with get_conn() as conn:
            msg_type = "status_completed" if status == "completed" else "status_error"
            conn.execute(
                "INSERT INTO ChatMessages (SessionID, Sender, Content, MessageType) VALUES (?, ?, ?, ?)",
                (session_id, "agent", response, msg_type),
            )
            conn.execute(
                "UPDATE ChatSessions SET Status = ? WHERE SessionID = ?",
                (status, session_id),
            )

    # Step 4: fresh connection just to read back the final message list.
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT MessageID, Sender, Content, MessageType, CreatedAt FROM ChatMessages "
            "WHERE SessionID = ? ORDER BY CreatedAt",
            (session_id,),
        ).fetchall()
        messages = [
            MessageOut(
                message_id=r["MessageID"],
                sender=r["Sender"],
                content=r["Content"],
                message_type=r["MessageType"],
                created_at=r["CreatedAt"],
            )
            for r in rows
        ]

    return {
        "session_id": session_id,
        "agent_id": req.agent_id,
        "agent_name": internal_name,
        "status": status,
        "messages": messages,
    }

@router.get("/sessions", response_model=List[SessionOut])
def list_sessions(user_id: int):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT SessionID, AgentName, Status, CreatedAt, UpdatedAt "
            "FROM ChatSessions WHERE UserID = ? ORDER BY UpdatedAt DESC",
            (user_id,),
        ).fetchall()
    
    return [
        SessionOut(
            session_id=r["SessionID"],
            agent_id=_to_agent_id(r["AgentName"]),
            agent_name=r["AgentName"],
            status=r["Status"],
            created_at=r["CreatedAt"],
            updated_at=r["UpdatedAt"],
        )
        for r in rows
    ]

@router.post("/sessions/{session_id}/messages", response_model=Dict[str, Any])
def send_message(session_id: int, req: SendMessageRequest):
    with get_conn() as conn:
        session = conn.execute(
            "SELECT AgentName, Status, RunID FROM ChatSessions WHERE SessionID = ?",
            (session_id,),
        ).fetchone()
        
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        
        agent_name = session["AgentName"]
        current_status = session["Status"]
        run_id = session["RunID"]
        
        conn.execute(
            "INSERT INTO ChatMessages (SessionID, Sender, Content) VALUES (?, ?, ?)",
            (session_id, "user", req.content),
        )
        
        if is_state_graph_agent(agent_name):
            if current_status in ("paused_hitl", "ticket_open"):
                reply = (
                    f"⏸️ This conversation is currently **{current_status.replace('_', ' ')}**. "
                    f"Please wait for an admin to resolve it before sending more messages."
                )
                conn.execute(
                    "INSERT INTO ChatMessages (SessionID, Sender, Content, MessageType) VALUES (?, ?, ?, ?)",
                    (session_id, "agent", reply, "status_hitl" if current_status == "paused_hitl" else "status_ticket"),
                )
                conn.commit()
                return {"session_id": session_id, "status": current_status, "reply": reply}
            
            elif current_status == "completed":
                reply = "✅ This task has already been completed. Please start a new session if you have another request."
                conn.execute(
                    "INSERT INTO ChatMessages (SessionID, Sender, Content, MessageType) VALUES (?, ?, ?, ?)",
                    (session_id, "agent", reply, "status_completed"),
                )
                conn.commit()
                return {"session_id": session_id, "status": "completed", "reply": reply}
            
            else:
                reply = "🔄 The agent is still processing. Please wait."
                conn.execute(
                    "INSERT INTO ChatMessages (SessionID, Sender, Content, MessageType) VALUES (?, ?, ?, ?)",
                    (session_id, "agent", reply, "text"),
                )
                conn.commit()
                return {"session_id": session_id, "status": current_status, "reply": reply}
        else:
            status, response = run_legacy_agent(agent_name, req.content)
            msg_type = "status_completed" if status == "completed" else "status_error"
            conn.execute(
                "INSERT INTO ChatMessages (SessionID, Sender, Content, MessageType) VALUES (?, ?, ?, ?)",
                (session_id, "agent", response, msg_type),
            )
            conn.execute(
                "UPDATE ChatSessions SET Status = ? WHERE SessionID = ?",
                (status, session_id),
            )
            conn.commit()
            return {"session_id": session_id, "status": status, "reply": response}

@router.get("/sessions/{session_id}/messages", response_model=List[MessageOut])
def get_messages(session_id: int):
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM ChatSessions WHERE SessionID = ?", (session_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Session not found")
        
        rows = conn.execute(
            "SELECT MessageID, Sender, Content, MessageType, CreatedAt FROM ChatMessages "
            "WHERE SessionID = ? ORDER BY CreatedAt",
            (session_id,),
        ).fetchall()
    
    return [
        MessageOut(
            message_id=r["MessageID"],
            sender=r["Sender"],
            content=r["Content"],
            message_type=r["MessageType"],
            created_at=r["CreatedAt"],
        )
        for r in rows
    ]