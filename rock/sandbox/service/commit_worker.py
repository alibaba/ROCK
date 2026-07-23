import asyncio
import base64
import re
import shlex
import time
from datetime import datetime

from pydantic import BaseModel

from rock import env_vars
from rock.actions import CommandResponse, CommitErrorCode, CommitPhase, CommitStatusResponse
from rock.admin.proto.request import CommitRequest, SandboxCommand
from rock.deployments.constants import Port
from rock.sandbox.remote_sandbox import RemoteSandboxRuntime
from rock.sdk.common.exceptions import WorkerCommitError
from rock.utils import ImageUtil, extract_nohup_pid

COMMIT_TIMEOUT_SECONDS = 7200
WORKER_STATUS_DIR = f"{env_vars.ROCK_SERVICE_STATUS_DIR}/commit"
DISPATCH_TIMEOUT_SECONDS = 30
ERROR_MESSAGE_MAX_BYTES = 4096

_PROTOCOL_KEYS = {
    "dispatch",
    "phase",
    "image_tag",
    "pid",
    "pid_start_ticks",
    "run_started_at",
    "started_at",
    "completed_at",
    "exit_code",
    "failed_stage",
    "error_code",
    "error_message_b64",
}
_REQUIRED_STATE_KEYS = {"phase", "image_tag", "pid", "pid_start_ticks", "run_started_at", "started_at"}
_PID_MARKER = re.compile(r"PIDSTART[0-9]+PIDEND")
_TEMPLATE_PLACEHOLDER = re.compile(r"__([A-Z_]+)__")


class WorkerCommitState(BaseModel):
    phase: CommitPhase
    image_tag: str
    pid: int
    pid_start_ticks: int
    run_started_at: int
    started_at: str
    completed_at: str | None = None
    exit_code: int | None = None
    failed_stage: str | None = None
    error_code: CommitErrorCode | None = None
    error_message: str | None = None

    def to_response(self, sandbox_id: str) -> CommitStatusResponse:
        return CommitStatusResponse(
            sandbox_id=sandbox_id,
            image_tag=self.image_tag,
            phase=self.phase,
            started_at=self.started_at,
            completed_at=self.completed_at,
            exit_code=self.exit_code,
            failed_stage=self.failed_stage,
            error_code=self.error_code,
            error_message=self.error_message,
        )


def _parse_protocol(stdout: str) -> tuple[str | None, WorkerCommitState]:
    try:
        fields: dict[str, str] = {}
        for line in stdout.split("\n"):
            if not line:
                continue
            if _PID_MARKER.fullmatch(line):
                continue
            if "=" not in line:
                raise ValueError(f"invalid protocol line: {line!r}")
            key, value = line.split("=", 1)
            if key not in _PROTOCOL_KEYS:
                raise ValueError(f"unknown protocol key: {key!r}")
            if key in fields:
                raise ValueError(f"duplicate protocol key: {key!r}")
            fields[key] = value

        missing = _REQUIRED_STATE_KEYS - fields.keys()
        if missing:
            raise ValueError(f"missing protocol keys: {', '.join(sorted(missing))}")

        dispatch = fields.pop("dispatch", None)
        if dispatch is not None and dispatch not in {"NEW", "EXISTING"}:
            raise ValueError(f"invalid dispatch value: {dispatch!r}")

        error_message_b64 = fields.pop("error_message_b64", None)
        if error_message_b64 is not None:
            error_message = base64.b64decode(error_message_b64, validate=True).decode("utf-8", errors="replace")
            error_message_bytes = error_message.encode()
            if len(error_message_bytes) > ERROR_MESSAGE_MAX_BYTES:
                error_message = error_message_bytes[-ERROR_MESSAGE_MAX_BYTES:].decode("utf-8", errors="ignore")
            fields["error_message"] = error_message

        return dispatch, WorkerCommitState.model_validate(fields)
    except WorkerCommitError:
        raise
    except Exception as exc:
        raise WorkerCommitError(CommitErrorCode.STATUS_CORRUPTED, str(exc)) from exc


def _quote(value: str | int) -> str:
    return shlex.quote(str(value))


def _registry_from_image(image_tag: str) -> str:
    registry, _ = ImageUtil.parse_registry_and_others(image_tag)
    if registry:
        return registry
    first_component, separator, _ = image_tag.partition("/")
    if separator and first_component == "localhost":
        return first_component
    return "docker.io"


