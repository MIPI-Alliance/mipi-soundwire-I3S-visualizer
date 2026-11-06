"""Results stores: an Arrow/Parquet command table for display and export; an audio store with
min/max pyramids and an optional memmap backend. Session keeps its results in memory.

`AudioStore` imports without pyarrow; the command-table names load pyarrow on first use."""
from .audio_store import AudioStore, container_bits, sign_extend

_COMMAND_STORE_NAMES = ("SCHEMA", "commands_to_table", "save_commands", "load_commands",
                        "register_writes")

__all__ = [*_COMMAND_STORE_NAMES, "AudioStore", "sign_extend", "container_bits"]


def __getattr__(name: str):
    if name in _COMMAND_STORE_NAMES:
        from . import command_store
        return getattr(command_store, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
