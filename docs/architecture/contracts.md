# Typed Architecture Contracts

These are design-level Python contracts, not yet product implementation. They define the interfaces the implementation plan must preserve.

## Identity and surfaces

```python
from datetime import datetime
from typing import Annotated, Literal, Protocol
from uuid import UUID
from pydantic import BaseModel, Field

Surface = Literal["alice", "telegram", "web", "api", "edge"]
PrincipalId = UUID
ConversationId = UUID
JobId = UUID

class ExternalIdentity(BaseModel):
    provider: Literal[
        "alice", "telegram_bot", "telegram_mtproto",
        "google", "email", "slack", "notion", "edge"
    ]
    subject: str
    principal_id: PrincipalId

Capability = Literal["read", "search", "create", "update", "delete", "send", "admin"]

class ResourceGrant(BaseModel):
    principal_id: PrincipalId
    resource_kind: str
    resource_id: str
    capabilities: set[Capability]

class AliceContext(BaseModel):
    kind: Literal["alice"] = "alice"
    session_id: str
    yandex_user_id: str | None
    application_id: str
    has_screen: bool
    deadline_at: datetime

class TelegramContext(BaseModel):
    kind: Literal["telegram"] = "telegram"
    chat_id: int
    user_id: int
    message_id: int | None
    thread_id: int | None = None
    is_group: bool = False

class WebContext(BaseModel):
    kind: Literal["web"] = "web"
    session_id: str

class ApiContext(BaseModel):
    kind: Literal["api"] = "api"
    client_id: str

class EdgeContext(BaseModel):
    kind: Literal["edge"] = "edge"
    device_id: UUID

SurfaceContext = Annotated[
    AliceContext | TelegramContext | WebContext | ApiContext | EdgeContext,
    Field(discriminator="kind"),
]

class Turn(BaseModel):
    id: UUID
    principal_id: PrincipalId
    conversation_id: ConversationId
    text: str
    locale: str = "ru"
    surface: SurfaceContext
    created_at: datetime
    trace_id: str
```

## Answers and rendering

```python
class AnswerAction(BaseModel):
    id: str
    title: str
    url: str | None = None
    payload: dict[str, object] | None = None

class RichDocument(BaseModel):
    summary: str
    body_markdown: str
    collapsible: bool = True

class Answer(BaseModel):
    display_text: str
    speech_text: str | None = None
    rich: RichDocument | None = None
    actions: list[AnswerAction] = Field(default_factory=list)
    end_conversation: bool = False

class RenderedNumber(BaseModel):
    canonical: str
    display_text: str
    speech_text: str
    approximate: bool
    fraction_numerator: int | None = None
    fraction_denominator: int | None = None
```

## Tools

```python
Risk = Literal["read", "low_write", "external_write", "sensitive", "destructive"]
Latency = Literal["instant", "fast", "slow", "background"]

class ToolSpec(BaseModel):
    name: str
    version: str
    title: str
    input_schema: dict[str, object]
    output_schema: dict[str, object]
    required_capabilities: set[str]
    risk: Risk
    latency: Latency
    idempotent: bool
    alice_sync_safe: bool

class ToolCall(BaseModel):
    id: UUID
    name: str
    arguments: dict[str, object]
    idempotency_key: str | None = None

class ToolFailure(BaseModel):
    code: str
    message: str
    retryable: bool

class ToolResult(BaseModel):
    call_id: UUID
    ok: bool
    value: object | None = None
    failure: ToolFailure | None = None

class ToolProvider(Protocol):
    async def list_tools(self, principal_id: PrincipalId) -> list[ToolSpec]: ...
    async def call(self, turn: Turn, call: ToolCall) -> ToolResult: ...
```

## Routing and models

