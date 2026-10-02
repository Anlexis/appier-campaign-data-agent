"""AGENTIC STAR Marketplace entrypoint — one-shot Pod process.

Referenced by this repo's Dockerfile as the image `CMD`. Compiles the agent,
provisions its secrets, then hands off to shared.bootstrap.marketplace_app
for the Marketplace lifecycle (identity, input, events, terminal delivery,
exit). Mirrors agentcore's own `agents/base/chat_agent/cli.py` (the pattern
this file was copied from).

`namespace=` here is the Marketplace secret-provisioning namespace — a different
concept from `config/agent.yaml`'s AgentRegistry `namespace:` key that happens to
share its value. Mirrors `src/api/server.py`'s existing
`secrets_factory(namespace="cmn", agent_name="AppierCampaignAgent")` call shape
rather than a per-template value: one Marketplace Pod deploys exactly one
template, so there is no cross-template secret-path collision to guard against.

config= reuses this repo's own src/graph/graph.load_runtime_config(), the same
function src/api/server.py already calls, so both entry points read
config/config.yaml identically.
"""

from shared.bootstrap.marketplace_app import run_agent_marketplace
from src.graph.graph import AppierCampaignAgent, load_runtime_config

if __name__ == "__main__":
    run_agent_marketplace(
        AppierCampaignAgent,
        agent_name="AppierCampaignAgent",
        namespace="cmn",
        config=load_runtime_config(),
    )
