# FlowControlPort (FCP) — pseudocode view of src/models/flow_control_port.py
#
# Independent bus peer of DataPort. Emits DRQ + guards + tails.
# Active only in flow modes RX_CONTROLLED or ASYNC (drq_enabled on parent DP).
# DRQ direction is inverted relative to DP data direction:
#   Sink DP → DRQ is SOURCE (DP sends DRQ onto bus)
#   Source DP → DRQ is SINK  (DP receives DRQ from bus)
# Collisions between DP and FCP on the same column are surfaced by the bus model's
# SAME_DEVICE clash detector — no arbitration here.
#
# This file is NOT executable. It parses as Python for editor syntax highlighting,
# but identifiers (registers, device methods, state fields, dp.*) are undefined here.
# Pyright exclusion at repo root silences the undefined-name warnings.


# --- Config (FCP-prefixed registers) ---
# FCP_HorizontalStart, FCP_BitWidth, FCP_TailWidth, FCP_Offset,
# FCP_GuardEnable, FCP_GuardPolarity


# --- State ---
# column, row_in_interval,
# drq_sent, wide_bit_remaining, stored_wide_bit_slot,
# guard_pending, tail_remaining


# --- Top-level ---

def initialize():
    # state ← initialized to idle-start values
    advance_interval()


def clock_tick():
    # 1) Wide-bit replay — a prior DRQ holds the bus for FCP_BitWidth more UIs.
    if stored_wide_bit_slot is not None:
        advance_wide_bit()
        advance_column()
        return

    # 2) Fresh DRQ trigger.
    if (dp.drq_enabled
            and not dp.interval_skipped
            and not drq_sent
            and row_in_interval == FCP_Offset
            and column == FCP_HorizontalStart):
        # Build a slot the engine can see via _derive_fcp_bit_slot for the replay UIs.
        slot = BitSlot(type=DRQ,
                       direction=SINK if dp.is_source else SOURCE)   # inverted from DP
        arm_drq_replay(slot)
        advance_column()
        return

    # 3) Post-DRQ drain (source-DRQ only — guard_pending / tail_remaining stay at
    #    defaults when DRQ is sink, so these branches are no-ops in that case).
    if guard_pending:
        guard_pending = False
    elif tail_remaining > 0:
        tail_remaining -= 1
    advance_column()


def arm_drq_replay(slot):
    # Latch fresh DRQ: mark sent, prime post-data state, stash slot for wide-bit
    # replay, advance one tick for this emission.
    drq_sent = True
    prime_post_data()
    wide_bit_remaining = FCP_BitWidth
    stored_wide_bit_slot = slot
    advance_wide_bit()


def prime_post_data():
    # DRQ is SOURCE iff DP is SINK. Only source DRQ emits guard+tail.
    guard_pending = False
    tail_remaining = 0
    if dp.is_source:
        return
    if FCP_GuardEnable:
        guard_pending = True
    tail_remaining = FCP_TailWidth


def advance_column():
    column += 1
    if column >= device.num_columns:
        advance_row()


def advance_row():
    column = 0
    # Post-data emission doesn't survive row wraps.
    guard_pending = False
    tail_remaining = 0
    # Wide-bit replay doesn't survive row wraps.
    wide_bit_remaining = 0
    stored_wide_bit_slot = None
    row_in_interval += 1
    if row_in_interval > dp.Interval:
        row_in_interval = 0
        advance_interval()


def advance_interval():
    reset_transport()


def reset_transport():
    drq_sent = False
    wide_bit_remaining = 0
    stored_wide_bit_slot = None


def advance_wide_bit():
    # Terminal: FCP's wide-bit is a one-shot replay, not part of a counter cascade
    # (unlike DP's advance_wide_bit which cascades to advance_bit_in_channel).
    wide_bit_remaining -= 1
    if wide_bit_remaining < 0:
        stored_wide_bit_slot = None