def _render(template: str, **substitutions: str) -> str:
    normalized = {name.upper(): value for name, value in substitutions.items()}

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in normalized:
            raise ValueError(f"missing shell template substitution: {name}")
        return normalized[name]

    return _TEMPLATE_PLACEHOLDER.sub(replace, template)


_SUPERVISOR_SCRIPT = r"""
set -u
exec 9>&-
pid_file=__PID_FILE__
lock_file=__LOCK_FILE__
log_file=__LOG_FILE__
image_tag=__IMAGE_TAG__
run_started_at=__RUN_STARTED_AT__
started_at=__STARTED_AT__

exec 9>"$lock_file"
flock -x 9 || exit 1
[ -f "$pid_file" ] || exit 0
published_phase=$(awk -F= '$1 == "phase" {print substr($0, index($0,"=")+1)}' "$pid_file")
published_run_started_at=$(awk -F= '$1 == "run_started_at" {print substr($0, index($0,"=")+1)}' "$pid_file")
published_pid=$(awk -F= '$1 == "pid" {print substr($0, index($0,"=")+1)}' "$pid_file")
published_pid_start_ticks=$(awk -F= '$1 == "pid_start_ticks" {print substr($0, index($0,"=")+1)}' "$pid_file")
supervisor_pid=$$
supervisor_pid_start_ticks=$(awk '{print $22}' "/proc/$supervisor_pid/stat" 2>/dev/null) || exit 0
[ "$published_phase" = RUNNING ] || exit 0
[ "$published_run_started_at" = "$run_started_at" ] || exit 0
[ "$published_pid" = "$supervisor_pid" ] || exit 0
[ "$published_pid_start_ticks" = "$supervisor_pid_start_ticks" ] || exit 0
exec > "$log_file" 2>&1 || exit 1
exec 9>&-

if docker_config=$(mktemp -d); then
  trap 'rm -rf "$docker_config"' EXIT
  export DOCKER_CONFIG="$docker_config"
  __WRAPPED_DOCKER_SEQUENCE__
  result=$?
else
  result=1
fi

case "$result" in
  0)
    phase=SUCCEEDED
    failed_stage=
    error_code=
    ;;
  11)
    phase=FAILED
    failed_stage=login
    error_code=LOGIN_FAILED
    ;;
  12)
    phase=FAILED
    failed_stage=commit
    error_code=COMMIT_FAILED
    ;;
  13)
    phase=FAILED
    failed_stage=push
    error_code=PUSH_FAILED
    ;;
  124|137)
    phase=FAILED
    failed_stage=supervisor
    error_code=TIMEOUT
    ;;
  *)
    phase=FAILED
    failed_stage=supervisor
    error_code=COMMIT_FAILED
    ;;
esac
completed_at=$(date '+%Y-%m-%dT%H:%M:%S%z')

exec 9>"$lock_file"
flock -x 9 || exit 1
[ -f "$pid_file" ] || exit 0
current_run_started_at=$(awk -F= '$1 == "run_started_at" {print substr($0, index($0,"=")+1)}' "$pid_file")
[ "$current_run_started_at" = "$run_started_at" ] || exit 0
pid=$(awk -F= '$1 == "pid" {print substr($0, index($0,"=")+1)}' "$pid_file")
pid_start_ticks=$(awk -F= '$1 == "pid_start_ticks" {print substr($0, index($0,"=")+1)}' "$pid_file")
pid_file_tmp="$pid_file.tmp.$$"
publish_terminal_state() {
  if [ "$phase" = SUCCEEDED ]; then
    printf '%s\n' \
      'phase=SUCCEEDED' \
      "image_tag=$image_tag" \
      "pid=$pid" \
      "pid_start_ticks=$pid_start_ticks" \
      "run_started_at=$run_started_at" \
      "started_at=$started_at" \
      "completed_at=$completed_at" \
      'exit_code=0' > "$pid_file_tmp" || return 1
  else
    printf '%s\n' \
      'phase=FAILED' \
      "image_tag=$image_tag" \
      "pid=$pid" \
      "pid_start_ticks=$pid_start_ticks" \
      "run_started_at=$run_started_at" \
      "started_at=$started_at" \
      "completed_at=$completed_at" \
      "exit_code=$result" \
      "failed_stage=$failed_stage" \
      "error_code=$error_code" > "$pid_file_tmp" || return 1
  fi
  mv "$pid_file_tmp" "$pid_file"
}
if ! publish_terminal_state; then
  rm -f "$pid_file_tmp"
  exit 1
fi
""".strip()


