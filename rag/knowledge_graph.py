import os
import sys
import json
import re
import time
from typing import Dict, List, Any, Optional, Set, Tuple

# Ensure repository root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

class KnowledgeGraph:
    """
    Persistent Knowledge Graph engine tailored for the IronBridge policy corpus.
    Models entities, weighted relationships, and observation constraints.
    """
    def __init__(self, filepath: Optional[str] = "memory/knowledge_graph.json"):
        self.filepath = filepath
        self.entities: Dict[str, Dict[str, Any]] = {}
        self.relations: List[Dict[str, Any]] = []
        if self.filepath:
            self.load()

    def add_entity(self, name: str, entity_type: str = "General", observations: Optional[List[str]] = None) -> None:
        """Adds or updates an entity node with normalized canonical keys."""
        name_key = name.strip().lower()
        if name_key not in self.entities:
            self.entities[name_key] = {
                "canonical_name": name.strip(),
                "type": entity_type,
                "observations": set()
            }
        if observations:
            self.entities[name_key]["observations"].update(observations)

    def add_relation(self, source: str, relation: str, target: str, weight: float = 1.0) -> None:
        """Creates or updates a weighted relationship edge between two entity nodes."""
        src_key = source.strip().lower()
        tgt_key = target.strip().lower()

        self.add_entity(source)
        self.add_entity(target)

        for rel in self.relations:
            if rel["source"] == src_key and rel["relation"] == relation and rel["target"] == tgt_key:
                rel["weight"] = max(rel["weight"], weight)
                return

        self.relations.append({
            "source": src_key,
            "target": tgt_key,
            "relation": relation,
            "weight": weight
        })

    def get_subgraph(self, seed_entities: List[str], max_depth: int = 2) -> Dict[str, Any]:
        """Runs Breadth-First Search (BFS) graph expansion starting from identified seed nodes."""
        visited_entities: Set[str] = set()
        traversed_relations: List[Dict[str, Any]] = []
        queue: List[Tuple[str, int]] = []

        for seed in seed_entities:
            s_key = seed.strip().lower()
            if s_key in self.entities:
                visited_entities.add(s_key)
                queue.append((s_key, 0))

        while queue:
            curr_entity, depth = queue.pop(0)
            if depth >= max_depth:
                continue

            for rel in self.relations:
                next_entity = None
                if rel["source"] == curr_entity:
                    next_entity = rel["target"]
                elif rel["target"] == curr_entity:
                    next_entity = rel["source"]

                if next_entity:
                    traversed_relations.append(rel)
                    if next_entity not in visited_entities:
                        visited_entities.add(next_entity)
                        queue.append((next_entity, depth + 1))

        retrieved_entities = [
            {
                "name": self.entities[k]["canonical_name"],
                "type": self.entities[k]["type"],
                "observations": list(self.entities[k]["observations"])
            }
            for k in visited_entities if k in self.entities
        ]

        return {"entities": retrieved_entities, "relations": traversed_relations}

    def traverse_graph(
        self,
        start_entity: str,
        traversal_type: str = "BFS",
        max_depth: int = 2,
        direction: str = "both"
    ) -> Dict[str, Any]:
        """
        Traverses the graph starting from `start_entity` using BFS or DFS.

        Args:
            start_entity: The entity to start traversal from.
            traversal_type: "BFS" (Breadth-First Search) or "DFS" (Depth-First Search).
            max_depth: Maximum depth to traverse.
            direction: "forward" (source->target), "backward" (target->source), or "both".

        Returns:
            A dictionary with traversed entities and relations.
        """
        start_key = start_entity.strip().lower()
        if start_key not in self.entities:
            return {"entities": [], "relations": []}

        visited_entities: Set[str] = {start_key}
        traversed_relations: List[Dict[str, Any]] = []
        seen_relations: Set[str] = set()  # To deduplicate relations

        queue: List[Tuple[str, int]] = [(start_key, 0)] if traversal_type == "BFS" else []
        stack: List[Tuple[str, int]] = [(start_key, 0)] if traversal_type == "DFS" else []

        while queue or stack:
            if traversal_type == "BFS":
                if not queue:
                    break
                curr_entity, depth = queue.pop(0)
            else:
                if not stack:
                    break
                curr_entity, depth = stack.pop()

            if depth >= max_depth:
                continue

            for rel in self.relations:
                next_entity = None
                if direction in ("forward", "both") and rel["source"] == curr_entity:
                    next_entity = rel["target"]
                elif direction in ("backward", "both") and rel["target"] == curr_entity:
                    next_entity = rel["source"]

                if next_entity:
                    # Create a unique identifier for the relation to avoid duplicates
                    rel_id = f"{rel['source']}->{rel['relation']}->{rel['target']}"
                    if rel_id not in seen_relations:
                        seen_relations.add(rel_id)
                        traversed_relations.append(rel)
                    if next_entity not in visited_entities:
                        visited_entities.add(next_entity)
                        if traversal_type == "BFS":
                            queue.append((next_entity, depth + 1))
                        else:
                            stack.append((next_entity, depth + 1))

        retrieved_entities = [
            {
                "name": self.entities[k]["canonical_name"],
                "type": self.entities[k]["type"],
                "observations": list(self.entities[k]["observations"])
            }
            for k in visited_entities if k in self.entities
        ]

        return {"entities": retrieved_entities, "relations": traversed_relations}

    def save(self) -> None:
        """Saves graph state to JSON file on disk."""
        if not self.filepath:
            return
        os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
        data = {
            "entities": {
                k: {
                    "canonical_name": v["canonical_name"],
                    "type": v["type"],
                    "observations": list(v["observations"])
                } for k, v in self.entities.items()
            },
            "relations": self.relations
        }
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def load(self) -> None:
        """Loads persistent graph state from disk."""
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k, v in data.get("entities", {}).items():
                    self.entities[k] = {
                        "canonical_name": v["canonical_name"],
                        "type": v["type"],
                        "observations": set(v["observations"])
                    }
                self.relations = data.get("relations", [])
        except (FileNotFoundError, json.JSONDecodeError):
            pass

