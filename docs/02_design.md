# Template Design Specification — CMN-C2-289 Appier Campaign Agent

## Position in the Framework

| Aspect | Value |
|--------|-------|
| Agent class | `AppierCampaignAgent` (`src/graph/graph.py`) |
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |
| Category | Cat 2 (multi-step domain workflow, tool-calling) |
| Composition | outer 5-node backbone; the domain pipeline is encapsulated in a `GraphNode` (`main` slot) wrapping an inner `BaseGraph` (`src/graph/domain_workflow_graph.py`) |
| Pattern | tool-calling — classify intent → extract campaign fields (budget / schedule / status) → build an Appier campaign-API request → call the tool → format the confirmation. No retrieval, no autonomous loop. |

**Three-layer separation**

- State: flat TypedDict `State(AgentState)` — no Pydantic (not msgpack-serializable, so it
  cannot be checkpointed).
- Node: template-method inheritance — `execute(self, state) -> dict` override only.
- Graph: composition — `register_nodes()` + `super().register_nodes()`; `add_edges()` is not
  overridden on the outer graph.

## Architecture Overview

### Outer graph — node configuration (`src/graph/graph.py`)

| Node | Responsibility | Input State | Output State | Trust | Inherits/Overrides |
|------|---------------|-------------|--------------|-------|-------------------|
| initialize | framework setup (schema, session, trust) | user_input | session/trust fields | framework default | InitializeNode (default) |
| pre_process | own the caller contract: screen the request text for injection markers, strip markup, cap length, validate every `input_context` field against the allowlist and its bounds, serialize the request into `validated_input` (JSON) and hand the validated campaign block to the state bridge | user_input, input_context | validated_input, campaign_hint, caller_campaign | **VERIFIED_EXTERNAL** (the single external gate) | PreProcessNode (FunctionNode) |
| main | run the inner Appier workflow subgraph | validated_input, caller_campaign | result, intent, campaign_id, record_id, record_ref, campaign_name, confirmation, appier_payload | GraphNode (caller context forwarded unchanged) | AppierWorkflowGraphNode (GraphNode) |
| post_process | shape caller-facing `formatted_output`; enforce the module-level output gate and CONTAIN a violation by clearing every output-bearing field | inner-result fields | formatted_output | ANONYMOUS | PostProcessNode (FunctionNode) |
| finalize | framework finalize (metadata, timing) | — | response_metadata | framework default | FinalizeNode (default) |

### Inner workflow — node configuration (`src/graph/domain_workflow_graph.py`)

The inner graph inherits `BaseGraph` (fully custom linear topology). The 5 pipeline steps map
1:1 to inner nodes. **Every inner domain node declares
`required_trust_level = TrustLevel.ANONYMOUS`** — the caller's `InvocationContext` is forwarded
into the subgraph unchanged, so the single external trust gate stays on the backbone
`pre_process`.

| Inner node | Step | Responsibility | Output | Trust |
|------|------|---------------|--------|-------|
| validate_input | 1 ValidateInput | empty/non-request guard; deterministic (regex) flag-and-redact of email/token-like strings before logging; resolves the **pre-action approval signal** `approval_granted` (True ONLY on an explicit `approve: yes` line / `[approved]` token in the text, or the `approved: true` envelope flag — never inferred) | validated_input, campaign_hint, redaction_flags, approval_granted | ANONYMOUS |
| classify_intent | 2 ClassifyIntent | deterministic keyword classification → lookup_campaign / update_campaign / set_status; low confidence → lookup_campaign (read-only default — never a write) | intent | ANONYMOUS |
| infer_appier_fields | 3 InferAppierFields | assemble the Appier campaign-API request body: the caller's validated campaign block first, then entities extracted from the request text (campaign id / quoted name / budget / ISO dates / `Key: value` settings). Every value that reaches the body passes the same bounds whichever source it came from. An unresolved campaign id is left empty (never invented); approval-gate control lines (`approve: …`) are never campaign settings | campaign_name, campaign_id, appier_payload | ANONYMOUS |
| call_appier_api | 4 CallAppierApi | GET /campaigns/{id} (lookup) / PATCH /campaigns/{id} (update) / PATCH /campaigns/{id}/status (status) via `AppierClient` with the configured deadline; key from the invocation context; 4xx/5xx → status=error. **Pre-action approval gate**: the write intents execute ONLY when `approval_granted is True`; without it NO client write method is called — the node returns SUCCESS with `approval_required=True` + a whitelisted `pending_action` preview (intent / campaign id / field NAMES or target status, never request-body values) | record_id, record_ref, campaign_id, campaign_name — or approval_required, pending_action | ANONYMOUS |
| confirm | 5 Confirm | two modes: (a) format intent + record id + reference into a human-readable completed-action confirmation; (b) when `approval_required` — render the request-for-approval message (pending action + exact how-to-approve instructions); no record evidence is demanded in mode (b) because no write happened | confirmation, result | ANONYMOUS |

