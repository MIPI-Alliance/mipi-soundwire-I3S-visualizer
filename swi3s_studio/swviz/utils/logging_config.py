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

