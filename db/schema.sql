-- IronBridge Construction — Procurement Assistant MCP Lab
-- Engine: SQLite (portable SQL — swap db.py's connection layer for Postgres in prod)
--
-- Schema follows the entities/fields given in the problem statement
-- exactly, with one addition: Employees.PinHash. The original spec has
-- no authentication mechanism, but the lab requires a genuine role
-- elevation flow to justify the Notifications concern (site engineers
-- vs. approvers seeing different tool sets), so we added a PIN-based
-- login for the assistant session only — never used for anything else,
-- and never exposed by any read tool.

PRAGMA foreign_keys = ON;

CREATE TABLE Projects (
    ProjectID           INTEGER PRIMARY KEY,
    ProjectName         TEXT NOT NULL,
    Client              TEXT NOT NULL,
    ProjectLocation     TEXT NOT NULL,
    Budget              REAL NOT NULL,
    RemainingBudget      REAL NOT NULL,
    ProjectManagerID     INTEGER NOT NULL,
    Status              TEXT NOT NULL CHECK (Status IN ('Planning', 'Active', 'Completed', 'Suspended')),
    FOREIGN KEY (ProjectManagerID) REFERENCES Employees(EmployeeID)
);

CREATE TABLE Employees (
    EmployeeID          INTEGER PRIMARY KEY,
    Name                TEXT NOT NULL,
    Role                TEXT NOT NULL CHECK (Role IN
        ('Site Engineer', 'Procurement Officer', 'Project Manager', 'Finance Officer', 'Warehouse Supervisor')),
    Department          TEXT NOT NULL,
    Email               TEXT NOT NULL UNIQUE,
    AuthorizationLevel   INTEGER NOT NULL,  -- 1 (lowest) .. 4 (highest)
    PinHash             TEXT,               -- added for assistant login only; NULL = no assistant access
    ProjectID           INTEGER,            -- primary project this employee is scoped to (NULL = cross-project, e.g. Finance)
    FOREIGN KEY (ProjectID) REFERENCES Projects(ProjectID)
);

CREATE TABLE MaterialInventory (
    MaterialID           INTEGER PRIMARY KEY,
    MaterialName         TEXT NOT NULL,
    Category             TEXT NOT NULL,
    QuantityAvailable     REAL NOT NULL,
    WarehouseLocation     TEXT NOT NULL,
    MinimumStockLevel     REAL NOT NULL,
    UnitPrice            REAL NOT NULL,
    LastUpdated          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE PurchaseRequests (
    RequestID        INTEGER PRIMARY KEY,
    ProjectID        INTEGER NOT NULL,
    EmployeeID       INTEGER NOT NULL,
    MaterialID       INTEGER NOT NULL,
    Quantity         REAL NOT NULL,
    EstimatedCost    REAL NOT NULL,
    RequestDate      TEXT NOT NULL DEFAULT (datetime('now')),
    ApprovalLevel    INTEGER NOT NULL DEFAULT 1,   -- bumped when escalated to management
    Status           TEXT NOT NULL CHECK (Status IN ('Pending', 'Approved', 'Rejected', 'Escalated', 'Reserved', 'Completed')),
    FOREIGN KEY (ProjectID) REFERENCES Projects(ProjectID),
    FOREIGN KEY (EmployeeID) REFERENCES Employees(EmployeeID),
    FOREIGN KEY (MaterialID) REFERENCES MaterialInventory(MaterialID)
);

CREATE TABLE Suppliers (
    SupplierID        INTEGER PRIMARY KEY,
    CompanyName       TEXT NOT NULL,
    ContactInfo       TEXT NOT NULL,
    MaterialCategory   TEXT NOT NULL,
    ContractStatus    TEXT NOT NULL CHECK (ContractStatus IN ('Active', 'Expired', 'Under Review'))
);

CREATE TABLE Equipment (
    EquipmentID          INTEGER PRIMARY KEY,
    EquipmentName        TEXT NOT NULL,
    EquipmentType        TEXT NOT NULL,
    CurrentSite          TEXT NOT NULL,
    Availability         TEXT NOT NULL CHECK (Availability IN ('Available', 'In Use', 'Under Maintenance')),
    MaintenanceStatus     TEXT NOT NULL,
    LastInspectionDate    TEXT NOT NULL
);

CREATE TABLE SafetyPolicies (
    PolicyID              INTEGER PRIMARY KEY,
    PolicyTitle           TEXT NOT NULL,
    Description           TEXT NOT NULL,
    ApplicableDepartment   TEXT NOT NULL,
    EffectiveDate         TEXT NOT NULL
);

-- Append-only audit trail every write tool logs to.
CREATE TABLE AuditLog (
    LogID       INTEGER PRIMARY KEY,
    ActorID     INTEGER NOT NULL,
    Action      TEXT NOT NULL,
    TargetID    INTEGER,
    Detail      TEXT,
    At          TEXT NOT NULL DEFAULT (datetime('now'))
);
