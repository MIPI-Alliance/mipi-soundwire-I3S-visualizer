"""Imposing a Visualizer config CSV on an open Session.

A capture that begins after the setup commit has no WriteA32 or commit on the wire, so its
port geometry cannot be snooped. `ConfigCsvImporter.apply` re-decodes the Session with the
CSV as the decode's configuration and seeds the bus grid and the register map from it
(Provenance.CSV). The CSV's state is kept on the Session, in the attributes its re-decode
and its rollback (`Session._undone_on_failure`) cover: `_config_csv` (the normalized path
the decode reads), `_config_csv_tmp` (an upgrade temp copy the Session owns), `_csv_seed_key`,
`_csv_replay`, `_csv_snap` and `_csv_base`, and the original path in `source["config_csv"]`.
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

import swi3score


class ConfigCsvImporter:
    """The global config CSV of one Session: applying it, removing it, and the grid and
    register baseline it encodes."""

    def __init__(self, session) -> None:
        self.session = session

    @staticmethod
    def normalized(path: str) -> Tuple[str, Optional[str]]:
        """(the v2.0 path the decode reads, the temp copy made for it or None) for the CSV
        at `path`. A legacy or older-v2.0 CSV is upgraded into a temp copy, which the caller
        owns."""
        from .ingest import visualizer_csv
        norm = visualizer_csv.normalized_path(path)
        return norm, (norm if norm != path else None)

    @staticmethod
    def report_warnings(norm: str, path: str) -> None:
        """Report, under `path`, what reading the normalized CSV `norm` did not take as
        written."""
        from .ingest import visualizer_csv
        visualizer_csv.report_csv_warnings(norm, path)

    @staticmethod
    def discard_tmp(tmp: Optional[str]) -> None:
        """Remove an upgrade temp copy (`normalized`), if there is one."""
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass

    @staticmethod
    def writes_replay(seed) -> List[dict]:
        """A config CSV's (device, address, value) triples as a WriteA32 + confirmed-commit
        replay, for the C++ register authority the grid and the register map use
        (registers_from_commands / grid_from_commands); it reproduces grid_from_csv and
        registers_from_csv. The trailing commit promotes the staged _NEXT writes, which a
        write alone only stages. Empty for an empty seed."""
        replay: List[dict] = [
            {"is_write": True, "device_mask": 1 << int(d), "address": int(a),
             "data": bytes([int(v) & 0xFF])} for d, a, v in seed]
        if replay:
            devices = 0
            for d, _a, _v in seed:
                devices |= 1 << int(d)
            replay.append({"is_commit": True, "commit_confirmed": True, "group_mask": 0xFFFF,
                           "device_mask": devices})
        return replay

    def reload_seed(self) -> None:
        """(Re)build the Session's CSV baseline from its applied config CSV: `_csv_replay`,
        the write + commit replay; `_csv_base`, the register model it leaves, which the bus
        grid replays the wire's commands on; and `_csv_snap`, the register snapshot seeded as
        Provenance.CSV. All are empty (None for `_csv_base`) when no CSV is applied.
        Memoized on the CSV path (`_csv_seed_key`), so a re-decode that leaves the CSV alone
        does not parse it again; `apply` clears the key."""
        s = self.session
        if s._config_csv == getattr(s, "_csv_seed_key", None):
            return
        seed = swi3score.registers_from_csv(s._config_csv) if s._config_csv else []
        s._csv_replay = self.writes_replay(seed)
        s._csv_snap = (swi3score.registers_from_commands(s._csv_replay)
                       if s._csv_replay else [])
        # The register model after the CSV replay, which grid_cells / grid_cells_at
        # continue from (grid_from_commands base=) instead of replaying the CSV each call.
        s._csv_base = (swi3score.ReplayCheckpoint(s._csv_replay)
                       if s._csv_replay else None)
        s._csv_seed_key = s._config_csv

    def apply(self, path: Optional[str]) -> None:
        """Impose the config CSV at `path` and re-decode; None or "" removes it. Then, since
        the CSV is what gives a partial capture its port geometry, an SSPA on the wire may
        anchor the phase (Session.lock_ssp_from_sspa). A CSV that does not read, or a decode
        that fails or is stopped (the CSV's or that re-phase's), leaves the Session, its
        decode and its source descriptor as they were and removes the upgrade temp copy it
        made; on success the previous one is removed."""
        s = self.session
        old_tmp = getattr(s, "_config_csv_tmp", None)
        new_tmp: List[Optional[str]] = [None]
        with s._framing_warning_after():
            try:
                with s._undone_on_failure(whole=True):
                    self._set(path, new_tmp)
                    s.lock_ssp_from_sspa()
            except BaseException:
                self.discard_tmp(new_tmp[0])     # the kept decode reads the old one
                raise
        if old_tmp != new_tmp[0]:
            self.discard_tmp(old_tmp)

    def _set(self, path: Optional[str], new_tmp: List[Optional[str]]) -> None:
        """Write the CSV inputs for `path` and re-decode; the upgrade temp copy made goes in
        `new_tmp[0]`. The seed key is cleared, so the baseline is rebuilt even for the same
        path, whose file may have changed on disk."""
        s = self.session
        if path:
            norm, new_tmp[0] = self.normalized(path)
            self.report_warnings(norm, path)
            s.source["config_csv"] = path             # the original, for persistence
            s._config_csv_tmp = new_tmp[0]
            s._config_csv = norm
        else:
            s.source.pop("config_csv", None)
            s._config_csv_tmp = None
            s._config_csv = ""
        s._csv_seed_key = None
        s._redecode()
