# `registers.json` — SWI3S peripheral register map

Machine-readable register map for the SWI3S peripheral register space. It is the
**single source of truth** for SWI3S Studio's register-map view and for resolving
WriteA32/ReadA32 addresses to register + field names.

## Provenance

- **Spec:** MIPI SoundWire I3S (SWI3S) Specification v1.1r06 (2026-05-03).
- **Source tables:** 163, 164, 166, 167, 168, 169, 170, 171 (§15.2.1–15.2.5) for
  SLC/DP; 173/174 (CDS transport), 176/177 (PHY1/PHY2), 178/179/180 (PHY3);
  `PM_Action` values from Table 20 (§5.1.3).
- **Extracted via** Raven, library `@swi3s-specification`, thread
  `39b06b5c-93f8-4f49-8a88-e6255fa6b366` (reuse this thread for follow-ups).

## Schema

```
_meta:   { spec, source_tables[], source_sections[], conventions[] }
blocks:  [ { name, base_offset, instances, registers: [
             { offset, abs_address, name, reset, access, dual_ranked,
               curr_offset?, curr_abs_address?,         # when dual_ranked
               fields: [ { name, bits, reset, access, valid?, excess1?, description } ] }
         ] } ]
```

Blocks present: **SLC** (base `0x1000`, one per device, device ID / interrupts /
link control — `WakeControl`, `PM`/`PM_Action` — / dual-ranked geometry),
**CDS** (base `0x1100`, control-data-stream transport: drive type, bit width,
guard/tail), **PHY1** (`0x100`) / **PHY2** (`0x200`) / **PHY3** (`0x300`)
electrical + calibration registers, and **DP** (base `0x2000 + 0x100·n`, n=0–31,
incl. the `FCP_*` registers).

A field's optional `valid` string of `value=Name` tokens (e.g. PM_Action
`0x4=Enter_Dormant`, CDS_DriveType `0=Special…,1=Normal…`) is parsed into a
value→name **enum** so the register view and command table show symbolic values
(`PM_Action=Enter_Dormant`) instead of raw numbers. PHY *selection* (PHY1/2/3) is
**not** register-visible (there is no `ActivePHY` field) — it is set by the Cold
Start physical-layer signalling on the raw DP/DN lines (spec §5.1.2). That signal
*is* observable at the start of a capture, so the active PHY is recovered there by
`swi3s_studio/analysis/link_control.py` (not from the CDS or a register read).

## Conventions (from `_meta.conventions`)

- Offsets are within **one** peripheral; the target device is the 12-bit Phase
  Header **Device Mask**, not the address. Every peripheral has the identical map.
- `dual_ranked: true` → listed offset is `_NEXT`; `_CURR` is at `offset + 0x40`
  (`curr_offset`/`curr_abs_address`). Commit (SSCR/DSCR) copies `_NEXT→_CURR`; a
  direct `_CURR` write also copies to `_NEXT`. `_NEXT` cleared only by Cold Reset.
- `excess1: true` → actual = stored + 1 (`SampleSize`, `SampleGrouping`, `Interval`,
  `HorizontalCount`, `BitWidth`, `ConverterClockDiv`; `NumColumns`: ColumnCount =
  NumColumns + 1). Our placement engine already computes inclusive window ends as
  `HorizontalStart + HorizontalCount`, which is correct for the excess-1 field.
- Endianness: SLC `ManufacturerID`/`PartID`/`SkippingDenominator` are MSB-first;
  DP `Interval`/`Offset`/`ConverterClockDiv`/`SkippingNumerator` and `FCP_Offset`
  are LSB-first.

## Caveats to honour when wiring up the model

1. **Seed reset values, not zero.** Several fields reset to non-zero — notably
   **`PortControl.ScramblerEn` (DP `0x0B[3]`) resets to 1**, and SLC geometry
   (`NumColumns`, `RowRateRange`) has PHY-dependent resets. The RegisterMap model
   must initialize each field from its `reset` here; the wire-snoop only overlays
   *observed* writes on top. (The C++ snoop path returns 0 for unseen registers —
   fine for the plugin, but Studio's "default vs written" coloring needs the real
   resets from this file.)
2. **`EnableCh0`/`PrepareCh0` are not with the other channel enables** — they live
   in DP `0x81` bits [7]/[6] (sharing the byte with `HorizontalCount[4:0]`), while
   `EnableCh1–7`/`8–15` are at `0x90`/`0x91`. A classic address→field decode trap.
3. **`FlowMode` (DP `0x0E[1:0]`) rank is ambiguous in the spec** — Table 166 lists
   it single-ranked, Table 168 lists access "Dual". This file represents it
   single-ranked at `0x0E` (no `_CURR`). Verify against silicon/DisCo before
   treating `0x4E` as `FlowMode_CURR`.
4. **Collapsed arrays.** Large repeating arrays (`IntStat_SDCA[63:0]` `0x40`–`0x47`,
   `IntStat_ImpDef[31:0]` `0x4C`–`0x4F`, the SDCA/ImpDef enables, `EC_TestFailCh<c>`
   `0x20`–`0x2F`) are single range-entries with a bit-mapping description rather
   than one object per byte — expand programmatically if the UI needs per-byte rows.

Regenerate / extend: reuse the Raven thread above against `@swi3s-specification`.
