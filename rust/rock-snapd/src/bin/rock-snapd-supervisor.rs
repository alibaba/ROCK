//! Bounded in-container supervisor for the Rust snapd daemon.

use std::path::{Path, PathBuf};
use std::process::{ExitCode, ExitStatus};
use std::time::Duration;

use rock_snapd::daemon::bootstrap::{load_config, DaemonError};
use rock_snapd::observability::structured_log::{
    emit_stderr, StructuredComponent, StructuredEvent, StructuredEventName, StructuredLogLevel,
    StructuredOutcome,
};
use thiserror::Error;
use tokio::process::{Child, Command};

const DEFAULT_MAX_RESTARTS: usize = 3;
const MAX_RESTARTS: usize = 10;
const _: () = assert!(MAX_RESTARTS >= DEFAULT_MAX_RESTARTS);
const MAX_BACKOFF: Duration = Duration::from_secs(2);
const GRACEFUL_CHILD_TIMEOUT: Duration = Duration::from_secs(60);

#[derive(Debug, Error)]
enum SupervisorError {
    #[error(transparent)]
    Config(#[from] DaemonError),
    #[error("Rust snapd supervisor configuration is invalid: {0}")]
    Invalid(String),
    #[error("Rust snapd supervisor process failed: {0}")]
    Process(String),
}

fn supervisor_error(code: &'static str) {
    emit_stderr(
        StructuredEvent::new(
            StructuredLogLevel::Error,
            StructuredEventName::DaemonLifecycle,
            StructuredComponent::Daemon,
            StructuredOutcome::Error,
        )
        .operation("supervisor")
        .error_code(code),
    );
}

fn main() -> ExitCode {
    let arguments = std::env::args_os().skip(1).collect::<Vec<_>>();
    let (config, max_restarts) = match arguments.as_slice() {
        [flag, path] if flag == "--config" => (PathBuf::from(path), DEFAULT_MAX_RESTARTS),
        [flag, path, restart_flag, value]
            if flag == "--config" && restart_flag == "--max-restarts" =>
        {
            let Some(value) = value.to_str().and_then(|value| value.parse::<usize>().ok()) else {
                supervisor_error("invalid_arguments");
                return ExitCode::from(2);
            };
            (PathBuf::from(path), value)
        }
        _ => {
            supervisor_error("invalid_arguments");
            return ExitCode::from(2);
        }
    };
    let runtime = match tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    {
        Ok(runtime) => runtime,
        Err(_) => {
            supervisor_error("runtime_initialization_failed");
            return ExitCode::from(1);
        }
    };
    match runtime.block_on(supervise(&config, max_restarts)) {
        Ok(code) => ExitCode::from(u8::try_from(code).unwrap_or(1)),
        Err(_) => {
            supervisor_error("supervisor_failed");
            ExitCode::from(1)
        }
    }
}

async fn supervise(config_path: &Path, max_restarts: usize) -> Result<i32, SupervisorError> {
    if max_restarts > MAX_RESTARTS {
        return Err(SupervisorError::Invalid(format!(
            "max restarts must be within 0..={MAX_RESTARTS}"
        )));
    }
    load_config(config_path)?;
    let daemon = sibling_daemon_binary()?;
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        .map_err(|error| SupervisorError::Process(error.to_string()))?;
    let mut interrupt = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())
        .map_err(|error| SupervisorError::Process(error.to_string()))?;
    let mut restarts = 0_usize;
    loop {
        emit_stderr(
            StructuredEvent::new(
                StructuredLogLevel::Info,
                StructuredEventName::DaemonLifecycle,
                StructuredComponent::Daemon,
                StructuredOutcome::Started,
            )
            .operation("supervised_daemon"),
        );
        let mut child = Command::new(&daemon)
            .arg("--config")
            .arg(config_path)
            .kill_on_drop(false)
            .spawn()
            .map_err(|error| SupervisorError::Process(error.to_string()))?;
        let outcome = tokio::select! {
            status = child.wait() => ChildOutcome::Exited(
                status.map_err(|error| SupervisorError::Process(error.to_string()))?
            ),
            signal = terminate.recv() => {
                if signal.is_none() {
                    return Err(SupervisorError::Process("SIGTERM stream closed".into()));
                }
                ChildOutcome::Stopping(stop_supervised_process(&mut child, libc::SIGTERM).await?)
            },
            signal = interrupt.recv() => {
                if signal.is_none() {
                    return Err(SupervisorError::Process("SIGINT stream closed".into()));
                }
                ChildOutcome::Stopping(stop_supervised_process(&mut child, libc::SIGINT).await?)
            },
        };
        match outcome {
            ChildOutcome::Stopping(status) => {
                let code = shell_status(status);
                let mut event = StructuredEvent::new(
                    if code == 0 {
                        StructuredLogLevel::Info
                    } else {
                        StructuredLogLevel::Error
                    },
                    StructuredEventName::DaemonLifecycle,
                    StructuredComponent::Daemon,
                    if code == 0 {
                        StructuredOutcome::Stopped
                    } else {
                        StructuredOutcome::Error
                    },
                )
                .operation("supervised_daemon");
                if code != 0 {
                    event = event.error_code("graceful_stop_failed");
                }
                emit_stderr(event);
                return Ok(code);
            }
            ChildOutcome::Exited(status) if status.success() => {
                emit_stderr(
                    StructuredEvent::new(
                        StructuredLogLevel::Info,
                        StructuredEventName::DaemonLifecycle,
                        StructuredComponent::Daemon,
                        StructuredOutcome::Stopped,
                    )
                    .operation("supervised_daemon"),
                );
                return Ok(0);
            }
            ChildOutcome::Exited(status) => {
                if restarts >= max_restarts {
                    emit_stderr(
                        StructuredEvent::new(
                            StructuredLogLevel::Error,
                            StructuredEventName::DaemonLifecycle,
                            StructuredComponent::Daemon,
                            StructuredOutcome::Error,
                        )
                        .operation("supervised_daemon")
                        .error_code("restart_not_permitted"),
                    );
                    return Ok(shell_status(status));
                }
                emit_stderr(
                    StructuredEvent::new(
                        StructuredLogLevel::Warning,
                        StructuredEventName::DaemonLifecycle,
                        StructuredComponent::Daemon,
                        StructuredOutcome::Error,
                    )
                    .operation("supervised_daemon")
                    .error_code("daemon_restarting"),
                );
                restarts = restarts.saturating_add(1);
                let shift = u32::try_from(restarts.saturating_sub(1)).unwrap_or(u32::MAX);
                let multiplier = 1_u32.checked_shl(shift).unwrap_or(u32::MAX);
                let delay = Duration::from_millis(250)
                    .checked_mul(multiplier)
                    .unwrap_or(MAX_BACKOFF)
                    .min(MAX_BACKOFF);
                tokio::time::sleep(delay).await;
            }
        }
    }
}

