// pybind11 bindings for the SWI3S decode core (module `swi3score`).

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <pybind11/numpy.h>

#include <algorithm>          // std::copy
#include <limits>             // decode_saleae_v3_deltas overflow bound
#include <memory>             // pyOwner()
#include <stdexcept>          // requireAudio() throws

#include "ISampleSource.h"
#include "TransitionSampleSource.h"
#include "DlvSampleSource.h"
#include "Decoder.h"
#include "Demo.h"
#include "C8b10bDecoder.h"
#include "CCommandTransportParser.h"
#include "CCrc16.h"
#include "CRegisterModel.h"
#include "SosFilter.h"

namespace py = pybind11;
using namespace swi3score;

static py::dict commandToDict(const CommandRec& c)
{
    py::dict d;
    d["command"] = c.command;
    d["phase"] = c.phase;
    d["header_valid"] = c.headerValid;
    d["device_mask"] = c.deviceMask;
    d["has_manager_packet"] = c.hasManagerPacket;
    d["packet_length"] = c.packetLength;
    d["opcode"] = c.opcode;
    d["has_address"] = c.hasAddress;
    d["address"] = c.address;
    d["data"] = py::bytes(reinterpret_cast<const char*>(c.data.data()), c.data.size());
    d["crc_valid"] = c.crcValid;
    d["peripheral_response"] = c.peripheralResponse;
    d["manager_response"] = c.managerResponse;
    d["is_commit"] = c.isCommit;
    d["commit_confirmed"] = c.commitConfirmed;
    d["has_sync_point"] = c.hasSyncPoint;
    d["row_delay"] = c.rowDelay;
    d["group_mask"] = c.groupMask;
    if (c.hasPingStatus) {
        py::list ps;
        for (int i = 0; i < 12; ++i) ps.append(c.pingStatus[i]);
        d["ping_status"] = ps;
    } else {
        d["ping_status"] = py::none();
    }
    d["has_read_data"] = c.hasReadData;
    d["read_data"] = py::bytes(reinterpret_cast<const char*>(c.readData.data()), c.readData.size());
    d["read_data_crc_valid"] = c.readDataCrcValid;
    // ERROR: a WriteA32 to an EnableCh _CURR (0xC1/0xD0/0xD1) register address while the
    // addressed port's Interval != 0 (Interval > 1 Row) — see CommandRec::enablechCurrError.
    d["enablech_curr_error"] = c.enablechCurrError;
    // A single-ranked control register of a running port changed (a programming error,
    // not a protocol error) — see CommandRec::runningPortWriteError.
    d["running_port_write_error"] = c.runningPortWriteError;
    // A write covered a read-only byte (a programming error) — see
    // CommandRec::readOnlyWriteError.
    d["read_only_write_error"] = c.readOnlyWriteError;
    // ERROR: this command's SSP landed while a skipping port was part-way through its skip
    // pattern (Section 9.1.6.2.1) — see CommandRec::unexpectedSspError.
    d["unexpected_ssp_error"] = c.unexpectedSspError;
    d["bus_row"] = c.busRow;
    d["effective_row"] = c.effectiveRow;   // SSP row a confirmed sync-commit takes effect (-1 otherwise)
    d["start_sample"] = c.startSample;
    d["end_sample"] = c.endSample;
    return d;
}

static py::dict gridCellToDict(const GridCell& c)
{
    py::dict d;
    d["row"] = c.row;
    d["col"] = c.col;
    d["dp"] = c.dp;
    d["device"] = c.device;
    d["channel"] = c.channel;
    d["slot"] = c.slot;
    d["sample_here"] = c.sampleHere;
    d["is_cds"] = c.isCds;
    d["is_source"] = c.isSource;
    d["sample"] = c.sample;
    d["bit"] = c.bit;
    d["scrambler"] = c.scramblerEn;
    d["port_mode"] = c.portMode;
    return d;
}

// Serialize a decoded config to visualizer-CSV-ready fields (BusConfig attr names).
// Shared by decoder.config_dataports() (final config) and config_from_commands()
// (as-of-cursor config), so both export identically.
static py::dict configToDict(const SwI3sConfig& cfg)
{
    py::dict out;
    out["num_columns"] = cfg.NumColumns;
    out["skipping_denominator"] = cfg.SkippingDenominator;
    out["phy3_enabled"] = cfg.PHY3Enabled;
    out["row_rate_khz"] = cfg.RowRateKHz;
    out["description"] = cfg.description;
    // Control Data Stream. Exported under the BusConfig attribute names so a decoded
    // capture round-trips into an authoring CSV with its CDS settings intact — the other
    // half of registersFromConfig, which writes them.
    //
    // THE MANAGER SLOT (index 0) IS ALWAYS THE DEFAULT on this path. Only peripherals have
    // an addressable CDS block, so nothing on the wire reveals how the Manager drives the
    // CDS; see CRegisterModel::BuildConfig.
    out["cds_bit_width"] = cfg.CdsBitWidth;
    out["cds_guard"] = py::cast(cfg.CdsGuard);
    out["cds_tail"] = py::cast(cfg.CdsTailWidth);
    out["cds_drive_type"] = py::cast(cfg.CdsDriveType);
    out["cds_end_drive_early"] = py::cast(cfg.CdsEndDriveEarly);
    py::list dps;
    for (const auto& dp : cfg.dps) {
        py::dict e;
        e["enabled"] = dp.Enabled;
        e["device_number"] = dp.deviceNum;
        e["dp_number"] = dp.dpNumber;
        e["enable_ch"] = dp.EnableCh;
        e["channel_grouping"] = dp.ChannelGrouping;
        e["spacing"] = dp.Spacing;
        e["sample_size"] = dp.SampleSize;
        e["sample_grouping"] = dp.SampleGrouping;
        e["interval"] = dp.Interval;
        e["skipping_numerator"] = dp.SkippingNumerator;
        e["offset"] = dp.Offset;
        e["horizontal_start"] = dp.HorizontalStart;
        e["horizontal_count"] = dp.HorizontalCount;
        e["tail_width"] = dp.TailWidth;
        e["bit_width"] = dp.BitWidth;
        e["port_direction"] = dp.PortDirection;
        e["guard_enable"] = dp.GuardEnable;
        e["guard_polarity"] = dp.GuardPolarity;
        e["sub_row_interval"] = dp.SubRowInterval;
        e["flow_mode"] = dp.FlowMode;
        e["flow_control_delay"] = dp.FlowControlDelay;
        e["port_mode"] = dp.PortMode;
        e["scrambler_en"] = dp.ScramblerEn;
        e["fcp_horizontal_start"] = dp.FCP_HorizontalStart;
        e["fcp_bit_width"] = dp.FCP_BitWidth;
        e["fcp_tail_width"] = dp.FCP_TailWidth;
        e["fcp_offset"] = dp.FCP_Offset;
        e["fcp_guard_enable"] = dp.FCP_GuardEnable;
        e["fcp_guard_polarity"] = dp.FCP_GuardPolarity;
        e["fcp_scrambler_en"] = dp.FCP_ScramblerEn;
        dps.append(e);
    }
    out["dataports"] = dps;
    return out;
}

static py::dict audioToDict(const AudioRec& a)
{    py::dict d;
    d["dp"] = a.dp;
    d["device"] = a.deviceNum;
    d["channel"] = a.channel;
    d["value"] = a.value;
    d["sample_size"] = a.sampleSize;
    d["index"] = a.index;
    d["start_sample"] = a.startSample;
    d["end_sample"] = a.endSample;
    d["flow_mode"] = a.flowMode;
    d["drq_sample"] = a.drqSample;
    return d;
}

// Reading the decoded audio after release_audio() dropped it is a caller bug. Say so:
// handing back an empty list would be indistinguishable from a capture that carried no
// audio at all, which is a silent wrong answer rather than a loud one.
static void requireAudio(const Decoder& d)
{
    if (d.audioReleased())
        throw std::runtime_error(
            "swi3score: the decoded audio was released (release_audio) after being copied "
            "out. Read the copy — Session.audio_columns() / Session.audio — not the decoder.");
}

// An opaque owner handle for a C++ object that views a Python object's buffer: holds a
// reference to `obj` and drops it, under the GIL, when the last copy of the handle goes.
static std::shared_ptr<const void> pyOwner(py::object obj)
{
    auto* held = new py::object(std::move(obj));
    return std::shared_ptr<const void>(held, [](const void* p) {
        py::gil_scoped_acquire gil;
        delete static_cast<const py::object*>(p);
    });
}

