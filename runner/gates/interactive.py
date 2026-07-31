from pathlib import Path

from .base import ApprovalGate

_PROMPT = "Approve this plan? [y/n/f (feedback)]: "
_HELP = "  y = approve and implement\n  n = reject\n  f = give feedback (amends plan, then review again)"


class InteractiveGate(ApprovalGate):
    """
    Prints the plan and waits for y / n / f input.

    f (feedback): prompts for free-text amendments, appends them to plan.md
    under a '## Human Feedback' section, then redisplays the full plan for
    a final approve/reject decision. The worker sees the feedback alongside
    the original plan.
    """

    def request(self, plan_path: str) -> bool:
        plan = Path(plan_path)
        if not plan.exists():
            print(f"[gate] Plan file not found: {plan_path}")
            return False

        while True:
            self._print_plan(plan)
            answer = input(_PROMPT).strip().lower()

            if answer == "y":
                return True
            elif answer == "n":
                return False
            elif answer == "f":
                self._collect_feedback(plan)
                # Loop — redisplay the amended plan for final approval
            else:
                print(_HELP)

    def _print_plan(self, plan: Path) -> None:
        print("\n" + "=" * 60)
        print(f"PLAN: {plan}")
        print("=" * 60)
        print(plan.read_text())
        print("=" * 60 + "\n")

    def _collect_feedback(self, plan: Path) -> None:
        print("Enter feedback (blank line to finish):")
        lines = []
        while True:
            line = input()
            if line == "":
                break
            lines.append(line)

        if not lines:
            return

        feedback_block = "\n## Human Feedback\n\n" + "\n".join(f"- {l}" for l in lines) + "\n"

        existing = plan.read_text()
        if "## Human Feedback" in existing:
            # Append to existing feedback section rather than adding a duplicate
            plan.write_text(existing.rstrip() + "\n" + "\n".join(f"- {l}" for l in lines) + "\n")
        else:
            plan.write_text(existing.rstrip() + "\n" + feedback_block)

        print("[gate] Feedback added to plan.md.\n")