### Data Flow

```
Outer:  START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
                                              | (RETRY, max_retry) ^
Inner (inside main / AppierWorkflowGraphNode):
        START -> validate_input -> classify_intent -> infer_appier_fields
              -> call_appier_api -> confirm -> END
```

The request text travels as a JSON string: `pre_process` serializes
`{"text", "campaign_hint", "approved"}` into `validated_input`,
`AppierWorkflowGraphNode.extract_input()` hands that JSON to the subgraph, and the first inner
node (`validate_input`) parses it back.

**The caller's campaign data does not travel that way.** The subgraph call carries one string
and nothing else, and the framework's input gate rewrites personal-name and identifier shapes in
`user_input` / `validated_input` before any node reads them — a campaign named "Summer Sale"
arrives as "[MASKED]", and the agent would write that marker to the advertising platform. The
validated campaign block therefore crosses the boundary through the state bridge
(`src/graph/context_bridge.py`): `extract_input()` stashes it as it hands over control and the
inner graph's `_extra_initial_state()` seeds it into the inner state. The take is destructive, so
one request's data can never be picked up by the next. A text-extracted value that arrives with
the mask marker in it is refused, with a message pointing the caller at the structured channel —
refusing beats writing "[MASKED]" to a live campaign.

**Pre-action approval gate (confirmation/approval BEFORE externally impactful writes).**
Approval is an explicit input signal resolved deterministically in a single invoke — no HITL
interrupt machinery (the main slot keeps `propagate_hitl = False`; PB-7 remains the skip stub):

1. `validate_input` sets `approval_granted=True` ONLY on an explicit `approve: yes` line /
   `[approved]` token in the request text, or when the request context carried
   `approved: true` (forwarded by `pre_process` in the envelope). Never inferred from intent
   wording.
2. `call_appier_api` refuses an unapproved write (update_campaign / set_status): no client write
   method is called; it returns SUCCESS with `approval_required=True` + the whitelisted
   `pending_action` preview. `lookup_campaign` is read-only and unaffected.
3. `confirm` renders the request-for-approval message (what is pending + how to approve); the
   caller re-sends the SAME request with the approval signal to execute it. The output gate
   accepts the pending response WITHOUT record evidence but blocks any response claiming both
   pending and completed.

### State Definition (`src/schemas/state.py`)

All domain fields are declared `NotRequired[...]` — fields are absent until their producer node
writes them. Dict/list payloads are stored as JSON strings (`Optional[str]`) via the module
helpers `to_json` / `from_json`, used by every producer and consumer, so the state stays
msgpack-serializable for checkpointing.