_START_SCRIPT = r"""
status_dir=__STATUS_DIR__
pid_file=__PID_FILE__
lock_file=__LOCK_FILE__
log_file=__LOG_FILE__
sandbox_id=__SANDBOX_ID__
image_tag=__IMAGE_TAG__
run_started_at=__RUN_STARTED_AT__
started_at=__STARTED_AT__

set -u
mkdir -p "$status_dir"
exec 9>"$lock_file"
flock -x 9 || {
  printf 'error_code=DISPATCH_FAILED\n'
  exit 22
}

container_name=$(docker inspect --type container --format '{{.Name}}' "$sandbox_id" 2>/dev/null) || {
  printf 'error_code=SANDBOX_CONTAINER_NOT_FOUND\n'
  exit 20
}
[ "$container_name" = "/$sandbox_id" ] || {
  printf 'error_code=SANDBOX_CONTAINER_NOT_FOUND\n'
  exit 20
}

if [ -f "$pid_file" ]; then
  old_phase=$(awk -F= '$1 == "phase" {print substr($0, index($0,"=")+1)}' "$pid_file")
  if [ "$old_phase" = RUNNING ]; then
    old_image_tag=$(awk -F= '$1 == "image_tag" {print substr($0, index($0,"=")+1)}' "$pid_file")
    old_pid=$(awk -F= '$1 == "pid" {print substr($0, index($0,"=")+1)}' "$pid_file")
    old_pid_start_ticks=$(awk -F= '$1 == "pid_start_ticks" {print substr($0, index($0,"=")+1)}' "$pid_file")
    current_pid_state=
    current_pid_start_ticks=
    case "$old_pid" in
      ''|*[!0-9]*) ;;
      *)
        if [ -r "/proc/$old_pid/stat" ]; then
          current_pid_state=$(awk '{print $3}' "/proc/$old_pid/stat" 2>/dev/null || true)
          current_pid_start_ticks=$(awk '{print $22}' "/proc/$old_pid/stat" 2>/dev/null || true)
        fi
        ;;
    esac
    if [ "$current_pid_state" != Z ] && [ -n "$current_pid_start_ticks" ] && [ "$current_pid_start_ticks" = "$old_pid_start_ticks" ]; then
      if [ "$old_image_tag" = "$image_tag" ]; then
        printf 'dispatch=EXISTING\n'
        cat "$pid_file"
        exit 0
      fi
      printf 'error_code=COMMIT_CONFLICT\n'
      exit 21
    fi
  fi
fi

supervisor_command=__SUPERVISOR_COMMAND__
terminate_supervisor() {
  kill -TERM -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}
set -m
nohup bash -c "$supervisor_command" < /dev/null > /dev/null 2>&1 &
pid=$!
disown "$pid" 2>/dev/null || true
set +m
printf 'PIDSTART%sPIDEND\n' "$pid"
pid_start_ticks=$(awk '{print $22}' "/proc/$pid/stat") || {
  terminate_supervisor
  printf 'error_code=DISPATCH_FAILED\n'
  exit 22
}

pid_file_tmp="$pid_file.tmp.$$"
publish_running_state() {
  printf '%s\n' \
    'phase=RUNNING' \
    "image_tag=$image_tag" \
    "pid=$pid" \
    "pid_start_ticks=$pid_start_ticks" \
    "run_started_at=$run_started_at" \
    "started_at=$started_at" > "$pid_file_tmp" || return 1
  mv "$pid_file_tmp" "$pid_file"
}
if ! publish_running_state; then
  rm -f "$pid_file_tmp"
  terminate_supervisor
  printf 'error_code=DISPATCH_FAILED\n'
  exit 22
fi
printf 'dispatch=NEW\n'
cat "$pid_file"
""".strip()


