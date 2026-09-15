"""Pytest configuration for som tests.

This module provides test fixtures for the som (Set-of-Mark) package.
"""

from unittest.mock import Mock, patch

import pytest


@pytest.fixture
def mock_icon_detector():
    with patch("omniparser.IconDetector") as mock_detector:
        instance = Mock()
        mock_detector.return_value = instance
        yield instance