| Field | Type | Purpose | Producer |
|-------|------|---------|----------|
| campaign_hint | NotRequired[str] | caller-supplied campaign id; never inferred | pre_process / validate_input |
| campaign_id | NotRequired[str] | resolved Appier campaign id (v1: pass-through when the hint/text already carries an id) | infer_appier_fields |
| caller_campaign | NotRequired[Optional[str]] | JSON — the validated caller campaign-change block, carried across the graph boundary by the state bridge | pre_process |
| redaction_flags | NotRequired[Optional[str]] | JSON list of patterns redacted before logging | validate_input |
| approval_granted | NotRequired[bool] | pre-action approval signal — True ONLY on an explicit approval phrase/flag; never inferred | validate_input |
| approval_required | NotRequired[bool] | set when a write intent was refused pending approval (no client write happened) | call_appier_api |
| pending_action | NotRequired[Optional[str]] | JSON — whitelisted preview of the refused write (intent / campaign_id / field NAMES or target_status; never request-body values) | call_appier_api |
| campaign_name | NotRequired[str] | campaign display name / record label | infer_appier_fields / call_appier_api |
| appier_payload | NotRequired[Optional[str]] | JSON — assembled Appier campaign-API request body | infer_appier_fields |
| appier_config | NotRequired[Optional[str]] | JSON — the `appier:` section from `config/config.yaml`, forwarded by `_parent_config()` and injected via `_extra_initial_state()` | inner graph |
| runtime_config | NotRequired[Optional[str]] | JSON — the runtime knobs (`max_retry`, `timeout_s`) from `config/config.yaml`, same route | inner graph |
| record_id | NotRequired[str] | campaign id / record id returned by Appier | call_appier_api |
| record_ref | NotRequired[str] | human-readable record reference (`appier://campaigns/<id>`) | call_appier_api |
| confirmation | NotRequired[str] | human-readable confirmation | confirm |

`intent`, `result`, `validated_input`, `formatted_output` are inherited from `AgentState` and are
**not** re-declared.

**State constraints (mandatory, satisfied):**
- Flat TypedDict only (primitives + JSON-serializable) — no Pydantic/dataclass.
- No JWT / API keys / credentials in State — the Appier API key is accessed via `ctx.secrets`.
- `InvocationContext` read via `InvocationContext.from_state(state)`, never stored in State.

## Configuration (two files, and which one is read)

`config/agent.yaml` is the flat registry manifest: identity, entry point and declared
requirements, every key at ROOT level (no `agent:` block). `config/config.yaml` holds the runtime
parameters. The split matters because a value declared in one file and read from the other is
dead — silently, with a green suite.

| File | Key | Read by |
|------|-----|---------|
| `config/agent.yaml` | `id`, `name`, `namespace`, `version`, `enabled`, `category`, `generation_mode`, `industry`, `base_type`, `class`, `required_trust_level` | the registry, at discovery time |
| `config/agent.yaml` | `requires.secrets`, `requires.extras` | compile-time provisioning gates |
| `config/config.yaml` | `max_retry` | the framework backbone (validated at compile, used by the retry route) |
| `config/config.yaml` | `timeout_s` | `CallAppierApiNode` → `AppierClient` → the transport, as the per-request deadline |
| `config/config.yaml` | `appier.base_url` | `CallAppierApiNode`, to build the client |

`requires.secrets` is `[]` **by design**: the integration key is read with
`ctx.secrets.get("APPIER_API_KEY")`, not `require()`, because the default network-free transport
runs without it. Declaring it as required would make the agent fail to start wherever it is not
provisioned, for a call it does not make.

Nodes take **no constructor arguments**; configuration never rides on node instances.
`AppierWorkflowGraphNode._parent_config()` reads `config/config.yaml` and forwards the `appier`
section, the `llm` section if ever declared, and the runtime knobs to the inner graph under
`config["configurable"]` — never `{}`. The inner graph's `_extra_initial_state()` injects them
into State as JSON strings (`appier_config`, `runtime_config`), where the no-arg nodes read them
back. The standalone entry point loads the same file and passes it to the agent constructor, so
the framework sees the declared `max_retry`.

## Caller Contract (`POST /invoke`)

Two channels, treated differently:

- `input` — free natural-language text. Screened for injection markers, stripped of markup,
  length-capped.
- `input_context` — structured campaign data. A strict allowlist; every field validated against
  explicit bounds. Unrecognised fields are refused by count and position, never by echoing the
  caller's key names.

