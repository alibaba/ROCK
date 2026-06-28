"""Unit tests for rock-fc.yml activation.

Verifies review finding:
- C2: rock-fc.yml does not set runtime.operator_type='fc', FC mode cannot be activated.
"""

from pathlib import Path

import pytest
import yaml

FC_YAML = Path(__file__).resolve().parents[5] / "rock-conf" / "rock-fc.yml"


class TestFCYamlActivation:
    @pytest.mark.xfail(reason="C2: rock-fc.yml missing runtime.operator_type='fc'")
    def test_operator_type_is_fc(self):
        data = yaml.safe_load(FC_YAML.read_text())
        assert data.get("runtime", {}).get("operator_type") == "fc", (
            "rock-fc.yml must activate FC operator via runtime.operator_type='fc'"
        )

    def test_fc_section_present(self):
        data = yaml.safe_load(FC_YAML.read_text())
        assert "fc" in data, "rock-fc.yml must contain an fc configuration section"