```python
ModelAlias = Literal["fast", "balanced", "deep", "background"]
Effort = Literal["none", "low", "medium", "high", "xhigh", "max"]
ExecutionKind = Literal["deterministic", "model", "job"]

class RouteDecision(BaseModel):
    kind: ExecutionKind
    model_alias: ModelAlias | None = None
    effort: Effort = "none"
    tool_groups: list[str] = Field(default_factory=list)
    context_budget_tokens: int
    time_budget_ms: int
    reason_code: str

class RouteDecisionProvider(Protocol):
    async def decide(self, turn: Turn) -> RouteDecision: ...

class ModelRequest(BaseModel):
    route: RouteDecision
    messages: list[dict[str, object]]
    tools: list[ToolSpec]

class ModelProvider(Protocol):
    async def run(self, request: ModelRequest) -> Answer: ...
```

## Durable jobs

```python
JobStatus = Literal[
    "queued", "running", "waiting_user",
    "succeeded", "failed", "cancelled"
]

class JobProgress(BaseModel):
    at: datetime
    phase: str
    message: str
    percent: float | None = None

class PendingQuestion(BaseModel):
    prompt: str
    choices: list[str] = Field(default_factory=list)

class Job(BaseModel):
    id: JobId
    principal_id: PrincipalId
    conversation_id: ConversationId
    original_request: str
    status: JobStatus
    route: RouteDecision
    created_at: datetime
    updated_at: datetime
    progress: list[JobProgress] = Field(default_factory=list)
    pending_question: PendingQuestion | None = None
    result: Answer | None = None

class JobRunner(Protocol):
    async def start(self, turn: Turn, route: RouteDecision) -> Job: ...
    async def get(self, principal_id: PrincipalId, job_id: JobId) -> Job: ...
    async def cancel(self, principal_id: PrincipalId, job_id: JobId) -> Job: ...
```

## Delivery and connectors

```python
class TelegramDelivery(BaseModel):
    kind: Literal["telegram"] = "telegram"
    chat_id: int

class AliceInboxDelivery(BaseModel):
    kind: Literal["alice_inbox"] = "alice_inbox"
    principal_id: PrincipalId

class StationTtsDelivery(BaseModel):
    kind: Literal["station_tts"] = "station_tts"
    edge_device_id: UUID
    entity_id: str

DeliveryTarget = Annotated[
    TelegramDelivery | AliceInboxDelivery | StationTtsDelivery,
    Field(discriminator="kind"),
]

class DeliveryAdapter(Protocol):
    async def deliver(self, target: DeliveryTarget, answer: Answer) -> str: ...

class ConnectorCapability(BaseModel):
    name: str
    mode: Literal["read", "write"]
    risk: Risk

class PersonalConnector(Protocol):
    async def capabilities(self, principal_id: PrincipalId) -> list[ConnectorCapability]: ...
    async def search(self, principal_id: PrincipalId, query: str, limit: int) -> list[object]: ...
```

## Telegram recipient resolution

```python
class PeerCandidate(BaseModel):
    peer_id: int
    display_name: str
    username: str | None
    score: float
    reasons: list[str]

class RecipientResolution(BaseModel):
    query: str
    candidates: list[PeerCandidate]
    selected_peer_id: int | None
    requires_confirmation: bool

class TelegramPersonalConnector(PersonalConnector, Protocol):
    async def recent_dialogs(self, principal_id: PrincipalId, limit: int) -> list[object]: ...
    async def resolve_peer(self, principal_id: PrincipalId, query: str) -> RecipientResolution: ...
    async def recent_messages(self, principal_id: PrincipalId, peer_id: int, limit: int) -> list[object]: ...
    async def send_message(self, principal_id: PrincipalId, peer_id: int, text: str) -> object: ...
```

## Local edge executors

```python
class EdgeExecutorSpec(BaseModel):
    name: Literal["codex", "claude_code", "omp"]
    version: str
    device_id: UUID
    capabilities: set[str]

class EdgeExecutionRequest(BaseModel):
    job_id: JobId
    executor: str
    instruction: str
    workspace_ref: str | None = None

class EdgeExecutor(Protocol):
    async def discover(self) -> list[EdgeExecutorSpec]: ...
    async def start(self, request: EdgeExecutionRequest) -> str: ...
    async def cancel(self, execution_id: str) -> None: ...
```
