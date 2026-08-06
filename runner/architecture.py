"""
architecture.py — bounded CLAIM->DOUBT->RECONCILE architecture review cycle.

Self-contained: does not import from runner/loop.py.
"""

from pathlib import Path

from .drivers import get_driver
from .gates import get_gate
from .metrics import Metrics, _MeteredDriver

ARCHITECT_AGENT = "architect"
ARCH_CONVERGED_SIGNAL = "ARCHITECTURE: CONVERGED"
ARCH_REVISED_SIGNAL = "ARCHITECTURE: REVISED"
ARCH_MAX_ROUNDS = 3
ARCH_RECOMMENDATION_FILE = "architecture-recommendation.md"


def _architecture_verdict(text: str) -> tuple[bool, str]:
    """
    Parse the architect's marker-line output into (converged, recommendation_or_claim).

    Neither marker present is treated as not-converged (fail toward another
    round, never silently converge on ambiguous output).
    """
    if ARCH_CONVERGED_SIGNAL in text:
        return True, text.split(ARCH_CONVERGED_SIGNAL, 1)[1].strip()
    if ARCH_REVISED_SIGNAL in text:
        return False, text.split(ARCH_REVISED_SIGNAL, 1)[1].strip()
    return False, text.strip()


def run_architecture_review(target: str, project_root: Path) -> int:
    """
    Run the bounded CLAIM->DOUBT->RECONCILE cycle against target, capped at
    ARCH_MAX_ROUNDS rounds. Writes ARCH_RECOMMENDATION_FILE and pauses on the
    approval gate as a human checkpoint. Always returns 0 — the gate is a
    checkpoint, not a pass/fail decision.
    """
    metrics = Metrics()
    driver = _MeteredDriver(get_driver(), metrics)

    rounds: list[dict] = []
    claim_text = ""
    converged = False
    recommendation = ""

    for round_num in range(1, ARCH_MAX_ROUNDS + 1):
        if round_num == 1:
            claim_prompt = (
                "Mode: CLAIM+EXTRACT\n\n"
                f"Target: {target}\n\n"
                "Examine this file or module and produce exactly one specific, "
                "falsifiable architectural claim about its coupling, cohesion, or "
                "responsibility boundaries, plus the concrete evidence from code "
                "and docs supporting it."
            )
            claim_result = driver.run_subagent(ARCHITECT_AGENT, claim_prompt, cwd=project_root)
            claim_text = claim_result.text

        doubt_prompt = (
            "Mode: DOUBT\n\n"
            f"Target: {target}\n\n"
            f"Prior claim and evidence:\n{claim_text}\n\n"
            "Actively try to falsify this claim: look for counter-evidence, cases "
            "it ignored, documented constraints or decisions (ADRs, roadmap) "
            "explaining why the current shape is intentional, or reasons the "
            "claim is premature or wrong."
        )
        doubt_result = driver.run_subagent(ARCHITECT_AGENT, doubt_prompt, cwd=project_root)
        doubt_text = doubt_result.text

        reconcile_prompt = (
            "Mode: RECONCILE\n\n"
            f"Target: {target}\n\n"
            f"Claim and evidence:\n{claim_text}\n\n"
            f"Doubt raised:\n{doubt_text}\n\n"
            "Decide whether the claim survives, and end with exactly one marker line."
        )
        reconcile_result = driver.run_subagent(ARCHITECT_AGENT, reconcile_prompt, cwd=project_root)
        converged, recommendation = _architecture_verdict(reconcile_result.text)

        rounds.append(
            {
                "claim": claim_text,
                "doubt": doubt_text,
                "reconcile": reconcile_result.text,
                "converged": converged,
                "recommendation_or_revised_claim": recommendation,
            }
        )

        if converged:
            break
        claim_text = recommendation

    lines = [f"# Architecture Review: {target}", ""]
    for idx, entry in enumerate(rounds, 1):
        outcome = "CONVERGED" if entry["converged"] else "REVISED"
        lines.append(f"## Round {idx}")
        lines.append("")
        lines.append(f"**Claim:**\n\n{entry['claim']}")
        lines.append("")
        lines.append(f"**Doubt:**\n\n{entry['doubt']}")
        lines.append("")
        lines.append(f"**Reconcile ({outcome}):**\n\n{entry['reconcile']}")
        lines.append("")

    lines.append("## Final State")
    lines.append("")
    if converged:
        lines.append(f"Converged after {len(rounds)} round(s).\n\n{recommendation}")
    else:
        lines.append(
            f"No consensus reached after {ARCH_MAX_ROUNDS} round(s) — escalated for "
            f"human review.\n\nLast claim under discussion:\n\n{recommendation}"
        )
    lines.append("")
    lines.append(
        f"**Review metrics:** {metrics.calls} driver call(s), ${metrics.cost_usd:.4f}, "
        f"session {metrics.last_session_id}"
    )

    content = "\n".join(lines) + "\n"
    (project_root / ARCH_RECOMMENDATION_FILE).write_text(content)

    print(
        f"[metrics] Architecture review total: {metrics.calls} driver call(s), "
        f"${metrics.cost_usd:.4f}, session {metrics.last_session_id}"
    )

    if get_gate().request(ARCH_RECOMMENDATION_FILE):
        print("recommendation acknowledged")
    else:
        print("recommendation left for later review")

    return 0
