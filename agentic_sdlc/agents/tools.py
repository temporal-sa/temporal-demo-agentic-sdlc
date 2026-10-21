"""The tools coding agents use, backed by a git branch.

Each tool is a harness activity tool: the approval policy runs in the agent's Workflow and the
body runs as an Activity on any worker. The branch is the durable workspace. Every write is
committed and pushed before the tool returns, and every tool first brings the local checkout
up to the branch head, so a replacement worker continues exactly where a dead one stopped.
The local checkout is only a cache.

``workspace`` is injected per run from the agent's deps (harness_ext.workspace_toolset); the
model never sees it and can't point a tool at another branch.
"""


import asyncio
import base64
import contextlib
import functools
import hashlib
import os
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path

from temporalio.common import RetryPolicy
from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.agent import Injected

from agentic_sdlc import config
from agentic_sdlc.agents.harness_ext import heartbeating
from agentic_sdlc.models import Workspace

MAX_TOOL_OUTPUT = 30_000
TEST_TIMEOUT_SECONDS = 90
COMMIT_AUTHOR = "Orbit coding agent <agentic-sdlc-agent@users.noreply.github.com>"

# Short, bounded retries: a git push or a clone can hit a transient network error, but a
# tool that keeps failing should come back to the model as an error it can react to.
# A background heartbeat (see _heartbeats) lets a 10 second heartbeat timeout notice a dead
# worker, so an interrupted tool call is retried elsewhere within seconds.
TOOL_ACTIVITY = ActivityConfig(
    start_to_close_timeout=timedelta(minutes=3),
    heartbeat_timeout=timedelta(seconds=10),
    retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=3),
)
TEST_ACTIVITY = ActivityConfig(
    start_to_close_timeout=timedelta(seconds=TEST_TIMEOUT_SECONDS + 60),
    heartbeat_timeout=timedelta(seconds=10),
    retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=2),
)


# -- Tools ---------------------------------------------------------------------------------


