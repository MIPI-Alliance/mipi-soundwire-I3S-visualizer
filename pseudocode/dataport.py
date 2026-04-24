# DataPort — SWI3S Data Port algorithm (pseudocode view of src/models/dataport.py)
#
# Counter cascade:
#   wide_bit → bit_in_channel → channel → sample → channel_group → transport completion
# Transport phases: ACTIVE, SPACING, ROW_DONE, PATTERN_DONE
# Normal vs SRI:
#   Normal — one transport per SSP interval; channel groups structure the burst/space pattern
#   SRI    — multiple transports per row
#
# This file is NOT executable. It parses as Python for editor syntax highlighting,
# but all identifiers (registers, device methods, state fields) are undefined here.
# Pyright exclusion at repo root silences the undefined-name warnings.


# --- Config (registers) ---
# EnableCh                       16-bit bitmask of enabled channels (setter invalidates channel cache)
# ChannelGrouping, Spacing, SampleSize, SampleGrouping, Interval,
# SkippingNumerator, Offset, HorizontalStart, HorizontalCount, TailWidth,
# BitWidth, PortDirection, GuardEnable, GuardPolarity, SubRowInterval,
# FlowMode, PortMode, ScramblerEn


# --- Derived from config ---
num_channels = popcount(EnableCh)                                  # cached until EnableCh changes
enabled_channels = tuple(i for i in range(16) if EnableCh & (1 << i))
horizontal_end = HorizontalStart + HorizontalCount
is_source = not PortDirection                                       # PortDirection False=source, True=sink
txp_enabled = FlowMode in (TX_CONTROLLED, ASYNC)
drq_enabled = FlowMode in (RX_CONTROLLED, ASYNC)
effective_channel_grouping = ChannelGrouping if 0 < ChannelGrouping <= num_channels else num_channels

def channel(index):
    return enabled_channels[index]


# --- State (cascade + post-data drain) ---
# column, row_in_interval, transport_phase, interval_skipped, skipping_accumulator,
# sample_in_group, samples_in_group_remaining,
# channel_index, channel_group_base_channel, channels_in_group_remaining,
# bit_in_channel, wide_bit_remaining,
# spacing_slots_remaining,
# guard_pending, tail_remaining,
# txp_pending


# --- Top-level ---

def initialize():
    # state ← initialized from registers and derived values
    advance_interval()                      # arms first transport via skipping latch + reset_transport


def clock_tick():
    in_transport_window = (
        num_channels > 0
        and not interval_skipped
        and transport_phase not in (ROW_DONE, PATTERN_DONE)
        and row_in_interval >= Offset
        and column >= HorizontalStart
    )

    if in_transport_window:
        if column > horizontal_end:
            spacing_slots_remaining = 0
            transport_phase = ROW_DONE
        elif transport_phase == SPACING:
            spacing_slots_remaining -= 1
            if spacing_slots_remaining <= 0:
                transport_phase = ACTIVE
        else:
            # Owned slot — DATA or TX_PRESENT. Write/read the bus, then advance cascade.
            if is_source:
                if wide_bit_remaining == BitWidth:               # first UI of wide bit
                    device.write_txp() if txp_pending else device.write_data_bit_from_fifo()
                else:
                    device.write_held_bit()
            elif wide_bit_remaining == 0:                         # last UI: sink samples
                device.read_txp() if txp_pending else device.read_data_bit_to_fifo()
            # Sink mid-UIs: source drives the bus; sink samples only on the last UI.

            advance_wide_bit()
            prime_post_data()
            advance_column()
            return

    # Not owned (gated, window-exhausted, or SPACING) — drain one post-data slot.
    drain_post_data()
    advance_column()


def drain_post_data():
    if guard_pending:
        device.write_guard1() if GuardPolarity else device.write_guard0()
        guard_pending = False
    elif tail_remaining > 0:
        device.write_tail() if tail_remaining == TailWidth else device.write_held_bit()
        tail_remaining -= 1


def prime_post_data():
    # Primed after each owned source-port slot; drained on subsequent not-owned columns.
    guard_pending = False
    tail_remaining = 0
    if not is_source:
        return
    if GuardEnable:
        guard_pending = True
    tail_remaining = TailWidth


