"""Round-trip test for the .sal exporter: export_sal(capture) then
saleae_sal.load_capture(...) must reconstruct the same Capture exactly.

Run: python3 -m pytest tests/test_sal_export.py -q
"""
import json
import os
import struct
import tempfile
import zipfile

import numpy as np
import swi3score

from swi3s_studio.ingest import sal_export, saleae_binary, saleae_sal
from swi3s_studio.ingest.transitions import build_capture_from_levels


def _assert_captures_equal(cap, cap2):
    np.testing.assert_array_equal(cap2.clock_edges, cap.clock_edges)
    np.testing.assert_array_equal(cap2.data_edges, cap.data_edges)
    assert bool(cap2.initial_clock) == bool(cap.initial_clock)
    assert bool(cap2.initial_data) == bool(cap.initial_data)
    assert int(cap2.sample_rate_hz) == int(cap.sample_rate_hz)


def test_sal_export_roundtrip():
    cap = build_capture_from_levels(swi3score.make_demo_levels(64))
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "export.sal")
        sal_export.export_sal(cap, p, clock_channel=0, data_channel=1)

        info = saleae_sal.read_info(p)
        assert info.sample_rate_hz == cap.sample_rate_hz
        assert info.channels == [0, 1]

        cap2 = saleae_sal.load_capture(p, clock_channel=0, data_channel=1)
    _assert_captures_equal(cap, cap2)


def test_sal_export_roundtrip_custom_channels():
    # clock/data need not be channels 0/1 -- exercise a non-default mapping and
    # auto_clock (which shouldn't need to swap anything since the caller already
    # named the correct channel as clock).
    cap = build_capture_from_levels(swi3score.make_demo_levels(64))
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "export_custom.sal")
        sal_export.export_sal(cap, p, clock_channel=3, data_channel=5)

        info = saleae_sal.read_info(p)
        assert info.channels == [3, 5]

        cap2 = saleae_sal.load_capture(p, clock_channel=3, data_channel=5, auto_clock=True)
    _assert_captures_equal(cap, cap2)


def test_sal_export_rejects_same_channel():
    cap = build_capture_from_levels(swi3score.make_demo_levels(8))
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "bad.sal")
        try:
            sal_export.export_sal(cap, p, clock_channel=2, data_channel=2)
            assert False, "expected ValueError"
        except ValueError:
            pass


def test_sal_export_is_logic2_v3():
    """Logic 2 refuses a .sal whose blobs are the documented version-0 export
    ("an older version … could not be opened"); it opens only the version-3
    internal format + a meta.json with version 22. Pin the writer to that layout
    (reverse-engineered from a real Logic Pro 16 capture, see docs/saleae_sal_format.md)
    so it can't silently regress to v0."""
    cap = build_capture_from_levels(swi3score.make_demo_levels(64))
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "v3.sal")
        sal_export.export_sal(cap, p, clock_channel=0, data_channel=1)
        with zipfile.ZipFile(p) as z:
            meta = json.loads(z.read("meta.json"))
            d0 = z.read("digital-0.bin")
            d1 = z.read("digital-1.bin")
            names = set(z.namelist())
            trig = z.read("trigger-store.bin")

    assert meta["version"] == 22
    # Logic requires a trigger-store member or the project won't load ("Failed to load
    # file"); it's a fixed 32-byte <SALEAE> v3 type-103 blob, identical across real
    # captures.
    assert "trigger-store.bin" in names
    assert trig == bytes.fromhex(
        "3c53414c4541453e030000006700000001000000000000000000000000000000")
    assert meta["data"]["legacySettings"]["sampleRate"]["digital"] == cap.sample_rate_hz
    # Logic 2 validates meta.json against a schema before opening — it needs the full
    # set of data.* keys a real v22 capture has (verified across real Logic Pro 16
    # .sal files). Missing any of these is the "file schema is invalid" rejection.
    required = {"renderViewState", "captureStartTime", "timingMarkers", "measurements",
                "highLevelAnalyzers", "analyzers", "rowsSettings", "captureSettings",
                "legacyDevice", "legacySettings", "digitalTriggerTime", "name",
                "dataTable", "analyzerTrigger", "timeManager", "captureNotes"}
    assert required <= set(meta["data"]), \
        f"meta.json missing required data keys: {sorted(required - set(meta['data']))}"
    # the capture's sample rate must be offered in the device's rate menu (Logic
    # cross-checks legacySettings.sampleRate against capabilities.sampleRateOptions)
    opts = {o["digital"] for o in meta["data"]["legacyDevice"]["capabilities"]["sampleRateOptions"]}
    assert cap.sample_rate_hz in opts
    # binData maps each blob to its device channel (what saleae_sal.read_info reads).
    assert [(b["deviceChannel"], b["file"]) for b in meta["binData"]] == \
        [(0, "./digital-0.bin"), (1, "./digital-1.bin")]

    ends = []
    for blob, init, edges in ((d0, cap.initial_clock, cap.clock_edges),
                              (d1, cap.initial_data, cap.data_edges)):
        assert blob[:8] == saleae_binary.SALEAE_MAGIC
        version, ctype = struct.unpack_from("<II", blob, 8)
        assert version == saleae_binary.V3_VERSION       # 3, not the old 0
        assert ctype == saleae_binary.V3_TYPE_DIGITAL     # 100 (digital)
        assert blob[16] == 1                              # constant flag
        assert struct.unpack_from("<d", blob, 17)[0] == float(cap.sample_rate_hz)
        # 10-byte preamble [u8 flag=0, u64 block_count×256, u8=0] then block 0 at 51.
        flag, count_field, term = struct.unpack_from("<BQB", blob, 41)
        assert flag == 0 and term == 0
        assert count_field > 0 and count_field % 256 == 0   # block_count encoded ×256
        nblocks = count_field // 256
        # walk the block chain: it must start at sample 0, chain (A==prev B), have
        # exactly `nblocks` blocks, and block 0's level is the channel's initial state.
        o, prev_b, seen, last_b = 51, None, 0, 0
        while o + 26 <= len(blob):
            a, b, level, cnt = struct.unpack_from("<QQHQ", blob, o)
            if prev_b is None:
                assert a == 0 and level == int(bool(init))
            else:
                assert a == prev_b
            prev_b = b
            last_b = b
            seen += 1
            o += 26 + cnt
        assert o == len(blob) and seen == nblocks         # blocks tile the blob exactly
        ends.append(last_b)
        # blob decodes back to the exact edges (v3 stores integer deltas -> lossless)
        got = saleae_binary.parse_channel_v3(blob)
        np.testing.assert_array_equal(got.transition_samples.astype(np.int64),
                                      np.asarray(edges, dtype=np.int64))
        assert bool(got.initial_state) == bool(init)
    # both channels share one capture-end sample (last block B_end)
    assert ends[0] == ends[1]
