"""Activities, grouped by the system they talk to. Each class takes the worker's client."""

from agentic_sdlc.activities.agent_turn import AgentTurnActivities
from agentic_sdlc.activities.checks import CheckActivities
from agentic_sdlc.activities.deploy import DeployActivities
from agentic_sdlc.activities.github import GitHubActivities

ACTIVITY_CLASSES = [GitHubActivities, AgentTurnActivities, DeployActivities, CheckActivities]