_QUERY_SCRIPT = rf"""
status_dir=__STATUS_DIR__
pid_file=__PID_FILE__
lock_file=__LOCK_FILE__
log_file=__LOG_FILE__

set -u
mkdir -p "$status_dir"
exec 9>"$lock_file"
flock -x 9 || {{
  printf 'error_code=DISPATCH_FAILED\n'
  exit 22
}}

if [ ! -f "$pid_file" ]; then
  printf 'error_code=STATUS_NOT_FOUND\n'
  exit 23
fi

while :; do
  phase=$(awk -F= '$1 == "phase" {{print substr($0, index($0,"=")+1)}}' "$pid_file")
  if [ "$phase" = SUCCEEDED ]; then
    cat "$pid_file"
    exit 0
  fi
  if [ "$phase" = FAILED ]; then
    cat "$pid_file"
    error_message_b64=$(tail -c {ERROR_MESSAGE_MAX_BYTES} "$log_file" 2>/dev/null | base64 | tr -d '\n')
    printf 'error_message_b64=%s\n' "$error_message_b64"
    exit 0
  fi
  if [ "$phase" != RUNNING ]; then
    cat "$pid_file"
    exit 0
  fi

  pid=$(awk -F= '$1 == "pid" {{print substr($0, index($0,"=")+1)}}' "$pid_file")
  pid_start_ticks=$(awk -F= '$1 == "pid_start_ticks" {{print substr($0, index($0,"=")+1)}}' "$pid_file")
  original_run_started_at=$(awk -F= '$1 == "run_started_at" {{print substr($0, index($0,"=")+1)}}' "$pid_file")
  current_pid_state=
  current_pid_start_ticks=
  case "$pid" in
    ''|*[!0-9]*) ;;
    *)
      if [ -r "/proc/$pid/stat" ]; then
        current_pid_state=$(awk '{{print $3}}' "/proc/$pid/stat" 2>/dev/null || true)
        current_pid_start_ticks=$(awk '{{print $22}}' "/proc/$pid/stat" 2>/dev/null || true)
      fi
      ;;
  esac
  if [ "$current_pid_state" != Z ] && [ -n "$current_pid_start_ticks" ] && [ "$current_pid_start_ticks" = "$pid_start_ticks" ]; then
    cat "$pid_file"
    exit 0
  fi

  current_phase=$(awk -F= '$1 == "phase" {{print substr($0, index($0,"=")+1)}}' "$pid_file")
  current_run_started_at=$(awk -F= '$1 == "run_started_at" {{print substr($0, index($0,"=")+1)}}' "$pid_file")
  if [ "$current_phase" != RUNNING ] || [ "$current_run_started_at" != "$original_run_started_at" ]; then
    continue
  fi

  image_tag=$(awk -F= '$1 == "image_tag" {{print substr($0, index($0,"=")+1)}}' "$pid_file")
  started_at=$(awk -F= '$1 == "started_at" {{print substr($0, index($0,"=")+1)}}' "$pid_file")
  completed_at=$(date '+%Y-%m-%dT%H:%M:%S%z')
  pid_file_tmp="$pid_file.tmp.$$"
  publish_process_lost_state() {{
    printf '%s\n' \
      'phase=FAILED' \
      "image_tag=$image_tag" \
      "pid=$pid" \
      "pid_start_ticks=$pid_start_ticks" \
      "run_started_at=$original_run_started_at" \
      "started_at=$started_at" \
      "completed_at=$completed_at" \
      'exit_code=-1' \
      'failed_stage=supervisor' \
      'error_code=PROCESS_LOST' > "$pid_file_tmp" || return 1
    mv "$pid_file_tmp" "$pid_file"
  }}
  if ! publish_process_lost_state; then
    rm -f "$pid_file_tmp"
    printf 'error_code=DISPATCH_FAILED\n'
    exit 22
  fi
  printf 'worker commit supervisor process lost\n' >> "$log_file"
done
""".strip()


