# Product Boundary Contracts

These are **language-neutral schema sketches rendered in Python syntax** for readability. Production source-of-truth types for OpenClaw/AISIS packages are TypeScript (TypeBox/Zod/OpenClaw SDK types); domain services may generate/validate compatible schemas in their native stack.

They define AISIS-owned boundaries only. They do **not** redefine OpenClaw's provider, session, task, or plugin runtime APIs.

## Identity and resource grants

~~~python
from datetime import datetime
from typing import Annotated, Literal, Protocol
from uuid import UUID
from pydantic import BaseModel, Field

PrincipalId = UUID
ConversationId = UUID

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
~~~

## Surface-neutral product turn

OpenClaw channel plugins convert native channel events into OpenClaw messages first. AISIS uses the following product-level context only where a domain service needs cross-surface semantics.

~~~python
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

class SpeakerContext(BaseModel):
    kind: Literal["speaker"] = "speaker"
    device_id: UUID
    room: str | None = None
    speaker_label: str | None = None  # verification hint only, never an auth boundary
    wake_confidence: float | None = None
    has_screen: bool = False
    deadline_at: datetime

SurfaceContext = Annotated[
    AliceContext | TelegramContext | WebContext | ApiContext | EdgeContext | SpeakerContext,
    Field(discriminator="kind"),
]

class ProductTurn(BaseModel):
    id: UUID
    principal_id: PrincipalId
    conversation_id: ConversationId
    text: str
    locale: str = "ru"
    surface: SurfaceContext
    created_at: datetime
    trace_id: str
~~~

## Answers and rendering

~~~python
class AnswerAction(BaseModel):
    id: str
    title: str
    url: str | None = None
    payload: dict[str, object] | None = None

class RichDocument(BaseModel):
    summary: str
    body_markdown: str
    collapsible: bool = True

class ProductAnswer(BaseModel):
    display_text: str
    speech_text: str | None = None
    rich: RichDocument | None = None
    actions: list[AnswerAction] = Field(default_factory=list)

class RenderedNumber(BaseModel):
    canonical: str
    display_text: str
    speech_text: str
    approximate: bool
    fraction_numerator: int | None = None
    fraction_denominator: int | None = None
~~~

## Domain tools

OpenClaw owns the generic tool runtime. These contracts describe AISIS domain-service capabilities exposed *to* an OpenClaw tool plugin.

~~~python
Risk = Literal["read", "low_write", "external_write", "sensitive", "destructive"]
Latency = Literal["instant", "fast", "slow", "background"]

class DomainToolSpec(BaseModel):
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

class DomainToolFailure(BaseModel):
    code: str
    message: str
    retryable: bool

class DomainToolResult(BaseModel):
    ok: bool
    value: object | None = None
    failure: DomainToolFailure | None = None

class DomainToolProvider(Protocol):
    async def list_tools(self, principal_id: PrincipalId) -> list[DomainToolSpec]: ...
    async def call(
        self,
        principal_id: PrincipalId,
        tool_name: str,
        arguments: dict[str, object],
        idempotency_key: str | None = None,
    ) -> DomainToolResult: ...
~~~

## Routing extension

This is an AISIS policy result consumed by an OpenClaw adapter. It is **not** a second model-provider protocol.

~~~python
ModelAlias = Literal["fast", "balanced", "deep", "background"]
Effort = Literal["none", "low", "medium", "high", "xhigh", "max"]
ExecutionKind = Literal["deterministic", "model", "harness", "background"]

class RouteDecision(BaseModel):
    kind: ExecutionKind
    model_alias: ModelAlias | None = None
    harness: Literal["codex", "claude_code", "omp"] | None = None
    effort: Effort = "none"
    tool_groups: list[str] = Field(default_factory=list)
    context_budget_tokens: int
    time_budget_ms: int
    reason_code: str

class RouteDecisionProvider(Protocol):
    async def decide(self, turn: ProductTurn) -> RouteDecision: ...
~~~

## Runtime-task binding

OpenClaw owns background-task/Task Flow/Lobster execution state. AISIS stores only the cross-product binding required for identity, status lookup, and delivery.

~~~python
class RuntimeTaskBinding(BaseModel):
    id: UUID
    principal_id: PrincipalId
    conversation_id: ConversationId
    runtime: Literal["openclaw_task", "openclaw_automation", "lobster", "vibeflow"]
    runtime_task_id: str
    original_request: str
    originating_surface: SurfaceContext
    created_at: datetime
~~~

## Delivery targets

~~~python
class TelegramDelivery(BaseModel):
    kind: Literal["telegram"] = "telegram"
    chat_id: int

class AlicePendingDelivery(BaseModel):
    kind: Literal["alice_pending"] = "alice_pending"
    principal_id: PrincipalId

class StationTtsDelivery(BaseModel):
    kind: Literal["station_tts"] = "station_tts"
    principal_id: PrincipalId
    station_binding_id: UUID

class LocalSpeakerDelivery(BaseModel):
    kind: Literal["local_speaker"] = "local_speaker"
    device_id: UUID

DeliveryTarget = Annotated[
    TelegramDelivery | AlicePendingDelivery | StationTtsDelivery | LocalSpeakerDelivery,
    Field(discriminator="kind"),
]
~~~

## Telegram recipient resolution

~~~python
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

class TelegramPersonalConnector(Protocol):
    async def recent_dialogs(self, principal_id: PrincipalId, limit: int) -> list[object]: ...
    async def resolve_peer(self, principal_id: PrincipalId, query: str) -> RecipientResolution: ...
    async def recent_messages(
        self, principal_id: PrincipalId, peer_id: int, limit: int
    ) -> list[object]: ...
    async def send_message(
        self, principal_id: PrincipalId, peer_id: int, text: str
    ) -> object: ...
~~~

## Open Remote Commander harness extension

~~~python
HarnessName = Literal["codex", "claude_code", "omp"]
ExecutionId = str
WorkspaceId = str

class EdgeHarnessSpec(BaseModel):
    name: HarnessName
    version: str
    device_id: UUID
    supported_features: set[str]

class EdgeExecutionRequest(BaseModel):
    principal_id: PrincipalId
    device_id: UUID
    harness: HarnessName
    instruction: str
    workspace_id: WorkspaceId

class EdgeExecutionHandle(BaseModel):
    execution_id: ExecutionId
    principal_id: PrincipalId
    device_id: UUID
    harness: HarnessName
    workspace_id: WorkspaceId

class EdgeHarnessTransport(Protocol):
    async def discover(self, principal_id: PrincipalId) -> list[EdgeHarnessSpec]: ...
    async def start(self, request: EdgeExecutionRequest) -> EdgeExecutionHandle: ...
    async def cancel(
        self, principal_id: PrincipalId, execution_id: ExecutionId
    ) -> None: ...
~~~
