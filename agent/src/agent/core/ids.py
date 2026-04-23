from __future__ import annotations

import os
import threading
import time

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_LOCK = threading.Lock()
_LAST_TIMESTAMP_MS = -1
_LAST_RANDOM_BITS = 0


def generate_run_id() -> str:
    return f"run_{_new_ulid()}"


def generate_step_id() -> str:
    return f"step_{_new_ulid()}"


def generate_event_id() -> str:
    return f"event_{_new_ulid()}"


def generate_audit_id() -> str:
    return f"audit_{_new_ulid()}"


def generate_evidence_id() -> str:
    return f"evidence_{_new_ulid()}"


def generate_memory_entry_id() -> str:
    return f"memory_{_new_ulid()}"


def generate_repair_id() -> str:
    return f"repair_{_new_ulid()}"


def generate_conflict_id() -> str:
    return f"conflict_{_new_ulid()}"


def generate_browser_session_id() -> str:
    return f"browser_session_{_new_ulid()}"


def generate_browser_context_id() -> str:
    return f"context_{_new_ulid()}"


def generate_tab_id() -> str:
    return f"tab_{_new_ulid()}"


def generate_frame_id() -> str:
    return f"frame_{_new_ulid()}"


def _new_ulid() -> str:
    global _LAST_TIMESTAMP_MS, _LAST_RANDOM_BITS

    now_ms = int(time.time() * 1000)
    with _LOCK:
        if now_ms > _LAST_TIMESTAMP_MS:
            _LAST_TIMESTAMP_MS = now_ms
            _LAST_RANDOM_BITS = int.from_bytes(os.urandom(10), "big")
        else:
            _LAST_TIMESTAMP_MS += 1
            _LAST_RANDOM_BITS = (_LAST_RANDOM_BITS + 1) % (1 << 80)

        return _encode_timestamp(_LAST_TIMESTAMP_MS) + _encode_random(_LAST_RANDOM_BITS)


def _encode_timestamp(timestamp_ms: int) -> str:
    return _encode_base32(timestamp_ms, 10)


def _encode_random(random_bits: int) -> str:
    return _encode_base32(random_bits, 16)


def _encode_base32(value: int, length: int) -> str:
    chars = ["0"] * length
    for index in range(length - 1, -1, -1):
        chars[index] = _CROCKFORD_ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(chars)
