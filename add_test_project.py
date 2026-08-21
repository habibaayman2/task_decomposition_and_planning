from mcp_server.db import get_conn

with get_conn() as conn:
    conn.execute(
        "INSERT OR REPLACE INTO Projects "
        "(ProjectID, ProjectName, Client, ProjectLocation, Budget, RemainingBudget, ProjectManagerID, Status) "
        "VALUES (999, 'Test Small Budget Project', 'Test Client', 'Test Site', 500.0, 500.0, "
        "(SELECT ProjectManagerID FROM Projects WHERE ProjectID = 1), 'Active')"
    )

print("Project 999 created with $500 budget")