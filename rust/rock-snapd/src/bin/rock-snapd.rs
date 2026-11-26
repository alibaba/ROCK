use std::path::PathBuf;
use std::process::ExitCode;

use rock_snapd::observability::structured_log::{
    emit_stderr, StructuredComponent, StructuredEvent, StructuredEventName, StructuredLogLevel,
    StructuredOutcome,
};

fn startup_error(code: &'static str) {
    emit_stderr(
        StructuredEvent::new(
            StructuredLogLevel::Error,
            StructuredEventName::DaemonLifecycle,
            StructuredComponent::Daemon,
            StructuredOutcome::Error,
        )
        .error_code(code),
    );
}

fn main() -> ExitCode {
    let arguments = std::env::args_os().skip(1).collect::<Vec<_>>();
    if arguments.len() != 2 || arguments[0] != "--config" {
        startup_error("invalid_arguments");
        return ExitCode::from(2);
    }
    let path = PathBuf::from(&arguments[1]);
    let config = match rock_snapd::daemon::bootstrap::load_config(&path) {
        Ok(config) => config,
        Err(_) => {
            startup_error("config_invalid");
            return ExitCode::from(2);
        }
    };
    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
    {
        Ok(runtime) => runtime,
        Err(_) => {
            startup_error("runtime_initialization_failed");
            return ExitCode::from(1);
        }
    };
    match runtime.block_on(rock_snapd::daemon::bootstrap::serve(config)) {
        Ok(()) => ExitCode::SUCCESS,
        Err(_) => {
            startup_error("daemon_failed");
            ExitCode::from(1)
        }
    }
}
