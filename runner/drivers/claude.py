import subprocess
from pathlib import Path

from .base import AgentDriver, AgentResult

# Directories searched for agent markdown definitions, in order.
_AGENT_SEARCH_PATHS = [
    Path("agents"),
    Path(".claude/agents"),
]


def _load_agent_body(name: str) -> str | None:
    """
    Find agents/<name>.md and return its body (below the YAML frontmatter).
    Returns None if not found.
    """
    for base in _AGENT_SEARCH_PATHS:
        candidate = base / f"{name}.md"
        if candidate.exists():
            content = candidate.read_text()
            if content.startswith("---"):
                parts = content.split("---", 2)
                return parts[2].strip() if len(parts) == 3 else content.strip()
            return content.strip()
    return None


def _inject_context(prompt: str, context_files: list[str]) -> str:
    """
    Prepend the contents of context_files into the prompt.
    Portable: works with any CLI that accepts a single prompt string.
    """
    if not context_files:
        return prompt
    sections = []
    for path in context_files:
        p = Path(path)
        if p.exists():
            sections.append(f"<file path=\"{path}\">\n{p.read_text()}\n</file>")
    if sections:
        return "\n\n".join(sections) + "\n\n" + prompt
    return prompt


class ClaudeDriver(AgentDriver):
    """
    Drives the `claude` CLI via subprocess.
    Future: swap for ClaudeSDKDriver (python-sdk) without touching the loop.
    """

    def run(self, prompt: str, context_files: list[str] = []) -> AgentResult:
        full_prompt = _inject_context(prompt, context_files)
        result = subprocess.run(
            ["claude", "--print", "--dangerously-skip-permissions", full_prompt],
            capture_output=True,
            text=True,
        )
        return AgentResult(text=result.stdout.strip(), exit_code=result.returncode)

    def run_subagent(self, agent_name: str, prompt: str) -> AgentResult:
        """
        Load the agent definition from agents/<name>.md, compose its system
        prompt inline, and run via the Claude CLI.

        Inline composition (rather than a --agent flag) keeps this portable:
        the same pattern works for any CLI tool by changing the driver, not
        the loop.
        """
        body = _load_agent_body(agent_name)
        if body is None:
            return AgentResult(
                text=f"[error] Agent definition not found: '{agent_name}'. "
                     f"Searched: {[str(p) for p in _AGENT_SEARCH_PATHS]}",
                exit_code=1,
            )

        full_prompt = f"{body}\n\n---\n\n{prompt}"
        result = subprocess.run(
            ["claude", "--print", "--dangerously-skip-permissions", full_prompt],
            capture_output=True,
            text=True,
        )
        return AgentResult(text=result.stdout.strip(), exit_code=result.returncode)