def advance_column():
    column += 1
    if column >= device.num_columns:
        advance_row()


def advance_row():
    column = 0
    # Post-data emission doesn't survive row wraps.
    guard_pending = False
    tail_remaining = 0
    # Row-cut resume: pattern didn't complete last row → fresh row, flip phase back to ACTIVE.
    # Applies to non-SRI multi-row transports and SRI mid-pattern cuts.
    if transport_phase == ROW_DONE:
        transport_phase = ACTIVE
    row_in_interval += 1
    if row_in_interval > Interval:
        row_in_interval = 0
        advance_interval()


def advance_interval():
    interval_skipped = advance_skipping()
    reset_transport()


def advance_skipping():
    # Returns True iff this interval should be skipped (Payload Interval Skipping algo).
    if SkippingNumerator == 0:
        return False
    skipping_accumulator += SkippingNumerator
    if skipping_accumulator < SkippingDenominator:
        return False
    skipping_accumulator -= SkippingDenominator
    return True


def reset_transport():
    transport_phase = ACTIVE
    spacing_slots_remaining = 0
    sample_in_group = 0
    samples_in_group_remaining = SampleGrouping
    channel_group_base_channel = 0
    channel_index = 0
    channels_in_group_remaining = effective_channel_grouping - 1
    bit_in_channel = SampleSize
    wide_bit_remaining = BitWidth
    txp_pending = txp_enabled


# --- Counter cascade ---

def advance_wide_bit():
    # wide_bit_remaining counts UIs within a single DATA or TX_PRESENT wide bit.
    wide_bit_remaining -= 1
    if wide_bit_remaining < 0:
        wide_bit_remaining = BitWidth
        advance_bit_in_channel()


def advance_bit_in_channel():
    # First exhausts the TX_PRESENT slot (if any), then SampleSize DATA bits.
    if txp_pending:
        txp_pending = False
        return
    bit_in_channel -= 1
    if bit_in_channel < 0:
        bit_in_channel = SampleSize
        advance_channel()


def advance_channel():
    channel_index += 1
    channels_in_group_remaining -= 1
    txp_pending = txp_enabled                        # each new channel re-arms the TX_PRESENT slot
    if channels_in_group_remaining < 0:
        # Group exhausted → next sample (retargets channel_index back to group base).
        channel_index = channel_group_base_channel
        channels_in_group_remaining = effective_channel_grouping - 1
        advance_sample()


def advance_sample():
    sample_in_group += 1
    samples_in_group_remaining -= 1
    if samples_in_group_remaining < 0:
        sample_in_group = 0
        samples_in_group_remaining = SampleGrouping
        advance_channel_group()


def advance_channel_group():
    channel_group_size = effective_channel_grouping
    transport_pattern_complete = (channel_group_base_channel + channel_group_size >= num_channels)

    if transport_pattern_complete:
        if SubRowInterval:
            # SRI mid-row transport rollover. reset_transport() arms phase=ACTIVE;
            # the gap block below may overwrite it to SPACING/ROW_DONE.
            reset_transport()
        else:
            transport_phase = PATTERN_DONE
            return
    else:
        # Next CG within same transport. Cascade left inner counters (bit_in_channel,
        # wide_bit_remaining, txp_pending, sample_in_group, samples_in_group_remaining)
        # at fresh-transport values. Retarget CG-scope counters at the new group.
        channel_group_base_channel += channel_group_size
        remaining_channels = num_channels - channel_group_base_channel
        if remaining_channels > channel_group_size:
            remaining_channels = channel_group_size
        channels_in_group_remaining = remaining_channels - 1
        channel_index = channel_group_base_channel

    # Inter-group gap applies to: SRI (both transport-complete and next-CG paths) and
    # non-SRI next-CG. Non-SRI transport-complete has already returned above.
    if SubRowInterval or not transport_pattern_complete:
        if Spacing == 0:
            transport_phase = ROW_DONE               # SRI ends row here; next row's advance_interval arms fresh transport
        else:
            spacing_slots_remaining = Spacing - 1
            transport_phase = SPACING if Spacing > 1 else ACTIVE
