# ComfyUI WebSocket Behaviour

This note summarizes how ComfyUI's backend (as of upstream `master`) uses WebSockets to coordinate with the browser frontend. Source excerpts are vendored under `third_party/comfyui/` for reference.

## Connection lifecycle

* The `PromptServer` exposes a `/ws` endpoint backed by `aiohttp`'s `WebSocketResponse`. Each incoming connection either resumes an existing session supplied via the `clientId` query parameter or is assigned a freshly generated UUID. Session sockets and per-connection metadata are tracked in dictionaries so that reconnects replace stale sockets and their metadata.【F:third_party/comfyui/server.py†L188-L245】
* As soon as the socket is ready, the server pushes a `status` payload containing queue information and the negotiated session identifier. If the reconnecting client is currently executing a workflow, the last executing node ID is replayed so the UI can resynchronise.【F:third_party/comfyui/server.py†L204-L210】
* The first textual frame sent by the client is expected to be a `feature_flags` advertisement. The server caches the client's capabilities and responds with its own supported feature flags (e.g., preview metadata support and negotiated upload limits). This handshake is optional for older frontends; any JSON decoding issues are logged but do not tear down the socket.【F:third_party/comfyui/server.py†L211-L242】【F:third_party/comfyui/comfy_api/feature_flags.py†L12-L69】
* When the socket closes, its session entry and metadata are purged to avoid leaking stale references.【F:third_party/comfyui/server.py†L243-L245】

## Message dispatch pipeline

* `PromptServer.send` normalises outbound events. Binary preview payloads are rerouted to dedicated helpers (`send_image`, `send_image_with_metadata`), byte-like payloads are framed with a 4-byte event code, and everything else is serialised as JSON of shape `{ "type": <event>, "data": <payload> }`. All WebSocket writes go through the async-safe `send_socket_catch_exception` wrapper to log transient network errors.【F:third_party/comfyui/server.py†L844-L944】
* Binary event identifiers are defined in `protocol.BinaryEventTypes`. They cover preview images (encoded or raw), rich preview metadata, and textual streaming output (used for console/progress text). The server prepends the event code as a big-endian 32-bit integer before writing binary frames.【F:third_party/comfyui/protocol.py†L2-L6】【F:third_party/comfyui/server.py†L856-L933】
* Long-lived workers (prompt execution, model management, etc.) interact with WebSockets via the thread-safe `send_sync` helper. It enqueues `(event, data, sid)` tuples into an asyncio `Queue`, and the background `publish_loop` coroutine pulls from that queue and delegates to the async `send` routine. `queue_updated` is a small convenience wrapper that schedules a fresh `status` broadcast whenever the prompt queue changes.【F:third_party/comfyui/server.py†L946-L956】【F:third_party/comfyui/server.py†L950-L956】
* `main.run` starts the HTTP listener and the `publish_loop` concurrently, ensuring the dispatcher is always running next to the web server.【F:third_party/comfyui/main.py†L239-L245】

## Events emitted during prompt execution

Prompt execution relies on the `execution` module, which pushes higher-level events to the WebSocket channel.

* When cached node outputs are reused, the executor immediately emits `executed` notifications so the UI can mark nodes as done without recomputation.【F:third_party/comfyui/execution.py†L400-L408】
* As each node begins work, `executing` is sent (and `last_node_id` updated) so the frontend can highlight the active node. Errors trigger `execution_error` messages summarising the failure. Successful completions send `executed` with node UI data when available.【F:third_party/comfyui/execution.py†L448-L518】
* The higher-level `PromptExecutor.add_message` helper timestamps status events and streams them to the originating client (if any) or broadcasts them when required (e.g., interrupt notifications). It handles `execution_start`, `execution_cached`, `execution_interrupted`, and `execution_error` events.【F:third_party/comfyui/execution.py†L600-L646】【F:third_party/comfyui/execution.py†L659-L679】
* The executor records the initiating `client_id` from the REST `/prompt` submission in `extra_data`, allowing all downstream WebSocket notifications to be scoped to the correct session. When no client ID is provided, broadcasts are suppressed so headless runs do not emit UI-only events.【F:third_party/comfyui/server.py†L703-L704】【F:third_party/comfyui/execution.py†L654-L658】
* Once a prompt finishes (success or error), `PromptQueue.task_done` trims queue state and schedules a `status` update via `queue_updated`. The queue also triggers updates whenever items are enqueued, dequeued, or mutated, keeping the front-end's queue and history panes in sync.【F:third_party/comfyui/execution.py†L1077-L1171】

## Progress and preview streaming

ComfyUI streams fine-grained progress and preview imagery through the WebSocket using both JSON and binary events.

* `main.hijack_progress` registers a global hook that converts backend progress callbacks into WebSocket messages. Scalar progress updates become `progress` events, while preview images fall back to legacy `UNENCODED_PREVIEW_IMAGE` frames when the client has not negotiated support for richer metadata.【F:third_party/comfyui/main.py†L247-L276】
* The newer `WebUIProgressHandler` feeds progress state through the same socket. It emits `progress_state` events summarising per-node execution state and uses the `PREVIEW_IMAGE_WITH_METADATA` binary channel to send preview image bytes prefixed with JSON metadata (node IDs, prompt IDs, etc.) whenever the client advertises `supports_preview_metadata`.【F:third_party/comfyui/comfy_execution/progress.py†L150-L236】【F:third_party/comfyui/comfy_execution/progress.py†L220-L230】
* Textual streaming (e.g., console output associated with a node) is packaged via `PromptServer.send_progress_text`, which wraps the node identifier and UTF-8 text into a binary payload tagged with the `TEXT` event ID.【F:third_party/comfyui/server.py†L1008-L1018】

## Inbound messages

At present the backend primarily listens for the initial `feature_flags` message; there are no other structured inbound WebSocket commands. Workflow submission, queue management, and interruption all happen through HTTP endpoints (`/prompt`, `/queue`, `/interrupt`, etc.), and those routes stash any supplied `client_id` so that subsequent WebSocket events target the right browser tab.【F:third_party/comfyui/server.py†L674-L738】

## Key takeaways

* WebSockets are used strictly for server-initiated push updates; all mutating commands still go through REST endpoints.
* A thin message queue bridges synchronous execution code and the asyncio-based WebSocket transport, preventing blocking writes.
* Capability negotiation via feature flags enables gradual rollout of richer binary channels (preview metadata, larger uploads) while preserving compatibility with legacy clients.
* Execution progress is multi-faceted: JSON messages cover structural updates (`status`, `progress_state`, `executing`, etc.), while binary frames carry preview pixels and streaming text efficiently.

