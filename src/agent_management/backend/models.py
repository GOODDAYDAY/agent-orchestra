"""Domain models — dataclasses and enums used across all modules."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class AgentRole(str, Enum):
    product_manager = "product_manager"
    tech_director = "tech_director"
    developer = "developer"
    tester = "tester"
    user = "user"
    orchestrator = "orchestrator"   # REQ-012 v2: drives the workflow
    custom = "custom"


class AgentStatus(str, Enum):
    not_started = "not_started"
    starting = "starting"
    active = "active"
    paused = "paused"
    stopping = "stopping"
    stopped = "stopped"
    degraded = "degraded"


def _new_id() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Agent:
    name: str
    role: AgentRole
    working_dir: str
    id: str = field(default_factory=_new_id)
    system_prompt: str = ""
    system_prompt_file: str = ""
    paused: bool = False
    status: AgentStatus = AgentStatus.not_started
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


@dataclass
class Group:
    name: str
    id: str = field(default_factory=_new_id)
    workflow_id: str = "standard"   # REQ-012 v2 F-08
    created_at: str = field(default_factory=_now)


@dataclass
class GroupMember:
    group_id: str
    agent_id: str


@dataclass
class Session:
    agent_id: str
    group_id: str
    id: str = field(default_factory=_new_id)
    claude_session_id: str = field(default_factory=_new_id)  # pre-assigned UUID
    previous_session_id: str = ""
    tmux_session_name: str = ""
    tmux_pane_id: str = ""
    status: AgentStatus = AgentStatus.not_started
    started_at: str = ""
    stopped_at: str = ""


@dataclass
class RoleTemplate:
    """Editable default system prompt for an agent role."""
    role: AgentRole
    display_name: str
    system_prompt: str
