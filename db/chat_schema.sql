PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS ChatSessions (
    SessionID INTEGER PRIMARY KEY AUTOINCREMENT,
    UserID INTEGER NOT NULL,
    AgentName TEXT NOT NULL,
    RunID TEXT,
    Status TEXT NOT NULL DEFAULT 'active'
        CHECK (Status IN ('active', 'paused_hitl', 'ticket_open', 'closed', 'error')),
    CreatedAt TEXT NOT NULL DEFAULT (datetime('now')),
    UpdatedAt TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (UserID) REFERENCES Employees(EmployeeID)
);

CREATE TABLE IF NOT EXISTS ChatMessages (
    MessageID INTEGER PRIMARY KEY AUTOINCREMENT,
    SessionID INTEGER NOT NULL,
    Sender TEXT NOT NULL CHECK (Sender IN ('user', 'agent')),
    Content TEXT NOT NULL,
    NodeName TEXT,
    MessageType TEXT NOT NULL DEFAULT 'text'
        CHECK (MessageType IN ('text', 'status_hitl', 'status_ticket', 'status_completed')),
    CreatedAt TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (SessionID) REFERENCES ChatSessions(SessionID)
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_session
    ON ChatMessages(SessionID, CreatedAt);