PYBIND11_MODULE(swi3score, m)
{
    m.doc() = "SWI3S decode core: PHY1/PHY2 forwarded-clock (FBCSE) and PHY3 (DLV) bus decode.";

    // ABI version of the Python<->native interface. Bump whenever a binding's
    // signature / return shape changes in a way the Python side depends on, or when an
    // older module would silently ignore a new setting or behaviour the Python side
    // relies on. swi3s_studio checks this at decode time and asks the user to rebuild if
    // the loaded .so is stale. A purely additive binding the Python side reaches with
    // getattr is not a bump.
    m.attr("score_abi") = 18;
    // Protocol constants the Python side repeats (session._MIN_COLUMNS/_MAX_COLUMNS,
    // dlv_detect.DLV_CDS_COLUMN and MIN_UI_SAMPLES); the tests pin each copy to these.
    m.attr("min_column_count") = swi3s::kMinColumnCount;
    m.attr("max_hub_depth") = swi3s::kMaxHubDepth;      // a hub depth above it reads as it
    m.attr("max_grid_rows") = kMaxGridRows;
    m.attr("max_demo_samples") = kMaxDemoSamplesPerChannel;   // every make_*_levels length
    m.attr("max_column_count") = swi3s::kMaxColumnCount;
    m.attr("dlv_cds_column") = swi3s::kDlvCdsColumn;   // PHY3 CDS_HorizontalStart reset value
    m.attr("dlv_min_ui_samples") = DlvSampleSource::kMinUiSamples;

    py::class_<ISampleSource>(m, "ISampleSource");

    py::class_<MemorySampleSource, ISampleSource>(m, "MemorySampleSource")
        .def(py::init<std::vector<bool>, std::uint64_t, std::uint32_t>(),
             py::arg("levels"), py::arg("sample_rate_hz"), py::arg("samples_per_ui") = 4,
             "In-memory per-UI data-level source for tests/demos.");

    py::class_<TransitionSampleSource, ISampleSource>(m, "TransitionSampleSource")
        .def(py::init([](py::array_t<std::uint64_t, py::array::c_style> clockEdges,
                         py::array_t<std::uint64_t, py::array::c_style> dataEdges,
                         bool initialClock, bool initialData, std::uint64_t sampleRateHz) {
                 // The source views the buffers of the arrays the casters produced: the
                 // caller's own arrays when they are already C-contiguous uint64 (zero
                 // copy), otherwise converted copies that exist only in these parameters.
                 // The casters convert only what numpy casts to uint64 safely; a signed or
                 // floating-point array does not match this overload and raises TypeError.
                 // Both are handed to the source as its owner, so whichever buffers it
                 // views live exactly as long as it does.
                 const std::uint64_t* clock = clockEdges.data();
                 const std::uint64_t* data = dataEdges.data();
                 const auto clockN = static_cast<std::size_t>(clockEdges.size());
                 const auto dataN = static_cast<std::size_t>(dataEdges.size());
                 return new TransitionSampleSource(
                     clock, clockN, data, dataN, initialClock, initialData, sampleRateHz,
                     pyOwner(py::make_tuple(std::move(clockEdges), std::move(dataEdges))));
             }),
             py::arg("clock_edges"), py::arg("data_edges"),
             py::arg("initial_clock"), py::arg("initial_data"), py::arg("sample_rate_hz"),
             "Capture-backed source: clock/data transition arrays (uint64; viewed without a "
             "copy when C-contiguous uint64; a strided view, a list or a narrower unsigned "
             "dtype is converted once; a signed or floating-point dtype raises TypeError).");

    py::class_<DlvSampleSource, ISampleSource>(m, "DlvSampleSource")
        .def(py::init([](py::array_t<std::uint64_t, py::array::c_style> dpEdges,
                         bool initialDp, std::uint64_t sampleRateHz, int initialColumns,
                         double nominalUiSamples) {
                 // DLV: one differential signal; only the DP-wire edges are needed (DN is
                 // its complement). The source views the buffer of the array the caster
                 // produced (the caller's own when it is already C-contiguous uint64,
                 // otherwise a converted copy) and holds that array as its owner, as
                 // TransitionSampleSource does, with the same dtype rule.
                 const std::uint64_t* dp = dpEdges.data();
                 const auto dpN = static_cast<std::size_t>(dpEdges.size());
                 return new DlvSampleSource(
                     dp, dpN, initialDp, sampleRateHz, initialColumns, nominalUiSamples,
                     pyOwner(std::move(dpEdges)));
             }),
             py::arg("dp_edges"), py::arg("initial_dp"), py::arg("sample_rate_hz"),
             py::arg("initial_columns"), py::arg("nominal_ui_samples") = 0.0,
             "PHY3 (DLV) source: recovers the bit clock from the DP-wire edges (uint64; viewed "
             "without a copy when C-contiguous uint64; a strided view, a list or a narrower "
             "unsigned dtype is converted once; a signed or floating-point dtype raises "
             "TypeError) via a virtual "
             "DLL locked to the Sync1 row edges, sampling mid-UI. initial_columns = Safe-Lock "
             "count (outside 2..32: the cold-start count); the decoder pushes NumColumns "
             "changes. nominal_ui_samples seeds the loop's "
             "free-running UI estimate for the initial geometry (>0 = robust acquisition; the "
             "aperture tracks the once-per-row reference instead of the first two edges). "
             "Each later bring-up (DecoderSettings.restart_points) locks the loop again at its "
             "first Row Sync, at the seeded row period over its Safe-Lock count. The loop "
             "needs at least one sample per UI and the source ends where it has fewer; where "
             "the next rising edge is more than 64 rows past the predicted Row Sync the loop "
             "relocks at that edge, or ends when none remains and the capture runs on further.")
        .def("recovered_row_syncs", [](const DlvSampleSource& s) {
                 const auto& v = s.RecoveredRowSyncs();
                 return py::array_t<std::uint64_t>(
                     static_cast<py::ssize_t>(v.size()), v.data());
             }, "Recovered per-row Sync1 sample points (the bit-clock reference) collected "
                "during decode — for the Raw Capture recovered-clock overlay.")
        .def("recovered_ui_samples", &DlvSampleSource::RecoveredUiSamples,
             "The recovered UI period in capture samples (row period / column count).");

    py::class_<DecoderSettings>(m, "DecoderSettings")
        .def(py::init<>())
        .def_readwrite("forced_column_count", &DecoderSettings::forcedColumnCount)
        .def_readwrite("decode_audio", &DecoderSettings::decodeAudio)
        .def_readwrite("dlv", &DecoderSettings::dlv,
                       "PHY3 (DLV) mode: no NRZS, CDS at cds_horizontal_start, recovered clock.")
        .def_readwrite("cds_horizontal_start", &DecoderSettings::cdsHorizontalStart,
                       "DLV CDS column (CDS_HorizontalStart, e.g. 2 in Safe-Lock-4); a value "
                       "outside 1..columns-2 reads at the reset value 2. Ignored for FBCSE.")
        .def_readwrite("config_csv_path", &DecoderSettings::configCsvPath)
        .def_readwrite("scrambler_overrides", &DecoderSettings::scramblerOverrides,
                       "Per-dataport scrambler override: list of (device, dp, on) "
                       "tuples; present entries force ScramblerEn for that (device, "
                       "dp), absent ones keep the snooped/CSV value.")
        .def_readwrite("hub_depth_overrides", &DecoderSettings::hubDepthOverrides,
                       "Per-device hub depth: list of (device, depth 0..5) pairs. A "
                       "peripheral behind N hub levels has its response delayed by "
                       "2*N frame rows; the decoder reads its response token that "
                       "many CDS bits later. Absent devices = depth 0.")
        .def_readwrite("collect_bit_samples", &DecoderSettings::collectBitSamples,
                       "Diagnostic: collect a per-data-bit sample point (see "
                       "Decoder.bit_samples()). Off by default — it stores one "
                       "record per assembled data bit.")
        .def_readwrite("bit_sample_start", &DecoderSettings::bitSampleStart,
                       "Windowed bit-sample collection: only store bit points at "
                       "capture sample >= this (0 = from the start). Keeps the bounded "
                       "collection near the region being viewed on a dense capture.")
        .def_readwrite("register_overrides", &DecoderSettings::registerOverrides,
                       "What-if register overrides: list of (section_index, device, "
                       "address, value) tuples. Forced into the register model for "
                       "the matching config section only (section index from "
                       "segments()), so the same override drives the audio decode as "
                       "well as the grid + register map.")
        .def_readwrite("forced_column_sections", &DecoderSettings::forcedColumnSections,
                       "Region-scoped forced column count: list of (section_index, "
                       "column_count) pairs, section index keying segments() as "
                       "register_overrides does. Pins ONE config region's bus geometry "
                       "over the snooped NumColumns and the blind column detector — for "
                       "an imported config CSV whose width the wire never carried, or a "
                       "mis-locked detector. Per-region because forcing one width over a "
                       "capture that genuinely reconfigures misframes the other regions. "
                       "Out-of-range counts (outside 2..32) are ignored.")
        .def_readwrite("restart_points", &DecoderSettings::restartPoints,
                       "Later link bring-ups: list of (sample, column_count, cold, "
                       "inactive_from) per start sequence after a stopped clock, the first "
                       "bring-up excluded; any order, points at one sample merged. From "
                       "inactive_from (the first sample after the clock stopped; 0 = none) "
                       "to sample (the bring-up's first audio-mode clock edge) nothing is "
                       "decoded and every data port stops. At sample the decode restarts "
                       "as at the start of the capture: it re-aligns to the next rising "
                       "clock edge, frames at column_count (the Safe-Lock count, 2..32; "
                       "otherwise a Cold Start frames at the cold-start count and a Warm "
                       "Start keeps the current one), reconfigures the payload engine on "
                       "the new Column-0 phase and opens a new segment. cold (a Cold "
                       "Start) also resets the registers, stopping every data port until "
                       "configured again. A config CSV keeps its ports, and on FBCSE its "
                       "column count. With dlv the recovered clock locks again at the first "
                       "Row Sync at or after sample, at the nominal UI of column_count, and "
                       "is not run through the inactive stretch. Assign a whole list: like "
                       "every list field here, reading it returns a copy, so appending to "
                       "settings.restart_points changes nothing.")
        .def_readwrite("resume_points", &DecoderSettings::resumePoints,
                       "Clock pauses with no bring-up after them: list of the first clock "
                       "edge after each stop. Audio mode never pauses its clock, so samples "
                       "were lost or the capture was joined there. At each the decode checks "
                       "a fresh window's Column-0 phase at the current column count: in "
                       "phase it decodes on as if nothing happened; out of phase it re-frames "
                       "there, its payload engine on the new phase, in a new segment that is "
                       "no restart (geometry and registers kept). A restart point at the same "
                       "sample wins. FBCSE only: ignored when dlv, where a pause leaves no "
                       "row edge to find the row phase from. Assign a whole list.")
        .def_readwrite("imported_config_sections", &DecoderSettings::importedConfigSections,
                       "Section indices (keying segments()) whose payload config is "
                       "built from their register_overrides alone, not from the snooped "
                       "registers: a config CSV imported into that region. The wire "
                       "still frames and parses its commands.")
        .def_readwrite("ssp_row", &DecoderSettings::sspRow,
                       "Manual Stream Sync Point: force row_in_interval==0 to this bus "
                       "row (-1 = off). A post-commit capture has no SSPA/SSCR, so ports "
                       "with interval>1 decode at an arbitrary phase (1/N rows correct); "
                       "set the row and re-decode until the audio is clean. Any row — the "
                       "phase is anchored from the start, so the whole capture decodes to "
                       "that SSP.");

    py::class_<Decoder>(m, "Decoder")
        .def(py::init<ISampleSource&, const DecoderSettings&>(),
             py::arg("source"), py::arg("settings"),
             py::keep_alive<1, 2>())   // keep the source alive while the decoder lives
        .def("run", &Decoder::run, py::call_guard<py::gil_scoped_release>(),
             "Decode the whole source. Runs once per Decoder; a second call raises "
             "RuntimeError.")
        // commands() does NOT release the GIL, unlike audio_columns() / bit_samples() /
        // bit_samples_window() below: those release around a fill loop that is pure C++
        // over raw buffers, while this loop CREATES PYTHON OBJECTS (a dict per command),
        // which requires the GIL.
        .def("commands", [](const Decoder& d) {
            py::list out;
            for (const auto& c : d.commands()) out.append(commandToDict(c));
            return out;
        })
        .def("audio", [](const Decoder& d) {
            requireAudio(d);
            py::list out;
            for (const auto& a : d.audio()) out.append(audioToDict(a));
            return out;
        })
        .def("release_audio", [](Decoder& d) { d.releaseAudio(); },
             "Drop the decoded audio samples and return their memory (~72 B/sample). Call "
             "AFTER copying them out with audio_columns(): every consumer downstream reads "
             "that copy, and a re-decode builds a new Decoder. Irreversible for this "
             "Decoder — audio()/audio_columns() then raise rather than report no audio.")
        .def_property_readonly("audio_count", [](const Decoder& d) { return d.audio().size(); },
             "Decoded audio samples held, without copying them out (the memory watchdog "
             "sizes the column copy with it before making it).")
        .def_property_readonly("audio_capacity",
             [](const Decoder& d) { return d.audio().capacity(); },
             "Audio sample records allocated (held plus reserved), ~72 B each.")
        .def("audio_released", [](const Decoder& d) { return d.audioReleased(); },
             "True once release_audio() has dropped the samples.")
        .def("audio_columns", [](const Decoder& d) {
            requireAudio(d);
            // Columnar (struct-of-arrays) audio: the same data as audio() but as
            // NumPy arrays instead of N per-sample dicts. For dense captures (mic
            // arrays decode tens of millions of samples) building N Python dicts
            // dominates load time; typed arrays avoid that and let the Python store
            // group with NumPy.
            const auto& a = d.audio();
            const py::ssize_t n = static_cast<py::ssize_t>(a.size());
            py::array_t<std::int32_t> device(n), dp(n), channel(n), sample_size(n), flow_mode(n);
            py::array_t<std::uint32_t> value(n);
            py::array_t<std::uint64_t> index(n), start_sample(n), end_sample(n), drq_sample(n);
            auto* pdev = device.mutable_data();
            auto* pdp = dp.mutable_data();
            auto* pch = channel.mutable_data();
            auto* pss = sample_size.mutable_data();
            auto* pval = value.mutable_data();
            auto* pidx = index.mutable_data();
            auto* pstart = start_sample.mutable_data();
            auto* pend = end_sample.mutable_data();
            auto* pfm = flow_mode.mutable_data();
            auto* pdrq = drq_sample.mutable_data();
            {
                // The fill loop is pure C++ over raw buffers (no Python objects), and
                // for dense mic-array captures it copies tens of millions of samples.
                // Release the GIL across it so the loader's worker thread doesn't freeze
                // the GUI (the busy spinner) while building the audio store.
                py::gil_scoped_release rel;
                for (py::ssize_t i = 0; i < n; ++i) {
                    const auto& r = a[static_cast<std::size_t>(i)];
                    pdev[i] = r.deviceNum;
                    pdp[i] = r.dp;
                    pch[i] = r.channel;
                    pss[i] = r.sampleSize;
                    pval[i] = static_cast<std::uint32_t>(r.value);
                    pidx[i] = r.index;
                    pstart[i] = r.startSample;
                    pend[i] = r.endSample;
                    pfm[i] = r.flowMode;
                    pdrq[i] = r.drqSample;
                }
            }
            py::dict out;
            out["device"] = device;
            out["dp"] = dp;
            out["channel"] = channel;
            out["sample_size"] = sample_size;
            out["value"] = value;
            out["index"] = index;
            out["start_sample"] = start_sample;
            out["end_sample"] = end_sample;
            out["flow_mode"] = flow_mode;
            out["drq_sample"] = drq_sample;
            return out;
        }, "Decoded audio as columnar NumPy arrays (device, dp, channel, "
           "sample_size, value, index, start_sample, end_sample, flow_mode, "
           "drq_sample) — the fast path for large captures; equivalent data to "
           "audio() without per-sample dicts.")
        .def("flow_control_stats", [](const Decoder& d) {
            const auto& s = d.flowControlStats();
            py::dict out;
            out["checked"] = s.checked;
            out["fails"] = s.fails;
            out["drq_bits"] = s.drqBits;
            return out;
        }, "Flow-control DRQ<->TxPresent handshake validation (SWI3S §14.2.2 {ASW5205}): "
           "{checked, fails, drq_bits}. fails==0 means every transported sample was "
           "correctly requested and the response timing was legal.")
        .def_property_readonly("column_count", &Decoder::columnCount)
        .def_property_readonly("row_rate_khz", &Decoder::rowRateKHz)
        .def_property_readonly("measured_ui_rate_hz", &Decoder::measuredUiRateHz)
        .def_property_readonly("progress_uis", &Decoder::progressUis,
            "UIs consumed so far by run() (n of N) — poll from another thread while "
            "run() is in flight (it releases the GIL) to drive a load-progress dialog.")
        .def_property_readonly("total_uis", &Decoder::totalUis,
            "Total UI count for this run: read from the source when run() starts, then "
            "refreshed every 64k UIs and at the end of run(), never below progress_uis. "
            "Exact for a forwarded-clock source; an estimate that follows the current UI "
            "period for a recovered-clock source. 0 if the source can't report one (pure "
            "streaming) — treat 0 as unknown, not done.")
        .def("request_stop", &Decoder::requestStop,
            "Ask an in-flight run() to stop (call from another thread; run() releases the "
            "GIL). It returns within 4096 UIs of reading, plus at most one detect window "
            "already read, with what it decoded so far.")
        .def_property_readonly("stopped", &Decoder::stopped,
            "True if the last run() returned early because of request_stop().")
        .def("cancel", &Decoder::cancel,
            "Cancel an in-flight run() (thread-safe: call from another thread while run() "
            "holds no GIL, or before run()). run() returns as request_stop() makes it "
            "return, and cancelled is then True; what it decoded is incomplete, for the "
            "caller to discard. A cancel before run() makes run() return without reading "
            "the source.")
        .def_property_readonly("cancelled", &Decoder::cancelled,
            "True if the last run() returned early because of cancel(). False when the "
            "run completed, including a cancel that arrived after its last poll.")
        .def_property_readonly("progress", &Decoder::progress,
            "progress_uis / total_uis in [0, 1], or 0.0 if total_uis is unknown (0).")
        .def("segments", [](const Decoder& d) {
            py::list out;
            for (const auto& s : d.segments()) {
                py::dict e;
                e["start_ui"] = s.startUi;
                e["start_sample"] = s.startSample;
                e["column_count"] = s.columnCount;
                e["row_base"] = s.rowBase;
                e["restart"] = s.restart;
                e["cold_start"] = s.coldStart;
                e["cds_column"] = s.cdsColumn;
                out.append(e);
            }
            return out;
        }, "Decode segments (one per column count, PHY3 CDS column or link restart): "
           "each has start_ui (the UI of its first CDS bit), start_sample, column_count, "
           "row_base (cumulative bus row at the segment start), restart (opened by a "
           "later link bring-up, see restart_points), cold_start (that bring-up was a "
           "Cold Start) and cds_column (0 for FBCSE; the PHY3 CDS_HorizontalStart in "
           "effect). Bus rows are continuous across segments.")
        .def("bit_samples", [](const Decoder& d) {
            // Per-data-bit sample points (columnar), populated only when
            // settings.collect_bit_samples was set. Keys: sample, device, dp,
            // channel, is_start (MSB / start of a sample interval).
            const auto& b = d.bitSamples();
            const py::ssize_t n = static_cast<py::ssize_t>(b.size());
            py::array_t<std::uint64_t> sample(n);
            py::array_t<std::int32_t> device(n), dp(n), channel(n);
            py::array_t<std::uint8_t> is_start(n);
            auto* ps = sample.mutable_data();
            auto* pdev = device.mutable_data();
            auto* pdp = dp.mutable_data();
            auto* pch = channel.mutable_data();
            auto* pst = is_start.mutable_data();
            {
                py::gil_scoped_release rel;
                for (py::ssize_t i = 0; i < n; ++i) {
                    const auto& r = b[static_cast<std::size_t>(i)];
                    ps[i] = r.sample;
                    pdev[i] = r.deviceNum;
                    pdp[i] = r.dp;
                    pch[i] = r.channel;
                    pst[i] = r.isStart ? 1 : 0;
                }
            }
            py::dict out;
            out["sample"] = sample;
            out["device"] = device;
            out["dp"] = dp;
            out["channel"] = channel;
            out["is_start"] = is_start;
            return out;
        }, "Per-data-bit sample points (needs settings.collect_bit_samples): "
           "columnar sample/device/dp/channel/is_start.")
        .def("bit_samples_window", [](Decoder& d, std::uint64_t startUi,
                                      std::uint64_t endUi) {
            // Windowed per-data-bit sample points for source-UI range [start_ui, end_ui]
            // WITHOUT re-decoding the whole capture (restores the nearest transport
            // checkpoint recorded during run() and ticks the schedule forward). Same
            // columnar shape as bit_samples(); the Raw Capture overlay calls this on
            // demand per visible window instead of a full re-decode.
            std::vector<BitSample> b;
            {
                py::gil_scoped_release rel;
                b = d.bitSamplesInWindow(startUi, endUi);
            }
            const py::ssize_t n = static_cast<py::ssize_t>(b.size());
            py::array_t<std::uint64_t> sample(n);
            py::array_t<std::int32_t> device(n), dp(n), channel(n);
            py::array_t<std::uint8_t> is_start(n);
            auto* ps = sample.mutable_data();
            auto* pdev = device.mutable_data();
            auto* pdp = dp.mutable_data();
            auto* pch = channel.mutable_data();
            auto* pst = is_start.mutable_data();
            {
                // Fill the raw array buffers with the GIL released (no Python touched),
                // matching bit_samples() — the arrays are already allocated above.
                py::gil_scoped_release rel;
                for (py::ssize_t i = 0; i < n; ++i) {
                    const auto& r = b[static_cast<std::size_t>(i)];
                    ps[i] = r.sample;
                    pdev[i] = r.deviceNum;
                    pdp[i] = r.dp;
                    pch[i] = r.channel;
                    pst[i] = r.isStart ? 1 : 0;
                }
            }
            py::dict out;
            out["sample"] = sample;
            out["device"] = device;
            out["dp"] = dp;
            out["channel"] = channel;
            out["is_start"] = is_start;
            return out;
        }, py::arg("start_ui"), py::arg("end_ui"),
           "Windowed per-data-bit sample points for source-UI range [start_ui, end_ui] "
           "without a full re-decode: columnar sample/device/dp/channel/is_start. Needs a "
           "seekable source and settings.decode_audio (else empty); a window starting past "
           "the capture's last UI is empty. This is the on-demand "
           "path the Raw Capture 'Port Samples' overlay uses per visible window.")
        .def("grid_cells", [](const Decoder& d, int rows) {
            py::list out;
            for (const auto& c : d.gridLayout(rows)) out.append(gridCellToDict(c));
            return out;
        }, py::arg("rows") = 32, "2D bus-grid layout for the final config.")
        .def("row_sync_samples", [](const Decoder& d) {
            const auto& v = d.rowSyncSamples();
            return py::array_t<std::uint64_t>(
                static_cast<py::ssize_t>(v.size()), v.data());
        }, "DLV Row Sync Point samples (each Column-0 UI's sample), from the decoder's "
           "own framing — the authoritative row boundaries for the recovered-clock RSP "
           "marks. Empty for FBCSE.")
        .def("guessed_framing_samples", [](const Decoder& d) {
            const auto& v = d.guessedFramingSamples();
            return py::array_t<std::uint64_t>(
                static_cast<py::ssize_t>(v.size()), v.data());
        }, "Window-start samples where a known column count (forced, pinned, from a config "
           "CSV, or a restart's Safe-Lock count) was framed with no CRC-valid command to "
           "place Column 0 by: Column 0 is taken at the window's first UI, the UI after the "
           "alignment edge. Empty when every such framing was validated.")
        .def("audio_sample_rates", [](const Decoder& d) {
            py::dict out;
            for (const auto& t : d.audioSampleRates())
                out[py::make_tuple(std::get<0>(t), std::get<1>(t))] = std::get<2>(t);
            return out;
        }, "Per-dataport audio sample rate in Hz, keyed by (device, dp).")
        .def("config_registers", [](const Decoder& d) {
            py::list out;
            for (const auto& t : d.configRegisters())
                out.append(py::make_tuple(std::get<0>(t), std::get<1>(t), std::get<2>(t)));
            return out;
        }, "The decoder's effective config as (device, address, value) register "
           "writes — the comparison baseline (complete even for cold-start configs).")
        .def("committed_registers", [](const Decoder& d) {
            py::list out;
            for (const auto& t : d.committedRegisters())
                out.append(py::make_tuple(std::get<0>(t), std::get<1>(t), std::get<2>(t)));
            return out;
        }, "The decode authority's final committed (_CURR) register state as "
           "(device, address, value) tuples — the ground truth for parity-checking "
           "the display register model so the two can't silently diverge.")
        .def("config_dataports", [](const Decoder& d) {
            // The decoded (final) config as visualizer-CSV-ready fields, so the app can
            // export the analyzer's bus grid as a visualizer settings CSV.
            return configToDict(d.config());
        }, "Decoded config as visualizer-CSV fields: interface params + per-DP dicts.");

    m.attr("SLOT_NAMES") = py::make_tuple("Empty", "Data", "TxPresent",
                                          "Guard0", "Guard1", "Tail", "Drq");
    m.attr("SYMBOL_KINDS") = py::make_tuple("Invalid", "Comma", "RobustToken",
                                            "Dcode", "Kcode", "NoResponse");

    m.def("decode_symbols", [](ISampleSource& src, int columnCount, int maxSymbols,
                               std::uint64_t startUi, std::uint64_t rowOriginUi,
                               long rowBase, std::uint64_t endUi, bool dlv, int cdsColumn,
                               const std::vector<int>& hubDepths) {
        py::list out;
        for (const auto& s : decodeSymbols(src, columnCount, maxSymbols, startUi,
                                           rowOriginUi, rowBase, endUi, dlv, cdsColumn,
                                           hubDepths)) {
            py::dict d;
            d["start_sample"] = s.startSample;
            d["end_sample"] = s.endSample;
            d["row"] = s.row;
            d["raw"] = s.raw;
            d["kind"] = s.kind;
            d["value"] = s.value;
            d["aligned"] = s.aligned;
            out.append(d);
        }
        return out;
    }, py::arg("source"), py::arg("column_count"), py::arg("max_symbols") = 0,
       py::arg("start_ui") = 0, py::arg("row_origin_ui") = ~0ull, py::arg("row_base") = 0,
       py::arg("end_ui") = 0, py::arg("dlv") = false, py::arg("cds_column") = swi3s::kCdsColumn,
       py::arg("hub_depths") = std::vector<int>{},
       "Re-decode the CDS into classified 8b/10b symbols (optionally windowed). "
       "Pass row_origin_ui + row_base (a segment's Column-0 UI and cumulative row) "
       "to make symbol rows match the streaming decoder across segments. end_ui "
       "caps the decode at a segment boundary so it can't cross into a region with "
       "a different column width (0 = no cap). dlv=True reads the CDS as a plain-NRZ "
       "bit at cds_column (Column 2 for DLV) instead of NRZS at Column 0 — pass a "
       "DlvSampleSource as the source for the recovered-clock, mid-UI sampling. "
       "hub_depths (Device i's depth at index i) frames the symbols as the decode does: "
       "the 2*depth gap bits before a delayed device's response token are skipped.");

    m.def("grid_from_registers", [](const std::vector<std::tuple<int, std::uint32_t, int>>& writes,
                                    int fallbackColumns, int rows, int forceColumns,
                                    bool dlv, int cdsHorizontalStart) {
        std::vector<std::tuple<int, std::uint32_t, std::uint8_t>> w;
        w.reserve(writes.size());
        for (const auto& t : writes)
            w.emplace_back(std::get<0>(t), std::get<1>(t),
                           static_cast<std::uint8_t>(std::get<2>(t) & 0xFF));
        py::list out;
        for (const auto& c : gridFromRegisters(w, fallbackColumns, rows, forceColumns,
                                               dlv, cdsHorizontalStart))
            out.append(gridCellToDict(c));
        return out;
    }, py::arg("writes"), py::arg("fallback_columns") = 4, py::arg("rows") = 32,
       py::arg("force_columns") = 0, py::arg("dlv") = false,
       py::arg("cds_horizontal_start") = 0,
       "What-if grid layout from (device, address, value) register writes. "
       "force_columns (>=2) overrides the register-derived column count — e.g. to "
       "draw an earlier cold-start segment's physical width. dlv=true lays out the "
       "PHY3 (DLV) framing: CDS at cds_horizontal_start, S1 at Column 0, S0 last. "
       "Every grid_* layout holds rows to max_grid_rows and the column count to "
       "min_column_count..max_column_count.");

    m.def("grid_from_commands", [](const std::vector<py::dict>& cmds, int rows,
                                   int forceColumns,
                                   const std::vector<std::tuple<int, std::uint32_t, int>>& overrides,
                                   bool dlv, int cdsHorizontalStart) {
        std::vector<CommandReplay> replay;
        replay.reserve(cmds.size());
        for (const auto& d : cmds) {
            CommandReplay r;
            r.isWrite = d.contains("is_write") && d["is_write"].cast<bool>();
            r.isCommit = d.contains("is_commit") && d["is_commit"].cast<bool>();
            r.commitConfirmed = d.contains("commit_confirmed") &&
                                d["commit_confirmed"].cast<bool>();
            if (d.contains("device_mask"))
                r.deviceMask = static_cast<std::uint16_t>(d["device_mask"].cast<int>());
            if (d.contains("address"))
                r.address = d["address"].cast<std::uint32_t>();
            if (d.contains("group_mask"))
                r.groupMask = static_cast<std::uint16_t>(d["group_mask"].cast<int>());
            if (d.contains("data")) {
                py::bytes b = d["data"].cast<py::bytes>();
                std::string s = b;
                r.data.assign(s.begin(), s.end());
            }
            replay.push_back(std::move(r));
        }
        py::list out;
        for (const auto& c : gridFromCommands(replay, rows, forceColumns, overrides,
                                              dlv, cdsHorizontalStart))
            out.append(gridCellToDict(c));
        return out;
    }, py::arg("commands"), py::arg("rows") = 32, py::arg("force_columns") = 0,
       py::arg("register_overrides") = std::vector<std::tuple<int, std::uint32_t, int>>{},
       py::arg("dlv") = false, py::arg("cds_horizontal_start") = 0,
       "As-of-cursor grid: replay decoded write/commit command dicts (keys "
       "is_write, is_commit, commit_confirmed, device_mask, address, data, "
       "group_mask) through the real register model, honouring dual-ranked _NEXT "
       "staging (a _NEXT write takes effect only on a confirmed commit of its "
       "group). force_columns (>=2) overrides the derived width. register_overrides "
       "= (device, address, value) what-if forces folded in after the replay "
       "(already filtered to the cursor's config section by the caller). dlv=true "
       "lays out PHY3 (DLV) framing: CDS at cds_horizontal_start, S1 at Column 0, "
       "S0 at the last column.");

    m.def("config_from_commands", [](const std::vector<py::dict>& cmds, int forceColumns,
                                     const std::vector<std::tuple<int, std::uint32_t, int>>& overrides) {
        std::vector<CommandReplay> replay;
        replay.reserve(cmds.size());
        for (const auto& d : cmds) {
            CommandReplay r;
            r.isWrite = d.contains("is_write") && d["is_write"].cast<bool>();
            r.isCommit = d.contains("is_commit") && d["is_commit"].cast<bool>();
            r.commitConfirmed = d.contains("commit_confirmed") &&
                                d["commit_confirmed"].cast<bool>();
            if (d.contains("device_mask"))
                r.deviceMask = static_cast<std::uint16_t>(d["device_mask"].cast<int>());
            if (d.contains("address"))
                r.address = d["address"].cast<std::uint32_t>();
            if (d.contains("group_mask"))
                r.groupMask = static_cast<std::uint16_t>(d["group_mask"].cast<int>());
            if (d.contains("data")) {
                py::bytes b = d["data"].cast<py::bytes>();
                std::string s = b;
                r.data.assign(s.begin(), s.end());
            }
            replay.push_back(std::move(r));
        }
        return configToDict(configFromCommands(replay, forceColumns, overrides));
    }, py::arg("commands"), py::arg("force_columns") = 0,
       py::arg("register_overrides") = std::vector<std::tuple<int, std::uint32_t, int>>{},
       "As-of-cursor CONFIG (same replay + force_columns + overrides as "
       "grid_from_commands, minus the layout) as visualizer-CSV fields — so Save "
       "Visualizer Config can export the geometry in effect at the cursor's segment "
       "rather than always the final operational config.");

    m.def("registers_from_commands", [](const std::vector<py::dict>& cmds,
                                        const std::vector<std::tuple<int, std::uint32_t, int>>& overrides) {
        std::vector<CommandReplay> replay;
        replay.reserve(cmds.size());
        for (const auto& d : cmds) {
            CommandReplay r;
            r.isWrite = d.contains("is_write") && d["is_write"].cast<bool>();
            r.isCommit = d.contains("is_commit") && d["is_commit"].cast<bool>();
            r.isRead = d.contains("is_read") && d["is_read"].cast<bool>();
            r.commitConfirmed = d.contains("commit_confirmed") &&
                                d["commit_confirmed"].cast<bool>();
            if (d.contains("device_mask"))
                r.deviceMask = static_cast<std::uint16_t>(d["device_mask"].cast<int>());
            if (d.contains("address"))
                r.address = d["address"].cast<std::uint32_t>();
            if (d.contains("group_mask"))
                r.groupMask = static_cast<std::uint16_t>(d["group_mask"].cast<int>());
            if (d.contains("data")) {
                py::bytes b = d["data"].cast<py::bytes>();
                std::string s = b;
                r.data.assign(s.begin(), s.end());
            }
            replay.push_back(std::move(r));
        }
        py::list out;
        for (const auto& t : registersFromCommands(replay, overrides))
            out.append(py::make_tuple(std::get<0>(t), std::get<1>(t), std::get<2>(t),
                                      std::get<3>(t), std::get<4>(t), std::get<5>(t),
                                      std::get<6>(t), std::get<7>(t)));
        return out;
    }, py::arg("commands"),
       py::arg("register_overrides") = std::vector<std::tuple<int, std::uint32_t, int>>{},
       "As-of-cursor register state from the SAME replay + override fold as "
       "grid_from_commands, PLUS bus reads overlaid: list of (device, address, cur, "
       "has_cur, cur_src, next, has_next, next_src). *_src is provenance (1 written, "
       "2 read). Reads (is_read command dicts) reveal live values in the register map "
       "without perturbing the grid/audio config. NEXT display = next if has_next else "
       "cur; CURR = cur. The register map's value + provenance source, so register map "
       "/ grid / audio share one decode authority.");

    m.def("sosfilt", [](py::array_t<double, py::array::c_style | py::array::forcecast> sos,
                        py::array_t<double, py::array::c_style | py::array::forcecast> x,
                        py::array_t<double, py::array::c_style | py::array::forcecast> zi) {
        if (sos.ndim() != 2 || sos.shape(1) != 6)
            throw std::invalid_argument("sos must have shape (n_sections, 6)");
        const auto nSec = static_cast<std::size_t>(sos.shape(0));
        if (zi.ndim() != 2 || static_cast<std::size_t>(zi.shape(0)) != nSec || zi.shape(1) != 2)
            throw std::invalid_argument("zi must have shape (n_sections, 2)");
        if (x.ndim() != 1)
            throw std::invalid_argument("x must be one-dimensional");
        const double* c = sos.data();
        for (std::size_t s = 0; s < nSec; ++s)
            if (c[6 * s + 3] != 1.0)
                throw std::invalid_argument("each section's a0 must be 1");
        const auto n = static_cast<std::size_t>(x.shape(0));
        py::array_t<double> y(n);
        py::array_t<double> zf({static_cast<py::ssize_t>(nSec), static_cast<py::ssize_t>(2)});
        std::copy(zi.data(), zi.data() + 2 * nSec, zf.mutable_data());
        {
            py::gil_scoped_release rel;
            SosFilter(c, nSec, x.data(), y.mutable_data(), n, zf.mutable_data());
        }
        return py::make_tuple(y, zf);
    }, py::arg("sos"), py::arg("x"), py::arg("zi"),
       "Second-order-section IIR cascade (transposed direct form II, as scipy.signal.sosfilt).\n"
       "Returns (y, zf): the filtered signal and each section's final state.");

    m.def("grid_from_csv", [](const std::string& csvPath, int rows) {
        py::list out;
        for (const auto& c : gridFromCsv(csvPath, rows)) out.append(gridCellToDict(c));
        return out;
    }, py::arg("csv_path"), py::arg("rows") = 32,
       "Grid layout from a visualizer CSV config (the expected config).");

    m.def("registers_from_csv", [](const std::string& csvPath) {
        py::list out;
        for (const auto& t : registersFromCsv(csvPath))
            out.append(py::make_tuple(std::get<0>(t), std::get<1>(t), std::get<2>(t)));
        return out;
    }, py::arg("csv_path"),
       "Per-device register writes (device, address, value) encoding a visualizer "
       "CSV config — the EXPECTED registers, for the register-view comparison.");

    m.def("csv_warnings", [](const std::string& csvPath) {
        SwI3sConfig cfg;
        std::string err;
        py::list out;
        cfg.LoadCsv(csvPath, err);   // an unopenable file leaves its reason in warnings
        for (const auto& w : cfg.warnings) out.append(w);
        return out;
    }, py::arg("csv_path"),
       "What reading a visualizer CSV config did not take as written: one line per field "
       "(a port disabled for its DeviceNumber_REG, a value masked or clamped to its "
       "register field, a Reserved CDS_TailWidth 3, an unreadable cell, values past the "
       "last column, a key that is not a config field). A file that cannot be opened "
       "reports why. Empty for a clean file.");

    m.def("pattern_period", [](const std::string& csvPath, long long cap) {
        SwI3sConfig cfg;
        std::string err;
        cfg.LoadCsv(csvPath, err);
        return cfg.patternPeriod(cap);
    }, py::arg("csv_path"), py::arg("cap"),
       "The LCM of the transport pattern periods, in Rows, of a visualizer CSV config's "
       "enabled ports that carry a channel ((Interval + 1), times SkippingDenominator for a "
       "skipping port), as the manual SSP row is reduced by; 1 when there is none. An LCM "
       "above cap returns cap.");

    m.def("make_demo_levels", &MakeDemoLevels, py::arg("audio_samples_per_channel") = 32,
          "Synthetic PHY2 stream: two commits (2 -> 8 -> 16 columns) configuring four mono "
          "ports: two 16-bit PCM (DP0 carrying 44.1 kHz by skipping, DP1) and two 1-bit PDM "
          "(DP2, DP3), with DP1 and DP3 scrambled.");

    m.def("make_demo_levels_flow_control", &MakeDemoLevelsFlowControl,
          py::arg("audio_samples_per_channel") = 32, py::arg("fcp_scrambled") = false,
          "Synthetic PHY2 flow-control stream: four peripheral data ports (one per flow "
          "mode — NORMAL/TX_CONTROLLED/RX_CONTROLLED/ASYNC) on four devices, with 0/1 "
          "interval of transport jitter gated by TX_PRESENT (decodes bit-exact to a "
          "uniform 48 ksps stream). fcp_scrambled writes FCP_ScramblerEn = 1 and "
          "scrambles every DRQ bit with its port's LFSR.");

    m.def("make_demo_levels_phy1", &MakeDemoLevelsPhy1, py::arg("audio_samples_per_channel") = 32,
          "Synthetic PHY1 (slow FBCSE) stream: a constant 4-column geometry with two 16-bit "
          "PCM ports (interleaved, DP1 scrambled) + one PDM port; a mid-capture commit "
          "REPOSITIONS the ports (config_1 -> config_2) without changing the column count.");

    m.def("make_demo_levels_phy3", [](int n, int cdsColumn) {
              DemoLevels d = MakeDemoLevelsPhy3(n, cdsColumn);
              py::dict out;
              out["levels"] = d.levels;
              out["row_columns"] = d.rowColumns;
              return out;
          },
          py::arg("audio_samples_per_channel") = 32, py::arg("cds_column") = 2,
          "Synthetic PHY3 (DLV) stream: {'levels': per-UI logical differential levels "
          "(Sync1/CDS/payload/Sync0 framing), 'row_columns': each row's column count}. "
          "Safe-Lock-4 then a commit to 16 columns, same audio as make_demo_levels. "
          "row_columns lets build_dlv_capture_from_levels place edges at a constant row "
          "period so the UI/bit-clock rate rises at the commit (4 -> 16 columns). A "
          "cds_column other than 2 is programmed with the 16-column config and moves the "
          "CDS there at that commit. cds_column must lie in 1..14 and off the data-port "
          "columns 4..9; any other value raises ValueError.");

    m.def("make_skipping_levels", &MakeSkippingLevels,
          py::arg("samples_per_channel") = 200, py::arg("numerator") = 13,
          py::arg("denominator") = 160, py::arg("misaligned_sspa") = false,
          "Synthetic PHY2 stream exercising Payload Interval Skipping: one unscrambled "
          "16-bit PCM source port (Interval = 32 Rows) carrying a RAMP, transporting "
          "(denominator - numerator) of every denominator intervals and leaving the rest "
          "idle, with periodic SSPAs on the pattern period. Sample n has value n, so a "
          "decode that skips the wrong interval no longer counts up. misaligned_sspa puts "
          "the SSPAs one Interval off the pattern boundary (an unexpected SSP). A "
          "denominator below 1 is written as 0, which reads as the reset value 1. Raises "
          "ValueError for a numerator or denominator above 4095, or for a ratio whose "
          "samples_per_channel * denominator / (denominator - numerator) intervals exceed "
          "4 * max_demo_samples.");

    m.def("make_dscr_disable_levels", &MakeDscrDisableLevels,
          py::arg("samples_per_channel") = 32, py::arg("disable_all") = false,
          "Test fixture: two 16-bit PCM ports; a DSCR disables dp1 (or both, disable_all) "
          "mid-stream. Raises ValueError when samples_per_channel is too small to place the "
          "DSCR before its commit point.");
    m.def("make_dscr_enable_levels", &MakeDscrEnableLevels, py::arg("samples_per_channel") = 32,
          py::arg("enable_channel") = false,
          "Test fixture: one 16-bit PCM port; a DSCR enables a second port (or, with "
          "enable_channel, a second channel on the running port) mid-stream. Raises "
          "ValueError when samples_per_channel is too small to place the DSCR before its "
          "commit point.");
    m.def("make_dscr_channel_set_levels", &MakeDscrChannelSetLevels,
          py::arg("samples_per_channel") = 32, py::arg("from_mask") = 0x3,
          py::arg("to_mask") = 0x1, py::arg("row_in_interval") = 0,
          "Test fixture: one scrambled 16-bit PCM port with channels from_mask (bits 0-2; "
          "0 reads as channel 0); a DSCR commits to_mask (bits 0-2), its commit point "
          "row_in_interval (0..63) rows into an interval. Raises ValueError when "
          "samples_per_channel is too small to place the DSCR before its commit point.");

    m.def("make_channel_reseed_levels", &MakeChannelReseedLevels,
          py::arg("samples_per_phase") = 32,
          "Test fixture: one scrambled two-channel PCM port in seven phases; channel 1 is "
          "enabled, disabled and re-enabled across SSCRs, then disabled and re-enabled by "
          "DSCRs, while channel 0 runs throughout; then single-ranked 0x0B writes clear and "
          "set ScramblerEn. A channel's scrambler is seeded when the channel is enabled or "
          "ScramblerEn turns on.");
    m.def("make_fcp_reseed_levels", &MakeFcpReseedLevels,
          py::arg("intervals_per_phase") = 96, py::arg("generator_seed") = "enable",
          "Test fixture: one RX_CONTROLLED port with a scrambled FCP across an SSCR, a "
          "DSCR, and single-ranked 0x0E writes that remove and re-enable the FCP, then clear "
          "and set FCP_ScramblerEn. generator_seed ('enable', 'sscr', 'dscr', 'interval', "
          "'resume', 'continue') says where the wire's DRQ scrambler is seeded; 'enable' "
          "seeds it at each FCP enable and where FCP_ScramblerEn turns on.");
    m.def("make_fcp_move_levels", &MakeFcpMoveLevels,
          py::arg("intervals_per_phase") = 96, py::arg("dscr") = false,
          py::arg("fcp_h_start") = 13, py::arg("fcp_offset") = 12,
          py::arg("flow_control_delay") = 0, py::arg("scrambled") = false,
          py::arg("fcp_bit_width") = 0, py::arg("reenable") = false,
          py::arg("commit_row_offset") = 0, py::arg("columns_after") = 16,
          "Test fixture: one RX_CONTROLLED port (data from Row 2) with its DRQ at Column 11, "
          "Row 12 and FlowControlDelay flow_control_delay (0 or 1); an SSCR (or a DSCR when "
          "dscr) then commits FCP_HorizontalStart fcp_h_start, FCP_Offset fcp_offset and "
          "FCP_BitWidth fcp_bit_width (0..3) alone, and the DRQ is sent at the new cell from "
          "the commit point. TX_PRESENT follows the DRQ history from the port's enable, "
          "across the commit. With reenable the second commit disables the port and a third "
          "enables it again with the new placement, where its DRQ history starts again. "
          "scrambled sets ScramblerEn: TX_PRESENT and the data bits pass through channel 0's "
          "LFSR. commit_row_offset (0..15, an SSCR without reenable) lands the second "
          "commit that many rows past the interval boundary, where the DRQ history starts "
          "again; columns_after (16 or 32, 16 with reenable) is the column count the second "
          "commit also commits. Raises ValueError for an intervals_per_phase too small to "
          "place the commit (the smallest is 65 with the default arguments, 64 with a "
          "commit_row_offset of 6 or more, 75 with reenable or columns_after 32), a "
          "flow_control_delay other than 0 or 1, an "
          "fcp_bit_width outside 0..3, a DRQ whose fcp_bit_width + 1 UIs leave Columns "
          "1..15 or Rows 0..15 or reach Columns 2..14 of Rows 2..4, or a commit_row_offset "
          "or columns_after outside its range.");
    m.def("make_fcp_guard_tail_levels", &MakeFcpGuardTailLevels,
          py::arg("intervals_per_phase") = 96, py::arg("dscr") = false,
          py::arg("fcp_h_start") = 8, py::arg("fcp_offset") = 12,
          py::arg("fcp_bit_width") = 0, py::arg("fcp_tail_width") = 0,
          py::arg("fcp_guard_enable") = false, py::arg("fcp_guard_polarity") = false,
          py::arg("async_mode") = false,
          py::arg("first_guard_enable") = false, py::arg("first_guard_polarity") = false,
          "Test fixture: one RX_CONTROLLED (ASYNC when async_mode) sink port (data from Row 2) "
          "whose source DRQ sits at Column 11, Row 12 with no tail and its guard set by "
          "first_guard_enable and first_guard_polarity (off by default); an SSCR (or a "
          "DSCR when dscr) then commits FCP_HorizontalStart fcp_h_start, FCP_Offset "
          "fcp_offset, FCP_BitWidth fcp_bit_width, FCP_TailWidth fcp_tail_width (each 0..3), "
          "FCP_GuardEnable fcp_guard_enable and FCP_GuardPolarity fcp_guard_polarity alone. "
          "From the commit point the DRQ is followed in its row by the guard UI, driven to "
          "its polarity, and fcp_tail_width tail UIs holding the bus level. Raises ValueError "
          "for an intervals_per_phase below 86 (too few to place the commit), a width "
          "outside 0..3, or a cell outside Columns 1..15, Rows 0..15, or in Columns 2..14 "
          "of Rows 2..4.");

    m.def("make_immediate_scrambler_levels", &MakeImmediateScramblerLevels,
          py::arg("samples_per_channel") = 32, py::arg("mid_interval") = false,
          "Test fixture: one scrambled PCM port; a single-ranked write clears ScramblerEn "
          "mid-stream (immediate). mid_interval=True lands the reconfigure mid-interval.");

    m.def("make_immediate_sample_size_levels", &MakeImmediateSampleSizeLevels,
          py::arg("samples_per_channel") = 32, py::arg("from_size") = 23,
          py::arg("to_size") = 15, py::arg("rows_into_sample") = 8,
          "Test fixture: one scrambled PCM port; a single-ranked write changes SampleSize "
          "from from_size to to_size (register values, bits - 1) with the reconfigure "
          "rows_into_sample rows into a sample, so that sample is in flight across it.");
    m.def("make_enablech_curr_write_levels", &MakeEnableChCurrWriteLevels,
          py::arg("samples_per_channel") = 32, py::arg("interval") = 0,
          "Test fixture: one PCM port committed with Interval=`interval`; a WriteA32 then "
          "re-asserts EnableCh0 directly at its _CURR address (0xC1). The decode must flag "
          "the command's enablech_curr_error iff interval != 0 (Interval > 1 Row). Raises "
          "ValueError for an interval above 4095, past the 12-bit Interval register, or a "
          "samples_per_channel x (interval + 1) above 3072000 Rows (48000 samples at a "
          "64-row Interval).");

    m.def("make_running_port_write_levels", &MakeRunningPortWriteLevels,
          py::arg("samples_per_channel"), py::arg("offset"), py::arg("value"),
          py::arg("base") = 0x2000u, py::arg("before_run") = false,
          "Test fixture: one running PCM port; a single WriteA32 writes `value` to the "
          "register at `base` + `offset` (`offset` 0..0xFF; the default base is dp0's block) "
          "mid-stream, or with `before_run` as the last setup write before the SSCR (the "
          "wire keeps the committed config).");

    m.def("decode_codeword", [](std::uint16_t raw) {
        U8 value = 0; bool isControl = false, isComma = false;
        bool valid = C8b10bDecoder::DecodeSymbol(raw, value, isControl, isComma);
        py::dict d;
        d["valid"] = valid;
        d["value"] = value;
        d["is_control"] = isControl;
        d["is_comma"] = isComma;
        return d;
    }, py::arg("raw"),
       "Decode one raw 10-bit 8b/10b codeword (a@bit9..j@bit0): {valid, value, "
       "is_control, is_comma}. Exposes the symbol decoder for both running-disparity "
       "forms so tests can verify RD-/RD+ coverage.");

    m.def("encode_codeword", [](std::uint8_t value) {
        return static_cast<int>(C8b10bDecoder::EncodeByte(value));
    }, py::arg("value"),
       "Test hook: the raw 10-bit D-code symbol (a@bit9..j@bit0) EncodeByte emits for `value`.");

    m.def("register_reset", [](std::uint32_t addr) {
        return (int)CRegisterModel::resetOf(addr);
    }, py::arg("address"),
       "Spec-defined reset byte the decode model uses for the register at `address` "
       "(what an un-written register reads as). Cross-checked vs data/registers.json.");

    m.def("register_read_only", [](std::uint32_t addr) {
        return CRegisterModel::IsReadOnly(addr);
    }, py::arg("address"),
       "Test hook: True when a WriteA32 to `address` changes nothing because the byte is "
       "read-only (a register whose fields are all RO, or a CDS _CURR alias).");

    m.def("register_rw1c_mask", [](std::uint32_t addr) {
        return static_cast<int>(CRegisterModel::Rw1cMask(addr));
    }, py::arg("address"),
       "Test hook: the write-1-to-clear bits of the register at `address` (0 when none).");

    m.def("register_write_is_immediate", [](std::uint32_t addr) {
        return CRegisterModel().WriteIsImmediate(addr);
    }, py::arg("address"),
       "Test hook: True when a WriteA32 to `address` takes committed effect at once "
       "(a single-ranked register or a writable _CURR alias), False when it stages in "
       "_NEXT or the byte is read-only (see register_read_only).");

    m.def("_token_symbol", [](int token) {
        return static_cast<int>(C8b10bDecoder::EncodeToken(token));
    }, py::arg("token"),
       "Test hook: the raw 10-bit symbol of Robust Token `token` (0..15); 0 otherwise.");

    m.def("_parse_cds_test", [](const std::vector<bool>& bits, std::vector<int> hubDepths,
                                bool shortSpacer) {
        CCommandTransportParser parser;
        int depths[swi3s::kMaxPeripherals] = {0};
        for (int i = 0; i < swi3s::kMaxPeripherals && i < (int)hubDepths.size(); ++i)
            depths[i] = hubDepths[i];
        parser.SetHubDepths(depths);
        parser.SetShortProtocolSpacer(shortSpacer);
        py::list out;
        for (size_t k = 0; k < bits.size(); ++k) {
            if (!parser.PushCdsBit(bits[k], (U64)k)) continue;
            const SwI3sCommand& c = parser.Command();
            py::dict d;
            d["phase"] = c.PhaseName();
            d["command"] = c.CommandName();
            d["header_valid"] = c.headerValid;
            d["crc_valid"] = c.crcValid;
            d["device_mask"] = (int)c.deviceMask;
            d["packet_length"] = (int)c.packetLength;
            d["peripheral_response"] = c.peripheralResponse;
            d["manager_response"] = c.managerResponse;
            out.append(d);
        }
        return out;
    }, py::arg("bits"), py::arg("hub_depths") = std::vector<int>{},
       py::arg("short_spacer") = false,
       "Test hook: push CDS bits through a command parser with the given hub depths and "
       "ShortProtocolSpacer; returns one dict per completed phase (phase, command, "
       "header_valid, crc_valid, device_mask, packet_length, peripheral_response, "
       "manager_response).");

    m.def("_cds_symbols_test", [](const std::vector<bool>& bits) {
        C8b10bDecoder dec;
        py::list out;
        for (size_t k = 0; k < bits.size(); ++k) {
            C8b10bDecoder::Symbol sym;
            if (!dec.PushBit(bits[k], sym) || k + 1 < swi3s::kSymbolBits) continue;
            int kind = 0, value = -1;
            classifySymbol(sym, kind, value);
            py::dict d;
            d["kind"] = kind;
            d["value"] = value;
            d["raw"] = static_cast<int>(sym.raw);
            out.append(d);
        }
        return out;
    }, py::arg("bits"),
       "Test hook: the symbols decode_symbols reports for CDS bits `bits` (kind, value, raw "
       "per symbol, classified as decode_symbols classifies them).");

    // Test hook for the hub-delay decode path: build a Ping CDS bitstream whose
    // device tokens are 0,1,..,11, inserting 2*(emit_depth[i]-emit_depth[i-1])
    // gap bits before device i's token (a peripheral N hubs deep responds 2*N
    // frame rows late). Decode it through a parser configured with parse_depths
    // and return the 12 decoded ping tokens. With parse_depths == emit_depths the
    // tokens come back 0..11; with mismatched depths they garble.
    m.def("_hub_ping_test", [](std::vector<int> emitDepths, std::vector<int> parseDepths) {
        auto depthAt = [](const std::vector<int>& v, int i) {
            return (i >= 0 && i < (int)v.size()) ? v[i] : 0;
        };
        std::vector<bool> bits;
        auto appendSym = [&](U16 s) { for (int i = 9; i >= 0; --i) bits.push_back((s >> i) & 1); };
        appendSym(C8b10bDecoder::CommaSymbol());
        int hdr[6] = { swi3s::kPhaseGetStatus, 0xF, 0xF, 0xF, 0, 1 };
        for (int t : hdr) appendSym(C8b10bDecoder::EncodeToken(t));
        U8 pkt[1] = { 0x00 };
        appendSym(C8b10bDecoder::EncodeByte(pkt[0]));
        U16 crc = CCrc16::Compute(pkt, 1);
        appendSym(C8b10bDecoder::EncodeByte(crc >> 8));
        appendSym(C8b10bDecoder::EncodeByte(crc & 0xFF));
        appendSym(0x155);   // MP spacer
        for (int i = 0; i < swi3s::kMaxPeripherals; ++i) {
            int gap = 2 * (depthAt(emitDepths, i) - (i > 0 ? depthAt(emitDepths, i - 1) : 0));
            for (int g = 0; g < gap; ++g) bits.push_back(false);   // hub-delay gap bits
            appendSym(C8b10bDecoder::EncodeToken(i));              // distinct token per device
        }
        appendSym(0x155);

        CCommandTransportParser parser;
        int depths[swi3s::kMaxPeripherals] = {0};
        for (int i = 0; i < swi3s::kMaxPeripherals; ++i) depths[i] = depthAt(parseDepths, i);
        parser.SetHubDepths(depths);
        py::list out;
        for (size_t k = 0; k < bits.size(); ++k) {
            if (parser.PushCdsBit(bits[k], (U64)k)) {
                const SwI3sCommand& c = parser.Command();
                for (int i = 0; i < swi3s::kMaxPeripherals; ++i) out.append(c.pingStatus[i]);
                return out;
            }
        }
        for (int i = 0; i < swi3s::kMaxPeripherals; ++i) out.append(-2);   // no command decoded
        return out;
    }, py::arg("emit_depths"), py::arg("parse_depths"),
       "Test hook: decode a synthetic hub-delayed Ping; returns 12 ping tokens.");

    m.def("decode_saleae_v3_deltas", [](py::array_t<std::uint8_t, py::array::c_style | py::array::forcecast> buf,
                                        std::size_t off, std::size_t nbytes) {
        // Decode `nbytes` of the Saleae version-3 base-128 transition-delta codec at
        // byte offset `off` in `buf` into an int64 array of deltas (each >= 1). A byte
        // < 0x40 is a single-byte code; otherwise the first byte is the MSB digit
        // (byte - 0x40), interior bytes >= 0x80 are digits (byte - 0x80), and the first
        // byte < 0x80 terminates (raw low digit). Equal to the Python decode_v3_deltas
        // for every code whose delta fits int64; any other code throws. This is the hot loop when
        // reading 100s-of-millions-of-edges .sal captures, so it lives in C++.
        py::buffer_info info = buf.request();
        const std::uint8_t* base = static_cast<const std::uint8_t*>(info.ptr);
        const std::size_t total = static_cast<std::size_t>(info.size);
        if (off > total || nbytes > total - off)
            throw std::invalid_argument("decode_saleae_v3_deltas: range past end of buffer");
        const std::uint8_t* const begin = base + off;
        const std::uint8_t* const end = begin + nbytes;
        // Pass 1: count codes (so the output is allocated exactly once). Advances the
        // pointer identically to the value pass below, so the counts agree.
        std::size_t count = 0;
        {
            py::gil_scoped_release rel;
            const std::uint8_t* p = begin;
            while (p < end) {
                if (*p++ < 0x40) { ++count; continue; }   // single-byte code
                while (p < end && *p >= 0x80) ++p;        // interior digits
                if (p < end) ++p;                         // terminal digit
                ++count;
            }
        }
        py::array_t<std::int64_t> out(static_cast<py::ssize_t>(count));
        std::int64_t* o = out.mutable_data();
        // Pass 2: accumulate each code's base-128 value.
        bool overflow = false;
        {
            py::gil_scoped_release rel;
            const std::uint8_t* p = begin;
            std::size_t k = 0;
            // val * 128 + digit + 1 must fit int64: a digit that would take the delta past
            // INT64_MAX refuses the code.
            constexpr std::int64_t kMax = std::numeric_limits<std::int64_t>::max();
            while (p < end && !overflow) {
                std::uint8_t b = *p++;
                if (b < 0x40) { o[k++] = static_cast<std::int64_t>(b) + 1; continue; }
                std::int64_t val = static_cast<std::int64_t>(b) - 0x40;   // MSB digit
                while (p < end) {
                    std::uint8_t bb = *p++;
                    const bool interior = bb >= 0x80;
                    const std::int64_t digit = interior ? bb - 0x80 : bb;
                    if (val > (kMax - digit - 1) / 128) { overflow = true; break; }
                    val = val * 128 + digit;
                    if (!interior) break;                                 // terminal digit
                }
                if (overflow) break;
                o[k++] = val + 1;
            }
        }
        if (overflow)
            throw std::invalid_argument(
                "decode_saleae_v3_deltas: a delta code is too long for int64");
        return out;
    }, py::arg("buf"), py::arg("off"), py::arg("nbytes"),
       "Decode a Saleae v3 base-128 transition-delta run (uint8 buffer, offset, length) "
       "to an int64 delta array (each >= 1). Raises ValueError for a code whose delta does not "
       "fit int64.");
}
