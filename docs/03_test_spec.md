# Test Specification - CMN-C2-289 Appier Campaign Agent

## Test Strategy

- Test types: Unit (per node + service + validation contract + injection screen + config + inner
  graph + framework compliance) / Proof-of-Boundary (full outer-graph invoke, end-to-end invoke
  through the real HTTP entry point, import isolation, state safety, server boot, HITL stub).
- Location: `tests/unit/`, `tests/proof_of_boundary/`. `tests/integration/` is an empty package;
  end-to-end coverage lives in the boundary suite, which drives the real compiled graph.
- The Appier call is exercised through the deterministic, network-free v1 stub transport (the
  default) and through injected fake clients; no live Appier call is ever made.
- **Node-invocation convention**: per-node unit tests invoke the node as `node(state)`, so
  `BaseNode.__call__` routes the full security pipeline (trust gate → input mask → `execute()` →
  credential scan). State builders set `caller_trust_level = TrustLevel.VERIFIED_EXTERNAL.value`
  for `PreProcessNode` (the single external gate) and `TrustLevel.ANONYMOUS.value` for every
  other node.
  Two deliberate exceptions:
  - `TestCallerContractRefusesDirectly` (`test_pre_process_node.py`) and
    `TestCallerBlockWinsAndIsBounded` (`test_infer_appier_fields_node.py`) call `execute()`
    DIRECTLY. Going through `__call__` would put the framework's input gate in front, and that
    gate refuses some of those payloads on its own — so a green result there would say nothing
    about whether THIS template refuses them. Measured against the installed runtime, the
    framework gate does not catch the `<<SYS>>` block or a directive spliced with markup, and
    where the gate is absent or configured off it catches none of them.
  - `CallAppierApiNode.execute(state, config=...)`, whose second argument `__call__` cannot
    forward.
- The trust-rejection test asserts on the RETURNED error dict (`status == AgentStatus.ERROR.value`,
  "trust gate denied" in `error_log`, execute-only keys absent) — `__call__` never raises for a
  trust denial.
- Assertion contract: the invoke surface is `result["output"]` / `status` / `trace_id` /
  `correlation_id` / `node_history` (never `formatted_output` at the invoke surface); status is
  compared to `AgentStatus.SUCCESS`/`.value`; the outer graph is called as
  `invoke(user_input=..., ctx=..., input_context=...)`; audit spies assert on `call.args[1]` (the
  event payload), never the whole-call repr.
- Runtime behaviours the suite encodes: `__call__` short-circuits on an incoming errored state
  (`execute()` is skipped; error status/error_log pass through); the framework's input mask
  rewrites Title-Case bigrams (across newlines), emails and digit groups in
  `user_input`/`validated_input` to `[MASKED]` before `execute()` sees the text — so positive
  payloads are PII-free, intentional-PII tests assert the `[MASKED]` path, and the caller's
  campaign data is asserted to survive INTACT on the context channel, which the mask does not
  touch.
- Domain audit events are muted per module via an autouse fixture patching
  `src.nodes.<mod>.emit_trace_event` (never a `sys.modules` stub of `shared.*`).

## Unit Tests (`tests/unit/`)