class GraphRAGRetriever:
    """Graph RAG module with context formatting and Self-RAG verification."""
    def __init__(self, kg: KnowledgeGraph):
        self.kg = kg

    def retrieve(self, query: str, seed_entity_keys: Optional[List[str]] = None, max_depth: int = 2) -> str:
        """Identifies seed entities from query terms and returns a formatted Markdown context block."""
        if not seed_entity_keys:
            query_words = set(re.findall(r"\w+", query.lower()))
            seed_entity_keys = [
                k for k in self.kg.entities.keys()
                if any(word in k for word in query_words if len(word) > 2)
            ]

        if not seed_entity_keys:
            return "No relevant Knowledge Graph facts found."

        subgraph = self.kg.get_subgraph(seed_entity_keys, max_depth=max_depth)

        lines = ["### Knowledge Graph Context (IronBridge Corpus)"]
        for ent in subgraph["entities"]:
            if ent["observations"]:
                obs = "; ".join(ent["observations"])
                lines.append(f"* **Entity: {ent['name']}** ({ent['type']}) - {obs}")

        visited_rels = set()
        for rel in subgraph["relations"]:
            rel_id = f"{rel['source']}->{rel['relation']}->{rel['target']}"
            if rel_id not in visited_rels:
                visited_rels.add(rel_id)
                src = self.kg.entities.get(rel['source'], {}).get('canonical_name', rel['source'])
                tgt = self.kg.entities.get(rel['target'], {}).get('canonical_name', rel['target'])
                lines.append(f"* **Relationship:** ({src}) --[{rel['relation']}]--> ({tgt})")

        return "\n".join(lines)

    def verify_self_rag(self, query: str, retrieved_context: str) -> bool:
        """Self-RAG check ensuring retrieved context contains key query terms."""
        if "No relevant Knowledge Graph facts found." in retrieved_context:
            return False
        query_terms = [w.lower() for w in re.findall(r"\w+", query) if len(w) > 2]
        return any(term in retrieved_context.lower() for term in query_terms)