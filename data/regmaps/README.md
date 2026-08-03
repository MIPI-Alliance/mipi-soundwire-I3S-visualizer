# Peripheral register maps

Vendor (chip-specific) register maps for the peripherals on the bus, in SWI3S
Studio's **normalized JSON** format. Registers here live in the device-defined
address space (`0x10000000–0x3FFFFFFF` local) — outside the SWI3S spec registers
in `../registers.json`.

## Using a map

In SWI3S Studio: **Registers ▸ Import Peripheral Register Map…**, then either

- browse to a vendor file (e.g. Cirrus-style `.xml`) — the import assistant
  normalizes it (coalescing indexed bits, slicing per-field resets, reporting
  anything that looked off), or
- browse to a normalized `.json` in this folder.

Pick the target device and accept. The map is decoded from the same captured
writes and shown as a *Peripheral Registers* block under that device in the
Register Map pane. The chosen map is embedded in the saved workspace, so a
shared workspace carries its register maps with it.

## Format

```json
{
  "_meta": { "schema": "swi3s-studio-regmap/1", "name": "<map name>" },
  "registers": [
    {
      "name": "AMP_LEVEL",
      "address": "0x10000007",      // absolute 32-bit address (byte register)
      "reset": "0x0A",
      "access": "RW",
      "fields": [
        { "name": "amp_lvl", "bits": "[5:0]", "reset": 10, "access": "RW" }
      ]
    }
  ]
}
```

`example_amp.json` is a small synthetic map showing the shape — not a real
device. Drop your own normalized maps here, or import a vendor file directly.
