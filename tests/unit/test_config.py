# CMN-C2-289 - Unit tests: the two config files and what actually reads them.
#
# config/agent.yaml is the flat registry manifest (identity + entry point +
# declared requirements, every key at root level). config/config.yaml holds the
# runtime parameters. The split matters here because a value declared in one
# and read from the other is dead - so these tests assert the declarations AND
# that the readers point at the file the value lives in.

import pathlib

import pytest

try:
    import yaml  # pyyaml (transitive dep of the framework wheel)

    _YAML_ERROR = None
except Exception as exc:  # pragma: no cover
    yaml = None
    _YAML_ERROR = exc

_CONFIG_DIR = pathlib.Path(__file__).parents[2] / "config"
_MANIFEST_PATH = _CONFIG_DIR / "agent.yaml"
_RUNTIME_PATH = _CONFIG_DIR / "config.yaml"

pytestmark = pytest.mark.skipif(_YAML_ERROR is not None, reason=f"pyyaml unavailable: {_YAML_ERROR}")


def _load(path):
    return yaml.safe_load(path.read_text())


def test_manifest_identity():
    data = _load(_MANIFEST_PATH)
    assert data["id"] == "CMN-C2-289"
    assert data["category"] == "Cat 2"
    assert data["industry"] == "CMN"
    assert data["namespace"] == "cmn"
    assert data["base_type"] == "ToolCallingAgent"
    assert data["enabled"] is True


def test_manifest_entry_point():
    data = _load(_MANIFEST_PATH)
    # A single dotted import path at root level, not split module:/class: keys.
    assert data["class"] == "src.graph.graph.AppierCampaignAgent"
    assert data["generation_mode"] == "deterministic"


def test_manifest_security():
    data = _load(_MANIFEST_PATH)
    # Agent-level entry trust, enforced by the outer backbone pre_process gate;
    # inner domain nodes stay ANONYMOUS.
    assert data["required_trust_level"] == "VERIFIED_EXTERNAL"


def test_manifest_declares_no_compile_time_requirements():
    """The declared requirements must match what the code actually demands.

    The integration key is read optionally, so the agent compiles and runs on
    the network-free transport without it. Declaring it as required would make
    the agent fail to start wherever it is not provisioned, for a call it does
    not make.
    """
    data = _load(_MANIFEST_PATH)
    assert data["requires"]["secrets"] == []
    assert data["requires"]["extras"] == []


def test_runtime_config_holds_the_parameters_the_code_reads():
    data = _load(_RUNTIME_PATH)
    assert isinstance(data["max_retry"], int)
    assert isinstance(data["timeout_s"], int)
    # Integration section: forwarded to the inner graph by the subgraph node.
    assert data["appier"]["base_url"] == "https://api.appier.com/v1"


def test_the_forwarder_reads_the_runtime_file_not_the_manifest():
    """The config forwarding must point at the file the values live in.

    A forwarder still reading the manifest returns an empty mapping after the
    split, and every declared value silently stops arriving - which no other
    test in this suite would notice.
    """
    from src.graph.graph import AppierWorkflowGraphNode

    forwarded = AppierWorkflowGraphNode()._parent_config()["configurable"]
    runtime = _load(_RUNTIME_PATH)
    assert forwarded["appier"] == runtime["appier"]
    assert forwarded["max_retry"] == runtime["max_retry"]
    assert forwarded["timeout_s"] == runtime["timeout_s"]
