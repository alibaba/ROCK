from unittest.mock import MagicMock

import pytest

from rock.sdk.sandbox.deploy import Deploy


@pytest.fixture
def deploy() -> Deploy:
    """Create a Deploy instance with working_dir set."""
    mock_sandbox = MagicMock()
    d = Deploy(mock_sandbox)
    d._working_dir = "/tmp/rock_workdir_test"
    return d


class TestDeployFormat:
    """Tests for Deploy.format method."""

    def test_format_dollar_syntax(self, deploy: Deploy):
        """Test ${} syntax with working_dir substitution."""
        result = deploy.format("mv ${working_dir}/config.json /root/.app/")

        assert result == "mv /tmp/rock_workdir_test/config.json /root/.app/"

    def test_format_angle_syntax(self, deploy: Deploy):
        """Test <<>> syntax substitution."""
        deploy._working_dir = "/tmp/rock_workdir_xyz789"

        result = deploy.format("cat <<working_dir>>/<<prompt>>", prompt="test.txt")

        assert result == "cat /tmp/rock_workdir_xyz789/test.txt"

    def test_format_mixed_syntax(self, deploy: Deploy):
        """Test mixing ${} and <<>> syntax in same template."""
        deploy._working_dir = "/tmp/rock_workdir_mixed"

        result = deploy.format("cd ${working_dir} && cat <<config>>/<<file>>", config="etc", file="app.conf")

        assert result == "cd /tmp/rock_workdir_mixed && cat etc/app.conf"

    def test_format_with_kwargs(self, deploy: Deploy):
        """Test format with kwargs only."""
        deploy._working_dir = None

        result = deploy.format("${user}/${project}", user="admin", project="myapp")

        assert result == "admin/myapp"

    def test_format_undefined_var_preserved(self, deploy: Deploy):
        """Test that undefined variables are preserved."""
        result = deploy.format("echo ${working_dir}/${undefined}")

        assert result == "echo /tmp/rock_workdir_test/${undefined}"

    def test_format_shell_syntax_preserved(self, deploy: Deploy):
        """Test that shell << >> syntax is preserved."""
        deploy._working_dir = "/work"

        result = deploy.format("echo $((3 << 2 >> 1))")

        assert result == "echo $((3 << 2 >> 1))"

    def test_format_angle_brackets_unknown_var_preserved(self, deploy: Deploy):
        """Test that <<unknown_var>> without substitution is preserved."""
        deploy._working_dir = "/work"

        result = deploy.format("cat <<unknown>>")

        assert result == "cat <<unknown>>"

    def test_format_empty_template(self, deploy: Deploy):
        """Test format with empty template."""
        deploy._working_dir = None

        result = deploy.format("")

        assert result == ""
