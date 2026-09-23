# Central Agent Runtime: OpenClaw vs Hermes

## Decision

Use OpenClaw as the primary AISIS central runtime for V1.

Do not fork OpenClaw unless plugin/public extension points prove insufficient. Prefer normal OpenClaw packages/plugins and pin supported OpenClaw versions in AISIS compatibility tests.

Hermes remains a reference and possible future alternative runtime for selected deployments.

## Why OpenClaw

OpenClaw already provides the capabilities AISIS otherwise needs to build:
- persistent cross-channel main session;
- plugin SDK for custom chat channels such as Alice;
- broad provider support plus Codex and Claude CLI/harness runtimes;
- built-in memory and cross-conversation recall;
- Skill Workshop and autonomous/proposed self-learning;
- scheduled automations and proactive heartbeat;
- background tasks and managed Task Flow;
- Lobster typed workflows with approval/resume;
- tool policy/permissions and plugins;
- official voice-call plugin;
- desktop/node architecture.

This lets AISIS focus on personal-resource integration and UX instead of another generic agent framework.

## Hermes strengths

Hermes is valuable and should be tracked because it has:
- Python/uv implementation and library use;
- explicit self-improving learning loop and skill creation;
- broad messaging gateway;
- built-in Home Assistant REST tools plus WebSocket state-change gateway;
- cron jobs with skill attachment and webhook triggers;
- subscription/OAuth provider options including Codex/Claude;
- strong code-execution/delegation primitives.

Hermes is especially useful as a reference for HA event filtering and learn-from-experience behavior.

## Why not Hermes as V1 core

Hermes currently has cron/skills/webhooks but no equivalent visual workflow authoring surface, and OpenClaw's current Task Flow + Lobster + plugin/channel SDK is a better fit for AISIS's durable cross-surface orchestration and VibeFlow integration.

This is not a quality judgment about the agents' model behavior; it is an architectural fit decision.

## Compatibility strategy

Domain APIs (calendar, finance, calculator, HA, calls, personal connectors) expose explicit JSON-schema-compatible contracts independent of OpenClaw internals.

AISIS-specific central UX code may use OpenClaw native capabilities directly where doing so avoids duplicating robust infrastructure.

A future Hermes adapter should wrap those domain APIs, not force every OpenClaw feature behind an artificial universal runtime interface.
