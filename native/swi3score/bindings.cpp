// pybind11 bindings for the SWI3S decode core (module `swi3score`).

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <pybind11/numpy.h>

#include "ISampleSource.h"
#include "TransitionSampleSource.h"
#include "DlvSampleSource.h"
#include "Decoder.h"
#include "Demo.h"
#include "C8b10bDecoder.h"
#include "CCommandTransportParser.h"
#include "CCrc16.h"
#include "CRegisterModel.h"

namespace py = pybind11;
using namespace swi3score;

static py::dict commandToDict(const CommandRec& c)
{
    py::dict d;
    d["command"] = c.command;
    d["phase"] = c.phase;
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

PYBIND11_MODULE(swi3score, m)
{
    m.doc() = "SWI3S PHY2 decode core (reused from the Saleae plugin).";

    // ABI version of the Python<->native interface. Bump whenever a binding's
    // signature / return shape changes in a way the Python side depends on (e.g.
    // registers_from_commands going from 6- to 8-tuples). swi3s_studio checks this at
    // decode time and asks the user to rebuild if the loaded .so is stale — so a stale
    // module gives a clear "rebuild" message instead of a cryptic unpack error.
    // 7: audio() / audio_columns() gained flow_mode + drq_sample (flow-control decode:
    //    TX_PRESENT gating + DRQ handshake surfacing).
    // 8: DecoderSettings gained forced_column_sections (region-scoped forced column
    //    count). Setting it on an ABI-7 module silently does nothing — the Python side
    //    would show an "overridden" region badge over an unchanged decode — so it's a
    //    bump even though no return shape changed.
    m.attr("score_abi") = 8;

    py::class_<ISampleSource>(m, "ISampleSource");

    py::class_<MemorySampleSource, ISampleSource>(m, "MemorySampleSource")
        .def(py::init<std::vector<bool>, std::uint64_t, std::uint32_t>(),
             py::arg("levels"), py::arg("sample_rate_hz"), py::arg("samples_per_ui") = 4,
             "In-memory per-UI data-level source for tests/demos.");

    py::class_<TransitionSampleSource, ISampleSource>(m, "TransitionSampleSource")
        .def(py::init([](py::array_t<std::uint64_t, py::array::c_style> clockEdges,
                         py::array_t<std::uint64_t, py::array::c_style> dataEdges,
                         bool initialClock, bool initialData, std::uint64_t sampleRateHz) {
                 // Zero-copy: reference the NumPy buffers directly; the arrays are
                 // kept alive by keep_alive below (no copy of the edge data).
                 return new TransitionSampleSource(
                     clockEdges.data(), static_cast<std::size_t>(clockEdges.size()),
                     dataEdges.data(), static_cast<std::size_t>(dataEdges.size()),
                     initialClock, initialData, sampleRateHz);
             }),
             py::arg("clock_edges"), py::arg("data_edges"),
             py::arg("initial_clock"), py::arg("initial_data"), py::arg("sample_rate_hz"),
             py::keep_alive<1, 2>(),    // keep clock_edges alive while the source lives
             py::keep_alive<1, 3>(),    // keep data_edges alive while the source lives
             "Capture-backed source: clock/data transition arrays (uint64, zero-copy).");

    py::class_<DlvSampleSource, ISampleSource>(m, "DlvSampleSource")
        .def(py::init([](py::array_t<std::uint64_t, py::array::c_style> dpEdges,
                         bool initialDp, std::uint64_t sampleRateHz, int initialColumns,
                         double nominalUiSamples) {
                 // DLV: one differential signal; only the DP-wire edges are needed (DN is
                 // its complement). Owning copy (the DLL keeps derived rising-edge state).
                 return new DlvSampleSource(
                     std::vector<std::uint64_t>(dpEdges.data(), dpEdges.data() + dpEdges.size()),
                     initialDp, sampleRateHz, initialColumns, nominalUiSamples);
             }),
             py::arg("dp_edges"), py::arg("initial_dp"), py::arg("sample_rate_hz"),
             py::arg("initial_columns"), py::arg("nominal_ui_samples") = 0.0,
             "PHY3 (DLV) source: recovers the bit clock from the DP-wire edges via a virtual "
             "DLL locked to the Sync1 row edges, sampling mid-UI. initial_columns = Safe-Lock "
             "count; the decoder pushes NumColumns changes. nominal_ui_samples seeds the loop's "
             "free-running UI estimate for the initial geometry (>0 = robust acquisition; the "
             "aperture tracks the once-per-row reference instead of the first two edges).")
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
                       "DLV CDS column (e.g. 2 in Safe-Lock-4); 0 for FBCSE.")
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
        .def("run", &Decoder::run, py::call_guard<py::gil_scoped_release>())
        .def("commands", [](const Decoder& d) {
            py::list out;
            for (const auto& c : d.commands()) out.append(commandToDict(c));
            return out;
        })
        .def("audio", [](const Decoder& d) {
            py::list out;
            for (const auto& a : d.audio()) out.append(audioToDict(a));
            return out;
        })
        .def("audio_columns", [](const Decoder& d) {
            // Columnar (struct-of-arrays) audio: the same data as audio() but as
            // NumPy arrays instead of N per-sample dicts. For dense captures (mic
            // arrays decode tens of millions of samples) building N Python dicts
            // dominates load time; handing over typed arrays is ~two orders of
            // magnitude cheaper and lets the Python store group with NumPy.
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
                // for dense mic-array captures it copies tens of millions of samples
                // -> ~1-2 s. Release the GIL across it so the loader's worker thread
                // doesn't freeze the GUI (the busy spinner) during this late phase of
                // building the audio store. (run() already releases; this didn't.)
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
            "Total UI count for this run, fixed once run() starts; 0 if the source "
            "can't report one up front (pure streaming) — treat 0 as unknown, not done.")
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
                out.append(e);
            }
            return out;
        }, "Decode segments (one per column count): each has start_ui, "
           "start_sample, column_count and row_base (cumulative bus row at the "
           "segment start). Bus rows are continuous across segments.")
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
           "seekable source and settings.decode_audio (else empty). This is the on-demand "
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
                               long rowBase, std::uint64_t endUi, bool dlv, int cdsColumn) {
        py::list out;
        for (const auto& s : decodeSymbols(src, columnCount, maxSymbols, startUi,
                                           rowOriginUi, rowBase, endUi, dlv, cdsColumn)) {
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
       "Re-decode the CDS into classified 8b/10b symbols (optionally windowed). "
       "Pass row_origin_ui + row_base (a segment's Column-0 UI and cumulative row) "
       "to make symbol rows match the streaming decoder across segments. end_ui "
       "caps the decode at a segment boundary so it can't cross into a region with "
       "a different column width (0 = no cap). dlv=True reads the CDS as a plain-NRZ "
       "bit at cds_column (Column 2 for DLV) instead of NRZS at Column 0 — pass a "
       "DlvSampleSource as the source for the recovered-clock, mid-UI sampling.");

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
       "PHY3 (DLV) framing: CDS at cds_horizontal_start, S1 at Column 0, S0 last.");

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

    m.def("make_demo_levels", &MakeDemoLevels, py::arg("audio_samples_per_channel") = 32,
          "Synthetic PHY2 stream: config + commit + scrambled stereo audio.");

    m.def("make_demo_levels_flow_control", &MakeDemoLevelsFlowControl,
          py::arg("audio_samples_per_channel") = 32,
          "Synthetic PHY2 flow-control stream: four peripheral data ports (one per flow "
          "mode — NORMAL/TX_CONTROLLED/RX_CONTROLLED/ASYNC) on four devices, with 0/1 "
          "interval of transport jitter gated by TX_PRESENT (decodes bit-exact to a "
          "uniform 48 ksps stream).");

    m.def("make_demo_levels_phy1", &MakeDemoLevelsPhy1, py::arg("audio_samples_per_channel") = 32,
          "Synthetic PHY1 (slow FBCSE) stream: a constant 4-column geometry with two 16-bit "
          "PCM ports (interleaved, DP1 scrambled) + one PDM port; a mid-capture commit "
          "REPOSITIONS the ports (config_1 -> config_2) without changing the column count.");

    m.def("make_demo_levels_phy3", [](int n) {
              DemoLevels d = MakeDemoLevelsPhy3(n);
              py::dict out;
              out["levels"] = d.levels;
              out["row_columns"] = d.rowColumns;
              return out;
          },
          py::arg("audio_samples_per_channel") = 32,
          "Synthetic PHY3 (DLV) stream: {'levels': per-UI logical differential levels "
          "(Sync1/CDS/payload/Sync0 framing), 'row_columns': each row's column count}. "
          "Safe-Lock-4 then a commit to 16 columns, same audio as make_demo_levels. "
          "row_columns lets build_dlv_capture_from_levels place edges at a constant row "
          "period so the UI/bit-clock rate rises at the commit (4 -> 16 columns).");

    m.def("make_dscr_disable_levels", &MakeDscrDisableLevels,
          py::arg("samples_per_channel") = 32, py::arg("disable_all") = false,
          "Test fixture: two 16-bit PCM ports; a DSCR disables dp1 (or both, disable_all) mid-stream.");

    m.def("make_immediate_scrambler_levels", &MakeImmediateScramblerLevels,
          py::arg("samples_per_channel") = 32, py::arg("mid_interval") = false,
          "Test fixture: one scrambled PCM port; a single-ranked write clears ScramblerEn "
          "mid-stream (immediate). mid_interval=True lands the reconfigure mid-interval.");

    m.def("make_enablech_curr_write_levels", &MakeEnableChCurrWriteLevels,
          py::arg("samples_per_channel") = 32, py::arg("interval") = 0,
          "Test fixture: one PCM port committed with Interval=`interval`; a WriteA32 then "
          "re-asserts EnableCh0 directly at its _CURR address (0xC1). The decode must flag "
          "the command's enablech_curr_error iff interval != 0 (Interval > 1 Row).");

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

    m.def("register_reset", [](std::uint32_t addr) {
        return (int)CRegisterModel::resetOf(addr);
    }, py::arg("address"),
       "Spec-defined reset byte the decode model uses for the register at `address` "
       "(what an un-written register reads as). Cross-checked vs data/registers.json.");

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
        // byte < 0x80 terminates (raw low digit). Bit-identical to the Python
        // decode_v3_deltas — this is the hot loop when reading 100s-of-millions-of-edges
        // .sal captures, so it lives in C++ (the Python byte loop took ~a minute).
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
        {
            py::gil_scoped_release rel;
            const std::uint8_t* p = begin;
            std::size_t k = 0;
            while (p < end) {
                std::uint8_t b = *p++;
                if (b < 0x40) { o[k++] = static_cast<std::int64_t>(b) + 1; continue; }
                std::int64_t val = static_cast<std::int64_t>(b) - 0x40;   // MSB digit
                while (p < end) {
                    std::uint8_t bb = *p++;
                    if (bb >= 0x80) { val = val * 128 + (bb - 0x80); }    // interior digit
                    else { val = val * 128 + bb; break; }                 // terminal digit
                }
                o[k++] = val + 1;
            }
        }
        return out;
    }, py::arg("buf"), py::arg("off"), py::arg("nbytes"),
       "Decode a Saleae v3 base-128 transition-delta run (uint8 buffer, offset, length) "
       "to an int64 delta array. Bit-identical to the Python decode_v3_deltas.");
}
