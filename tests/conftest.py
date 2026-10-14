"""Stubs for Hermes core imports when running tests outside a Hermes install.

The plugin subclasses ``agent.context_compressor.ContextCompressor`` and uses
``agent.model_metadata.estimate_messages_tokens_rough`` and
``agent.auxiliary_client``. When the repo is tested standalone (no Hermes
install on sys.path), these are stubbed so the pure logic in tree.py,
store.py and compactor.py can be exercised without the full runtime.
"""
import sys
import types


def _install_stubs():
    if "agent" in sys.modules:
        return  # running inside a real Hermes install

    agent = types.ModuleType("agent")
    agent.__path__ = []
    sys.modules["agent"] = agent

    cc = types.ModuleType("agent.context_compressor")

    COMPRESSED_SUMMARY_METADATA_KEY = "compressed_summary"
    SUMMARY_PREFIX = "[COMPRESSED SUMMARY]"

    class ContextCompressor:
        def __init__(self, model=""):
            self.model = model
            self.base_url = ""
            self.api_key = ""
            self.provider = ""
            self.api_mode = ""
            self.context_length = 0
            self.threshold_tokens = 0
            self.threshold_percent = 0.0
            self._base_threshold_percent = 0.0
            self._config_threshold_percent = 0.0
            self.protect_first_n = 0
            self.protect_last_n = 0
            self.summary_target_ratio = 0.0
            self.quiet_mode = False
            self.max_tokens = 0
            self.model_thresholds = {}

        def update_model(self, **kwargs):
            for k, v in kwargs.items():
                if v is not None:
                    setattr(self, k, v)

        def compress(self, messages, current_tokens=None, focus_topic=None,
                     force=False, memory_context=""):
            return messages

    cc.COMPRESSED_SUMMARY_METADATA_KEY = COMPRESSED_SUMMARY_METADATA_KEY
    cc.SUMMARY_PREFIX = SUMMARY_PREFIX
    cc.ContextCompressor = ContextCompressor
    sys.modules["agent.context_compressor"] = cc
    agent.context_compressor = cc

    mm = types.ModuleType("agent.model_metadata")

    def estimate_messages_tokens_rough(messages, model=None):
        return 0

    mm.estimate_messages_tokens_rough = estimate_messages_tokens_rough
    sys.modules["agent.model_metadata"] = mm
    agent.model_metadata = mm

    ac = types.ModuleType("agent.auxiliary_client")

    def call_llm(**kwargs):
        raise RuntimeError("auxiliary_client stub: no LLM available in tests")

    def extract_content_or_reasoning(resp):
        return ""

    ac.call_llm = call_llm
    ac.extract_content_or_reasoning = extract_content_or_reasoning
    sys.modules["agent.auxiliary_client"] = ac
    agent.auxiliary_client = ac


_install_stubs()