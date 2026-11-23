"""Community-inspired L5 scheduling extensions for labshift (Ideas #07 and #08)."""

from __future__ import annotations

from community_extensions.night_shift_batcher import (
    DualRegimePackerResult,
    ProtocolRequest,
    ScheduledProtocolSlot,
    pack_dual_regime_72h,
    pack_fifo_human_gated_72h,
    run_benchmark as run_night_shift_benchmark,
    verify_dual_regime_schedule,
)
from community_extensions.rolling_contingency_scheduler import (
    ContingencyRepairResult,
    FaultSpec,
    PlateJobSpec,
    ScheduledPlateOp,
    WaitWindowLink,
    naive_fifo_delay_reschedule,
    rolling_contingency_repair,
    run_benchmark as run_contingency_benchmark,
    verify_contingency_schedule,
    verify_schedule,
)

__all__ = [
    "ContingencyRepairResult",
    "DualRegimePackerResult",
    "FaultSpec",
    "PlateJobSpec",
    "ProtocolRequest",
    "ScheduledPlateOp",
    "ScheduledProtocolSlot",
    "WaitWindowLink",
    "naive_fifo_delay_reschedule",
    "pack_dual_regime_72h",
    "pack_fifo_human_gated_72h",
    "rolling_contingency_repair",
    "run_contingency_benchmark",
    "run_night_shift_benchmark",
    "verify_contingency_schedule",
    "verify_dual_regime_schedule",
    "verify_schedule",
]
