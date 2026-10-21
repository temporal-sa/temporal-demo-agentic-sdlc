"""Checks the pipeline runs itself rather than taking an agent's word for them."""

from __future__ import annotations

from pydantic import BaseModel
from temporalio import activity

from agentic_sdlc.agents import tools
from agentic_sdlc.models import Workspace


class TestRun(BaseModel):
    passed: bool
    output: str


class CheckActivities:
    def __init__(self, _client=None) -> None:
        pass

    def activities(self) -> list:
        return [self.run_branch_tests]

    @activity.defn
    async def run_branch_tests(self, workspace: Workspace) -> TestRun:
        """Run the test suite on what the agent actually pushed to its branch."""
        async with tools.workspace_lock(workspace):
            root = await tools.checkout(workspace)
            code, output = await tools.run_sandboxed(root, ["node", "--test", "tests/"])
        return TestRun(passed=code == 0, output=output)