enum ChildOutcome {
    Exited(ExitStatus),
    Stopping(ExitStatus),
}

async fn stop_supervised_process(
    child: &mut Child,
    signal: libc::c_int,
) -> Result<ExitStatus, SupervisorError> {
    let Some(pid) = child.id() else {
        return child
            .wait()
            .await
            .map_err(|error| SupervisorError::Process(error.to_string()));
    };
    // SAFETY: `pid` is the still-owned direct child returned by Command.
    let result = unsafe { libc::kill(pid as libc::pid_t, signal) };
    if result != 0 {
        let error = std::io::Error::last_os_error();
        if error.raw_os_error() != Some(libc::ESRCH) {
            return Err(SupervisorError::Process(error.to_string()));
        }
    }
    match tokio::time::timeout(GRACEFUL_CHILD_TIMEOUT, child.wait()).await {
        Ok(status) => status.map_err(|error| SupervisorError::Process(error.to_string())),
        Err(_) => {
            child
                .kill()
                .await
                .map_err(|error| SupervisorError::Process(error.to_string()))?;
            child
                .wait()
                .await
                .map_err(|error| SupervisorError::Process(error.to_string()))
        }
    }
}

fn sibling_daemon_binary() -> Result<PathBuf, SupervisorError> {
    let executable =
        std::env::current_exe().map_err(|error| SupervisorError::Process(error.to_string()))?;
    let parent = executable.parent().ok_or_else(|| {
        SupervisorError::Process("supervisor executable has no parent directory".into())
    })?;
    let daemon = parent.join("rock-snapd");
    if !daemon.is_file() {
        return Err(SupervisorError::Process(format!(
            "sibling Rust snapd binary {} is missing",
            daemon.display()
        )));
    }
    Ok(daemon)
}

fn shell_status(status: ExitStatus) -> i32 {
    use std::os::unix::process::ExitStatusExt;

    status
        .code()
        .or_else(|| status.signal().map(|signal| 128 + signal))
        .unwrap_or(1)
}
