"""Results stores: an Arrow/Parquet command table (a persisted cache with no reader); an
audio store with min/max pyramids and an optional memmap backend; the temp directory a large
capture's Session keeps both in (`results_store.ResultsStore`).

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
