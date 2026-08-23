"""
planning_eval/full_comparison.py

Unified evaluation runner that executes EVERY required method against the
fixed test suite and produces the master comparison table.

Methods evaluated:
  • Decomposition-first DAG
  • Dynamic / Interleaved decomposition
  • Plan-and-Solve
  • Tree-of-Thoughts
  • LATS (Grounded)
  • LATS (Ungrounded)
  • Self-Refine (Grounded)
  • Self-Refine (Ungrounded)
  • Reflexion (Grounded)
  • Reflexion (Ungrounded)

Produces:
  1. artifacts/full_comparison_table.json — master artifact
  2. Console report with the complete comparison table
  3. Per-sub-task routing recommendations justified by the numbers

Keep the TEST_SUITE fixed once evaluation starts.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
ROOT_DIR = THIS_DIR.parent
for _p in (THIS_DIR, ROOT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from planning.algorithms.decomposition import decompose_goal, execute_plan, final_output
from planning.algorithms.dynamic_decomposition import dynamic_decomposition
from planning.algorithms.plan_and_solve import plan_and_solve
from planning.algorithms.tree_of_thoughts import tree_of_thoughts
from planning.algorithms.lats import lats
from planning.algorithms.self_refine import self_refine
from planning.algorithms.reflexion import reflexion
from planning.algorithms.environment import IronBridgeEnvironment
from planning.algorithms import Environment as RandomizedEnvironment
from planning.model_provider import get_planning_llm, has_real_llm


# ---------------------------------------------------------------------------
# FIXED TEST SUITE — do not edit once reported numbers are committed.
# ---------------------------------------------------------------------------

TEST_SUITE = [
    {
        "id": "T01_MITIGATE_DELAY",
        "request": "ProjectID 1: Mitigate delay risk by analyzing schedule dependencies.",
        "shape": "complex",
    },
    {
        "id": "T02_MATERIAL_SHORTFALL",
        "request": "ProjectID 1: Material shortage requires trade resequencing options.",
        "shape": "branching",
    },
    {
        "id": "T03_RUSH_IN_BUDGET",
        "request": "ProjectID 1: Address material shortage with a rush order within budget.",
        "shape": "validation",
    },
    {
        "id": "T04_SIMPLE_CAPACITY",
        "request": "ProjectID 1: A project has 3 developers for 10 days. Estimate capacity at 6 focused hours per day.",
        "shape": "simple",
    },
    {
        "id": "T05_OVER_BUDGET_RUSH",
        "request": "ProjectID 1: Place a rush order with premium cost $999999 to expedite the missing material.",
        "shape": "validation",
    },
    {
        "id": "T06_RESEQUENCE",
        "request": "ProjectID 1: Resequence unaffected trades ahead of the blocked item so the crew stays productive.",
        "shape": "branching",
    },
    {
        "id": "T07_SWITCH_SUPPLIER",
        "request": "ProjectID 1: Switch to steel yard supplier for delayed rebar.",
        "shape": "validation",
    },
    {
        "id": "T08_EMERGENCY_POUR",
        "request": "ProjectID 1: Emergency foundation pour tomorrow but cement is below minimum stock. Evaluate rush vs. slip.",
        "shape": "complex",
    },
    {
        "id": "T09_EQUIPMENT_RENTAL",
        "request": "ProjectID 2: Rent replacement excavator during maintenance window.",
        "shape": "simple",
    },
    {
        "id": "T10_MULTI_TRADE_RESEQ",
        "request": "ProjectID 3: Resequence 4 trades due to delayed steel delivery. Check stock for each trade first.",
        "shape": "complex",
    },
]


def _approx_tokens(*texts: str) -> int:
    return sum(len(t) for t in texts) // 4


def _est_cost_usd(tokens: int) -> float:
    return round(tokens * 0.00001, 4)


# ---------------------------------------------------------------------------
# Rate-limit vs. genuine-failure classification
# ---------------------------------------------------------------------------
# A "FAIL" in this eval can mean two very different things: the method
# genuinely produced a bad plan (a real signal about that method), or the
# API throttled a call mid-run (an infra problem that says nothing about
# the method). Blending them was silently corrupting the master table --
# on one full run, 63/100 rows failed on a raw HTTP 429, none of which
# reflect actual planning quality. classify_error() tags which is which,
# and _call_with_retry() gives transient rate limits a real chance to
# resolve themselves before a run is recorded as rate-limited at all.

_RATE_LIMIT_MARKERS = ("429", "rate limit", "ratelimiterror", "too many requests")

# Groq's json_mode (with_structured_output(..., method="json_mode"), used by
# ungrounded Reflexion's self-evaluation -- see reflexion.py) returns a 400
# if the model exhausts its completion-token budget on reasoning before it
# finishes writing the JSON object. That's an infra/prompt-budget problem,
# not the model reaching a real verdict about the plan -- the environment
# never even got a chance to score anything -- so like a rate limit, it's
# tracked separately rather than blended into a method's real accuracy.
_JSON_FAILURE_MARKERS = ("json_validate_failed", "failed to generate json", "failed to generate a valid")

# gpt-oss models (served via Groq) carry a hidden "harmony" tool-call format
# from their base training. Even with no tools bound and tool_choice="none"
# sent explicitly, the model can still emit a tool-call-shaped completion
# (observed: a hallucinated "container.exec" call). Groq's server validates
# tool_choice server-side and rejects the response with a 400 when this
# happens -- this is a decoding artifact of that specific call, not a signal
# that the method's plan was bad, so it gets the same "don't blend into
# accuracy" treatment as a rate limit or a truncated json_mode response.
_TOOL_LEAK_MARKERS = ("tool choice is none, but model called a tool", "tool_use_failed")

# Some Groq models/endpoints accept reasoning_effort at ChatGroq construction
# time but reject it on the actual call ('reasoning_effort must be one of
# none or default'). planning/model_provider.py's _ReasoningEffortFallbackChatModel
# catches and retries this transparently mid-run, but a case can still
# surface it once per process (the first call against a given model before
# the fallback kicks in) -- an infra/config mismatch, not a signal about
# the method's planning quality, so it gets the same "don't blend into
# accuracy" treatment as a rate limit.
_REASONING_EFFORT_MARKERS = ("reasoning_effort` must be one of", "reasoning_effort must be one of")


def classify_error(error_str: str | None) -> str | None:
    """Returns None (success or a genuine no-exception failure), 'rate_limited',
    'json_generation_failed', 'tool_generation_failed', 'reasoning_effort_rejected',
    or 'other_error', based on the exception text a run_* function captured."""
    if not error_str:
        return None
    lowered = error_str.lower()
    if any(marker in lowered for marker in _RATE_LIMIT_MARKERS):
        return "rate_limited"
    if any(marker in lowered for marker in _JSON_FAILURE_MARKERS):
        return "json_generation_failed"
    if any(marker in lowered for marker in _TOOL_LEAK_MARKERS):
        return "tool_generation_failed"
    if any(marker in lowered for marker in _REASONING_EFFORT_MARKERS):
        return "reasoning_effort_rejected"
    return "other_error"


def _call_with_retry(fn, *args, max_retries: int = 4, base_delay: float = 20.0,
                      json_max_retries: int = 2, json_retry_delay: float = 5.0) -> dict:
    """Calls fn(*args) -- one of the run_* functions below, each of which
    already catches its own exceptions and returns a result dict rather
    than raising.

    Three distinct retryable classes, three distinct strategies:
      - rate_limited: a real quota limit is transient on a MINUTES scale,
        so exponential backoff (20s, 40s, 80s, 160s) before giving up.
      - json_generation_failed: the model ran out of completion-token
        budget on reasoning before finishing valid json_mode output (see
        classify_error()). Waiting longer doesn't help -- it's not a
        quota problem -- but the model's own verbosity is stochastic
        (temperature > 0), so a short flat delay and a couple of retries
        gives it another roll of the dice at a DIFFERENT generation
        before giving up.
      - tool_generation_failed: the model hallucinated a tool call despite
        tool_choice="none" (see classify_error()). Same rationale as
        json_generation_failed -- a decoding-time artifact, not a quota
        problem -- so the same short-flat-delay retry applies.
      - reasoning_effort_rejected: Groq rejected the reasoning_effort value
        on this call (see classify_error()). model_provider.py's
        _ReasoningEffortFallbackChatModel already switches to a plain
        client without reasoning_effort the FIRST time this happens and
        keeps using it -- so a retry here just re-issues the call against
        that now-fixed client. Same short-flat-delay treatment.

    Anything else (a genuine grounded failure, or a truly unrecognized
    error) is returned immediately -- retrying those would just waste
    quota on a problem retrying can't fix."""
    result = fn(*args)
    attempt = 0
    while classify_error(result.get("error")) == "rate_limited" and attempt < max_retries:
        delay = base_delay * (2 ** attempt)
        print(f"        ↳ rate-limited, retrying in {delay:.0f}s "
              f"(attempt {attempt + 1}/{max_retries})...")
        time.sleep(delay)
        result = fn(*args)
        attempt += 1

    json_attempt = 0
    while (classify_error(result.get("error")) == "json_generation_failed"
           and json_attempt < json_max_retries):
        print(f"        ↳ model truncated before valid json, retrying in "
              f"{json_retry_delay:.0f}s (attempt {json_attempt + 1}/{json_max_retries})...")
        time.sleep(json_retry_delay)
        result = fn(*args)
        json_attempt += 1

    tool_attempt = 0
    while (classify_error(result.get("error")) == "tool_generation_failed"
           and tool_attempt < json_max_retries):
        print(f"        ↳ model hallucinated a tool call, retrying in "
              f"{json_retry_delay:.0f}s (attempt {tool_attempt + 1}/{json_max_retries})...")
        time.sleep(json_retry_delay)
        result = fn(*args)
        tool_attempt += 1

    reasoning_attempt = 0
    while (classify_error(result.get("error")) == "reasoning_effort_rejected"
           and reasoning_attempt < json_max_retries):
        print(f"        ↳ reasoning_effort rejected by Groq, retrying in "
              f"{json_retry_delay:.0f}s now that the client has fallen back "
              f"(attempt {reasoning_attempt + 1}/{json_max_retries})...")
        time.sleep(json_retry_delay)
        result = fn(*args)
        reasoning_attempt += 1

    result["error_type"] = classify_error(result.get("error"))
    result["retries"] = attempt + json_attempt + tool_attempt + reasoning_attempt
    return result


