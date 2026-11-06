"""A WAV reader for the files AudioStore.export_wav writes (WAVE_FORMAT_EXTENSIBLE).

`wave.open` reads only WAVE_FORMAT_PCM before Python 3.12, so tests read exported files
through `open(path)` here: the same getters as `wave.Wave_read`, plus the extensible
header's fields.
"""
import pathlib
import struct

import numpy as np

SUBTYPE_PCM = bytes.fromhex("0100000000001000800000aa00389b71")


class WavFile:
    def __init__(self, path):
        blob = pathlib.Path(path).read_bytes()
        assert blob[:4] == b"RIFF" and blob[8:12] == b"WAVE", "not a RIFF/WAVE file"
        assert struct.unpack("<I", blob[4:8])[0] == len(blob) - 8, "RIFF size is wrong"
        self.fmt = None
        self.data = b""
        pos = 12
        last = b""
        while pos < len(blob):
            assert pos + 8 <= len(blob), "a chunk header runs past the end of the file"
            cid = blob[pos:pos + 4]
            size = struct.unpack("<I", blob[pos + 4:pos + 8])[0]
            assert pos + 8 + size <= len(blob), f"the {cid!r} chunk runs past the end of the file"
            body = blob[pos + 8:pos + 8 + size]
            if cid == b"fmt ":
                self.fmt = body
            elif cid == b"data":
                self.data = body
            last = cid
            pos += 8 + size + (size & 1)
        assert pos == len(blob), "the last chunk is not word-padded to the end of the file"
        assert last == b"data", "the data chunk is not the last chunk"
        assert self.fmt is not None, "no fmt chunk"
        (self.format_tag, self.nchannels, self.framerate, self.byte_rate, self.block_align,
         self.container_bits) = struct.unpack("<HHIIHH", self.fmt[:16])
        self.cb_size = struct.unpack("<H", self.fmt[16:18])[0] if len(self.fmt) > 16 else 0
        self.valid_bits = self.channel_mask = self.subformat = None
        if len(self.fmt) >= 40:
            self.valid_bits, self.channel_mask = struct.unpack("<HI", self.fmt[18:24])
            self.subformat = self.fmt[24:40]
        assert len(self.data) % self.block_align == 0, "data is not a whole number of frames"
        self._pos = 0

    def getnchannels(self):
        return self.nchannels

    def getsampwidth(self):
        return self.container_bits // 8

    def getframerate(self):
        return self.framerate

    def getnframes(self):
        return len(self.data) // self.block_align

    def readframes(self, n):
        raw = self.data[self._pos * self.block_align:(self._pos + n) * self.block_align]
        self._pos += n
        return raw

    def samples(self):
        """The signed integer samples, frames x channels, as stored (left-justified)."""
        width = self.container_bits // 8
        if width == 3:
            b = np.frombuffer(self.data, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
            v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
            v = np.where(v & 0x800000, v - (1 << 24), v)
        else:
            v = np.frombuffer(self.data, dtype=f"<i{width}")
        return v.reshape(-1, self.nchannels).astype(np.int64)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def open(path, mode="rb"):  # noqa: A001 - mirrors wave.open
    return WavFile(path)
