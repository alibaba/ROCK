use std::process::ExitCode;

use rock_snapd::observability::structured_log::{
    emit_stderr, StructuredComponent, StructuredEvent, StructuredEventName, StructuredLogLevel,
    StructuredOutcome,
};

fn worker_error(code: &'static str) {
    emit_stderr(
        StructuredEvent::new(
            StructuredLogLevel::Error,
            StructuredEventName::DaemonLifecycle,
            StructuredComponent::Daemon,
            StructuredOutcome::Error,
        )
        .operation("base_image_worker")
        .error_code(code),
    );
}

fn main() -> ExitCode {
    let mut arguments = std::env::args().skip(1).collect::<Vec<_>>();
    if arguments.first().map(String::as_str) != Some("_rootfs_worker") {
        worker_error("invalid_arguments");
        return ExitCode::from(2);
    }
    arguments.remove(0);
    match rock_snapd::snapshot::image::oci::rootfs_worker(&arguments) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            worker_error("base_image_build_failed");
            eprintln!(
                "{}",
                rock_snapd::snapshot::image::oci::rootfs_worker_error_line(&error)
            );
            ExitCode::FAILURE
        }
    }
}