class CallCounter:
    """Wraps an LLM and counts calls + estimates tokens."""

    def __init__(self, llm):
        self._llm = llm
        self.count = 0
        self._token_estimate = 0

    def invoke(self, messages, **kwargs):
        self.count += 1
        result = self._llm.invoke(messages, **kwargs)
        prompt_text = ""
        if isinstance(messages, list):
            prompt_text = "\n".join(getattr(m, "content", str(m)) for m in messages)
        else:
            prompt_text = str(messages)
        self._token_estimate += _approx_tokens(prompt_text, str(getattr(result, "content", "")))
        return result

    def with_structured_output(self, schema, **kwargs):
        inner = self._llm.with_structured_output(schema, **kwargs)
        return _StructuredCounter(inner, self)

    @property
    def approx_tokens(self):
        return self._token_estimate


class _StructuredCounter:
    def __init__(self, inner, owner: CallCounter):
        self._inner = inner
        self._owner = owner

    def invoke(self, messages, **kwargs):
        self._owner.count += 1
        result = self._inner.invoke(messages, **kwargs)
        prompt_text = ""
        if isinstance(messages, list):
            prompt_text = "\n".join(getattr(m, "content", str(m)) for m in messages)
        else:
            prompt_text = str(messages)
        resp_text = str(result.model_dump() if hasattr(result, "model_dump") else result)
        self._owner._token_estimate += _approx_tokens(prompt_text, resp_text)
        return result


