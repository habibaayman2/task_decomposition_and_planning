"""
planning_lab/cli.py

CLI entry point. Uses Groq by default when GROQ_API_KEY is set;
falls back to deterministic fake LLM for offline testing.

Examples:
    # Decomposition-first DAG with grounded critique
    python -m planning_lab.cli "Design a 60-minute phishing-awareness workshop"

    # Dynamic decomposition
    python -m planning_lab.cli "Investigate why customer onboarding completion fell" --mode dynamic

    # Plan-and-Solve
    python -m planning_lab.cli "Estimate capacity for 3 developers" --mode ps

    # Tree-of-Thoughts
    python -m planning_lab.cli "Propose a launch strategy" --mode tot --depth 2 --beam-width 2

    # Reflexion
    python -m planning_lab.cli "Create a security checklist" --mode reflexion --max-trials 3

    # LATS
    python -m planning_lab.cli "Create a security checklist" --mode lats --iterations 2 --n-actions 2
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from planning.algorithms import (
    decompose_goal,
    dynamic_decomposition,
    execute_plan,
    final_output,
    flatten_lats_tree,
    lats,
    plan_and_solve,
    reflexion,
    reflect_and_refine,
    Environment,
    tree_of_thoughts,
)
from planning.model_provider import get_planning_llm

ROOT = Path(__file__).resolve().parents[1]


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="Week 4: decomposition, planning, and reflection lab")
    cli.add_argument("goal", nargs="?", default="Design a 60-minute phishing-awareness workshop for new employees")
    cli.add_argument(
        "--mode",
        choices=["dag", "dynamic", "ps", "tot", "reflexion", "lats"],
        default="dag",
    )
    cli.add_argument("--model", default=None, help="Groq model name (default: openai/gpt-oss-120b)")
    cli.add_argument("--depth", type=int, default=2, choices=range(1, 4))
    cli.add_argument("--beam-width", type=int, default=2, choices=range(1, 4))
    cli.add_argument("--max-trials", type=int, default=3, choices=range(1, 6))
    cli.add_argument("--memory-size", type=int, default=3, choices=range(1, 6))
    cli.add_argument("--iterations", type=int, default=2, choices=range(1, 6))
    cli.add_argument("--n-actions", type=int, default=2, choices=range(1, 4))
    cli.add_argument("--success-threshold", type=float, default=0.6)
    cli.add_argument("--no-reflection", action="store_true")
    return cli


def save_artifact(payload: dict) -> Path:
    artifact_dir = ROOT / "artifacts"
    artifact_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = artifact_dir / f"run-{stamp}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = parser().parse_args()
    load_dotenv(ROOT / ".env")

    # Groq is the default provider; no API key check needed here because
    # get_planning_llm() falls back to DeterministicPlanningLLM automatically.
    llm = get_planning_llm(model=args.model)

    payload: dict = {"mode": args.mode, "model": args.model or "default", "goal": args.goal}

    if args.mode == "dag":
        plan = decompose_goal(args.goal, llm)
        print("Execution batches:", plan.execution_batches())
        outputs = execute_plan(plan, llm)
        draft = final_output(plan, outputs)
        
        
        env = Environment(success_threshold=args.success_threshold)
        reflection = reflect_and_refine(args.goal, llm, environment=env) if not args.no_reflection else None
        
        result = reflection.revised if reflection else draft
        payload.update(plan=plan.model_dump(), outputs=outputs, result=result)
        if reflection:
            payload["reflection"] = {
                "grounded_issues": reflection.grounded_issues,
                "critique": reflection.critique,
                "revised": reflection.revised != reflection.draft,
            }

    elif args.mode == "dynamic":
        history = dynamic_decomposition(args.goal, llm)
        result = history[-1][1] if history else "Planner reported the goal was already complete."
        payload.update(history=history, result=result)

    elif args.mode == "ps":
        result = plan_and_solve(args.goal, llm)
        payload["result"] = result

    elif args.mode == "tot":
        thoughts = tree_of_thoughts(args.goal, llm, args.depth, args.beam_width)
        result = thoughts[0].state if thoughts else "No viable thought survived."
        payload.update(thoughts=[thought.model_dump() for thought in thoughts], result=result)

    elif args.mode == "reflexion":
        environment = Environment(success_threshold=args.success_threshold)
        outcome = reflexion(args.goal, llm, environment, args.max_trials, args.memory_size)
        result = outcome.output
        payload.update(
            success=outcome.success,
            trials=[
                {
                    "number": trial.number,
                    "attempt": trial.attempt,
                    "feedback": trial.feedback.model_dump(),
                    "reflection": trial.reflection,
                }
                for trial in outcome.trials
            ],
            memory=outcome.memory,
            result=result,
        )

    else:  # lats
        environment = Environment(success_threshold=args.success_threshold)
        outcome = lats(args.goal, llm, environment, args.iterations, args.n_actions)
        result = outcome.output
        payload.update(
            success=outcome.success,
            best_score=outcome.best_score,
            iterations=outcome.iterations,
            tree=flatten_lats_tree(outcome.root),
            result=result,
        )

    artifact = save_artifact(payload)
    print("\nRESULT\n======\n" + result)
    print(f"\nRun artifact: {artifact}")


if __name__ == "__main__":
    main()