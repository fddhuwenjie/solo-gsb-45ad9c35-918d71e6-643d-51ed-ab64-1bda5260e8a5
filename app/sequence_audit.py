"""Optional sequence-number continuity audit for collection sessions.

A session may opt into the audit at creation time; the pinned protocol must
then declare an unsigned integer ``sequence_field`` whose effective bit width
is at least two bits (full-byte unsigned fields qualify, as do unsigned
bit-fields that are at least two bits wide).

Every successfully append compares only the frames newly confirmed by that
append against the last confirmed sequence number. Sequence numbers are
compared modulo ``2**width``:

* equal to the previous number          -> ``duplicate``
* forward by more than one and less
  than half a turn                      -> ``suspected_gap`` (+ missing count)
* forward by more than half a turn      -> ``backward``
* exactly half a turn                   -> ``direction_unknown``

Only ``suspected_gap`` infers missing frames; ``backward`` and
``direction_unknown`` never do. Every valid frame, including anomalous ones,
becomes the baseline for the frame that follows it. Bad frames (rejected
candidates) and the unresolved pending tail never move the baseline, because
they are never handed to :func:`audit_new_frames` at all.
"""
from __future__ import annotations

from .errors import SequenceAuditUnsupportedError
from .models import FieldSpec, ProtocolDefinition

# Anomaly type names.
DUPLICATE = "duplicate"
SUSPECTED_GAP = "suspected_gap"
BACKWARD = "backward"
DIRECTION_UNKNOWN = "direction_unknown"

# A frame's frame_offset points at its first raw byte (session coordinates).


def sequence_field_width(proto: ProtocolDefinition) -> int:
    """Effective unsigned bit width of the protocol's sequence field.

    Raises ``SEQUENCE_AUDIT_UNSUPPORTED`` (structured 422) when the protocol
    declares no sequence field, the field is a signed integer type, or the
    effective width is below two bits.
    """
    name = proto.sequence_field
    fields_by_name = {f.name: f for f in proto.fields_}
    field: FieldSpec | None = fields_by_name.get(name) if name is not None else None
    if field is None:
        raise SequenceAuditUnsupportedError(
            message=(
                f"protocol '{proto.protocol_id}' version {proto.version} declares no "
                "sequence_field usable for continuity auditing"
            ),
            details={
                "reason": "missing_sequence_field",
                "protocol_id": proto.protocol_id,
                "version": proto.version,
            },
        )

    signed_type = field.type.startswith("s")
    if signed_type or field.signed:
        raise SequenceAuditUnsupportedError(
            message=(
                f"sequence_field '{name}' is a signed integer type ({field.type}); "
                "continuity auditing requires an unsigned field"
            ),
            details={
                "reason": "signed_sequence_field",
                "sequence_field": name,
                "type": field.type,
            },
        )

    width = field.bit_width if field.bit_width is not None else field.size_bytes * 8
    if width < 2:
        raise SequenceAuditUnsupportedError(
            message=(
                f"sequence_field '{name}' is only {width} bit wide; continuity "
                "auditing requires an effective width of at least 2 bits"
            ),
            details={
                "reason": "sequence_field_too_narrow",
                "sequence_field": name,
                "bit_width": width,
            },
        )
    return width


def classify(previous: int, current: int, width: int) -> tuple[str, int | None]:
    """Classify the modular step ``previous -> current`` on a width-bit ring.

    Returns ``(type, missing_count)``; ``missing_count`` is only set for a
    suspected gap.
    """
    modulus = 1 << width
    half = modulus >> 1
    delta = (current - previous) % modulus
    if delta == 0:
        return DUPLICATE, None
    if delta == half:
        return DIRECTION_UNKNOWN, None
    if delta < half:
        if delta == 1:
            return "", None
        return SUSPECTED_GAP, delta - 1
    return BACKWARD, None


def audit_new_frames(
    new_frames: list[dict],
    *,
    last_sequence: int | None,
    width: int,
    totals: dict[str, int],
) -> tuple[list[dict], int | None, dict[str, int]]:
    """Audit only frames newly confirmed by one append.

    ``last_sequence`` is the baseline carried over from earlier appends (None
    until the first valid frame arrives). ``totals`` maps anomaly type to the
    session-wide count accumulated so far and is updated in place.

    Returns ``(anomalies, new_last_sequence, new_totals)``. Each valid frame
    becomes the next baseline, including frames that participate in an
    anomaly; frames with no sequence value are skipped and leave the baseline
    untouched.
    """
    anomalies: list[dict] = []
    previous = last_sequence
    for frame in new_frames:
        seq = frame.get("sequence")
        if seq is None:
            continue
        if previous is not None:
            kind, missing = classify(previous, int(seq), width)
            if kind:
                totals[kind] = totals.get(kind, 0) + 1
                entry = {
                    "frame_offset": frame["start"],
                    "frame_end": frame["end"],
                    "previous_sequence": previous,
                    "sequence": int(seq),
                    "type": kind,
                }
                if missing is not None:
                    entry["missing_count"] = missing
                anomalies.append(entry)
        previous = int(seq)
    return anomalies, previous, totals