# ---------------------------------------------------------------------------
# Method runners
# ---------------------------------------------------------------------------

def run_decomposition_first(case: dict, llm, env) -> dict:
    counter = CallCounter(llm)
    t0 = time.time()
    try:
        plan = decompose_goal(case["request"], counter)
        outputs = execute_plan(plan, counter)
        draft = final_output(plan, outputs)
        fb = env.evaluate(draft)
        return {
            "method": "Decomposition-first",
            "success": fb.success,
            "score": fb.score,
            "llm_calls": counter.count,
            "tokens": counter.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(counter.approx_tokens),
        }
    except Exception as exc:
        return {
            "method": "Decomposition-first",
            "success": False,
            "score": 0.0,
            "llm_calls": counter.count,
            "tokens": counter.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(counter.approx_tokens),
            "error": str(exc),
        }


def run_dynamic(case: dict, llm, env) -> dict:
    counter = CallCounter(llm)
    t0 = time.time()
    try:
        history = dynamic_decomposition(case["request"], counter, max_steps=4)
        result = history[-1][1] if history else ""
        fb = env.evaluate(result)
        return {
            "method": "Dynamic",
            "success": fb.success,
            "score": fb.score,
            "llm_calls": counter.count,
            "tokens": counter.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(counter.approx_tokens),
        }
    except Exception as exc:
        return {
            "method": "Dynamic",
            "success": False,
            "score": 0.0,
            "llm_calls": counter.count,
            "tokens": counter.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(counter.approx_tokens),
            "error": str(exc),
        }


