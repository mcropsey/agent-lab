import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("AGENTLAB_CONFIG", ROOT / "config" / "lab.yaml"))
DATA_DIR = ROOT / "data"
WORK_DIR = Path(os.environ.get("AGENTLAB_WORKDIR", "/tmp/agentlab"))

HOST = os.environ.get("AGENTLAB_HOST") or os.uname().nodename
BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://192.168.1.101:4000/v1")


def load() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def persona_cfg(name: str) -> dict:
    return load()["personas"][name]


def key_for(name: str) -> str:
    var = f"AGENTLAB_KEY_{name.upper()}"
    try:
        return os.environ[var]
    except KeyError:
        raise RuntimeError(f"{var} is not set - is this persona deployed to this host?") from None
