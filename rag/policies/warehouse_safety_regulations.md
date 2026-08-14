# Warehouse Safety Regulations (IronBridge Safety Policy #2)

Applicable department: Warehouse
Effective: 2026-01-01

## Purpose
Sets access control, fire safety, and stacking-height requirements for
all IronBridge warehouse locations, and defines the low-stock approval
workflow referenced by the `reserve_material` tool.

## Low-stock approval workflow
A material reservation is only released automatically when the
resulting `QuantityAvailable` stays **at or above** `MinimumStockLevel`.
If releasing the requested quantity would drop stock below that
threshold, the Warehouse Supervisor must explicitly confirm the release
before it proceeds — this protects other active projects that may be
relying on the same material, and is why `reserve_material` pauses for
confirmation (`elicitation/create`) rather than auto-releasing.

## General warehouse access
- Only Warehouse Supervisors and authorized Procurement Officers may
  physically release materials from a warehouse.
- Fire lanes must remain clear at all times; no pallet may be stored
  within 1 meter of a marked fire exit.

This document is read by the assistant as reference context, not
called as a tool, because it's reasoning material rather than a lookup
with parameters.