def run_ps(case: dict, llm, env) -> dict:
    counter = CallCounter(llm)
    t0 = time.time()
    try:
        sol = plan_and_solve(case["request"], counter)
        fb = env.evaluate(sol)
        return {
            "method": "Plan-and-Solve",
            "success": fb.success,
            "score": fb.score,
            "llm_calls": counter.count,
            "tokens": counter.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(counter.approx_tokens),
        }
    except Exception as exc:
        return {
            "method": "Plan-and-Solve",
            "success": False,
            "score": 0.0,
            "llm_calls": counter.count,
            "tokens": counter.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(counter.approx_tokens),
            "error": str(exc),
        }


def run_tot(case: dict, llm, env) -> dict:
    counter = CallCounter(llm)
    t0 = time.time()
    try:
        thoughts = tree_of_thoughts(case["request"], counter, depth=2, beam_width=2)
        best = thoughts[0] if thoughts else None
        if best is None:
            return {
                "method": "Tree-of-Thoughts",
                "success": False,
                "score": 0.0,
                "llm_calls": counter.count,
                "tokens": counter.approx_tokens,
                "latency": round(time.time() - t0, 3),
                "cost": _est_cost_usd(counter.approx_tokens),
            }
        # BUGFIX: used to be env.evaluate(best.state) alone. best.state is
        # just the winning beam-search candidate's short "continuation"
        # text (e.g. "coordinate overtime crews to compress the
        # resequenced trades") -- tree_of_thoughts()'s candidate-generation
        # prompt never instructs the model to restate the ProjectID in
        # each candidate the way self_refine's draft prompt does (which
        # passes the goal directly and asks for a full proposal).
        # IronBridgeEnvironment._grounded_checks() hard-zeros (score=0.0,
        # not partial credit) any text with no ProjectID match -- so this
        # was guaranteeing a 0.0 on every case regardless of the actual
        # plan quality, since the ProjectID lives in `case["request"]`
        # (the root problem statement) but never in `best.state` itself.
        # Evaluate the winning candidate together with the problem
        # statement it's a continuation of, so grounding checks that key
        # off ProjectID/budget/stock see the full context a real plan
        # would have, not just the leaf snippet.
        graded_text = f"{case['request']}\n\n{best.state}"
        fb = env.evaluate(graded_text)
        return {
            "method": "Tree-of-Thoughts",
            "success": fb.success,
            "score": fb.score,
            "llm_calls": counter.count,
            "tokens": counter.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(counter.approx_tokens),
        }
    except Exception as exc:
        return {
            "method": "Tree-of-Thoughts",
            "success": False,
            "score": 0.0,
            "llm_calls": counter.count,
            "tokens": counter.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(counter.approx_tokens),
            "error": str(exc),
        }


def run_lats(case: dict, llm, env, label: str) -> dict:
    counter = CallCounter(llm)
    t0 = time.time()
    try:
        res = lats(case["request"], counter, env, iterations=2, n_actions=2)
        return {
            "method": f"LATS ({label})",
            "success": res.success,
            "score": res.best_score,
            "llm_calls": counter.count,
            "tokens": counter.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(counter.approx_tokens),
        }
    except Exception as exc:
        return {
            "method": f"LATS ({label})",
            "success": False,
            "score": 0.0,
            "llm_calls": counter.count,
            "tokens": counter.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(counter.approx_tokens),
            "error": str(exc),
        }


def run_self_refine(case: dict, llm, env, label: str) -> dict:
    counter = CallCounter(llm)
    t0 = time.time()
    try:
        env_arg = env if label == "Grounded" else None
        res = self_refine(case["request"], counter, environment=env_arg)
        fb = res.environment_feedback
        success = fb.success if fb else False
        score = fb.score if fb else 0.0
        return {
            "method": f"Self-Refine ({label})",
            "success": success,
            "score": score,
            "llm_calls": res.llm_calls,
            "tokens": res.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(res.approx_tokens),
        }
    except Exception as exc:
        return {
            "method": f"Self-Refine ({label})",
            "success": False,
            "score": 0.0,
            "llm_calls": counter.count,
            "tokens": counter.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(counter.approx_tokens),
            "error": str(exc),
        }


