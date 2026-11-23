"""Talk to Claude.

Two interchangeable backends, picked with the MATHFINDER_LLM environment variable:
  cli  (default) runs the `claude` command line tool: uses your Claude subscription, no key
  api            calls the Anthropic API with the official SDK: needs ANTHROPIC_API_KEY

Every other module only calls `complete(system, prompt)` or `complete_json(...)`,
so it never needs to know which backend is in use.
"""

import json
import os
import re
import subprocess
import tempfile

MODEL = "claude-opus-5-5"
CLI_TIMEOUT = 600  # seconds; a hard proof plan can take a few minutes to write
API_MAX_TOKENS = 64_000


class LLMError(Exception):
    """The model couldn't give us a usable answer (crash, timeout, refusal, bad JSON)."""


def complete_cli(system: str, prompt: str) -> str:
    cmd = [
        "claude", "-p",
        "--system-prompt", system,
        "--tools", "",  # text only: it may not run commands or edit files
        "--no-session-persistence",
        "--strict-mcp-config",
        "--setting-sources", "",  # ignore your personal settings and hooks
        "--output-format", "text",
    ]
    if os.environ.get("MATHFINDER_CLI_MODEL"):
        cmd += ["--model", os.environ["MATHFINDER_CLI_MODEL"]]
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,  # the prompt goes in on stdin, so it can be any length
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT,
            cwd=tempfile.gettempdir(),  # run outside the project so no project files leak in
        )
    except subprocess.TimeoutExpired:
        raise LLMError(f"claude CLI timed out after {CLI_TIMEOUT}s")
    if proc.returncode != 0:
        raise LLMError(f"claude CLI failed: {proc.stderr.strip()[-500:]}")
    text = proc.stdout.strip()
    if not text:
        raise LLMError("claude CLI returned an empty answer")
    return text


_client = None


def get_client():
    """Create the API client once and reuse it (it keeps connections open)."""
    global _client
    if _client is None:
        import anthropic  # imported here so the cli backend works even without a key

        _client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    return _client


def complete_api(system: str, prompt: str) -> str:
    with get_client().beta.messages.stream(
        model=MODEL,
        max_tokens=API_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": "high"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # if a safety check declines, the server retries on a fallback model
    ) as stream:
        message = stream.get_final_message()
    if message.stop_reason == "refusal":
        raise LLMError("the model declined this request")
    text = "".join(block.text for block in message.content if block.type == "text").strip()
    if not text:
        raise LLMError(f"empty answer (stop_reason={message.stop_reason})")
    return text


BACKENDS = {"cli": complete_cli, "api": complete_api}


def complete(system: str, prompt: str) -> str:
    """Ask Claude one question and return its text answer."""
    name = os.environ.get("MATHFINDER_LLM", "cli")
    if name not in BACKENDS:
        raise LLMError(f"unknown MATHFINDER_LLM={name!r}; use one of {sorted(BACKENDS)}")
    return BACKENDS[name](system, prompt)


def extract_json(text: str):
    """Pull a JSON value out of a model answer, with or without a ```json fence."""
    fenced = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    else:
        starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
        if not starts:
            raise LLMError("no JSON found in the answer")
        start = min(starts)
        end = text.rfind("}" if text[start] == "{" else "]")
        candidate = text[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        raise LLMError(f"invalid JSON: {e}")


def complete_json(system: str, prompt: str, retries: int = 1):
    """Like complete(), but returns parsed JSON. On bad JSON, asks again with the error."""
    answer = complete(system, prompt)
    for attempt in range(retries + 1):
        try:
            return extract_json(answer)
        except LLMError as e:
            if attempt == retries:
                raise
            answer = complete(
                system,
                f"{prompt}\n\nYour previous answer could not be parsed ({e}). "
                "Reply again with ONLY the JSON, inside a ```json block.",
            )
