from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
import networkx as nx
from pydantic import BaseModel, ConfigDict, Field, model_validator

# Ensure project root is on path for cross-module imports
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$")
    instruction: str = Field(min_length=5)
    depends_on: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=5)
    tasks: list[Task] = Field(min_length=1, max_length=8)

    # Private cached fields — computed once during validation
    _graph: nx.DiGraph | None = None
    _task_map: dict[str, Task] | None = None
    _topo_order: list[str] | None = None
    _exec_batches: list[list[str]] | None = None
    _terminal_tasks: list[str] | None = None
    _out_degree_map: dict[str, int] | None = None

    @model_validator(mode="after")
    def validate_dag(self) -> "Plan":
        ids = [task.id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("Task ids must be unique")
        known = set(ids)
        for task in self.tasks:
            missing = set(task.depends_on) - known
            if missing:
                raise ValueError(f"{task.id} has unknown dependencies: {sorted(missing)}")
            if task.id in task.depends_on:
                raise ValueError(f"{task.id} cannot depend on itself")

        g = nx.DiGraph()
        g.add_nodes_from(ids)
        g.add_edges_from(
            (dependency, task.id)
            for task in self.tasks
            for dependency in task.depends_on
        )
        if not nx.is_directed_acyclic_graph(g):
            cycle = nx.find_cycle(g)
            blocked = sorted({node for edge in cycle for node in edge[:2]})
            raise ValueError(f"Cycle detected; blocked tasks: {blocked}")

        self._graph = g
        self._task_map = {t.id: t for t in self.tasks}
        self._topo_order = list(nx.topological_sort(g))
        self._exec_batches = [sorted(generation) for generation in nx.topological_generations(g)]
        self._out_degree_map = dict(g.out_degree())
        self._terminal_tasks = [node for node, deg in self._out_degree_map.items() if deg == 0]
        return self

    @property
    def graph(self) -> nx.DiGraph:
        if self._graph is None:
            raise RuntimeError("Plan graph accessed before validation")
        return self._graph

    def topological_order(self) -> list[str]:
        if self._topo_order is None:
            raise RuntimeError("Plan topological_order accessed before validation")
        return list(self._topo_order)

    def execution_batches(self) -> list[list[str]]:
        if self._exec_batches is None:
            raise RuntimeError("Plan execution_batches accessed before validation")
        return [list(batch) for batch in self._exec_batches]

    def task(self, task_id: str) -> Task:
        if self._task_map is None:
            raise RuntimeError("Plan task_map accessed before validation")
        try:
            return self._task_map[task_id]
        except KeyError as exc:
            raise ValueError(f"Task {task_id!r} not found in plan") from exc

    def terminal_tasks(self) -> list[str]:
        if self._terminal_tasks is None:
            raise RuntimeError("Plan terminal_tasks accessed before validation")
        return list(self._terminal_tasks)

    def to_json(self) -> str:
        """Serialize to JSON string with cached structure metadata."""
        return json.dumps({
            "goal": self.goal,
            "tasks": [t.model_dump() for t in self.tasks],
            "topological_order": self.topological_order(),
            "execution_batches": self.execution_batches(),
            "terminal_tasks": self.terminal_tasks(),
        }, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "Plan":
        """Deserialize from JSON. Only goal and tasks are required;
        cached fields are recomputed during validation."""
        data = json.loads(raw)
        return cls.model_validate({
            "goal": data["goal"],
            "tasks": data["tasks"],
        })


class Thought(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str
    score: float = Field(ge=0.0, le=1.0)
    rationale: str = ""


class EnvironmentFeedback(BaseModel):
    """A grounded signal produced outside the language model."""

    success: bool
    score: float = Field(ge=0.0, le=1.0)
    details: list[str] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")
