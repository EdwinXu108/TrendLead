"""VOO snapshot selection with explicit availability time (no future leakage)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .dataio import Inputs, SnapshotMeta, SnapshotMember


@dataclass
class SnapshotChoice:
    snapshot: SnapshotMeta | None
    members: list[SnapshotMember]
    reason: str


def availability_instant(snap: SnapshotMeta, inputs: Inputs) -> datetime:
    """Instant from which this snapshot may influence signals."""
    return snap.available_at


def choose_snapshot(inputs: Inputs, signal_at: datetime) -> SnapshotChoice:
    """Pick latest holdings_as_of, then latest revision known at signal_at.

    retrieved_at is ignored for historical availability.
    """
    usable: list[SnapshotMeta] = []
    for snap in inputs.snapshots:
        inst = availability_instant(snap, inputs)
        # holdings_as_of is a calendar date; treat as the date itself not after signal day.
        if snap.holdings_as_of.isoformat() > signal_at.isoformat()[:10]:
            # holdings date is after the signal calendar day -> future information
            continue
        if inst > signal_at:
            continue
        usable.append(snap)

    if not usable:
        return SnapshotChoice(snapshot=None, members=[], reason="no_available_snapshot")

    usable.sort(key=lambda s: (s.holdings_as_of, s.available_at, s.snapshot_id))
    best = usable[-1]
    # Prefer the latest revision among the same holdings_as_of already selected.
    same = [s for s in usable if s.holdings_as_of == best.holdings_as_of]
    same.sort(key=lambda s: (s.available_at, s.snapshot_id))
    chosen = same[-1]

    members = [m for m in inputs.members if m.snapshot_id == chosen.snapshot_id]
    members.sort(key=lambda m: m.security_id)
    return SnapshotChoice(
        snapshot=chosen,
        members=members,
        reason=f"selected:{chosen.snapshot_id}",
    )
