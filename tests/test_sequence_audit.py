"""Tests for the optional sequence-number continuity audit on sessions.

Opt-in at session creation (``enable_sequence_audit``); the pinned protocol
must declare an unsigned sequence field at least two effective bits wide.

Coverage:

* creation validation (missing/signed/1-bit sequence fields -> 422);
* per-append anomalies for newly confirmed frames only: duplicate,
  suspected_gap (with missing_count), backward, direction_unknown (half turn);
* modular comparison at 8-bit / 16-bit / 2-bit widths, including wraps;
* every valid frame becomes the next baseline (also anomalous ones) while
  bad CRC frames and the unresolved tail never move it;
* baselines and cumulative counts are carried across chunks and restarts;
* identical-chunk retry replays the stored audit result without counting
  twice; conflicts, oversize and decode failures change nothing;
* non-audit sessions and old databases behave exactly as before;
* multi-instance racing keeps audit state consistent with the winner.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import b64, crc_ccitt, standard_protocol

SYNC = b"\xaa\x55"
ZERO_COUNTS = {
    "duplicate": 0,
    "suspected_gap": 0,
    "backward": 0,
    "direction_unknown": 0,
}


def register(client, **overrides):
    r = client.post("/protocols", json=standard_protocol(**overrides))
    assert r.status_code == 201, r.text
    return r


def register_u16(client, protocol_id="seq16-line", version=1):
    """11-byte body: u16 seq @0, addr @2, s16 temp @3, length byte @5, CRC @9."""
    proto = {
        "protocol_id": protocol_id,
        "version": version,
        "sync": [0xAA, 0x55],
        "endian": "big",
        "length_field": {"offset": 5, "size": 1, "adjustment": 0},
        "crc": {"preset": "CRC-16/CCITT-FALSE", "offset": 9},
        "fields": [
            {"name": "seq", "type": "u16", "offset": 0},
            {"name": "addr", "type": "u8", "offset": 2},
            {"name": "temp", "type": "s16", "offset": 3, "scale": "0.01"},
        ],
        "sequence_field": "seq",
        "max_frame_length": 64,
    }
    r = client.post("/protocols", json=proto)
    assert r.status_code == 201, r.text


def make_session(client, *, protocol_id="sensor-line", version=1, audit=False):
    r = client.post(
        "/sessions",
        json={
            "protocol_id": protocol_id,
            "version": version,
            "enable_sequence_audit": audit,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def append(client, sid, offset, chunk):
    return client.post(
        "/sessions/append",
        json={"session_id": sid, "expected_offset": offset, "chunk_b64": b64(chunk)},
    )


def frame(seq, addr=3, temp_s16=0) -> bytes:
    """Standard 9-byte protocol wire frame; seq occupies the low bits of body[0]."""
    return SYNC + raw_logical(seq, addr, temp_s16)


def raw_logical(seq, addr, temp_s16, status=0) -> bytes:
    body = bytearray(9)
    body[0] = seq & 0xFF
    body[1] = addr & 0xFF
    body[2:4] = (temp_s16 & 0xFFFF).to_bytes(2, "big")
    body[4] = 9
    body[5] = status
    crc = crc_ccitt(bytes(body[:6]) + bytes(body[8:]))
    body[6:8] = crc.to_bytes(2, "big")
    return bytes(body)


def corrupt_crc(wire_frame: bytes) -> bytes:
    """Flip a CRC byte of an otherwise valid wire frame."""
    b = bytearray(wire_frame)
    b[-1] ^= 0xFF
    return bytes(b)


def frame16(seq, addr=3, temp_s16=0) -> bytes:
    body = bytearray(11)
    body[0:2] = (seq & 0xFFFF).to_bytes(2, "big")
    body[2] = addr & 0xFF
    body[3:5] = (temp_s16 & 0xFFFF).to_bytes(2, "big")
    body[5] = 11
    crc = crc_ccitt(bytes(body[:9]) + bytes(body[11:]))
    body[9:11] = crc.to_bytes(2, "big")
    return SYNC + bytes(body)


# --------------------------------------------------------- creation rules ----


def test_audit_session_creation_reports_config(client):
    register(client)
    s = make_session(client, audit=True)
    audit = s["sequence_audit"]
    assert audit == {
        "enabled": True,
        "sequence_field": "seq",
        "bit_width": 8,
        "last_sequence": None,
        "anomalies": [],
        "counts": dict(ZERO_COUNTS),
    }

    # The progress endpoint starts in the same state.
    p = client.get(f"/sessions/{s['session_id']}").json()["sequence_audit"]
    assert p["enabled"] is True
    assert p["bit_width"] == 8
    assert p["last_sequence"] is None
    assert p["counts"] == dict(ZERO_COUNTS)


def test_non_audit_session_has_no_audit_block(client):
    register(client)
    s = make_session(client, audit=False)
    assert "sequence_audit" not in s
    r = append(client, s["session_id"], 0, frame(1) + frame(1))
    assert r.status_code == 200
    assert r.json()["sequence_audit"] is None
    p = client.get(f"/sessions/{s['session_id']}").json()
    assert p["sequence_audit"] is None


def test_audit_requires_sequence_field(client):
    register(client, sequence_field=None)
    r = client.post(
        "/sessions",
        json={"protocol_id": "sensor-line", "version": 1, "enable_sequence_audit": True},
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "SEQUENCE_AUDIT_UNSUPPORTED"
    assert err["details"]["reason"] == "missing_sequence_field"

    # Nothing is created.
    listed = client.get("/sessions/anything")
    assert listed.status_code == 404


def test_audit_rejects_signed_sequence_field(client):
    register(
        client,
        protocol_id="signed-seq",
        fields=[
            {"name": "seq", "type": "s8", "offset": 0},
            {"name": "addr", "type": "u8", "offset": 1},
            {"name": "temp", "type": "s16", "offset": 2, "scale": "0.01"},
            {"name": "state", "type": "u8", "offset": 5, "bit_offset": 0, "bit_width": 3},
            {"name": "alarm", "type": "u8", "offset": 5, "bit_offset": 3, "bit_width": 1},
        ],
        sequence_field="seq",
    )
    r = client.post(
        "/sessions",
        json={"protocol_id": "signed-seq", "version": 1, "enable_sequence_audit": True},
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "SEQUENCE_AUDIT_UNSUPPORTED"
    assert err["details"]["reason"] == "signed_sequence_field"


def test_audit_rejects_one_bit_sequence_field(client):
    register(
        client,
        protocol_id="onebit-seq",
        fields=[
            {"name": "seq", "type": "u8", "offset": 0, "bit_offset": 0, "bit_width": 1},
            {"name": "addr", "type": "u8", "offset": 1},
            {"name": "temp", "type": "s16", "offset": 2, "scale": "0.01"},
            {"name": "state", "type": "u8", "offset": 5, "bit_offset": 0, "bit_width": 3},
            {"name": "alarm", "type": "u8", "offset": 5, "bit_offset": 3, "bit_width": 1},
        ],
    )
    r = client.post(
        "/sessions",
        json={"protocol_id": "onebit-seq", "version": 1, "enable_sequence_audit": True},
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "SEQUENCE_AUDIT_UNSUPPORTED"
    assert err["details"]["reason"] == "sequence_field_too_narrow"
    assert err["details"]["bit_width"] == 1


def test_audit_accepts_two_bit_sequence_field(client):
    register(
        client,
        protocol_id="twobit-seq",
        fields=[
            {"name": "seq", "type": "u8", "offset": 0, "bit_offset": 0, "bit_width": 2},
            {"name": "addr", "type": "u8", "offset": 1},
            {"name": "temp", "type": "s16", "offset": 2, "scale": "0.01"},
            {"name": "state", "type": "u8", "offset": 5, "bit_offset": 0, "bit_width": 3},
            {"name": "alarm", "type": "u8", "offset": 5, "bit_offset": 3, "bit_width": 1},
        ],
    )
    s = make_session(client, protocol_id="twobit-seq", audit=True)
    assert s["sequence_audit"]["bit_width"] == 2


def test_audit_session_unknown_protocol_still_404(client):
    register(client)
    r = client.post(
        "/sessions",
        json={"protocol_id": "nope", "version": 1, "enable_sequence_audit": True},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PROTOCOL_NOT_FOUND"


# --------------------------------------------------------- classifications ---


def test_first_frame_sets_baseline_without_anomaly(client):
    register(client)
    sid = make_session(client, audit=True)["session_id"]
    d = append(client, sid, 0, frame(5)).json()
    assert d["sequence_audit"]["anomalies"] == []
    assert d["sequence_audit"]["last_sequence"] == 5
    assert d["sequence_audit"]["counts"] == dict(ZERO_COUNTS)


def test_consecutive_and_wrap_advance_are_clean(client):
    register(client)
    sid = make_session(client, audit=True)["session_id"]

    d = append(client, sid, 0, frame(254) + frame(255) + frame(0) + frame(1)).json()
    assert d["sequence_audit"]["anomalies"] == []
    assert d["sequence_audit"]["last_sequence"] == 1
    assert d["sequence_audit"]["counts"] == dict(ZERO_COUNTS)


def test_duplicate_classification(client):
    register(client)
    sid = make_session(client, audit=True)["session_id"]

    f1, f2 = frame(7), frame(7)
    d = append(client, sid, 0, f1 + f2).json()
    anomalies = d["sequence_audit"]["anomalies"]
    assert len(anomalies) == 1
    a = anomalies[0]
    assert a == {
        "frame_offset": len(f1),
        "frame_end": len(f1) + len(f2),
        "previous_sequence": 7,
        "sequence": 7,
        "type": "duplicate",
        "missing_count": None,
    }
    assert d["sequence_audit"]["last_sequence"] == 7  # duplicate still becomes baseline
    assert d["sequence_audit"]["counts"]["duplicate"] == 1


def test_suspected_gap_reports_missing_count(client):
    register(client)
    sid = make_session(client, audit=True)["session_id"]

    f1, f2 = frame(10), frame(13)  # delta 3 -> two missing frames
    d = append(client, sid, 0, f1 + f2).json()
    (a,) = d["sequence_audit"]["anomalies"]
    assert a["type"] == "suspected_gap"
    assert a["previous_sequence"] == 10
    assert a["sequence"] == 13
    assert a["missing_count"] == 2
    assert a["frame_offset"] == len(f1)
    assert d["sequence_audit"]["counts"]["suspected_gap"] == 1


def test_forward_half_turn_is_direction_unknown_without_inference(client):
    register(client)
    sid = make_session(client, audit=True)["session_id"]

    # width 8: half a turn is 128.
    d = append(client, sid, 0, frame(10) + frame((10 + 128) & 0xFF)).json()
    (a,) = d["sequence_audit"]["anomalies"]
    assert a["type"] == "direction_unknown"
    assert a["previous_sequence"] == 10
    assert a["sequence"] == 138
    assert a["missing_count"] is None
    counts = d["sequence_audit"]["counts"]
    assert counts["direction_unknown"] == 1
    assert counts["suspected_gap"] == 0
    assert counts["backward"] == 0
    assert d["sequence_audit"]["last_sequence"] == 138

    # The exactly-half-turn frame is the new baseline; a normal +1 step on
    # the modular ring is clean again.
    d2 = append(client, sid, len(frame(10) + frame(138)), frame(139)).json()
    assert d2["sequence_audit"]["anomalies"] == []
    assert d2["sequence_audit"]["last_sequence"] == 139


def test_more_than_half_turn_is_backward_without_inference(client):
    register(client)
    sid = make_session(client, audit=True)["session_id"]

    f1, f2 = frame(5), frame(200)  # forward delta 195 > half (128): backwards
    d = append(client, sid, 0, f1 + f2).json()
    (a,) = d["sequence_audit"]["anomalies"]
    assert a["type"] == "backward"
    assert a["previous_sequence"] == 5
    assert a["sequence"] == 200
    assert a["missing_count"] is None
    counts = d["sequence_audit"]["counts"]
    assert counts["backward"] == 1
    assert counts["suspected_gap"] == 0
    assert d["sequence_audit"]["last_sequence"] == 200


def test_mixed_anomalies_accumulate_in_order(client):
    register(client)
    sid = make_session(client, audit=True)["session_id"]

    # 0 -> 2 (gap, 1 missing) -> 2 (duplicate) -> 200 (backward) -> 200 ...
    chunk = frame(0) + frame(2) + frame(2) + frame(200)
    d = append(client, sid, 0, chunk).json()
    types = [a["type"] for a in d["sequence_audit"]["anomalies"]]
    assert types == ["suspected_gap", "duplicate", "backward"]
    offsets = [a["frame_offset"] for a in d["sequence_audit"]["anomalies"]]
    fl = len(frame(0))
    assert offsets == [fl, 2 * fl, 3 * fl]
    counts = d["sequence_audit"]["counts"]
    assert counts == {
        "duplicate": 1,
        "suspected_gap": 1,
        "backward": 1,
        "direction_unknown": 0,
    }


def test_u16_wrap_and_half_turn(client):
    register_u16(client)
    sid = make_session(client, protocol_id="seq16-line", audit=True)["session_id"]

    # 65535 -> 0 is a clean one-step wrap on a 16-bit ring.
    d = append(client, sid, 0, frame16(65535) + frame16(0)).json()
    assert d["sequence_audit"]["anomalies"] == []
    assert d["sequence_audit"]["last_sequence"] == 0

    # 0 -> 32768 is exactly half of 65536.
    d2 = append(client, sid, 2 * len(frame16(0)), frame16(32768)).json()
    (a,) = d2["sequence_audit"]["anomalies"]
    assert a["type"] == "direction_unknown"
    assert a["previous_sequence"] == 0
    assert a["sequence"] == 32768

    # 32768 -> 32767 is one physical step down: forward delta 65535 > half
    # of the ring, so it is classified as backward.
    d3 = append(client, sid, 3 * len(frame16(0)), frame16(32767)).json()
    (a3,) = d3["sequence_audit"]["anomalies"]
    assert a3["type"] == "backward"
    assert a3["previous_sequence"] == 32768
    assert a3["sequence"] == 32767


def test_two_bit_ring_has_no_gap_class(client):
    register(
        client,
        protocol_id="twobit-seq",
        fields=[
            {"name": "seq", "type": "u8", "offset": 0, "bit_offset": 0, "bit_width": 2},
            {"name": "addr", "type": "u8", "offset": 1},
            {"name": "temp", "type": "s16", "offset": 2, "scale": "0.01"},
            {"name": "state", "type": "u8", "offset": 5, "bit_offset": 0, "bit_width": 3},
            {"name": "alarm", "type": "u8", "offset": 5, "bit_offset": 3, "bit_width": 1},
        ],
    )
    sid = make_session(client, protocol_id="twobit-seq", audit=True)["session_id"]

    # 0 -> 2 is exactly half a turn (2 out of 4): unknown direction.
    d = append(client, sid, 0, frame(0) + frame(2)).json()
    assert [a["type"] for a in d["sequence_audit"]["anomalies"]] == ["direction_unknown"]

    # 2 -> 1 is one step backwards (forward delta 3 > 2).
    d2 = append(client, sid, 2 * len(frame(0)), frame(1)).json()
    assert [a["type"] for a in d2["sequence_audit"]["anomalies"]] == ["backward"]


# --------------------------------------------------------- bad frames --------


def test_bad_crc_frame_does_not_move_baseline(client):
    register(client)
    sid = make_session(client, audit=True)["session_id"]

    good = frame(10)
    bad = corrupt_crc(frame(11))  # rejected candidate, never becomes a frame
    nxt = frame(11)
    chunk = good + bad + nxt

    d = append(client, sid, 0, chunk).json()
    # Only two valid frames were confirmed (10 and 11): the bad CRC frame in
    # between is invisible to the audit and the step 10 -> 11 is clean.
    assert [f["sequence"] for f in d["frames"]] == [10, 11]
    assert d["sequence_audit"]["anomalies"] == []
    assert d["sequence_audit"]["last_sequence"] == 11
    assert d["sequence_audit"]["counts"] == dict(ZERO_COUNTS)
    assert len(d["discarded"]) >= 1  # the rejected bytes were still reported


def test_pending_tail_does_not_move_baseline(client):
    register(client)
    sid = make_session(client, audit=True)["session_id"]

    f1 = frame(10)
    # Chunk ends inside the next (eventually seq-12) frame: half stays pending.
    f2 = frame(12)
    cut = len(f1) + 4
    d1 = append(client, sid, 0, f1 + f2[:4]).json()
    assert [f["sequence"] for f in d1["frames"]] == [10]
    assert d1["sequence_audit"]["last_sequence"] == 10
    assert d1["sequence_audit"]["anomalies"] == []
    assert d1["sequence_audit"]["counts"] == dict(ZERO_COUNTS)

    # Completing the frame now reveals the gap once, on this append.
    d2 = append(client, sid, cut, f2[4:]).json()
    (a,) = d2["sequence_audit"]["anomalies"]
    assert a["type"] == "suspected_gap"
    assert (a["previous_sequence"], a["sequence"]) == (10, 12)
    assert a["missing_count"] == 1
    assert a["frame_offset"] == len(f1)
    assert d2["sequence_audit"]["last_sequence"] == 12


# --------------------------------------------------------- across chunks ------


def test_baseline_and_counts_carry_across_chunks(client):
    register(client)
    sid = make_session(client, audit=True)["session_id"]

    fl = len(frame(0))
    d1 = append(client, sid, 0, frame(0) + frame(2)).json()  # gap of 1
    assert d1["sequence_audit"]["counts"]["suspected_gap"] == 1
    assert d1["sequence_audit"]["last_sequence"] == 2

    d2 = append(client, sid, 2 * fl, frame(2)).json()  # duplicate vs prior chunk
    assert [a["type"] for a in d2["sequence_audit"]["anomalies"]] == ["duplicate"]
    assert d2["sequence_audit"]["counts"]["suspected_gap"] == 1
    assert d2["sequence_audit"]["counts"]["duplicate"] == 1
    assert d2["sequence_audit"]["last_sequence"] == 2

    p = client.get(f"/sessions/{sid}").json()["sequence_audit"]
    assert p["last_sequence"] == 2
    assert p["counts"] == {
        "duplicate": 1,
        "suspected_gap": 1,
        "backward": 0,
        "direction_unknown": 0,
    }


def test_frame_split_across_chunks_audited_once(client):
    register(client)
    sid = make_session(client, audit=True)["session_id"]

    f1 = frame(0)
    f2 = frame(3)  # gap: 1, 2 missing
    cut = len(f1) + 4
    d1 = append(client, sid, 0, f1 + f2[:4]).json()
    assert d1["sequence_audit"]["anomalies"] == []

    d2 = append(client, sid, cut, f2[4:]).json()
    (a,) = d2["sequence_audit"]["anomalies"]
    assert a["type"] == "suspected_gap"
    assert a["missing_count"] == 2
    assert d2["sequence_audit"]["counts"]["suspected_gap"] == 1


# --------------------------------------------------------- retry / failure ---


def test_retry_replays_audit_without_double_counting(client):
    register(client)
    sid = make_session(client, audit=True)["session_id"]
    chunk = frame(0) + frame(2)

    d1 = append(client, sid, 0, chunk).json()
    assert d1["replayed"] is False
    assert d1["sequence_audit"]["counts"]["suspected_gap"] == 1

    d2 = append(client, sid, 0, chunk).json()
    assert d2["replayed"] is True
    assert d2["sequence_audit"] == d1["sequence_audit"]

    p = client.get(f"/sessions/{sid}").json()["sequence_audit"]
    assert p["last_sequence"] == 2
    assert p["counts"]["suspected_gap"] == 1


def test_conflict_does_not_change_audit_state(client):
    register(client)
    sid = make_session(client, audit=True)["session_id"]
    chunk = frame(0) + frame(2)
    append(client, sid, 0, chunk)

    # Same offset, different content: rejected, audit untouched.
    r = append(client, sid, 0, frame(5))
    assert r.status_code == 409
    p = client.get(f"/sessions/{sid}").json()["sequence_audit"]
    assert p["last_sequence"] == 2
    assert p["counts"]["suspected_gap"] == 1

    # A legitimate continuation still works from the committed baseline.
    fl = len(frame(0))
    d = append(client, sid, 2 * fl, frame(3)).json()
    assert d["sequence_audit"]["anomalies"] == []
    assert d["sequence_audit"]["last_sequence"] == 3


def test_decode_failure_neither_advances_baseline_nor_counts(client):
    register(client)
    sid = make_session(client, audit=True)["session_id"]

    d0 = append(client, sid, 0, frame(10)).json()
    assert d0["sequence_audit"]["last_sequence"] == 10

    # Ambiguous stream: hard 422, session state (incl. audit) stays put.
    ambiguous = bytes.fromhex("67aa55710aaa550c7845ca08aab4be")
    r = append(client, sid, len(frame(10)), ambiguous)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "AMBIGUOUS_FRAME"

    p = client.get(f"/sessions/{sid}").json()["sequence_audit"]
    assert p["last_sequence"] == 10
    assert p["counts"] == dict(ZERO_COUNTS)

    # Retrying at the same offset with the real next frame still compares
    # against the preserved baseline (10).
    d = append(client, sid, len(frame(10)), frame(11)).json()
    assert d["sequence_audit"]["anomalies"] == []
    assert d["sequence_audit"]["last_sequence"] == 11


def test_oversize_append_does_not_change_audit_state(db_path):
    with TestClient(create_app(Settings(db_path=db_path, max_decode_bytes=10))) as c:
        register(c)
        sid = make_session(c, audit=True)["session_id"]
        append(c, sid, 0, frame(0)[:9])  # 9 bytes, pending tail only

        r = append(c, sid, 9, frame(0))  # tail + chunk over the limit
        assert r.status_code == 413
        p = c.get(f"/sessions/{sid}").json()
        assert p["received_offset"] == 9
        assert p["sequence_audit"]["last_sequence"] is None
        assert p["sequence_audit"]["counts"] == dict(ZERO_COUNTS)


# --------------------------------------------------------- restart -----------


def test_audit_state_persists_across_restart(db_path):
    with TestClient(create_app(Settings(db_path=db_path, max_decode_bytes=1 << 20))) as c:
        register(c)
        sid = make_session(c, audit=True)["session_id"]
        fl = len(frame(0))
        d1 = append(c, sid, 0, frame(0) + frame(2)).json()
        assert d1["sequence_audit"]["counts"]["suspected_gap"] == 1
        last_off = 2 * fl

    # Brand-new app on the same database file: baseline and counts continue.
    with TestClient(create_app(Settings(db_path=db_path, max_decode_bytes=1 << 20))) as c:
        p = c.get(f"/sessions/{sid}").json()["sequence_audit"]
        assert p["last_sequence"] == 2
        assert p["counts"]["suspected_gap"] == 1

        # Replaying the last chunk returns the stored audit result verbatim.
        replay = append(c, sid, 0, frame(0) + frame(2)).json()
        assert replay["replayed"] is True
        assert replay["sequence_audit"]["counts"]["suspected_gap"] == 1

        # Continuing compares against the persisted baseline (2).
        d2 = append(c, sid, last_off, frame(2)).json()
        assert [a["type"] for a in d2["sequence_audit"]["anomalies"]] == ["duplicate"]
        assert d2["sequence_audit"]["counts"]["suspected_gap"] == 1
        assert d2["sequence_audit"]["counts"]["duplicate"] == 1


def test_old_database_sessions_migrate_and_audit_still_works(db_path):
    # Build a database file exactly as an older release would: protocols
    # table plus the pre-audit sessions schema, with one live session whose
    # last chunk has already been accepted (two identical frames).
    chunk = frame(1) + frame(1)
    stored_response = {
        "session_id": "oldsess0000000000000000000001",
        "protocol_id": "sensor-line",
        "version": 1,
        "received_offset": 0,
        "next_offset": len(chunk),
        "frames": [],
        "discarded": [],
        "tail": {"start": len(chunk), "end": len(chunk), "payload_b64": ""},
        "frame_count": 2,
        "frames_total": 2,
        "replayed": False,
    }
    conn = sqlite3.connect(db_path)
    conn.executescript(        """
        CREATE TABLE protocols (
            protocol_id TEXT NOT NULL, version INTEGER NOT NULL,
            definition TEXT NOT NULL, definition_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (protocol_id, version)
        );
        CREATE TABLE decode_cache (
            protocol_id TEXT NOT NULL, version INTEGER NOT NULL,
            input_sha256 TEXT NOT NULL, result TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (protocol_id, version, input_sha256)
        );
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY, protocol_id TEXT NOT NULL,
            version INTEGER NOT NULL, received_offset INTEGER NOT NULL,
            tail_b64 TEXT NOT NULL, frames_total INTEGER NOT NULL DEFAULT 0,
            last_chunk_offset INTEGER, last_chunk_sha256 TEXT,
            last_response TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    conn.execute(
        "INSERT INTO protocols (protocol_id, version, definition, definition_sha256) "
        "VALUES (?, 1, ?, ?)",
        (
            "sensor-line",
            json.dumps(standard_protocol()),
            hashlib.sha256(b"old-def").hexdigest(),
        ),
    )
    conn.execute(
        "INSERT INTO sessions "
        "(session_id, protocol_id, version, received_offset, tail_b64, frames_total, "
        " last_chunk_offset, last_chunk_sha256, last_response) "
        "VALUES (?, 'sensor-line', 1, ?, '', 2, 0, ?, ?)",
        (
            stored_response["session_id"],
            len(chunk),
            hashlib.sha256(chunk).hexdigest(),
            json.dumps(stored_response),
        ),
    )
    conn.commit()
    conn.close()

    # A new app instance opens the old file: the schema migration adds the
    # audit columns, the old session stays non-audited and still replays.
    with TestClient(create_app(Settings(db_path=db_path, max_decode_bytes=1 << 20))) as c:
        p_old = c.get("/sessions/oldsess0000000000000000000001").json()
        assert p_old["sequence_audit"] is None
        assert p_old["frames_total"] == 2
        assert p_old["received_offset"] == len(chunk)

        replay = append(c, "oldsess0000000000000000000001", 0, chunk).json()
        assert replay["replayed"] is True
        assert replay["frames_total"] == 2

        # A freshly created audited session works on the migrated database.
        sid_new = make_session(c, audit=True)["session_id"]
        d = append(c, sid_new, 0, frame(0) + frame(2)).json()
        assert d["sequence_audit"]["counts"]["suspected_gap"] == 1


