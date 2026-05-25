import logging
from typing import Tuple

from .base import (
    BaseAccessibilityHandler,
    BaseAutomationHandler,
    BaseDesktopHandler,
    BaseFileHandler,
    BaseWindowHandler,
)
from .windows import WindowsAccessibilityHandler, WindowsAutomationHandler
from .generic import GenericDesktopHandler, GenericFileHandler, GenericWindowHandler

logger = logging.getLogger(__name__)

OS_TYPE = "windows"


class HandlerFactory:
    """Factory for creating Windows handlers."""

    @staticmethod
    def create_handlers() -> Tuple[
        BaseAccessibilityHandler,
        BaseAutomationHandler,
        BaseFileHandler,
        BaseDesktopHandler,
        BaseWindowHandler,
    ]:
        return (
            WindowsAccessibilityHandler(),
            WindowsAutomationHandler(),
            GenericFileHandler(),
            GenericDesktopHandler(),
            GenericWindowHandler(),
        )
