"""Persistent runtime state helpers for pocket-claude."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from backends import normalize_agent

STATE_DIR = os.path.dirname(os.path.abspath(__file__))
BIND_FILE = os.path.join(STATE_DIR, "bindings.json")
JSONL_ID_FILE = os.path.join(STATE_DIR, "jsonl_ids.json")
BACKEND_FILE = os.path.join(STATE_DIR, "session_backends.json")


@dataclass
class BridgeState:
    chat_session_map: dict[str, str] = field(default_factory=dict)
    session_jsonl_id: dict[str, str] = field(default_factory=dict)
    session_backend: dict[str, str] = field(default_factory=dict)


def _load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def load_state(
    bind_file: str = BIND_FILE,
    jsonl_id_file: str = JSONL_ID_FILE,
    backend_file: str = BACKEND_FILE,
) -> BridgeState:
    """Load chat/session/backend state from JSON files."""
    backends = {
        k: normalize_agent(v)
        for k, v in _load_json(backend_file).items()
    }
    return BridgeState(
        chat_session_map=_load_json(bind_file),
        session_jsonl_id=_load_json(jsonl_id_file),
        session_backend=backends,
    )


def save_state(
    state: BridgeState,
    bind_file: str = BIND_FILE,
    jsonl_id_file: str = JSONL_ID_FILE,
    backend_file: str = BACKEND_FILE,
):
    """Save chat/session/backend state to JSON files."""
    with open(bind_file, "w") as f:
        json.dump(state.chat_session_map, f, ensure_ascii=False, indent=2)
    with open(jsonl_id_file, "w") as f:
        json.dump(state.session_jsonl_id, f, ensure_ascii=False, indent=2)
    with open(backend_file, "w") as f:
        json.dump(state.session_backend, f, ensure_ascii=False, indent=2)
