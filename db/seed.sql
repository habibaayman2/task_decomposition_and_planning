-- Seed data.
-- Assistant login PINs (Employees with a PinHash can call
-- authenticate_as_approver; everyone else can still use read tools and
-- create_purchase_request without logging in):
--   Rania (Project Manager, Project 1):     5521
--   Omar  (Finance Officer, cross-project): 3390
--   Dalia (Warehouse Supervisor):           7744
--   Sami  (Project Manager, Project 2):     1108

INSERT INTO Employees (EmployeeID, Name, Role, Department, Email, AuthorizationLevel, PinHash, ProjectID) VALUES
 (1, 'Karim Fathy',    'Site Engineer',        'Engineering',  'karim.fathy@ironbridge.example',    1, NULL, NULL),
 (2, 'Rania Adel',     'Project Manager',      'Management',   'rania.adel@ironbridge.example',     4, '9fa4354c24b3c7a2b0d40f870a1f57da4907bd816f130033f994ee9dcfdf08d1', NULL),
 (3, 'Youssef Nabil',  'Procurement Officer',  'Procurement',  'youssef.nabil@ironbridge.example',  2, NULL, NULL),
 (4, 'Omar Sherif',    'Finance Officer',      'Finance',      'omar.sherif@ironbridge.example',    4, '32cf64b0b2b2318c13fa640cb7eba9a4ff8a3d8f26d04f5605520eb3f8037868', NULL),
 (5, 'Dalia Hassan',   'Warehouse Supervisor', 'Warehouse',    'dalia.hassan@ironbridge.example',   3, '03ca1c3278cf01923ea5fbf6780b3dace51a548cf7829816be97d6621a66cc8e', NULL),
 (6, 'Sami Ghanem',    'Project Manager',      'Management',   'sami.ghanem@ironbridge.example',    4, '737c6b9773fa031bee4787ad780d2c9c9ecb3375a7c1f4dbd8047a6cd5c67b4c', NULL),
 (7, 'Layla Mostafa',  'Site Engineer',        'Engineering',  'layla.mostafa@ironbridge.example',  1, NULL, NULL);

INSERT INTO Projects (ProjectID, ProjectName, Client, ProjectLocation, Budget, RemainingBudget, ProjectManagerID, Status) VALUES
 (1, 'Riverside Tower',      'Nile Development Group', 'Cairo, Egypt',       2500000, 42000,  2, 'Active'),
 (2, 'Ironbridge Overpass',  'Ministry of Transport',   'Alexandria, Egypt', 1800000, 210000, 6, 'Active');

-- Backfill employee -> primary project now that Projects exists
-- (Employees.ProjectID and Projects.ProjectManagerID are mutually
-- referencing, so one side has to be inserted NULL and filled in after).
UPDATE Employees SET ProjectID = 1 WHERE EmployeeID IN (1, 2, 3);
UPDATE Employees SET ProjectID = 2 WHERE EmployeeID IN (6, 7);
-- Omar (Finance) and Dalia (Warehouse) stay cross-project (ProjectID NULL).

INSERT INTO MaterialInventory (MaterialID, MaterialName, Category, QuantityAvailable, WarehouseLocation, MinimumStockLevel, UnitPrice, LastUpdated) VALUES
 (1, 'Cement, OPC 42.5',        'Cement',    850,  'Warehouse A — Cairo',       500, 6.50,   '2026-07-20'),
 (2, 'Reinforcement Steel 12mm','Steel',     18,   'Warehouse B — Alexandria', 20,  780.00, '2026-07-22'),   -- already below MinimumStockLevel (edge case)
 (3, 'PVC Pipes, 4in',          'Plumbing',  1200, 'Warehouse A — Cairo',       300, 4.20,   '2026-07-18'),
 (4, 'Electrical Cables, 10AWG','Electrical',60,   'Warehouse A — Cairo',       100, 2.75,   '2026-07-21'),
 (5, 'Concrete Blocks, 8in',    'Concrete',  4000, 'Warehouse B — Alexandria', 1000, 1.10,   '2026-07-19');

INSERT INTO Suppliers (SupplierID, CompanyName, ContactInfo, MaterialCategory, ContractStatus) VALUES
 (1, 'Central Cement Co.',       'orders@centralcement.example', 'Cement',    'Active'),
 (2, 'Ironbridge Steel Yard',    'dispatch@ibsteel.example',      'Steel',     'Active'),
 (3, 'Nile Plumbing Supplies',   'sales@nileplumbing.example',    'Plumbing',  'Under Review'),
 (4, 'Delta Electrical Supply',  'sales@deltaelectrical.example', 'Electrical','Expired');

INSERT INTO Equipment (EquipmentID, EquipmentName, EquipmentType, CurrentSite, Availability, MaintenanceStatus, LastInspectionDate) VALUES
 (1, 'Excavator EX-14',      'Excavator',      'Riverside Tower',     'In Use',           'OK',                  '2026-06-01'),
 (2, 'Tower Crane TC-3',     'Crane',          'Riverside Tower',     'Available',        'OK',                  '2026-07-01'),
 (3, 'Concrete Mixer CM-8',  'Concrete Mixer', 'Ironbridge Overpass', 'Under Maintenance','Scheduled — brakes',  '2026-05-15'),
 (4, 'Bulldozer BD-2',       'Bulldozer',      'Ironbridge Overpass', 'Available',        'OK',                  '2026-07-10');

INSERT INTO SafetyPolicies (PolicyID, PolicyTitle, Description, ApplicableDepartment, EffectiveDate) VALUES
 (1, 'Material Handling Procedures',
     'Defines safe lifting, storage, and transport procedures for construction materials, including required PPE and load limits per material category.',
     'Warehouse', '2026-01-01'),
 (2, 'Warehouse Safety Regulations',
     'Sets access control, fire safety, and stacking-height requirements for all IronBridge warehouse locations.',
     'Warehouse', '2026-01-01'),
 (3, 'Equipment Operation Safety Rules',
     'Certification and pre-use inspection requirements for operating heavy construction equipment.',
     'Engineering', '2026-01-01');

-- Purchase requests: mix of statuses including edge cases the write
-- tools must handle correctly (already-rejected, already-completed,
-- one that will exceed remaining budget, one under the rush/expensive
-- threshold, one over it).
INSERT INTO PurchaseRequests (RequestID, ProjectID, EmployeeID, MaterialID, Quantity, EstimatedCost, ApprovalLevel, Status) VALUES
 (1, 1, 1, 1, 200,  1300.00,   1, 'Pending'),     -- cement, cheap, straightforward approval
 (2, 1, 3, 2, 60,   46800.00,  1, 'Pending'),     -- steel, expensive (>$10k) AND exceeds Project 1's remaining budget ($42,000 < ~implied need) -- edge case for both elicitation and budget-escalation paths
 (3, 1, 1, 4, 500,  1375.00,   1, 'Rejected'),    -- already rejected (edge case)
 (4, 2, 7, 3, 100,  420.00,    1, 'Completed'),   -- already completed (edge case)
 (5, 2, 7, 5, 300,  330.00,    1, 'Pending');     -- concrete blocks, cheap, project 2
