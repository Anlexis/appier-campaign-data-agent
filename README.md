# CMN-C2-289 — Appier Campaign Agent

> **Category**: Cat 2 (multi-step domain workflow)
> **Industry**: Cross-industry

## Overview

Manage advertising campaigns on the Appier marketing platform from a plain-language request.
The agent reads an instruction such as "look up campaign 1001" or "pause it", works out
which of three operations is being asked for (look up settings, update settings, change
status), assembles the corresponding Appier campaign-API request from the request text and a
structured request context, and returns a human-readable confirmation together with the
campaign reference.

Two properties are worth knowing before you adapt it. **Writes need explicit approval**: an
update or status change is never executed on the first request — the agent returns a preview
of what it would change and waits for the caller to re-send with an approval signal, because a
budget or status change spends real money. And **the campaign target is never guessed**: an
unresolved campaign id is an error rather than a best effort, so the agent cannot act on the
wrong campaign.

The bundled Appier client is a deterministic, network-free stub, so the pipeline runs and tests
end to end out of the box without a live Appier tenant. A real deployment injects `get` /
`patch` transports at client construction; the request and response shapes already follow the
documented Appier campaign-management endpoints, so no pipeline change is needed.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Provided by the platform environment, not resolved from the default package index. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent fails at graph
compile / start-up preflight rather than starting in a partially working state. This is
intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and operational documentation
```

See `docs/02_design.md` for the architecture and the caller contract, and
`docs/03_test_spec.md` for what the test suite covers.

## Customising

1. Point `config/config.yaml` at your own Appier base URL and adjust the call deadline.
2. Inject live `get` / `patch` transports in `src/services/appier_client.py` and provision
   the integration key through the platform's secret provider.
3. Adjust the intent keywords and the field-extraction patterns under `src/nodes/` for your
   own request wording, and the caller-input bounds in `src/services/validation.py` for your
   own campaign naming.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