| Context field | Contract |
|---------------|----------|
| `campaign_id` / `campaign_hint` / `campaign_code` | 1–20 characters of letters, digits, `-` or `_`. Priority in that order; a malformed value is refused rather than skipped |
| `approved` | exact JSON boolean; a truthy string is refused |
| `campaign.name` | render alphabet, ≤100 chars |
| `campaign.daily_budget` | finite number in (0, 1e9]; NaN / ±Infinity / bools / out-of-range refused |
| `campaign.start_date` / `campaign.end_date` | ISO `YYYY-MM-DD` calendar dates; end must not precede start |
| `campaign.status` | `active` or `paused` |
| `campaign.settings` | ≤20 entries of `{name, value}`; name is a label, value is render alphabet ≤200 chars |

The **render alphabet** is word characters (Unicode — Japanese campaign names are ordinary here),
spaces, and `- . / & ( ) ' , + #`. It excludes `< > | [ ] { } @ : "` and every control character
by construction, so markup, chat-template control tokens and mail addresses cannot enter the
channel at all rather than being redacted after the fact.

An adapter-level cap refuses an oversized context envelope before anything else runs.

## Security Design

- **Trust gate** — the single external trust gate is on the outer backbone
  `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; every inner domain node —
  **including the write-capable `CallAppierApiNode`** — declares `TrustLevel.ANONYMOUS`.
  `GraphNode.execute()` forwards the caller's `InvocationContext` into the subgraph **unchanged**
  (no elevation), and `VERIFIED_EXTERNAL < INTERNAL`, so declaring an inner node `INTERNAL` would
  deny a legitimate external caller before the call runs — the boundary is enforced exactly once,
  at `pre_process`. Agent-level default trust `VERIFIED_EXTERNAL` is declared in
  `config/agent.yaml`. `src/api/server.py` enforces the standalone entry-point Bearer-token auth
  boundary (`INVOKE_AUTH_TOKEN` → VERIFIED_EXTERNAL elevation).
- **Injection screening** — `PreProcessNode` screens the request text on BOTH representations:
  raw, where chat-template control tokens (`<|…|>`, `[INST]`, `<<SYS>>`) are still visible, and
  post-sanitize, where a directive spliced with markup has been re-assembled into a matchable
  phrase. Neither pass alone sees both, and the markup strip on its own is not a defence: it
  deletes the token and forwards the directive as ordinary text. The parsed context is screened
  depth-first including KEYS, after JSON decoding, so escaped payloads cannot slip past. Findings
  report the marker CLASS, never the matched text. The patterns are anchored on their own
  structure so ordinary campaign wording ("ignore duplicates", "system settings", "override the
  daily cap") is unaffected — a screen that blocks real work fails closed on the job.
- **Input flag-and-redact** — `ValidateInputNode.execute()` runs a deterministic (regex, not
  model-based) scan for email addresses and access-token-like strings (`eyJ…`, `secret_…`, `sk-…`)
  and redacts them before any logging. A campaign request legitimately names a campaign and its
  settings, so this is flag-and-redact for safe logging, not a hard reject; the framework's own
  input mask additionally rewrites emails/phones/names in `user_input`/`validated_input`. The only
  deterministic auto-reject is the empty/non-request guard.
- **Caller numbers are finite and bounded** — every caller-controlled number (the budget, and the
  deployment-supplied `timeout_s`) goes through `finite_in_range` in
  `src/services/validation.py`, which rejects bools, non-numerics, NaN, ±Infinity and
  out-of-range magnitudes and fails CLOSED with a field-naming error. NaN in particular parses
  cleanly and compares False against everything, so an unchecked amount would pass the very
  range check meant to stop it.
- **Credentials** — the integration key is read via `ctx.secrets.get("APPIER_API_KEY")`
  (`InvocationContext.from_state(state)`), never `os.environ`, never stored in State. A missing
  key is tolerated **only** while the deterministic network-free stub transport is active (no
  live call is made); with a live transport injected, a missing key is a hard `status=error`.
  The entry point additionally refuses a credential-shaped value in `input_context` with a 400
  naming the field: the framework copies the caller context into the first node's result and
  scans every value of every result, so such a request fails inside the first node with a stack
  trace the caller cannot act on. The check calls the same detector the framework's gate calls,
  so the refused set matches the blocked set exactly.
- **Output gate** — the domain output gate is the **module-level** `_security_gate_output()` in
  `src/nodes/post_process_node.py`, called from `PostProcessNode.execute()` (the framework gate
  methods are final and the extension hooks are auto-wrapped, so domain checks live in a
  module-level helper invoked inline). It blocks any SUCCESS response that lacks record evidence
  (record_id/record_ref) and any credential-shaped string anywhere in `formatted_output`,
  including inside the nested payload and pending-action preview. The ONE exception is an
  explicit approval-required response (`approval_required=True` — the pre-action gate refused the
  write, so no record exists BY DESIGN): it must carry the request-for-approval confirmation and
  must NOT carry record evidence (pending and completed are mutually exclusive — the gate blocks
  a response claiming both).
  **A violation is CONTAINED, not just labelled**: the node returns ERROR *and clears every
  output-bearing state field*. The invoke envelope falls back to `state["result"]` whenever
  `formatted_output` is empty, including on an error status, so a gate that only flipped the
  status — or that raised — would still ship the un-gated inner answer inside the error envelope.
- **No rounding grid** — the agent renders identifiers, the caller's own requested settings and
  prose. It computes and renders no monetary aggregate, so there is no output-precision grid to
  enforce; a requested budget must reach the platform as the exact figure the caller asked for,
  and rounding it would be the defect rather than the control. The invariant this template does
  enforce is the record-evidence / pending-exclusivity / credential rule above, and it is
  enforced for every representation including nested ones.
- **Audit** — every node's `execute()` emits exactly one positional
  `emit_trace_event("<node>_complete", {small non-PII payload}, state)` on its success path
  (intent / presence signals only — never request text, campaign content, or credentials).
  `__call__()` is never overridden. Event names (documented for operations):

  | Node | Event |
  |------|-------|
  | pre_process | `pre_process_complete` |
  | validate_input | `validate_input_complete` |
  | classify_intent | `classify_intent_complete` |
  | infer_appier_fields | `infer_appier_fields_complete` |
  | call_appier_api | `call_appier_api_complete` (write/lookup executed) / `call_appier_api_approval_required` (unapproved write refused) |
  | confirm | `confirm_complete` |
  | post_process | `post_process_complete` / `post_process_output_blocked` (gate violation contained) |

## v1 Implementation Note — model-backed synthesis

v1 is fully deterministic: intent classification (`ClassifyIntentNode`) uses a keyword heuristic
and field inference (`InferAppierFieldsNode`) uses regex/line-structure extraction, so the
template runs and tests without a model backend. **No model client is constructed anywhere in v1**
and no system prompt is read (no dead config), which is why the manifest declares
`generation_mode: "deterministic"` and `requires.extras: []`. Model-backed synthesis (richer
intent classification, free-text-to-field mapping, natural-language campaign summaries) is a
documented follow-up: an `llm:` section in `config/config.yaml` is already forwarded to the inner
graph by `_parent_config()`, so wiring one in is additive and requires no graph-shape change.

## v1 Limitation — Appier client (documented)

`src/services/appier_client.py` ships a **deterministic, network-free v1 stub** as its default
transport: it returns the documented Appier campaign-API response shapes (a `campaign` object for
lookups; an update-receipt with a campaign-id echo for setting/status writes, derived from the
request) so the pipeline is runnable and testable without a live Appier tenant or an HTTP client
library. It does **not** perform a live Appier call — the limitation is documented rather than
papered over with a fake live call. To go live, inject real `patch`/`get` transports at
construction; the transport contract is
`(url, headers, json_body, timeout_s) -> (status_code, response_dict)` and the method contracts
follow the Appier campaign-management REST shape (`GET /campaigns/{id}`,
`PATCH /campaigns/{id}`, `PATCH /campaigns/{id}/status`), so no business-logic change is
required. (The stub also runs without a live credential — see Security Design; a live transport
requires `APPIER_API_KEY`.)

## Framework Utilization

### Shared Components Used
- [x] `InvocationContext` — read in `CallAppierApiNode` via `InvocationContext.from_state(state)` (secrets + trust)
- [x] Trust gate — single external gate `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; inner domain nodes (incl. `CallAppierApiNode`) declare `TrustLevel.ANONYMOUS`
- [x] Secrets — `ctx.secrets.get("APPIER_API_KEY")`; entry-point `bound_secrets` / `secrets_factory` / `provision_secrets` in `src/api/server.py`
- [x] Credential detector — `detect_credentials_in_value`, reused at the entry point so the adapter's refusal set matches the framework gate's block set
- [x] `emit_trace_event()` — one positional call per node on the success path; framework lifecycle events (node_start/node_complete/node_error) are not re-emitted