def _heartbeats(fn):
    """Heartbeat every two seconds while a tool body runs, holding the branch's lock: a model
    can issue several tool calls at once, and they must not interleave git commands."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        workspace = kwargs.get("workspace", args[0] if args else None)
        async with heartbeating(), workspace_lock(workspace):
            return await fn(*args, **kwargs)

    return wrapper


@agent.activity_tool_defn(inherently_safe=True, activity_config=TOOL_ACTIVITY)
@_heartbeats
async def list_files(workspace: Injected[Workspace]) -> str:
    """List every file in the repository, one path per line."""
    root = await checkout(workspace)
    out = await _git(root, "ls-files")
    return _cap(out)


@agent.activity_tool_defn(inherently_safe=True, activity_config=TOOL_ACTIVITY)
@_heartbeats
async def read_file(workspace: Injected[Workspace], path: str) -> str:
    """Read a file. `path` is relative to the repository root, e.g. "src/cart.js"."""
    root = await checkout(workspace)
    target = _resolve(root, path)
    if not target.is_file():
        return f"No such file: {path}"
    return _cap(target.read_text(errors="replace"))


@agent.activity_tool_defn(inherently_safe=True, activity_config=TOOL_ACTIVITY)
@_heartbeats
async def search_code(workspace: Injected[Workspace], pattern: str) -> str:
    """Search tracked files for a regular expression (git grep). Returns `path:line:text` matches."""
    root = await checkout(workspace)
    proc = await asyncio.create_subprocess_exec(
        "git", "grep", "-n", "-E", pattern, cwd=root,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return _cap(out.decode(errors="replace")) or "No matches."


@agent.activity_tool_defn(activity_config=TOOL_ACTIVITY)
@_heartbeats
async def write_file(workspace: Injected[Workspace], path: str, content: str) -> str:
    """Create or overwrite a file with `content`. The change is committed and pushed to your branch."""
    root = await checkout(workspace)
    target = _resolve(root, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return await _commit_and_push(root, workspace, path, f"Write {path}")


@agent.activity_tool_defn(activity_config=TOOL_ACTIVITY)
@_heartbeats
async def edit_file(workspace: Injected[Workspace], path: str, old_text: str, new_text: str) -> str:
    """Replace the one occurrence of `old_text` in a file with `new_text`. `old_text` must match
    exactly once, including whitespace. The change is committed and pushed to your branch."""
    root = await checkout(workspace)
    target = _resolve(root, path)
    if not target.is_file():
        return f"No such file: {path}"
    text = target.read_text()
    count = text.count(old_text)
    if count == 0 and new_text in text:
        # A retried Activity whose first attempt already applied and pushed the edit.
        return f"{path} already contains the new text; nothing to change."
    if count != 1:
        return f"old_text matched {count} times in {path}; it must match exactly once."
    target.write_text(text.replace(old_text, new_text, 1))
    return await _commit_and_push(root, workspace, path, f"Edit {path}")


@agent.activity_tool_defn(inherently_safe=True, activity_config=TEST_ACTIVITY)
@_heartbeats
async def run_tests(workspace: Injected[Workspace]) -> str:
    """Run the test suite (`node --test tests/`) and return the result."""
    root = await checkout(workspace)
    code, output = await run_sandboxed(root, ["node", "--test", "tests/"])
    status = "PASSED" if code == 0 else f"FAILED (exit {code})"
    return f"Tests {status}\n\n{output}"


@agent.activity_tool_defn(activity_config=TEST_ACTIVITY)
@_heartbeats
async def run_command(workspace: Injected[Workspace], command: str) -> str:
    """Run a shell command in the repository, e.g. `node --check src/app.js`. No network
    access is intended and no credentials are available. Needs approval in supervised mode."""
    root = await checkout(workspace)
    code, output = await run_sandboxed(root, ["/bin/sh", "-c", command])
    return f"exit {code}\n\n{output}"


CODER_TOOLS = [list_files, read_file, search_code, write_file, edit_file, run_tests, run_command]
READ_ONLY_TOOLS = [list_files, read_file, search_code, run_tests]
ALL_TOOLS = CODER_TOOLS


# -- Checkout cache ------------------------------------------------------------------------

_locks: dict[str, asyncio.Lock] = {}


def _checkout_dir(workspace: Workspace) -> Path:
    return config.WORKDIR_ROOT / hashlib.sha256(f"{workspace.repo}@{workspace.branch}".encode()).hexdigest()[:16]


@contextlib.asynccontextmanager
async def workspace_lock(workspace: Workspace | None):
    """One git operation at a time per branch checkout on this worker."""
    if workspace is None:
        yield
        return
    async with _locks.setdefault(str(_checkout_dir(workspace)), asyncio.Lock()):
        yield


async def checkout(workspace: Workspace) -> Path:
    """Bring this worker's cached checkout of the branch to the branch head, cloning if needed.
    Callers hold workspace_lock."""
    root = _checkout_dir(workspace)
    if not (root / ".git").exists():
        shutil.rmtree(root, ignore_errors=True)
        root.parent.mkdir(parents=True, exist_ok=True)
        await _git(root.parent, "clone", "--quiet", "--branch", workspace.branch, "--single-branch",
                   remote_url(workspace.repo), str(root))
    else:
        await _git(root, "fetch", "--quiet", "origin", workspace.branch)
        await _git(root, "reset", "--quiet", "--hard", f"origin/{workspace.branch}")
        await _git(root, "clean", "-fdq")
    return root


async def _commit_and_push(root: Path, workspace: Workspace, path: str, message: str) -> str:
    await _git(root, "add", "--", path)
    status = await _git(root, "status", "--porcelain")
    if not status.strip():
        return f"{path} unchanged; nothing to commit."
    await _git(root, "-c", f"user.name={COMMIT_AUTHOR.split(' <')[0]}",
               "-c", f"user.email={COMMIT_AUTHOR.split('<')[1].rstrip('>')}",
               "commit", "--quiet", "-m", message)
    await _git(root, "push", "--quiet", "origin", f"HEAD:{workspace.branch}")
    sha = (await _git(root, "rev-parse", "--short", "HEAD")).strip()
    return f"Saved {path} (commit {sha} on {workspace.branch})."


def remote_url(repo: str) -> str:
    """The git remote for a repo. Tests point GIT_REMOTE_TEMPLATE at local bare repos."""
    if template := os.environ.get("GIT_REMOTE_TEMPLATE"):
        return template.format(repo=repo)
    return f"https://github.com/{repo}.git"


async def _git(cwd: Path, *args: str) -> str:
    """Run git with the token supplied as a header per command, so it is never written into
    the checkout's .git/config or a remote URL."""
    auth: list[str] = []
    if config.GITHUB_TOKEN and not os.environ.get("GIT_REMOTE_TEMPLATE"):
        basic = base64.b64encode(f"x-access-token:{config.GITHUB_TOKEN}".encode()).decode()
        auth = ["-c", f"http.https://github.com/.extraheader=Authorization: Basic {basic}"]
    proc = await asyncio.create_subprocess_exec(
        "git", *auth, *args, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {err.decode(errors='replace').strip()[:500]}")
    return out.decode(errors="replace")


# -- Running agent-written code ------------------------------------------------------------


async def run_sandboxed(root: Path, argv: list[str]) -> tuple[int, str]:
    """Run a command over agent-written code with no credentials in its environment.

    This is a demo-grade boundary: a clean environment (no API keys, no GitHub token), a
    throwaway HOME, the checkout as the working directory and a timeout. A production system
    would run it in an isolated sandbox (a container or microVM with no network).
    """
    home = tempfile.mkdtemp(prefix="agent-home-")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home, "NODE_OPTIONS": "", "CI": "1"}
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=root, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=TEST_TIMEOUT_SECONDS)
        except TimeoutError:
            proc.kill()
            return 124, f"Timed out after {TEST_TIMEOUT_SECONDS}s."
        return proc.returncode or 0, _cap(out.decode(errors="replace"), tail=True)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _resolve(root: Path, path: str) -> Path:
    target = (root / path).resolve()
    if root.resolve() not in target.parents or ".git" in target.relative_to(root.resolve()).parts:
        raise ValueError(f"{path} is outside the repository")
    return target


def _cap(text: str, *, tail: bool = False) -> str:
    if len(text) <= MAX_TOOL_OUTPUT:
        return text
    note = f"\n…[{len(text) - MAX_TOOL_OUTPUT} characters cut]…\n"
    return note + text[-MAX_TOOL_OUTPUT:] if tail else text[:MAX_TOOL_OUTPUT] + note
