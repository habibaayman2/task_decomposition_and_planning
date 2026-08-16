from __future__ import annotations
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Add the project root directory to sys.path to resolve relative package imports
sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, ConfigDict, Field

# Direct absolute/module imports relative to the newly added path root
from ..models import EnvironmentFeedback
from .environment import IronBridgeEnvironment


class LATSAction(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action: str = Field(min_length=2)
    state: str = Field(min_length=2)


class LATSActionBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actions: list[LATSAction] = Field(min_length=1, max_length=3)


class ValueEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# FIX: llama-3.3-70b-versatile on Groq occasionally invents extra fields
# (e.g. "project_id", "discount_negotiated", "external_feedback") when the
# surrounding prompt mentions that kind of information, even though the
# schema only defines `score`. Because every schema here uses
# `model_config = ConfigDict(extra="forbid")`, those extra fields make Groq
# reject the tool call outright (400 tool_use_failed) before it ever reaches
# our own pydantic validation. Two changes fix this:
#   1. The prompts below are shortened and end with an explicit
#      "respond with ONLY the <field(s)> defined by the schema" instruction,
#      so the model has less to imitate back into the tool call.
#   2. `_invoke_structured_with_retry` retries a failed structured call a
#      couple of times with an even more constrained follow-up instruction,
#      so a single malformed generation doesn't crash the whole run.
# ---------------------------------------------------------------------------

def _invoke_structured_with_retry(
    llm: BaseChatModel,
    schema: type[BaseModel],
    messages: list[tuple[str, str]],
    temperature: float,
    retries: int = 2,
):
    structured = llm.with_structured_output(schema, method="function_calling")
    last_err: Exception | None = None
    attempt_messages = list(messages)
    for attempt in range(retries + 1):
        try:
            return structured.invoke(attempt_messages, temperature=temperature)
        except Exception as e:  # groq.BadRequestError / pydantic.ValidationError, etc.
            last_err = e
            attempt_messages = list(messages) + [
                (
                    "human",
                    "Your previous response included fields that are not part of the "
                    "required schema, or was not valid for it. Respond again with a "
                    "single function call containing ONLY the fields defined by the "
                    "schema -- no extra keys, no explanation text outside the call.",
                )
            ]
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
    raise last_err


@dataclass
class LATSNode:
    state: str
    action: str = "root"
    parent: LATSNode | None = field(default=None, repr=False)
    children: list[LATSNode] = field(default_factory=list, repr=False)
    visits: int = 0
    value_sum: float = 0.0
    environment_score: float = 0.0
    model_score: float = 0.0
    feedback: EnvironmentFeedback | None = None
    reflections: list[str] = field(default_factory=list)

    @property
    def mean_value(self) -> float:
        return self.value_sum / self.visits if self.visits else 0.0


@dataclass
class LATSResult:
    success: bool
    output: str
    best_score: float
    iterations: int
    root: LATSNode
    pruned_count: int = 0


def _uct(node: LATSNode, exploration_weight: float) -> float:
    if node.visits == 0:
        return float("inf")
    parent_visits = max(node.parent.visits if node.parent else 1, 1)
    return node.mean_value + exploration_weight * math.sqrt(math.log(parent_visits) / node.visits)


def _select_leaf(root: LATSNode, exploration_weight: float) -> LATSNode:
    node = root
    while node.children:
        node = max(node.children, key=lambda child: _uct(child, exploration_weight))
    return node


def _backpropagate(node: LATSNode, value: float) -> None:
    while node is not None:
        node.visits += 1
        node.value_sum += value
        node = node.parent


def _trajectory_reflections(node: LATSNode) -> list[str]:
    path: list[str] = []
    while node is not None:
        path.extend(node.reflections)
        node = node.parent
    return list(reversed(path))


def lats(
    task: str,
    llm: BaseChatModel,
    environment: IronBridgeEnvironment,
    iterations: int = 2,
    n_actions: int = 2,
    exploration_weight: float = 1.414,
    max_depth: int = 5,
    prune_threshold: float = 0.0,
) -> LATSResult:
    
    if iterations < 1:
        raise ValueError("iterations must be positive")
    if n_actions < 1:
        raise ValueError("n_actions must be positive")
    if max_depth < 1:
        raise ValueError("max_depth must be positive")
    if iterations < 1 or n_actions < 1:
        raise ValueError("iterations and n_actions must be positive")
    root = LATSNode(state="No attempt yet.")
    best = root
    completed_iterations = 0
    for iteration in range(1, iterations + 1):
        completed_iterations = iteration
        leaf = _select_leaf(root, exploration_weight)
        lessons = _trajectory_reflections(leaf)
        lesson_text = "\n".join(f"- {item}" for item in lessons[-4:]) or "- None yet."

        proposed = _invoke_structured_with_retry(
            llm,
            LATSActionBatch,
            [
                ("system", "You are the action generator in LATS."),
                ("human", f"""Task: {task}
Current trajectory/state:
{leaf.state}
Reflections learned from failed branches:
{lesson_text}

Propose exactly {n_actions} distinct complete candidate solution(s). Each state must
contain the fully written solution, not a placeholder or description of a solution.
Respond with ONLY the "actions" field (a list of {{action, state}} objects) -- no
other fields."""),
            ],
            temperature=0.5,
        )

        for item in proposed.actions[:n_actions]:
            child = LATSNode(state=item.state.strip(), action=item.action, parent=leaf)
            leaf.children.append(child)
            feedback = environment.evaluate(child.state)
            child.feedback = feedback
            child.environment_score = feedback.score

            # NOTE: the fix here is deliberately NOT passing feedback.details
            # (a list of free-text strings that can contain project ids,
            # dollar figures, etc.) into the prompt at all -- that free text
            # was exactly what the model was imitating back as bogus extra
            # fields ("project_id", "discount_negotiated", ...). Only the
            # numeric score is passed; the qualitative "why" already lives
            # in feedback.details on the node for the reflection step below
            # and for the eval trace, it just isn't fed back into this call.
            value_judgment = _invoke_structured_with_retry(
                llm,
                ValueEstimate,
                [
                    ("system", "You are the LATS value function."),
                    ("human", f"""Task: {task}
Candidate state:
{child.state}
External score so far: {feedback.score:.2f}

Estimate this candidate's future usefulness as a single number between 0.0 and 1.0.
Respond with ONLY the "score" field -- no other fields, no explanation."""),
                ],
                temperature=0.1,
            )
            child.model_score = value_judgment.score
            combined_value = 0.75 * child.environment_score + 0.25 * child.model_score

            if not feedback.success:
                response = llm.invoke([
                    ("system", "Create a branch-level LATS reflection grounded in environment feedback."),
                    ("human", f"""Task: {task}
Action: {child.action}
Resulting state: {child.state}
External feedback: {feedback.details}
Explain briefly why this branch failed and how a later expansion should change."""),
                ], temperature=0.2)
                reflection = response.content
                if not isinstance(reflection, str) or not reflection.strip():
                    raise RuntimeError("The chat model returned an empty or unsupported response")
                reflection = reflection.strip()
                child.reflections.append(reflection)

            _backpropagate(child, combined_value)
            if best is root or child.environment_score > best.environment_score:
                best = child
            if feedback.success:
                break  # exit loop, will return after pruning count

    # Count pruned nodes
    pruned_count = 0
    if prune_threshold > 0:
        def _count_pruned(node: LATSNode):
            nonlocal pruned_count
            for child in node.children:
                if child.environment_score < prune_threshold:
                    pruned_count += 1
                _count_pruned(child)
        _count_pruned(root)

    if best is not root and best.environment_score > 0 and (best.feedback is None or best.feedback.success):
        return LATSResult(True, best.state, best.environment_score, completed_iterations, root, pruned_count)
    return LATSResult(False, best.state, best.environment_score, completed_iterations, root, pruned_count)


def flatten_lats_tree(root: LATSNode) -> list[dict]:
    records: list[dict] = []
    queue: list[tuple[LATSNode, str | None]] = [(root, None)]
    next_id = 0
    while queue:
        node, parent_id = queue.pop(0)
        node_id = f"n{next_id}"
        next_id += 1
        records.append(
            {
                "id": node_id,
                "parent_id": parent_id,
                "action": node.action,
                "state": node.state,
                "visits": node.visits,
                "mean_value": node.mean_value,
                "environment_score": node.environment_score,
                "model_score": node.model_score,
                "feedback": node.feedback.model_dump() if node.feedback else None,
                "reflections": node.reflections,
            }
        )
        queue.extend((child, node_id) for child in node.children)
        
    return records
