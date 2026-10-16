"""PySide6 UI for SWI3S Studio."""
from .singleton_pin import pin_singletons

pin_singletons()    # before any PySide call can over-release None or True (see singleton_pin)
