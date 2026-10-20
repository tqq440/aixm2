"""固定本机 ASR 实例管理契约。"""

from datetime import datetime
from typing import Literal

from pydantic import Field, field_serializer

from backend.schema.common.api_schema import ApiSchema
from backend.schema.models.model_schema import RegistryListQuery
from backend.schema.nodes.node_schema import RegistryId
from backend.utils.time.time_utils import as_utc

AgentState = Literal['stopped', 'starting', 'ready', 'busy', 'stopping', 'stop_unconfirmed', 'failed', 'unknown']


class ManagementCommand(ApiSchema):
    agent_session_id: RegistryId
    expected_revision: int = Field(ge=0, strict=True)


class ManagementStatus(ApiSchema):
    instance_id: RegistryId
    node_id: RegistryId
    management_mode: Literal['node_agent'] = 'node_agent'
    state: AgentState
    reported_state: str | None = None
    desired_state: Literal['running', 'stopped'] = 'stopped'
    agent_available: bool = False
    agent_session_id: RegistryId | None = None
    agent_pid: int | None = None
    worker_session_id: RegistryId | None = None
    child_pid: int | None = None
    command_revision: int = 0
    model_ready: bool = False
    accepting_tasks: bool = False
    active_task_id: str | None = None
    last_error: str | None = None
    heartbeat_at: datetime | None = None
    exit_confirmed_at: datetime | None = None
    shutdown_confirmed_at: datetime | None = None
    evaluated_at: datetime

    @field_serializer('heartbeat_at', 'exit_confirmed_at', 'shutdown_confirmed_at', 'evaluated_at')
    def serialize_time(self, value):
        return as_utc(value).isoformat() if value else None


class ManagedSessionView(ApiSchema):
    worker_session_id: RegistryId
    instance_id: RegistryId
    agent_session_id: RegistryId
    agent_pid: int
    child_pid: int | None
    state: AgentState
    started_at: datetime
    ended_at: datetime | None
    exit_confirmed_at: datetime | None
    last_error: str | None

    @field_serializer('started_at', 'ended_at', 'exit_confirmed_at')
    def serialize_time(self, value):
        return as_utc(value).isoformat() if value else None


class ManagedSessionPage(RegistryListQuery):
    items: list[ManagedSessionView]
    total: int