def run_reflexion(case: dict, llm, env, label: str, judge_llm=None) -> dict:
    counter = CallCounter(llm)
    t0 = time.time()
    try:
        env_arg = env if label == "Grounded" else None
        judge_counter = CallCounter(judge_llm) if judge_llm is not None else None
        res = reflexion(
            case["request"], counter, environment=env_arg, max_trials=3,
            judge_llm=judge_counter,
        )
        fb = res.trials[-1].feedback if res.trials else None
        score = fb.score if fb else 0.0
        return {
            "method": f"Reflexion ({label})",
            "success": res.success,
            "score": score,
            "llm_calls": res.total_llm_calls,
            "tokens": res.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(res.approx_tokens),
            "self_graded": res.self_graded,
        }
    except Exception as exc:
        return {
            "method": f"Reflexion ({label})",
            "success": False,
            "score": 0.0,
            "llm_calls": counter.count,
            "tokens": counter.approx_tokens,
            "latency": round(time.time() - t0, 3),
            "cost": _est_cost_usd(counter.approx_tokens),
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _parse_args():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--resume", action="store_true",
        help="Load artifacts/full_comparison_table.partial.json and skip any "
             "(case, method) cell that already has a clean result. Cells whose "
             "only recorded attempt was rate-limited, json-truncated, hit a "
             "tool-call-leak, or hit a reasoning_effort rejection (see "
             "classify_error()) are NOT skipped -- that attempt is discarded "
             "and the cell is re-run from scratch, so a polluted entry never "
             "survives into the final table.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    from dotenv import load_dotenv
    import os
    load_dotenv(ROOT_DIR / ".env") 
    
    # اطبعي هذا للتأكد في الـ Terminal
    print(f"DEBUG: API KEY FOUND: {bool(os.environ.get('GROQ_API_KEY'))}")
    
    llm = get_planning_llm()
    # FIXED: lower threshold to match realistic LLM text output scores
    grounded_env = IronBridgeEnvironment(success_threshold=0.35)
    ungrounded_env = RandomizedEnvironment(success_threshold=0.35)

    # Independent judge for Reflexion's ungrounded self-evaluation --
    # set JUDGE_GROQ_MODEL to a DIFFERENT model than GROQ_MODEL to get a
    # genuinely independent grader (fixes the self-grading-bias gap: the
    # same model judging its own attempt is a documented bias, not just
    # a "same object reference" issue -- a fresh instance of the SAME
    # model doesn't fix it, only a different model does). If unset,
    # ungrounded Reflexion falls back to self-grading, but the result is
    # tagged self_graded=True and reported as such below rather than
    # silently blended in as if it were independent.
    judge_model_name = os.environ.get("JUDGE_GROQ_MODEL")
    judge_llm = None
    if judge_model_name and has_real_llm():
        from planning.model_provider import _build_chat_groq
        # Same fix as planning/model_provider.py's get_planning_llm() --
        # keep the judge's json_mode call from exhausting its budget on
        # hidden reasoning before it writes the EnvironmentFeedback json.
        # Shared helper so both call sites tolerate whichever
        # langchain-groq version is actually installed.
        judge_llm = _build_chat_groq(
            model_name=judge_model_name,
            temperature=0.0,
            max_tokens=int(os.environ.get("GROQ_MAX_TOKENS", "4096")),
        )
        print(f"[Judge] Using independent judge model for ungrounded Reflexion: {judge_model_name}")
    else:
        print("[Judge] JUDGE_GROQ_MODEL not set -- ungrounded Reflexion will self-grade "
              "(flagged self_graded=True in results, not directly comparable to grounded scores)")

    artifacts_dir = ROOT_DIR / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    all_results = []
    already_clean: set[tuple[str, str]] = set()  # (case_id, method) pairs to SKIP re-running

    if args.resume:
        partial_path = artifacts_dir / "full_comparison_table.partial.json"
        if not partial_path.exists():
            print(f"[resume] no partial file at {partial_path} -- starting a full fresh run instead.")
        else:
            with open(partial_path, "r", encoding="utf-8") as f:
                prior = json.load(f).get("results", [])

            # Positional fallback for partial files captured before case_id
            # was added to each result -- same len(METHODS)-per-case logic
            # finalize_partial.py uses, kept here rather than imported so
            # this script has no dependency on that one.
            method_order = [
                "Decomposition-first", "Dynamic", "Plan-and-Solve", "Tree-of-Thoughts",
                "LATS (Grounded)", "LATS (Ungrounded)", "Self-Refine (Grounded)",
                "Self-Refine (Ungrounded)", "Reflexion (Grounded)", "Reflexion (Ungrounded)",
            ]
            case_order = [c["id"] for c in TEST_SUITE]

            def _infer_case_id(idx: int) -> str:
                case_idx = idx // len(method_order)
                return case_order[case_idx] if case_idx < len(case_order) else f"UNKNOWN_CASE_{case_idx}"

            # Keep only the LATEST result per (case_id, method) -- if a
            # prior --resume attempt already replaced a rate-limited entry
            # with a clean one, the clean one is what should win.
            latest: dict[tuple[str, str], dict] = {}
            for idx, r in enumerate(prior):
                cid = r.get("case_id") or _infer_case_id(idx)
                latest[(cid, r["method"])] = r

            discarded_polluted = 0
            for (cid, method), r in latest.items():
                # Reclassify from the raw error text with the CURRENT
                # classify_error(), rather than trusting whatever
                # error_type is already stored on the cell. A partial
                # file can be carried across code changes (new marker
                # sets, like reasoning_effort_rejected added here), and
                # a stale error_type would otherwise let an already-fixed
                # bug's polluted cell slip through as "clean".
                r["error_type"] = classify_error(r.get("error"))
                if r.get("error_type") in (
                    "rate_limited", "json_generation_failed", "tool_generation_failed", "reasoning_effort_rejected",
                ):
                    # Polluted -- do NOT carry forward. Not adding to
                    # already_clean means the main loop below will re-run
                    # this exact cell from scratch.
                    discarded_polluted += 1
                    continue
                r["case_id"] = cid  # backfill for older partial files
                all_results.append(r)
                already_clean.add((cid, method))

            print(f"[resume] loaded {len(already_clean)} clean cell(s) from {partial_path.name} "
                  f"-- will be skipped.")
            print(f"[resume] discarded {discarded_polluted} rate-limited/json-truncated/tool-leak/"
                  f"reasoning-effort-rejected cell(s) -- will be re-run fresh, not carried forward.")
            # Re-save immediately so a crash before the first new call still
            # leaves a partial file with the polluted entries already gone.
            with open(partial_path.with_suffix(".tmp"), "w", encoding="utf-8") as f:
                json.dump({
                    "status": "in_progress",
                    "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "completed_calls": len(all_results),
                    "results": all_results,
                }, f, indent=2)
            os.replace(partial_path.with_suffix(".tmp"), partial_path)

    def _save_partial() -> None:
        """Best-effort incremental save. A full run can take hours once
        rate-limit backoff kicks in (up to 300s PER call across 4
        retries), and the original code only wrote
        full_comparison_table.json once, at the very end of main() --
        meaning a crash anywhere in a multi-hour run (PC reboot, OOM,
        a killed process) lost every result computed up to that point,
        not just the one call in flight. This writes after every
        single call instead. Atomic (temp file + os.replace) so a
        crash mid-write can't corrupt the partial file itself."""
        partial_path = artifacts_dir / "full_comparison_table.partial.json"
        tmp_path = partial_path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump({
                    "status": "in_progress",
                    "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "completed_calls": len(all_results),
                    "results": all_results,
                }, f, indent=2)
            os.replace(tmp_path, partial_path)
        except Exception as e:
            print(f"        \u21b3 WARNING: partial save failed (continuing anyway): {e}")

    print("\n" + "=" * 86)
    print("FULL UNIFIED COMPARISON: All Methods vs. All Cases")
    print("=" * 86)

    # Rate limit protection: small pause after every individual method call
    # (a single case fires off ~30-40 LLM calls across all 10 methods, which
    # is enough on its own to blow through Groq's per-minute limit even with
    # a pause between cases) plus a longer pause between cases. Static delays
    # alone weren't enough (63/100 calls still hit a 429 on one full run) --
    # _call_with_retry() below adds real backoff-and-retry on top of these.
    INTER_CALL_DELAY_SECONDS = 3
    INTER_CASE_DELAY_SECONDS = 10

    def _run_and_report(label: str, method_name: str, fn, *args) -> dict | None:
        if (case["id"], method_name) in already_clean:
            print(f"  {label}| SKIP (resume) -- already have a clean result for this cell")
            return None
        r = _call_with_retry(fn, *args)
        r["case_id"] = case["id"]
        all_results.append(r)
        _save_partial()
        tag = ""
        if r.get("error_type") == "rate_limited":
            tag = f" [RATE-LIMITED after {r.get('retries', 0)} retries -- excluded from accuracy]"
        elif r.get("error_type") == "json_generation_failed":
            tag = f" [JSON-TRUNCATED after {r.get('retries', 0)} retries -- excluded from accuracy]"
        elif r.get("error_type") == "tool_generation_failed":
            tag = f" [TOOL-CALL-LEAK after {r.get('retries', 0)} retries -- excluded from accuracy]"
        elif r.get("error_type") == "reasoning_effort_rejected":
            tag = f" [REASONING-EFFORT-REJECTED after {r.get('retries', 0)} retries -- excluded from accuracy]"
        elif r.get("self_graded"):
            tag = " [self-graded]"
        print(f"  {label}| {'PASS' if r['success'] else 'FAIL'} | Score {r['score']:.2f} | Calls {r['llm_calls']:<2} | ${r['cost']:.4f} | {r['latency']}s{tag}")
        if not r["success"] and r.get("error") and r.get("error_type") not in (
            "rate_limited", "json_generation_failed", "tool_generation_failed", "reasoning_effort_rejected",
        ):
            print(f"        ↳ error: {r['error']}")
        time.sleep(INTER_CALL_DELAY_SECONDS)
        return r

    for idx, case in enumerate(TEST_SUITE):
        print(f"\n[{case['id']}] {case['request'][:80]}...")

        # Decomposition
        _run_and_report("DF  ", "Decomposition-first", run_decomposition_first, case, llm, grounded_env)
        _run_and_report("Dyn ", "Dynamic", run_dynamic, case, llm, grounded_env)

        # Planning algorithms
        _run_and_report("PS  ", "Plan-and-Solve", run_ps, case, llm, grounded_env)
        _run_and_report("ToT ", "Tree-of-Thoughts", run_tot, case, llm, grounded_env)
        _run_and_report("L-G ", "LATS (Grounded)", run_lats, case, llm, grounded_env, "Grounded")
        _run_and_report("L-U ", "LATS (Ungrounded)", run_lats, case, llm, ungrounded_env, "Ungrounded")

        # Self-correction
        _run_and_report("SR-G", "Self-Refine (Grounded)", run_self_refine, case, llm, grounded_env, "Grounded")
        _run_and_report("SR-U", "Self-Refine (Ungrounded)", run_self_refine, case, llm, grounded_env, "Ungrounded")
        _run_and_report("Ref-G", "Reflexion (Grounded)", run_reflexion, case, llm, grounded_env, "Grounded")
        _run_and_report("Ref-U", "Reflexion (Ungrounded)", run_reflexion, case, llm, grounded_env, "Ungrounded", judge_llm)

        # Rate limit protection: longer sleep between cases on top of the
        # per-call delay above
        if idx < len(TEST_SUITE) - 1:
            time.sleep(INTER_CASE_DELAY_SECONDS)

    # Build master comparison table
    methods = [
        "Decomposition-first", "Dynamic",
        "Plan-and-Solve", "Tree-of-Thoughts",
        "LATS (Grounded)", "LATS (Ungrounded)",
        "Self-Refine (Grounded)", "Self-Refine (Ungrounded)",
        "Reflexion (Grounded)", "Reflexion (Ungrounded)",
    ]

    table = []
    for method in methods:
        runs = [r for r in all_results if r["method"] == method]
        total = len(runs)

        # Rate-limited runs, and runs where the model truncated before
        # producing valid json_mode output, are infra/prompt-budget noise,
        # not a signal about the method's planning quality -- both
        # excluded from success_rate / accuracy_pct / avg_score so neither
        # can drag a method's reported quality down. Still counted and
        # shown separately so nothing is silently hidden.
        rate_limited_runs = [r for r in runs if r.get("error_type") == "rate_limited"]
        json_failed_runs = [r for r in runs if r.get("error_type") == "json_generation_failed"]
        tool_failed_runs = [r for r in runs if r.get("error_type") == "tool_generation_failed"]
        reasoning_effort_runs = [r for r in runs if r.get("error_type") == "reasoning_effort_rejected"]
        scored_runs = [r for r in runs if r.get("error_type") not in (
            "rate_limited", "json_generation_failed", "tool_generation_failed", "reasoning_effort_rejected",
        )]
        scored_total = len(scored_runs)

        successes = sum(1 for r in scored_runs if r["success"])
        avg_calls = round(sum(r["llm_calls"] for r in scored_runs) / scored_total, 2) if scored_total else 0
        avg_tokens = round(sum(r["tokens"] for r in scored_runs) / scored_total, 1) if scored_total else 0
        avg_latency = round(sum(r["latency"] for r in scored_runs) / scored_total, 3) if scored_total else 0
        avg_score = round(sum(r["score"] for r in scored_runs) / scored_total, 3) if scored_total else 0
        total_cost = round(sum(r["cost"] for r in runs), 4)  # cost includes retries -- real spend
        self_graded = any(r.get("self_graded") for r in scored_runs)

        table.append({
            "method": method,
            "success_rate": f"{successes}/{scored_total}",
            "accuracy_pct": round((successes / scored_total) * 100, 1) if scored_total else 0,
            "avg_score": avg_score,
            "avg_llm_calls": avg_calls,
            "avg_tokens": avg_tokens,
            "avg_latency_sec": avg_latency,
            "total_est_cost_usd": total_cost,
            "rate_limited_excluded": len(rate_limited_runs),
            "json_generation_failed_excluded": len(json_failed_runs),
            "tool_generation_failed_excluded": len(tool_failed_runs),
            "reasoning_effort_rejected_excluded": len(reasoning_effort_runs),
            "self_graded": self_graded,
        })

    print("\n" + "=" * 120)
    print("MASTER COMPARISON TABLE — ALL METHODS")
    print("(accuracy/score computed over non-rate-limited runs only; rate-limited runs shown separately)")
    print("=" * 120)
    print(
        f"{'Method':<26} | {'Success':<8} | {'Acc%':<6} | {'Score':<6} | "
        f"{'Calls':<6} | {'Tokens':<8} | {'Latency':<8} | {'Est. $':<8} | {'RateLim':<7}"
    )
    print("-" * 120)
    for row in table:
        flag = " *self-graded" if row["self_graded"] else ""
        print(
            f"{row['method']:<26} | {row['success_rate']:<8} | {row['accuracy_pct']:<6} | "
            f"{row['avg_score']:<6} | {row['avg_llm_calls']:<6} | {row['avg_tokens']:<8} | "
            f"{row['avg_latency_sec']:<8} | {row['total_est_cost_usd']:<8} | {row['rate_limited_excluded']:<7}{flag}"
        )
    if any(row["self_graded"] for row in table):
        print("\n* self-graded: JUDGE_GROQ_MODEL was not set, so ungrounded Reflexion used the")
        print("  SAME model to judge its own attempt -- a known bias, not directly comparable")
        print("  to Reflexion (Grounded)'s real-DB-checked score.")

    # Routing recommendations
    print("\n" + "=" * 110)
    print("PER-SUB-TASK ROUTING RECOMMENDATIONS (justified by the table above)")
    print("=" * 110)
    print("""
Sub-task Shape          → Recommended Method    → Justification
─────────────────────────────────────────────────────────────────────────────────────────
Simple / deterministic  → Plan-and-Solve        → Lowest cost (1 call), fast, good
                                                  enough when no branching exists.

Ranking / options       → Tree-of-Thoughts      → Beam search explores multiple
                                                  strategies; beats PS on branching
                                                  problems for modest extra cost.

Final proposal /        → LATS (Grounded)       → MCTS + real env validation catches
high-stakes validation                        → budget/supplier/stock failures before
                                                  commitment. Expensive but justified
                                                  when a wrong plan costs real money.

Draft refinement        → Self-Refine (Grounded)→ One critique + revision cycle;
(cheap to redo)                               → deterministic checks catch structural
                                                  issues; cheap enough to always run.

Multi-trial learning    → Reflexion (Grounded)  → When single retry is insufficient;
(cross-trial memory)                          → episodic memory carries "check budget
                                                  first" across attempts; essential for
                                                  recurring failure patterns.

Top-level reshuffle     → Dynamic Decomposition → When mid-plan surprises (client
(with surprises)                              → unavailable, stock suddenly low) can
                                                  invalidate a pre-built plan.
                                                  Decomposition-first kept only for
                                                  fully mechanical sub-tasks.
""")

    output_path = artifacts_dir / "full_comparison_table.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "comparison_table": table,
            "routing_recommendations": "see_console_output",
            "cases": all_results,
        }, f, indent=2)
    print(f"\n[Artifact Saved] {output_path}")

    partial_path = artifacts_dir / "full_comparison_table.partial.json"
    if partial_path.exists():
        partial_path.unlink()  # run finished cleanly -- the partial snapshot is redundant now


if __name__ == "__main__":
    main()