| TC-ID | Test file | Focus | Expected |
|-------|-----------|-------|----------|
| U-01 | test_trust_gate.py | trust boundary: ANONYMOUS caller on the VERIFIED_EXTERNAL pre_process gate; inner nodes ANONYMOUS; trust-posture declarations | denial RETURNS an error dict ("trust gate denied" in error_log, execute-only keys absent); VERIFIED_EXTERNAL passes; every inner node declares ANONYMOUS |
| U-02 | test_pre_process_node.py | request shaping (text + campaign hint + approval flag → `validated_input` JSON); HTML strip; hint priority campaign_id > campaign_hint > campaign_code; **and the caller contract, refused directly**: injection markers (control tokens, directives, the spliced form), the malformed-context matrix, unrecognised fields counted not echoed, a valid campaign block carried verbatim | hint resolved by priority; `<script>` stripped; `approved: true` forwarded, a truthy string refused; empty/missing → error; every malformed context fails closed with no `validated_input`; rejected values never appear in `error_log`; audit payload carries presence signals only |
| U-03 | test_validate_input_node.py | empty/short guard; JSON-shaped input; framework `[MASKED]` path for emails; node-level token flag-and-redact (`secret_*`); approval extraction (`approve: yes` line / `[approved]` token / envelope boolean) | email → `[MASKED]` before execute; token → `[REDACTED]` + `redaction_flags=["token"]` (JSON string); `approval_granted` True ONLY on the explicit forms; empty/short → error; audit payload carries flags + approval signal only |
| U-04 | test_classify_intent_node.py | intent = lookup_campaign / update_campaign / set_status (keyword, status-first then update then lookup, read-only default) | correct intent per keyword; status keyword beats update; write beats lookup; no signal → lookup_campaign with a non-fatal note; empty → error; audit emits the intent label only |
| U-05 | test_infer_appier_fields_node.py | campaign-id resolution (text > id-shaped hint; never invented); quoted name; budget / ISO date range / `Key: value` settings; request body per intent; activation-before-pause status resolution; approval-gate control lines excluded from settings; **caller-block precedence and bounds** (direct execute) | lookup `{campaign_id}`; update `{campaign_id, campaign{name, daily_budget, schedule, settings}}`; set_status `{campaign_id, status}` with "unpause" → active; unresolved id left `""`; a MALFORMED hint is refused, not silently dropped; `approve: yes` never appears in `campaign.settings`; the caller block overrides the text; a hostile caller block is refused here too; a mask-marked text value is refused with a pointer at the context channel |
| U-06 | test_call_appier_api_node.py | lookup/update/set_status via the network-free stub; **pre-action approval gate** (a spy client proves the write method is NOT called without `approval_granted is True`, IS called with it; `False` still gates; lookup unaffected); `appier_config` state field + `execute(state, config=...)` override; API error / unresolved id / empty update / unresolved status / unknown intent / missing payload; credential posture (a live transport refuses to run unauthenticated; the key is read from the invocation context, never env/state) | approved write → record_id/record_ref + one client write call; unapproved write → SUCCESS with `approval_required=True` + a whitelisted `pending_action`, spy write calls == [], no record evidence; payload validation errors still precede the gate; 403 surfaces in error_log; live+no-secret → error; audit emits presence signals with `stub_transport=True` |
| U-07 | test_confirm_node.py | two modes: completed-action confirmation per intent verb (ref/id formatting; name fallback) vs request-for-approval (pending fields / target status + how-to-approve instructions) | "Retrieved / Updated / Changed campaign … ref=… id=…"; approval mode: "Approval required - no change has been made" + pending detail + the `approve: yes` instruction, completed verbs absent, record evidence NOT required; completed mode missing evidence → error |
| U-08 | test_post_process_node.py | `formatted_output` shaping (JSON payload round-trip); errored state passes through `__call__` un-masked (short-circuit); the output gate incl. the approval-required carve-out (module-level helper, full-node path + direct function tests incl. a credential-shaped value); **containment on violation** | success shape with parsed `appier_payload` + `approval_required`/`pending_action`; error status/error_log preserved, no success shape fabricated; SUCCESS without record evidence blocked UNLESS `approval_required=True` (which must carry the confirmation and NO record evidence); a blocked output clears `result`, `formatted_output`, `confirmation`, `appier_payload`, `pending_action`, `record_id`, `record_ref`, `campaign_name` |
| U-09 | test_appier_client.py | Appier REST client: GET /campaigns/{id}, PATCH /campaigns/{id}, PATCH /campaigns/{id}/status; `X-Api-Key` header; `AppierApiError` on non-2xx (`errors` join + `message` fallback); stub shapes (campaign object / update receipt + campaign_id echo, `_stub` marker); `uses_stub_transport`; the `timeout_s` transport argument | correct URLs/headers/bodies; 404/400 raise with the extracted message; stub shapes deterministic; the configured deadline reaches every request, and defaults when unset |
| U-10 | test_config.py | the two config files and what actually reads them | flat manifest identity/entry-point/trust; `requires.secrets`/`extras` empty (the key is read optionally, so declaring it required would break start-up); runtime file holds `max_retry`/`timeout_s`/`appier.base_url`; the forwarder reads the RUNTIME file, not the manifest |
| U-11 | test_domain_workflow_graph.py | inner `AppierWorkflowGraph`: identity, `_extra_initial_state()` injection, `route()` short-circuit, `get_output` contract, compile, direct inner invoke on the stub — lookup + BOTH approval-gate write paths | name/state_schema correct; config forwarded as a JSON string; error → END; inner lookup invoke runs validate → classify → infer → call → confirm to SUCCESS with record evidence; unapproved write → SUCCESS with `approval_required=True`, pending preview, no record evidence; same request + `approve: yes` → the write executes |
| U-12 | test_framework_compliance_tc06_tc07.py | the framework's input/output gate methods are final on FunctionNode | overriding `_security_gate_input` / `_security_gate_output` raises TypeError at class definition |
| U-13 | test_validation.py | the caller-input validation contract as pure functions: a non-finite matrix per parser, bools, non-numerics, out-of-range magnitudes, the identifier alphabet, the render alphabet, setting labels, ISO calendar dates, the status set, bounded integers | every non-finite spelling (`"NaN"`, `"Infinity"`, `"-Infinity"`, raw `float("nan")`, raw `float("inf")`) rejected per field; bools rejected; ordinary campaign wording (including Japanese) accepted; mail addresses, markup and control tokens rejected by alphabet; `2026-02-29` rejected as a non-calendar date; error messages never carry the value |
| U-14 | test_security_screen.py | sanitizing vs refusal: control tokens as a class, directive phrasing, the raw pass, the post-strip pass, depth-first structural screening with keys, and the false-positive direction | control tokens and directives found; the markup strip DELETES a control token (which is why the raw pass exists) and re-assembles a spliced directive (which is why the sanitized pass exists); hostile keys reported by position, never echoed; six real campaign phrasings containing screened words are NOT refused |
| U-15 | test_context_bridge.py | the outer/inner state bridge | a stashed payload is returned once, the take is destructive, an empty stash clears a previous one |