# --------------------------------------------------------- concurrency -------


def _make_instance(db_path):
    app = create_app(Settings(db_path=db_path, max_decode_bytes=1 << 20))
    return app, TestClient(app)


def _gate_first_session_read(app, barrier):
    db = app.state.db
    real_get_session = db.get_session
    gated = False

    def get_session(session_id):
        nonlocal gated
        row = real_get_session(session_id)
        if not gated:
            gated = True
            barrier.wait(timeout=10)
        return row

    db.get_session = get_session


def test_two_instances_race_identical_audited_chunks_count_once(db_path):
    app_a, client_a = _make_instance(db_path)
    app_b, client_b = _make_instance(db_path)
    with client_a, client_b:
        register(client_a)
        sid = make_session(client_a, audit=True)["session_id"]
        chunk = frame(0) + frame(2)  # one suspected gap

        barrier = threading.Barrier(2)
        _gate_first_session_read(app_a, barrier)
        _gate_first_session_read(app_b, barrier)

        with ThreadPoolExecutor(max_workers=2) as pool:
            fa = pool.submit(append, client_a, sid, 0, chunk)
            fb = pool.submit(append, client_b, sid, 0, chunk)
            ra, rb = fa.result(), fb.result()

        assert ra.status_code == rb.status_code == 200
        bodies = [ra.json(), rb.json()]
        assert sorted(b["replayed"] for b in bodies) == [False, True]
        assert all(
            b["sequence_audit"]["counts"]["suspected_gap"] == 1 for b in bodies
        )
        assert all(b["sequence_audit"]["last_sequence"] == 2 for b in bodies)

        p = client_b.get(f"/sessions/{sid}").json()["sequence_audit"]
        assert p["counts"]["suspected_gap"] == 1
        assert p["last_sequence"] == 2


