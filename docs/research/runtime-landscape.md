# Central Runtime / Workflow Landscape — September 2026

## OpenClaw

Useful V1 capabilities:
- custom channel plugin SDK;
- one main personal session across channels;
- memory and active cross-conversation recall;
- Skill Workshop and self-learning modes;
- automations, heartbeat, hooks, background tasks, standing orders;
- managed Task Flow;
- Lobster typed/resumable workflow runtime with approval checkpoints;
- provider plugins plus Codex and Claude CLI/harness support;
- official voice-call plugin;
- broad plugin ecosystem.

References:
- https://docs.openclaw.ai/plugins/sdk-channel-plugins
- https://docs.openclaw.ai/automation
- https://docs.openclaw.ai/tools/self-learning
- https://docs.openclaw.ai/tools/lobster
- https://docs.openclaw.ai/plugins/voice-call
- https://docs.openclaw.ai/gateway/config-agents/runtime-and-cli-backends

## Hermes Agent

Useful capabilities:
- self-improving skills/learning loop;
- Python/uv codebase and library mode;
- broad messaging gateway;
- natural-language cron jobs with skill attachment;
- Home Assistant REST tools and filtered real-time WebSocket events;
- Codex/Claude subscription/provider integrations;
- MCP and delegation.

References:
- https://hermes-agent.nousresearch.com/docs/
- https://hermes-agent.nousresearch.com/docs/user-guide/features/cron
- https://hermes-agent.nousresearch.com/docs/user-guide/messaging/homeassistant

## No-code conclusion

Neither project currently exposes a mature n8n-style visual graph authoring environment.

OpenClaw's Lobster is a constrained typed DSL/runtime, not a visual editor. Its Task Flow/Automations are execution/orchestration primitives.

Hermes automations are primarily natural-language/CLI cron + skills/webhook configuration.

VibeFlow therefore fills a real product gap rather than duplicating either central runtime.
