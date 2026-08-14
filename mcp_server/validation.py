"""
Server-side validation and authorization for the procurement assistant.

Separate from the JSON Schemas declared on each tool in server.py — a
schema can check shape, never business rules like "does this request
belong to this employee's project" or "would this exceed the remaining
budget." Every write tool handler calls into here before touching the
database.
"""

from dataclasses import dataclass

import db

EXPENSIVE_PURCHASE_THRESHOLD = 10_000.00   # over this, approve_purchase_request must elicit confirmation
APPROVER_ROLES = {"Project Manager", "Finance Officer"}
WAREHOUSE_ROLES = {"Warehouse Supervisor"}


class AuthorizationError(Exception):
    pass


class ValidationError(Exception):
    pass


@dataclass
class ActingEmployee:
    id: int
    name: str
    role: str
    project_id: int | None


def load_acting_employee(employee_id: int) -> ActingEmployee:
    row = db.get_employee(employee_id)
    if row is None:
        raise AuthorizationError(f"No such EmployeeID {employee_id}")
    return ActingEmployee(id=row["EmployeeID"], name=row["Name"], role=row["Role"], project_id=row["ProjectID"])


def require_role(employee: ActingEmployee, allowed_roles: set[str]):
    if employee is None:
        raise AuthorizationError("This action requires an authenticated approver session.")
    if employee.role not in allowed_roles:
        raise AuthorizationError(
            f"{employee.name} has role '{employee.role}', which is not permitted to perform "
            f"this action (requires one of {sorted(allowed_roles)})."
        )


def require_project_scope(employee: ActingEmployee, project_id: int):
    """Finance Officers and Warehouse Supervisors are cross-project by
    design (ProjectID is NULL in Employees). Project Managers are scoped
    to their own project only."""
    if employee.role == "Project Manager" and employee.project_id != project_id:
        raise AuthorizationError(
            f"{employee.name} manages project {employee.project_id}, not project {project_id}. "
            "Cross-project approval requires Finance."
        )


def validate_request_pending(req: dict):
    if req is None:
        raise ValidationError("Purchase request does not exist.")
    if req["Status"] not in ("Pending",):
        raise ValidationError(f"Request {req['RequestID']} is '{req['Status']}', not Pending — nothing to approve.")


def validate_request_approved(req: dict):
    if req is None:
        raise ValidationError("Purchase request does not exist.")
    if req["Status"] != "Approved":
        raise ValidationError(f"Request {req['RequestID']} is '{req['Status']}', not Approved — cannot reserve material for it.")


def validate_within_budget(req: dict, project: dict):
    """Requests that exceed the project's remaining budget must be
    escalated to management, never auto-approved — this is a hard
    block, not something elicitation can override."""
    if req["EstimatedCost"] > project["RemainingBudget"]:
        raise ValidationError(
            f"Request {req['RequestID']} costs ${req['EstimatedCost']:,.2f}, which exceeds project "
            f"{project['ProjectID']}'s remaining budget of ${project['RemainingBudget']:,.2f}. "
            "This must go through escalate_purchase_request, not direct approval."
        )


def needs_elicitation(estimated_cost: float) -> bool:
    return estimated_cost > EXPENSIVE_PURCHASE_THRESHOLD


def would_breach_min_stock(material: dict, quantity: float) -> bool:
    return (material["QuantityAvailable"] - quantity) < material["MinimumStockLevel"]


def validate_sufficient_stock(material: dict, quantity: float):
    if quantity > material["QuantityAvailable"]:
        raise ValidationError(
            f"Only {material['QuantityAvailable']} units of {material['MaterialName']} are available; "
            f"cannot reserve {quantity}."
        )