def test_two_instances_race_different_audited_chunks_loser_counts_nothing(db_path):
    app_a, client_a = _make_instance(db_path)
    app_b, client_b = _make_instance(db_path)
    with client_a, client_b:
        register(client_a)
        sid = make_session(client_a, audit=True)["session_id"]

        chunk_a = frame(0) + frame(2)  # gap anomaly
        chunk_b = frame(0) + frame(1)  # clean
        barrier = threading.Barrier(2)
        _gate_first_session_read(app_a, barrier)
        _gate_first_session_read(app_b, barrier)

        with ThreadPoolExecutor(max_workers=2) as pool:
            fa = pool.submit(append, client_a, sid, 0, chunk_a)
            fb = pool.submit(append, client_b, sid, 0, chunk_b)
            ra, rb = fa.result(), fb.result()

        assert sorted(r.status_code for r in (ra, rb)) == [200, 409]
        winner = ra if ra.status_code == 200 else rb
        loser = rb if winner is ra else ra
        assert loser.json()["error"]["details"]["reason"] == "content_mismatch"

        p = client_a.get(f"/sessions/{sid}").json()["sequence_audit"]
        # The committed audit state matches exactly the winner's response.
        assert p["last_sequence"] == winner.json()["sequence_audit"]["last_sequence"]
        assert p["counts"] == winner.json()["sequence_audit"]["counts"]
