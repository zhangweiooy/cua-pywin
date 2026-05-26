import logging
from dataclasses import dataclass
from typing import Iterator

from .base import (
    BaseAccessibilityHandler,
    BaseAutomationHandler,
    BaseDesktopHandler,
    BaseFileHandler,
    BaseWindowHandler,
)
from .generic import GenericDesktopHandler, GenericFileHandler, GenericWindowHandler
from .windows import WindowsAccessibilityHandler, WindowsAutomationHandler

try:
    from .windows_window import WindowsWindowHandler
except Exception:  # pragma: no cover - import fallback for non-Windows tooling
    WindowsWindowHandler = GenericWindowHandler  # type: ignore[assignment]

logger = logging.getLogger(__name__)

OS_TYPE = "windows"


@dataclass(frozen=True)
class Handlers:
    """Named handler bundle returned by HandlerFactory.

    __iter__ preserves existing tuple-unpacking call sites while new code can
    use named attributes.
    """

    accessibility: BaseAccessibilityHandler
    automation: BaseAutomationHandler
    file: BaseFileHandler
    desktop: BaseDesktopHandler
    window: BaseWindowHandler

    def __iter__(self) -> Iterator[object]:
        yield self.accessibility
        yield self.automation
        yield self.file
        yield self.desktop
        yield self.window


class HandlerFactory:
    """Factory for creating Windows handlers."""

    @staticmethod
    def create_handlers() -> Handlers:
        return Handlers(
            accessibility=WindowsAccessibilityHandler(),
            automation=WindowsAutomationHandler(),
            file=GenericFileHandler(),
            desktop=GenericDesktopHandler(),
            window=WindowsWindowHandler(),
        )
