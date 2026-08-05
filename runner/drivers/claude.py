import json
import subprocess
from pathlib import Path

from .base import AgentDriver, AgentResult

# Directories searched for agent markdown definitions, in order.
_AGENT_SEARCH_PATHS = [
    Path("agents"),
    Path(".claude/agents"),
]


def _parse_frontmatter(content: str) -> tuple[dict[str, str], str]:
    """
    Split a markdown file into (frontmatter_fields, body).
    Returns ({}, full_content) if there is no YAML frontmatter block.
    """
    if not content.startswith("---"):
        return {}, content.strip()
    parts = content.split("---", 2)
    if len(parts) != 3:
        return {}, content.strip()
    fields: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields, parts[2].strip()


def _load_agent_definition(name: str) -> tuple[str | None, list[str]]:
    """
    Load an agent definition. Returns (body, tools_list).
    tools_list comes from the 'tools:' frontmatter field (comma-separated).
    Returns (None, []) if the agent is not found.
    """
    for base in _AGENT_SEARCH_PATHS:
        candidate = base / f"{name}.md"
        if candidate.exists():
            fields, body = _parse_frontmatter(candidate.read_text())
            tools_raw = fields.get("tools", "")
            tools = [t.strip() for t in tools_raw.split(",") if t.strip()] if tools_raw else []
            return body, tools
    return None, []


def _parse_result_json(stdout: str) -> tuple[str, float | None, str | None]:
    """
    Parse `claude --print --output-format json` stdout into (text, cost_usd, session_id).
    Falls back to (stdout.strip(), None, None) on invalid JSON or a non-dict payload,
    so a malformed or empty response degrades instead of raising.
    """
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout.strip(), None, None
    if not isinstance(parsed, dict):
        return stdout.strip(), None, None
    return parsed.get("result", ""), parsed.get("total_cost_usd"), parsed.get("session_id")


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

    def run(
        self,
        prompt: str,
        context_files: list[str] | None = None,
        cwd: Path | None = None,
    ) -> AgentResult:
        context_files = context_files or []
        full_prompt = _inject_context(prompt, context_files)
        result = subprocess.run(
            [
                "claude", "--print", "--dangerously-skip-permissions",
                "--output-format", "json",
                full_prompt,
            ],
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
        )
        text, cost_usd, session_id = _parse_result_json(result.stdout)
        return AgentResult(text=text, exit_code=result.returncode, cost_usd=cost_usd, session_id=session_id)

    def run_subagent(
        self,
        agent_name: str,
        prompt: str,
        cwd: Path | None = None,
    ) -> AgentResult:
        """
        Load the agent definition from agents/<name>.md, compose its system prompt inline,
        and run via the Claude CLI.

        If the agent declares a 'tools:' list in its frontmatter, --allowedTools is used
        instead of --dangerously-skip-permissions (tighter grant, no permission prompts).
        Inline composition keeps this portable: changing the driver is all that's needed
        to support a different tool.
        """
        body, tools = _load_agent_definition(agent_name)
        if body is None:
            return AgentResult(
                text=(
                    f"[error] Agent definition not found: '{agent_name}'. "
                    f"Searched: {[str(p) for p in _AGENT_SEARCH_PATHS]}"
                ),
                exit_code=1,
            )

        full_prompt = f"{body}\n\n---\n\n{prompt}"

        if tools:
            # --allowedTools restricts which tools are available (security boundary).
            # --dangerously-skip-permissions suppresses approval prompts for those tools
            # (required for unattended operation — without it, Write/Edit would hang waiting
            # for a human to confirm). Together: "only these tools, all auto-approved."
            cmd = [
                "claude", "--print",
                "--allowedTools", ",".join(tools),
                "--dangerously-skip-permissions",
                "--output-format", "json",
                full_prompt,
            ]
        else:
            cmd = [
                "claude", "--print", "--dangerously-skip-permissions",
                "--output-format", "json",
                full_prompt,
            ]

        result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, check=False)
        text, cost_usd, session_id = _parse_result_json(result.stdout)
        return AgentResult(text=text, exit_code=result.returncode, cost_usd=cost_usd, session_id=session_id)
