"""Tests for common port validation utilities."""
import pytest

from rock.common.port_validation import (
    PORT_FORWARD_MIN_PORT,
    PORT_FORWARD_MAX_PORT,
    PORT_FORWARD_EXCLUDED_PORTS,
    validate_port_forward_port,
)


class TestPortValidationConstants:
    """Tests for port validation constants."""

    def test_min_port_is_1024(self):
        """Minimum allowed port should be 1024."""
        assert PORT_FORWARD_MIN_PORT == 1024

    def test_max_port_is_65535(self):
        """Maximum allowed port should be 65535."""
        assert PORT_FORWARD_MAX_PORT == 65535

    def test_excluded_ports_contains_22(self):
        """SSH port 22 should be excluded."""
        assert 22 in PORT_FORWARD_EXCLUDED_PORTS


class TestValidatePortForwardPort:
    """Tests for validate_port_forward_port function."""

    def test_accepts_valid_port_in_range(self):
        """Valid port in allowed range should pass validation."""
        is_valid, error = validate_port_forward_port(8080)
        assert is_valid is True
        assert error is None

    def test_accepts_min_allowed_port(self):
        """Minimum allowed port (1024) should pass validation."""
        is_valid, error = validate_port_forward_port(1024)
        assert is_valid is True
        assert error is None

    def test_accepts_max_allowed_port(self):
        """Maximum allowed port (65535) should pass validation."""
        is_valid, error = validate_port_forward_port(65535)
        assert is_valid is True
        assert error is None

    def test_rejects_port_below_1024(self):
        """Port below 1024 should be rejected."""
        is_valid, error = validate_port_forward_port(1023)
        assert is_valid is False
        assert error is not None
        assert "1024" in error

    def test_rejects_port_above_65535(self):
        """Port above 65535 should be rejected."""
        is_valid, error = validate_port_forward_port(65536)
        assert is_valid is False
        assert error is not None
        assert "65535" in error

    def test_rejects_port_22(self):
        """SSH port 22 should be rejected."""
        is_valid, error = validate_port_forward_port(22)
        assert is_valid is False
        assert error is not None
        assert "22" in error

    def test_rejects_zero_port(self):
        """Port 0 should be rejected."""
        is_valid, error = validate_port_forward_port(0)
        assert is_valid is False
        assert error is not None

    def test_rejects_negative_port(self):
        """Negative port should be rejected."""
        is_valid, error = validate_port_forward_port(-1)
        assert is_valid is False
        assert error is not None
