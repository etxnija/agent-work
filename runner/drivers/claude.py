import json
import subprocess
from pathlib import Path
from typing import Any, NamedTuple

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


def _load_agent_definition(name: str) -> tuple[str | None, list[str], str | None]:
    """
    Load an agent definition. Returns (body, tools_list, model).
    tools_list comes from the 'tools:' frontmatter field (comma-separated).
    model comes from the 'model:' frontmatter field, or None if absent.
    Returns (None, [], None) if the agent is not found.
    """
    for base in _AGENT_SEARCH_PATHS:
        candidate = base / f"{name}.md"
        if candidate.exists():
            fields, body = _parse_frontmatter(candidate.read_text())
            tools_raw = fields.get("tools", "")
            tools = [t.strip() for t in tools_raw.split(",") if t.strip()] if tools_raw else []
            model = fields.get("model") or None
            return body, tools, model
    return None, [], None


class _Usage(NamedTuple):
    """Token counters from the CLI's top-level `usage` object; None when unavailable."""
    input_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None


def _int_or_none(value: Any) -> int | None:
    # bool is an int subclass; reject it explicitly.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parse_usage(usage: Any) -> _Usage:
    if not isinstance(usage, dict):
        return _Usage()
    return _Usage(
        input_tokens=_int_or_none(usage.get("input_tokens")),
        cache_read_tokens=_int_or_none(usage.get("cache_read_input_tokens")),
        cache_write_tokens=_int_or_none(usage.get("cache_creation_input_tokens")),
    )


def _parse_result_json(stdout: str) -> tuple[str, float | None, str | None, _Usage]:
    """
    Parse `claude --print --output-format json` stdout into
    (text, cost_usd, session_id, usage).
    usage holds input / cache-read / cache-write token counts from the top-level `usage`
    object (cumulative over all turns of the session); each is None when `usage` is
    missing or not a dict, the key is missing, or the value is not an int (bool rejected).
    Falls back to (stdout.strip(), None, None, all-None usage) on invalid JSON or a
    non-dict payload, so a malformed or empty response degrades instead of raising.
    """
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout.strip(), None, None, _Usage()
    if not isinstance(parsed, dict):
        return stdout.strip(), None, None, _Usage()
    return (
        parsed.get("result", ""),
        parsed.get("total_cost_usd"),
        parsed.get("session_id"),
        _parse_usage(parsed.get("usage")),
    )


def _model_args(model: str | None) -> list[str]:
    """Return the `--model` CLI flag for model, or [] when model is None/empty."""
    return ["--model", model] if model else []


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
        model: str | None = None,
    ) -> AgentResult:
        context_files = context_files or []
        full_prompt = _inject_context(prompt, context_files)
        result = subprocess.run(
            [
                "claude", "--print", "--dangerously-skip-permissions",
                "--output-format", "json",
                full_prompt,
            ] + _model_args(model),
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
        )
        text, cost_usd, session_id, usage = _parse_result_json(result.stdout)
        return AgentResult(
            text=text,
            exit_code=result.returncode,
            cost_usd=cost_usd,
            session_id=session_id,
            input_tokens=usage.input_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
        )

    def agent_model(self, agent_name: str) -> str | None:
        _, _, model = _load_agent_definition(agent_name)
        return model

    def run_subagent(
        self,
        agent_name: str,
        prompt: str,
        context_files: list[str] | None = None,
        cwd: Path | None = None,
    ) -> AgentResult:
        """
        Load the agent definition from agents/<name>.md, compose its system prompt inline,
        and run via the Claude CLI.

        If the agent declares a 'tools:' list in its frontmatter, --allowedTools is used
        instead of --dangerously-skip-permissions (tighter grant, no permission prompts).
        If the agent declares a 'model:' field in its frontmatter, --model is appended to
        route that subagent to a specific model; omitted entirely when absent.
        Inline composition keeps this portable: changing the driver is all that's needed
        to support a different tool.
        """
        body, tools, model = _load_agent_definition(agent_name)
        if body is None:
            return AgentResult(
                text=(
                    f"[error] Agent definition not found: '{agent_name}'. "
                    f"Searched: {[str(p) for p in _AGENT_SEARCH_PATHS]}"
                ),
                exit_code=1,
            )

        context_files = context_files or []
        full_prompt = _inject_context(f"{body}\n\n---\n\n{prompt}", context_files)

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

        cmd += _model_args(model)

        result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, check=False)
        text, cost_usd, session_id, usage = _parse_result_json(result.stdout)
        return AgentResult(
            text=text,
            exit_code=result.returncode,
            cost_usd=cost_usd,
            session_id=session_id,
            input_tokens=usage.input_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
        )
