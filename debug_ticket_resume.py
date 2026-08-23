from state_graph.core.checkpoint_store import default_store

run_id = "7c19d2ea-877e-4cc7-95af-84867e8c4d58"  # آخر run_id شفتيه في الـ tickets

loaded = default_store.load(run_id)
if loaded:
    state, current_node, status = loaded
    print("current_node:", current_node)
    print("status:", status)
    print("state:", state)
else:
    print("Run not found")