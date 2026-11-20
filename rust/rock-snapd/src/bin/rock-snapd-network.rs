//! Privileged, fixed-policy network lease broker for snapd workers.
use std::process::ExitCode;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};

#[tokio::main]
async fn main() -> ExitCode {
    if std::env::args_os().len() != 1 {
        return ExitCode::from(2);
    }
    let stop = Arc::new(AtomicBool::new(false));
    let running = Arc::clone(&stop);
    let mut task =
        tokio::task::spawn_blocking(move || rock_snapd::vm::runtime::network::serve(running));
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        .expect("SIGTERM handler");
    let result = tokio::select! {
        result = &mut task => result,
        _ = terminate.recv() => { stop.store(true, Ordering::Release); task.await },
        _ = tokio::signal::ctrl_c() => { stop.store(true, Ordering::Release); task.await },
    };
    match result {
        Ok(Ok(())) => ExitCode::SUCCESS,
        error => {
            eprintln!("Network helper failed: {error:?}");
            ExitCode::FAILURE
        }
    }
}
