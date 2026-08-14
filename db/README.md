# db/

SQLite database for the IronBridge Procurement Assistant.

- **`schema.sql`** — table definitions (`Projects`, `Employees`, `MaterialInventory`, `PurchaseRequests`, `Suppliers`, `Equipment`, `SafetyPolicies`, `AuditLog`)
- **`seed.sql`** — sample data, including edge cases (a rejected request, a completed request, an over-budget request, a material already below its minimum stock level)
-
