"""
Copyright (c) 2023 MIPI Alliance and other contributors. All Rights Reserved.

Logging configuration utilities.
"""

import logging
from typing import Dict


# Global logger cache
_loggers: Dict[str, logging.Logger] = {}


def get_logger(component: str) -> logging.Logger:
    """Get a logger for a specific component.

    Args:
        component: Component name (e.g., 'drawing', 'io', 'clash')

    Returns:
        Logger instance for the component
    """
    if component not in _loggers:
        _loggers[component] = logging.getLogger(f'swi3s_visualizer.{component}')
    return _loggers[component]


def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure application logging.

    Args:
        verbose: Enable INFO level logging (default: ERROR - quiet mode for GUI)

    Returns:
        Root logger instance
    """
    # In non-verbose mode, only show errors (not warnings)
    # Warnings like clash detection are shown in the error panel instead
    log_level = logging.INFO if verbose else logging.ERROR

    # Create logger
    logger = logging.getLogger('swi3s_visualizer')
    logger.setLevel(log_level)

    # Check if handlers already exist to avoid duplicates
    if not logger.handlers:
        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(log_level)

        # Format: [LEVEL] Component: Message
        formatter = logging.Formatter(
            '[%(levelname)s] %(name)s: %(message)s'
        )
        console_handler.setFormatter(formatter)

        logger.addHandler(console_handler)
    else:
        # Update existing handler levels
        for handler in logger.handlers:
            handler.setLevel(log_level)

    return logger
