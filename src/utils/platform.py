"""
Copyright (c) 2023 MIPI Alliance and other contributors. All Rights Reserved.

Platform-specific configuration and utilities.

This module centralizes all platform-specific code to make it easier to:
- Support new platforms
- Modify platform-specific behavior
- Test platform-specific functionality
"""

from dataclasses import dataclass
import platform


@dataclass(frozen=True)
class PlatformConfig:
    """Platform-specific UI configuration settings."""

    # Text and layout adjustments
    text_size_offset: int = 0
    column_size: int = 35  # In pixels
    entry_width: int = 6   # In characters

    # Event handler type
    mousewheel_handler: str = 'mac'  # 'mac' or 'windows'

    @classmethod
    def for_current_platform(cls) -> 'PlatformConfig':
        """Create configuration for the current platform.

        Returns:
            PlatformConfig: Platform-specific settings
        """
        system = platform.system()

        if system == 'Windows':
            return cls(
                text_size_offset=-3,
                column_size=33,
                entry_width=6,
                mousewheel_handler='windows'
            )
        elif system == 'Darwin':  # macOS
            return cls(
                text_size_offset=0,
                column_size=35,
                entry_width=6,
                mousewheel_handler='mac'
            )
        else:  # Linux and others
            return cls(
                text_size_offset=0,
                column_size=35,
                entry_width=6,
                mousewheel_handler='mac'
            )
