"""Shared time cursor + visible range — the spine of synchronized navigation.

Every panel reads/writes one TimeCursor; selecting a command, clicking the grid,
or scrubbing the timeline all funnel through here so the views stay in sync.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class TimeCursor(QObject):
    # 'qlonglong' (64-bit), not int: sample numbers exceed 2**31 for multi-second
    # captures at hundreds of MHz, which overflows a C++ 32-bit int and breaks
    # signal/slot dispatch.
    sampleChanged = Signal('qlonglong')  # absolute capture sample number

    def __init__(self) -> None:
        super().__init__()
        self._sample = 0

    @property
    def sample(self) -> int:
        return self._sample

    def set_sample(self, sample: int) -> None:
        sample = int(sample)
        if sample != self._sample:
            self._sample = sample
            self.sampleChanged.emit(sample)
