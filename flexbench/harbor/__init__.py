"""Harbor extensions for trusted execution on a workstation without containers."""

import os

# This integration calls only the native CLI; pricing comes from our profile.
os.environ.setdefault("HARBOR_TELEMETRY", "0")
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")

HARBOR_VERSION = "0.24.0"
