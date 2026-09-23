# Visual Workflows: VibeFlow + OpenClaw

## Goal

Make VibeFlow the no-code/low-code visual programming surface for AISIS while reusing OpenClaw's durable automation and workflow execution primitives.

## Existing VibeFlow assets

VibeFlow already has:
- a visual node/edge editor;
- n8n-compatible workflow execution;
- WebSocket collaboration/status;
- pause/resume via Wait nodes;
- HTTP/Telegram/control-flow/AI nodes;
- external n8n node loading;
- secrets service;
- execution persistence and streaming.

AISIS should extend this codebase rather than introducing n8n as a dependency.

## Workflow IR

Define a versioned, typed AISIS Workflow IR containing:
- workflow metadata/version;
- typed input/output schemas;
- nodes and typed ports;
- edges/control conditions;
- trigger definition;
- approval gates;
- wait/resume points;
- error/retry policy;
- resource grants;
- execution target hints;
- model/effort hints for LLM nodes.

VibeFlow's n8n-compatible representation can be imported/exported through adapters rather than being the canonical AISIS contract.

## Targets

### Lobster

Use for deterministic sequences, JSON-first tool pipelines, conditions, explicit approval gates, and resumable workflows.

### OpenClaw Task Flow / Automations

Use for durable detached/background work, schedules, external events, delivery, and managed task state.

### VibeFlow native

Use when the visual graph relies on n8n-compatible nodes, graph semantics, or custom execution that cannot be safely lowered to Lobster.

## Compiler UX

The editor shows a target compatibility badge per workflow.

Compilation errors are structural and actionable, e.g. “this graph contains a loop not supported by Lobster; run with VibeFlow native or rewrite as a Task Flow.”

## AI-assisted authoring

The central assistant can create/edit Workflow IR through typed tools, so a user can describe a workflow in natural language and then inspect/edit it visually in VibeFlow.

AI never writes opaque executable blobs when the same workflow can be represented as typed nodes/edges.
