"""Tests for phase failure metrics reporting via the monitor_sandbox_operation decorator."""

from unittest.mock import MagicMock

from rock.admin.metrics.constants import MetricsConstants
from rock.admin.metrics.decorator import _check_and_report_phase_failures, _record_metrics
from rock.admin.proto.response import SandboxStatusResponse
from rock.deployments.constants import Status
from rock.deployments.status import PhaseStatus

BASE_ATTRS = {
    "operation": "get_status",
    "sandbox_id": "test-sandbox",
    "method": "get_status",
    "user_id": "default",
    "experiment_id": "default",
    "namespace": "default",
}

FAILED_PHASES = {
    "image_pull": PhaseStatus(status=Status.FAILED, message="pull failed"),
    "docker_run": PhaseStatus(status=Status.WAITING, message="waiting"),
}

TIMEOUT_PHASES = {
    "image_pull": PhaseStatus(status=Status.SUCCESS, message="ok"),
    "docker_run": PhaseStatus(status=Status.TIMEOUT, message="docker run timeout"),
}

SUCCESS_PHASES = {
    "image_pull": PhaseStatus(status=Status.SUCCESS, message="ok"),
    "docker_run": PhaseStatus(status=Status.SUCCESS, message="ok"),
}


def _make_result(phases: dict) -> SandboxStatusResponse:
    return SandboxStatusResponse(sandbox_id="test-sandbox", status=phases)


class TestCheckAndReportPhaseFailures:
    def test_reports_failed_phase(self):
        monitor = MagicMock()
        _check_and_report_phase_failures(monitor, _make_result(FAILED_PHASES), BASE_ATTRS)

        monitor.record_counter_by_name.assert_called_once()
        reported_attrs = monitor.record_counter_by_name.call_args[0][2]
        assert reported_attrs["sandbox_id"] == "test-sandbox"
        assert reported_attrs["phase_name"] == "image_pull"
        assert reported_attrs["phase_status"] == "failed"

    def test_reports_timeout_phase(self):
        monitor = MagicMock()
        _check_and_report_phase_failures(monitor, _make_result(TIMEOUT_PHASES), BASE_ATTRS)

        reported_attrs = monitor.record_counter_by_name.call_args[0][2]
        assert reported_attrs["phase_name"] == "docker_run"
        assert reported_attrs["phase_status"] == "timeout"

    def test_no_report_when_all_succeed(self):
        monitor = MagicMock()
        _check_and_report_phase_failures(monitor, _make_result(SUCCESS_PHASES), BASE_ATTRS)
        monitor.record_counter_by_name.assert_not_called()

    def test_triggers_for_get_status(self):
        monitor = MagicMock()
        _record_metrics(monitor, _make_result(FAILED_PHASES), {**BASE_ATTRS}, 0.0, "request")

        phase_calls = [
            c
            for c in monitor.record_counter_by_name.call_args_list
            if c[0][0] == MetricsConstants.SANDBOX_PHASE_FAILURE
        ]
        assert len(phase_calls) == 1

    def test_skips_for_non_get_status(self):
        monitor = MagicMock()
        _record_metrics(
            monitor, _make_result(FAILED_PHASES), {**BASE_ATTRS, "operation": "start_async"}, 0.0, "request"
        )

        phase_calls = [
            c
            for c in monitor.record_counter_by_name.call_args_list
            if c[0][0] == MetricsConstants.SANDBOX_PHASE_FAILURE
        ]
        assert len(phase_calls) == 0