class CommitShellBuilder:
    def __init__(self, status_dir: str):
        self._status_dir = status_dir

    def _status_paths(self, sandbox_id: str) -> tuple[str, str, str, str]:
        if "\x00" in sandbox_id or "/" in sandbox_id:
            raise ValueError("sandbox_id cannot contain NUL or '/'")
        prefix = f"{self._status_dir}/{sandbox_id}"
        return self._status_dir, f"{prefix}.pid", f"{prefix}.lock", f"{prefix}.log"

    def build_start(
        self,
        request: CommitRequest,
        *,
        started_at: str | None = None,
        run_started_at: int | None = None,
    ) -> str:
        started_at = started_at or datetime.now().astimezone().isoformat()
        run_started_at = run_started_at if run_started_at is not None else time.time_ns()
        status_dir, pid_file, lock_file, log_file = self._status_paths(request.sandbox_id)
        registry = _registry_from_image(request.image_tag)
        login = (
            f"printf '%s' {_quote(request.password)} | docker login {_quote(registry)} "
            f"--username {_quote(request.username)} --password-stdin"
        )
        docker_sequence = "\n".join(
            (
                f"{login} || exit 11",
                f"docker commit {_quote(request.sandbox_id)} {_quote(request.image_tag)} || exit 12",
                f"docker push {_quote(request.image_tag)} || exit 13",
            )
        )
        wrapped_docker_sequence = (
            f"timeout --signal=TERM --kill-after=10s {COMMIT_TIMEOUT_SECONDS} bash -c {_quote(docker_sequence)}"
        )
        supervisor = _render(
            _SUPERVISOR_SCRIPT,
            pid_file=_quote(pid_file),
            lock_file=_quote(lock_file),
            log_file=_quote(log_file),
            image_tag=_quote(request.image_tag),
            run_started_at=_quote(run_started_at),
            started_at=_quote(started_at),
            wrapped_docker_sequence=wrapped_docker_sequence,
        )
        return _render(
            _START_SCRIPT,
            status_dir=_quote(status_dir),
            pid_file=_quote(pid_file),
            lock_file=_quote(lock_file),
            log_file=_quote(log_file),
            sandbox_id=_quote(request.sandbox_id),
            image_tag=_quote(request.image_tag),
            run_started_at=_quote(run_started_at),
            started_at=_quote(started_at),
            supervisor_command=_quote(supervisor),
        )

    def build_query(self, sandbox_id: str) -> str:
        status_dir, pid_file, lock_file, log_file = self._status_paths(sandbox_id)
        return _render(
            _QUERY_SCRIPT,
            status_dir=_quote(status_dir),
            pid_file=_quote(pid_file),
            lock_file=_quote(lock_file),
            log_file=_quote(log_file),
        )


def _response_error(response: CommandResponse) -> WorkerCommitError | None:
    error_codes = []
    for line in response.stdout.split("\n"):
        if line.startswith("error_code="):
            error_codes.append(line.removeprefix("error_code="))
    if len(error_codes) != 1:
        return None
    try:
        code = CommitErrorCode(error_codes[0])
    except ValueError:
        return None
    return WorkerCommitError(code, code.value)


class CommitWorkerExecutor:
    def __init__(
        self,
        *,
        _shell_builder: CommitShellBuilder | None = None,
    ):
        self._shell_builder = _shell_builder or CommitShellBuilder(status_dir=WORKER_STATUS_DIR)

    async def _execute(self, host_ip: str, sandbox_id: str, command: str) -> CommandResponse:
        runtime = RemoteSandboxRuntime(host=host_ip, port=Port.PROXY.value)
        request = SandboxCommand(
            sandbox_id=sandbox_id,
            command=["bash", "-c", command],
            shell=False,
            check=False,
            timeout=DISPATCH_TIMEOUT_SECONDS,
        )
        try:
            response = await asyncio.wait_for(runtime.execute(request), timeout=DISPATCH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            raise WorkerCommitError(CommitErrorCode.WORKER_UNREACHABLE, "worker execute timed out") from exc
        except Exception as exc:
            raise WorkerCommitError(CommitErrorCode.WORKER_UNREACHABLE, str(exc)) from exc
        return response

    async def start(self, host_ip: str, request: CommitRequest) -> WorkerCommitState:
        response = await self._execute(
            host_ip,
            request.sandbox_id,
            self._shell_builder.build_start(request),
        )
        if response.exit_code not in {0, None}:
            error = _response_error(response)
            if error is not None:
                raise error
            raise WorkerCommitError(CommitErrorCode.DISPATCH_FAILED, "worker commit dispatch failed")

        dispatch, state = _parse_protocol(response.stdout)
        if dispatch is None:
            raise WorkerCommitError(CommitErrorCode.STATUS_CORRUPTED, "start response is missing dispatch")
        if dispatch == "NEW":
            marker_pid = extract_nohup_pid(response.stdout)
            if marker_pid is None or marker_pid != state.pid:
                raise WorkerCommitError(CommitErrorCode.DISPATCH_FAILED, "worker commit PID marker mismatch")
        return state

    async def get_status(self, host_ip: str, sandbox_id: str) -> WorkerCommitState:
        response = await self._execute(host_ip, sandbox_id, self._shell_builder.build_query(sandbox_id))
        if response.exit_code not in {0, None}:
            error = _response_error(response)
            if error is not None:
                raise error
            raise WorkerCommitError(CommitErrorCode.DISPATCH_FAILED, "worker commit status query failed")

        dispatch, state = _parse_protocol(response.stdout)
        if dispatch is not None:
            raise WorkerCommitError(CommitErrorCode.STATUS_CORRUPTED, "query response contains dispatch")
        return state