### Composition Pattern

- **Pattern**: GraphNode (subgraph) — Cat 2 outer/inner split.
- **Composition target**: inner `AppierWorkflowGraph` (`BaseGraph`) via `AppierWorkflowGraphNode.get_subgraph()`.
- **Config forwarding**: `AppierWorkflowGraphNode._parent_config()` reads `config/config.yaml` and forwards `{appier, llm (if declared), max_retry, timeout_s}` under `config["configurable"]`.
- **Caller-data forwarding**: the ContextVar bridge in `src/graph/context_bridge.py` (the subgraph call carries one string, and the text channel is masked).
- **Error propagation strategy**: `propagate` (default) — inner errors re-raised as `SubgraphError`; per-step `status=error` + `error_log` for API/validation failures (no silent pass).
- **Conditional routes**: the inner topology is linear, so no `add_conditional_edges` call exists. `AppierWorkflowGraph.route()` is still annotated with the graph's own `State`, because a path callable's annotation is read as its input schema and fields outside it are projected away — a base-state annotation would make the routing fields invisible the day a branch is added.

## Import Isolation Confirmation
- [x] Template imports `framework/` and `shared/` only; no platform-SDK import anywhere
- [x] `src/services/appier_client.py`, `src/services/security.py` and `src/services/validation.py` have no framework imports (pure service layer, stdlib only)

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| Base class | AgentBaseGraph | AutonomousBaseGraph | AgentBaseGraph | Fixed multi-step pipeline (Cat 2), not an autonomous loop |
| Composition pattern | flat single main node | GraphNode + inner subgraph | GraphNode + inner subgraph | Cat 2 must not be flat; 5 domain steps live in the inner graph |
| Model dependency | model client in v1 | deterministic v1, synthesis as a documented follow-up | deterministic v1 | template runs/tests without a backend; no dead prompt/config reads |
| Appier client | live HTTP call | injectable transport + documented v1 stub default | injectable + v1 stub default | no live network in v1; document the limitation; go-live is a transport injection, no logic change |
| Node configuration | ctor-arg dependency injection | no-arg nodes + config forwarding via `_parent_config()` → `configurable` → state | no-arg nodes | nodes are no-arg (ctor args fail at graph build); the config files stay the single source |
| Runtime config location | keep tuning in the registry manifest | `config/config.yaml`, read by the code and the constructor | `config/config.yaml` | the flat manifest has no place for it; a value read from the wrong file is dead and green |
| Caller campaign data | inside the request text | validated `input_context` + state bridge | context + bridge | the framework's input mask rewrites names and digit groups in the text channel, so the text route would write corrupted values to a live campaign |
| Caller string safety | sanitize on the way out | restrict the render alphabet on the way in | alphabet on the way in | markup, control tokens and mail addresses cannot be constructed from the permitted characters at all |
| Write target | infer campaign id from NL freely | caller-supplied/explicit id only; unresolved left empty | explicit only | never write to the wrong campaign; unresolved id → status=error, not invented |
| Default intent | update_campaign | lookup_campaign | lookup_campaign | low-confidence classification must never default to a write (budget/status changes spend real money) |
| Structured output | plain text only | structured keys via `get_output()` override | structured keys | callers integrate the campaign id/ref programmatically; keys surface ONLY on success (fail-closed) |
| Write execution guard | execute writes as classified (post-hoc confirmation only) | pre-action approval gate: explicit approval required BEFORE update/set_status executes | pre-action approval gate | externally impactful writes spend real ad money; implemented as a deterministic single-invoke approval flag, no interrupt machinery |
| Output-gate violation | raise / relabel as error | return ERROR and clear every output-bearing field | clear the fields | the envelope falls back to `result` even on error, so labelling alone still ships the un-gated answer |