## Proof-of-Boundary Tests (`tests/proof_of_boundary/`)

| PB-ID | Boundary | Test | Expected |
|-------|----------|------|----------|
| PB-4 | Import isolation | test_import_isolation.py | AST scan of `src/`: no platform-SDK import |
| PB-2/PB-5 | State serialization | test_state_safety.py | `state.py`: no Pydantic/credential fields |
| PB-6 | Backbone invoke-order + external trust | test_pb_invoke_order.py | the payload is asserted byte-equal to `deploy/invoke_payload.json`; a VERIFIED_EXTERNAL caller yields `status=success` with `node_history == [InitializeNode, PreProcessNode, AppierWorkflowGraphNode, PostProcessNode, FinalizeNode]` and record evidence + confirmation in `result["output"]`; an ANONYMOUS caller is denied at pre_process (error, no post_process, no output); blank input → error, not a crash |
| PB-6b | End-to-end through the real HTTP entry point | test_pb_invoke_endpoint.py | runtime config reaches the graph AND the outbound client's deadline; real evidence from an authenticated request; caller campaign data reaches the request body INTACT while the same name in the request text is refused; schedule/settings arrive; the approval gate holds and the approved re-send writes; every intent path reachable; 401 on missing/wrong/non-ASCII token; the malformed-context matrix and the non-finite budget matrix fail closed; a bare `NaN` literal in a hand-built body is refused; rejected values never echoed; injection content refused (control tokens, directives, spliced, escaped, hostile field name); ordinary campaign wording still succeeds; a credential-shaped context value → HTTP 400 naming the field; oversized context → HTTP 413; no credential-shaped string anywhere in any response (with a control proving the scanner works); structural tokens survive byte-identical; a blocked response carries no released text, no traceback and no source paths |
| PB-7 | HITL interrupt propagation *(conditional)* | test_pb7_hitl_interrupt_propagation.py | **Skip stub — non-HITL**: the module inspects `src/graph/graph.py` for a class declaring `propagate_hitl = True`; this template declares `propagate_hitl = False` on the main-slot GraphNode and has no interrupt checkpoint. The stub body is a real AssertionError (never `assert True`), so enabling HITL propagation without implementing PB-7 fails loudly |
| PB (boot) | Server entry point | test_server_boot.py | importing `src.api.server` does not raise (construct + compile + provision secrets at import); the agent constructs and compiles via the supported path; `/invoke` + `/health` routes exposed |

> PB-1 (audit emission) is covered inside the unit suite via the audit emit-spy tests (every node
> module asserts on the event payload, `call.args[1]`).
> PB-3 (live external service) is exercised at deployment first-invoke, not in this suite — the
> v1 transport is the documented network-free stub.

## Test Execution Summary

- Runner: `python -m pytest tests/ -q` against the framework wheel the pipeline installs
  (`agenticstar-agentcore[...]==1.0.2`).
- Total tests: 373 pass, 1 skip (PB-7 — skip stub, non-HITL), 0 fail.
- Each protection in the list above was verified by breaking it deliberately and confirming the
  suite goes red: the injection screen, the structural screen, the config forwarding, the output
  containment, and the audit-trace gate.
