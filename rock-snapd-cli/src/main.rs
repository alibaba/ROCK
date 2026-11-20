use std::env;
use std::fs;
use std::io::{self, Read, Write};
use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::path::Path;
use std::process::ExitCode;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::Value;
use sha2::{Digest, Sha256};

// Share the daemon's dependency-light wire types/codecs without linking its
// Linux runtime or AWS SDK into this diagnostic CLI. JSON stays at the CLI
// input/output boundary, never on the P2P socket.
#[path = "../../rust/rock-snapd/src/binary_wire.rs"]
mod binary_wire;
#[path = "../../rust/rock-snapd/src/snapshot/metadata/kinds.rs"]
mod kind_models;
#[path = "../../rust/rock-snapd/src/snapshot/transport/p2p/native.rs"]
#[allow(dead_code)]
mod native;
#[path = "../../rust/rock-snapd/src/snapshot/transport/p2p/native_wire.rs"]
mod native_wire;
#[path = "../../rust/rock-snapd/src/snapshot/transport/p2p/protocol.rs"]
mod p2p_wire;
#[cfg(test)]
use p2p_wire as protocol;
// Exercise the independent transport on Unix hosts without linking the
// Linux-only daemon; production uses this exact module via vm::runtime.
#[cfg(all(test, unix))]
#[path = "../../rust/rock-snapd/src/vm/runtime/plugger.rs"]
mod plugger_transport;
use p2p_wire::{
    P2PBinaryFrame, P2PTcpRequest, P2PTcpResponse, P2P_TCP_MAGIC, P2P_TCP_MAX_FRAME_BYTES,
};

const PROTOCOL_VERSION: &str = "1";
const DEFAULT_HOST: &str = "http://127.0.0.1:8080";
const DEFAULT_TIMEOUT_SECONDS: u64 = 30;
const MAX_RESPONSE_BYTES: usize = 32 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Eq)]
struct Endpoint {
    connect_host: String,
    port: u16,
    host_header: String,
}

#[derive(Debug)]
struct Options {
    endpoint: Endpoint,
    timeout: Duration,
    request_id: Option<String>,
    compact: bool,
    command: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct Operation {
    method: String,
    path: String,
    body: Option<Vec<u8>>,
    diagnostics: bool,
    direct_p2p: Option<DirectP2POperation>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum DirectP2POperation {
    Availability {
        snapshot_id: String,
        spec_checksum: String,
    },
    ReadRange {
        snapshot_id: String,
        request: Value,
        output: String,
    },
}

#[derive(Debug)]
struct HttpResponse {
    status: u16,
    body: Vec<u8>,
}

fn main() -> ExitCode {
    match run(env::args().skip(1).collect()) {
        Ok(code) => ExitCode::from(code),
        Err(error) => {
            eprintln!("rock-snapd-cli: {error}");
            ExitCode::from(2)
        }
    }
}

fn run(arguments: Vec<String>) -> Result<u8, String> {
    if arguments
        .iter()
        .any(|argument| argument == "--help" || argument == "-h")
    {
        print_help();
        return Ok(0);
    }
    if arguments
        .iter()
        .any(|argument| argument == "--version" || argument == "-V")
    {
        println!("rock-snapd-cli {}", env!("CARGO_PKG_VERSION"));
        return Ok(0);
    }
    let options = parse_options(arguments)?;
    let operation = parse_operation(&options.command)?;
    let request_id = options
        .request_id
        .unwrap_or_else(|| uuid::Uuid::new_v4().simple().to_string());
    validate_request_id(&request_id)?;
    let deadline_unix_ms = now_unix_ms()?
        .checked_add(options.timeout.as_millis() as u64)
        .ok_or_else(|| "deadline overflowed u64".to_owned())?;
    if let Some(direct) = &operation.direct_p2p {
        return run_direct_p2p(
            &options.endpoint,
            direct,
            &request_id,
            deadline_unix_ms,
            options.timeout,
            options.compact,
        );
    }

    let response = send_request(
        &options.endpoint,
        &operation,
        &request_id,
        deadline_unix_ms,
        options.timeout,
    )
    .map_err(|error| {
        if !operation.diagnostics && operation.method != "GET" {
            format!("{error}; mutation result may be unknown—check status; request_id={request_id}")
        } else {
            error
        }
    })?;
    if operation.diagnostics {
        print_bytes(&response.body)?;
        return Ok(if (200..300).contains(&response.status) {
            0
        } else {
            4
        });
    }
    let code = render_control_response(&response, &request_id, options.compact)?;
    if code == 0 && operation.path.ends_with("/plugger") {
        return Ok(plugger_result_exit_code(&response));
    }
    Ok(code)
}

fn plugger_result_exit_code(response: &HttpResponse) -> u8 {
    let reply = serde_json::from_slice::<Value>(&response.body).ok();
    match reply
        .as_ref()
        .and_then(|value| value.get("result"))
        .and_then(|value| value.get("status"))
        .and_then(Value::as_u64)
    {
        Some(200..=299) => 0,
        // A failed guest plug must never let `plug && snapshot` proceed.
        _ => 4,
    }
}

fn parse_options(arguments: Vec<String>) -> Result<Options, String> {
    let mut host = env::var("ROCK_SNAPD_HOST").unwrap_or_else(|_| DEFAULT_HOST.to_owned());
    let mut timeout_seconds = DEFAULT_TIMEOUT_SECONDS;
    let mut request_id = None;
    let mut compact = false;
    let mut index = 0;
    while index < arguments.len() {
        match arguments[index].as_str() {
            "--host" => host = take_value(&arguments, &mut index, "--host")?,
            "--timeout" => {
                let raw = take_value(&arguments, &mut index, "--timeout")?;
                timeout_seconds = raw
                    .parse::<u64>()
                    .map_err(|_| "--timeout must be an integer number of seconds".to_owned())?;
                if timeout_seconds == 0 || timeout_seconds > 86_400 {
                    return Err("--timeout must be in 1..86400".into());
                }
            }
            "--request-id" => {
                request_id = Some(take_value(&arguments, &mut index, "--request-id")?)
            }
            "--compact" => {
                compact = true;
                index += 1;
            }
            value if value.starts_with('-') => {
                return Err(format!("unknown global option {value:?}"))
            }
            _ => break,
        }
    }
    let command = arguments[index..].to_vec();
    if command.is_empty() {
        return Err("a command is required; use --help for usage".into());
    }
    Ok(Options {
        endpoint: parse_endpoint(&host)?,
        timeout: Duration::from_secs(timeout_seconds),
        request_id,
        compact,
        command,
    })
}

fn take_value(arguments: &[String], index: &mut usize, option: &str) -> Result<String, String> {
    let value = arguments
        .get(*index + 1)
        .ok_or_else(|| format!("{option} requires a value"))?
        .clone();
    *index += 2;
    Ok(value)
}

fn parse_operation(arguments: &[String]) -> Result<Operation, String> {
    let values = arguments.iter().map(String::as_str).collect::<Vec<_>>();
    let operation = match values.as_slice() {
        ["health"] => diagnostic("/healthz"),
        ["metrics"] => diagnostic("/metrics"),
        ["base-image", "resolve", body] => {
            control("POST", "/v1/snapd/base-images/resolve", Some(body))?
        }
        ["base-image", "create", body] => control("POST", "/v1/snapd/base-images", Some(body))?,
        ["snapshot", "import-oci", body] => {
            control("POST", "/v1/snapd/snapshots/import-oci", Some(body))?
        }
        ["sandbox", "snapshot", sandbox, body] => control(
            "POST",
            &format!("/v1/snapd/sandboxes/{}/snapshot", path_identity(sandbox)?),
            Some(body),
        )?,
        ["sandbox", "fork", sandbox, body] => control(
            "POST",
            &format!("/v1/snapd/sandboxes/{}/fork", path_identity(sandbox)?),
            Some(body),
        )?,
        ["sandbox", "start", snapshot, body] => control(
            "POST",
            &format!("/v1/snapd/snapshots/{}/start", path_identity(snapshot)?),
            Some(body),
        )?,
        ["sandbox", "status", sandbox] => control(
            "GET",
            &format!("/v1/snapd/sandboxes/{}/status", path_identity(sandbox)?),
            None,
        )?,
        ["sandbox", "plugger", sandbox, action] => plugger_operation(sandbox, action, None)?,
        ["sandbox", "plugger", sandbox, action, payload] => {
            plugger_operation(sandbox, action, Some(payload))?
        }
        ["sandbox", "stop", sandbox, body] => control(
            "POST",
            &format!("/v1/snapd/sandboxes/{}/stop", path_identity(sandbox)?),
            Some(body),
        )?,
        ["sandbox", "delete", sandbox, body] => control(
            "DELETE",
            &format!("/v1/snapd/sandboxes/{}", path_identity(sandbox)?),
            Some(body),
        )?,
        ["snapshot", "status", snapshot] => control(
            "GET",
            &format!("/v1/snapd/snapshots/{}/status", path_identity(snapshot)?),
            None,
        )?,
        ["p2p", "availability", snapshot, checksum] => {
            direct_p2p_availability(path_identity(snapshot)?, sha256_identity(checksum)?)
        }
        ["p2p", "read-range", snapshot, body, "--output", output] => {
            if output.is_empty() {
                return Err("--output path must not be empty".into());
            }
            direct_p2p_read_range(
                path_identity(snapshot)?,
                read_json_value_source(body)?,
                (*output).to_owned(),
            )
        }
        ["snapshot", action, snapshot, body]
            if matches!(
                *action,
                "finalize" | "check-usage" | "abort-delete-fence" | "invalidate"
            ) =>
        {
            control(
                "POST",
                &format!("/v1/snapd/snapshots/{}/{action}", path_identity(snapshot)?),
                Some(body),
            )?
        }
        ["raw", method, path] => raw_operation(method, path, None)?,
        ["raw", method, path, body] => raw_operation(method, path, Some(body))?,
        _ => return Err("invalid command shape; use --help for usage".into()),
    };
    Ok(operation)
}

fn diagnostic(path: &str) -> Operation {
    Operation {
        method: "GET".into(),
        path: path.into(),
        body: None,
        diagnostics: true,
        direct_p2p: None,
    }
}

fn plugger_operation(
    sandbox: &str,
    action: &str,
    payload: Option<&str>,
) -> Result<Operation, String> {
    if !matches!(action, "configure" | "state" | "health") {
        return Err("proxy action must be configure, state, or health".into());
    }
    let payload = payload
        .map(read_json_value_source)
        .transpose()?
        .unwrap_or_else(|| serde_json::json!({}));
    if !payload.is_object() {
        return Err("plugger payload must be an object".into());
    }
    if serde_json::to_vec(&payload)
        .map_err(|error| error.to_string())?
        .len()
        > 64 * 1024
    {
        return Err("plugger payload exceeds 64 KiB".into());
    }
    Ok(Operation {
        method: "POST".into(),
        path: format!("/v1/snapd/sandboxes/{}/plugger", path_identity(sandbox)?),
        body: Some(
            serde_json::to_vec(&serde_json::json!({"action": action, "payload": payload}))
                .map_err(|error| error.to_string())?,
        ),
        diagnostics: false,
        direct_p2p: None,
    })
}

fn control(method: &str, path: &str, body: Option<&&str>) -> Result<Operation, String> {
    Ok(Operation {
        method: method.into(),
        path: path.into(),
        body: body.map(|source| read_json_source(source)).transpose()?,
        diagnostics: false,
        direct_p2p: None,
    })
}

fn raw_operation(method: &str, path: &str, body: Option<&&str>) -> Result<Operation, String> {
    let method = method.to_ascii_uppercase();
    if !matches!(method.as_str(), "GET" | "POST" | "DELETE") {
        return Err("raw method must be GET, POST, or DELETE".into());
    }
    if !path.starts_with('/') || !path.bytes().all(|byte| byte.is_ascii_graphic()) {
        return Err(
            "raw path must start with '/' and contain only printable ASCII without spaces".into(),
        );
    }
    if method == "GET" && body.is_some() {
        return Err("GET requests cannot have a body".into());
    }
    let diagnostics = matches!(path, "/healthz" | "/metrics");
    let body = body.map(|source| read_json_source(source)).transpose()?;
    if !diagnostics && method != "GET" && body.is_none() {
        return Err("POST and DELETE control requests require a JSON body".into());
    }
    Ok(Operation {
        method,
        path: path.into(),
        body,
        diagnostics,
        direct_p2p: None,
    })
}

fn direct_p2p_availability(snapshot_id: String, spec_checksum: String) -> Operation {
    Operation {
        method: "P2P".into(),
        path: format!("availability/{snapshot_id}"),
        body: Some(spec_checksum.as_bytes().to_vec()),
        diagnostics: false,
        direct_p2p: Some(DirectP2POperation::Availability {
            snapshot_id,
            spec_checksum,
        }),
    }
}

fn direct_p2p_read_range(snapshot_id: String, request: Value, output: String) -> Operation {
    Operation {
        method: "P2P".into(),
        path: format!("read-range/{snapshot_id}"),
        body: Some(serde_json::to_vec(&request).expect("JSON value serialization cannot fail")),
        diagnostics: false,
        direct_p2p: Some(DirectP2POperation::ReadRange {
            snapshot_id,
            request,
            output,
        }),
    }
}

fn read_json_source(source: &str) -> Result<Vec<u8>, String> {
    let payload = if source == "-" {
        let mut payload = Vec::new();
        io::stdin()
            .read_to_end(&mut payload)
            .map_err(|error| format!("cannot read JSON from stdin: {error}"))?;
        payload
    } else if let Some(path) = source.strip_prefix('@') {
        fs::read(path).map_err(|error| format!("cannot read JSON body {path:?}: {error}"))?
    } else {
        source.as_bytes().to_vec()
    };
    if payload.len() > 16 * 1024 * 1024 {
        return Err("JSON body exceeds the server's 16 MiB limit".into());
    }
    let value: Value = serde_json::from_slice(&payload)
        .map_err(|error| format!("request body is not valid JSON: {error}"))?;
    serde_json::to_vec(&value).map_err(|error| format!("cannot encode request JSON: {error}"))
}

fn read_json_value_source(source: &str) -> Result<Value, String> {
    serde_json::from_slice(&read_json_source(source)?)
        .map_err(|error| format!("cannot decode canonical request JSON: {error}"))
}

fn parse_endpoint(value: &str) -> Result<Endpoint, String> {
    let authority = value
        .strip_prefix("http://")
        .ok_or_else(|| "--host must use http:// (rock-snapd does not expose TLS)".to_owned())?;
    if authority.is_empty() || authority.contains(['/', '?', '#', '@']) {
        return Err("--host must contain only an HTTP host and optional port".into());
    }
    let (connect_host, port, host_header) = if let Some(rest) = authority.strip_prefix('[') {
        let close = rest
            .find(']')
            .ok_or_else(|| "invalid bracketed IPv6 host".to_owned())?;
        let host = &rest[..close];
        let suffix = &rest[close + 1..];
        let port = if suffix.is_empty() {
            80
        } else {
            suffix
                .strip_prefix(':')
                .ok_or_else(|| "invalid IPv6 host port".to_owned())?
                .parse::<u16>()
                .map_err(|_| "invalid host port".to_owned())?
        };
        (host.to_owned(), port, format!("[{host}]:{port}"))
    } else {
        let (host, port) = match authority.rsplit_once(':') {
            Some((host, port)) if !host.contains(':') => (
                host,
                port.parse::<u16>()
                    .map_err(|_| "invalid host port".to_owned())?,
            ),
            _ => (authority, 80),
        };
        if host.is_empty() {
            return Err("host is empty".into());
        }
        (host.to_owned(), port, format!("{host}:{port}"))
    };
    if port == 0 {
        return Err("host port must be in 1..65535".into());
    }
    Ok(Endpoint {
        connect_host,
        port,
        host_header,
    })
}

fn path_identity(value: &str) -> Result<String, String> {
    if value.is_empty()
        || value.len() > 4096
        || !value.bytes().all(|byte| byte.is_ascii_graphic())
        || value.contains(['/', '%', '?', '#'])
    {
        return Err(
            "resource identity must be 1..4096 printable ASCII bytes without '/', '%', '?', or '#'"
                .into(),
        );
    }
    Ok(value.to_owned())
}

fn sha256_identity(value: &str) -> Result<String, String> {
    if value.strip_prefix("sha256:").is_some_and(|hex| {
        hex.len() == 64
            && hex
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    }) {
        Ok(value.to_owned())
    } else {
        Err("checksum must be sha256:<64 lowercase hex>".into())
    }
}

fn validate_request_id(value: &str) -> Result<(), String> {
    if value.is_empty() || value.len() > 128 || !value.bytes().all(|byte| byte.is_ascii_graphic()) {
        return Err("request ID must be 1..128 printable ASCII bytes without spaces".into());
    }
    Ok(())
}

fn now_unix_ms() -> Result<u64, String> {
    let duration = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "system clock is before the Unix epoch".to_owned())?;
    u64::try_from(duration.as_millis()).map_err(|_| "system time overflowed u64".to_owned())
}

fn connect(endpoint: &Endpoint, timeout: Duration) -> Result<TcpStream, String> {
    let addresses = (endpoint.connect_host.as_str(), endpoint.port)
        .to_socket_addrs()
        .map_err(|error| format!("cannot resolve snapd host: {error}"))?
        .collect::<Vec<SocketAddr>>();
    if addresses.is_empty() {
        return Err("snapd host resolved to no addresses".into());
    }
    let mut last_error = None;
    for address in addresses {
        match TcpStream::connect_timeout(&address, timeout) {
            Ok(stream) => {
                stream
                    .set_read_timeout(Some(timeout))
                    .and_then(|_| stream.set_write_timeout(Some(timeout)))
                    .map_err(|error| format!("cannot configure socket timeout: {error}"))?;
                return Ok(stream);
            }
            Err(error) => last_error = Some(error),
        }
    }
    Err(format!(
        "cannot connect to rock-snapd: {}",
        last_error.map_or_else(|| "unknown error".to_owned(), |error| error.to_string())
    ))
}

fn run_direct_p2p(
    endpoint: &Endpoint,
    operation: &DirectP2POperation,
    request_id: &str,
    deadline_unix_ms: u64,
    timeout: Duration,
    compact: bool,
) -> Result<u8, String> {
    let mut stream = connect(endpoint, timeout)?;
    stream
        .set_nodelay(true)
        .map_err(|error| format!("cannot enable TCP_NODELAY: {error}"))?;
    stream
        .write_all(P2P_TCP_MAGIC)
        .map_err(|error| format!("cannot send P2P TCP preface: {error}"))?;
    let open_id = format!(
        "cli-open:{}",
        &format!("{:x}", Sha256::digest(request_id.as_bytes()))[..32]
    );
    write_p2p_frame(
        &mut stream,
        &serde_json::json!({
            "operation": "open",
            "protocol_version": p2p_wire::P2P_PROTOCOL_VERSION,
            "request_id": open_id.clone(),
            "deadline_unix_ms": deadline_unix_ms,
        }),
    )?;
    let ready = read_p2p_frame(&mut stream)?;
    if ready["result"] == "error" {
        return render_p2p_error(&ready, compact);
    }
    if ready["result"] != "ready"
        || ready["request_id"] != open_id
        || ready["protocol_version"] != p2p_wire::P2P_PROTOCOL_VERSION
    {
        return Err("peer returned an invalid P2P TCP ready response".into());
    }

    if let DirectP2POperation::ReadRange {
        snapshot_id,
        request,
        ..
    } = operation
    {
        let spec_checksum = request
            .get("spec_checksum")
            .and_then(Value::as_str)
            .ok_or_else(|| "P2P read-range request omitted spec_checksum".to_owned())?;
        sha256_identity(spec_checksum)?;
        let probe_id = format!(
            "cli-probe:{}",
            &format!("{:x}", Sha256::digest(request_id.as_bytes()))[..32]
        );
        write_p2p_frame(
            &mut stream,
            &serde_json::json!({
                "operation": "availability",
                "request_id": probe_id.clone(),
                "deadline_unix_ms": deadline_unix_ms,
                "snapshot_id": snapshot_id,
                "spec_checksum": spec_checksum,
            }),
        )?;
        let availability = read_p2p_frame(&mut stream)?;
        if availability["result"] == "error" {
            return render_p2p_error(&availability, compact);
        }
        if availability["result"] != "availability"
            || availability["request_id"] != probe_id
            || availability["availability"]["snapshot_id"].as_str() != Some(snapshot_id.as_str())
            || availability["availability"]["spec_checksum"].as_str() != Some(spec_checksum)
            || availability["availability"]["available"] != true
            || availability["availability"]["complete"] != true
        {
            return Err("peer is not completely available for this P2P read-range".into());
        }
    }

    let frame = match operation {
        DirectP2POperation::Availability {
            snapshot_id,
            spec_checksum,
        } => serde_json::json!({
            "operation": "availability",
            "request_id": request_id,
            "deadline_unix_ms": deadline_unix_ms,
            "snapshot_id": snapshot_id,
            "spec_checksum": spec_checksum,
        }),
        DirectP2POperation::ReadRange {
            snapshot_id,
            request,
            ..
        } => serde_json::json!({
            "operation": "read_range",
            "request_id": request_id,
            "deadline_unix_ms": deadline_unix_ms,
            "snapshot_id": snapshot_id,
            "request": request,
        }),
    };
    write_p2p_frame(&mut stream, &frame)?;
    let response = read_p2p_frame(&mut stream)?;
    if response["result"] == "error" {
        return render_p2p_error(&response, compact);
    }
    if response["request_id"] != request_id {
        return Err("peer P2P TCP response request ID differs".into());
    }
    match operation {
        DirectP2POperation::Availability { .. } => {
            if response["result"] != "availability" {
                return Err("peer returned an invalid P2P availability response".into());
            }
            let availability = response
                .get("availability")
                .ok_or_else(|| "peer omitted P2P availability result".to_owned())?;
            print_json(availability, compact)?;
            Ok(0)
        }
        DirectP2POperation::ReadRange {
            request, output, ..
        } => {
            if response["result"] != "range" {
                return Err("peer returned an invalid P2P range response".into());
            }
            let length = response["length"]
                .as_u64()
                .ok_or_else(|| "peer P2P range length is not u64".to_owned())?;
            if length == 0
                || length > MAX_RESPONSE_BYTES as u64
                || request.get("length").and_then(Value::as_u64) != Some(length)
            {
                return Err("peer P2P range length differs from the request".into());
            }
            let expected = response["range_sha256"]
                .as_str()
                .ok_or_else(|| "peer omitted P2P range checksum".to_owned())?;
            sha256_identity(expected)?;
            let mut payload = vec![0_u8; length as usize];
            stream
                .read_exact(&mut payload)
                .map_err(|error| format!("cannot read P2P range payload: {error}"))?;
            let actual = format!("sha256:{:x}", Sha256::digest(&payload));
            if expected != actual {
                return Err("P2P range payload checksum differs".into());
            }
            write_binary_payload(&payload, Path::new(output))
        }
    }
}

fn write_p2p_frame(stream: &mut TcpStream, value: &Value) -> Result<(), String> {
    let payload = if value.get("operation").is_some() {
        serde_json::from_value::<P2PTcpRequest>(value.clone())
            .map_err(|error| format!("invalid P2P request: {error}"))?
            .to_binary()
    } else {
        serde_json::from_value::<P2PTcpResponse>(value.clone())
            .map_err(|error| format!("invalid P2P response: {error}"))?
            .to_binary()
    }
    .map_err(|error| format!("cannot encode P2P binary frame: {error}"))?;
    if payload.is_empty() || payload.len() > P2P_TCP_MAX_FRAME_BYTES {
        return Err("P2P TCP frame exceeds 64 KiB".into());
    }
    stream
        .write_all(&(payload.len() as u32).to_be_bytes())
        .and_then(|_| stream.write_all(&payload))
        .map_err(|error| format!("cannot send P2P TCP frame: {error}"))
}

fn read_p2p_frame(stream: &mut TcpStream) -> Result<Value, String> {
    let mut length = [0_u8; 4];
    stream
        .read_exact(&mut length)
        .map_err(|error| format!("cannot read P2P TCP frame length: {error}"))?;
    let length = u32::from_be_bytes(length) as usize;
    if length == 0 || length > P2P_TCP_MAX_FRAME_BYTES {
        return Err("peer returned an invalid P2P TCP frame length".into());
    }
    let mut payload = vec![0_u8; length];
    stream
        .read_exact(&mut payload)
        .map_err(|error| format!("cannot read P2P TCP frame: {error}"))?;
    let value = if payload[0] < 128 {
        serde_json::to_value(
            P2PTcpRequest::from_binary(&payload)
                .map_err(|error| format!("invalid P2P binary request: {error}"))?,
        )
    } else {
        serde_json::to_value(
            P2PTcpResponse::from_binary(&payload)
                .map_err(|error| format!("invalid P2P binary response: {error}"))?,
        )
    };
    value.map_err(|error| format!("cannot render P2P response: {error}"))
}

fn render_p2p_error(response: &Value, compact: bool) -> Result<u8, String> {
    print_json(response, compact)?;
    if let Some(message) = response.get("message").and_then(Value::as_str) {
        eprintln!("snapd rejected P2P request: {message}");
    }
    Ok(4)
}

fn print_json(value: &Value, compact: bool) -> Result<(), String> {
    println!(
        "{}",
        if compact {
            serde_json::to_string(value)
        } else {
            serde_json::to_string_pretty(value)
        }
        .map_err(|error| error.to_string())?
    );
    Ok(())
}

fn write_binary_payload(payload: &[u8], output: &Path) -> Result<u8, String> {
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(output)
        .map_err(|error| {
            format!(
                "cannot create output {} without overwrite: {error}",
                output.display()
            )
        })?;
    file.write_all(payload)
        .and_then(|_| file.sync_all())
        .map_err(|error| format!("cannot write output {}: {error}", output.display()))?;
    println!("{}", output.display());
    Ok(0)
}

fn send_request(
    endpoint: &Endpoint,
    operation: &Operation,
    request_id: &str,
    deadline_unix_ms: u64,
    timeout: Duration,
) -> Result<HttpResponse, String> {
    let mut stream = connect(endpoint, timeout)?;
    let body = operation.body.as_deref().unwrap_or_default();
    let mut request = format!(
        "{} {} HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nContent-Length: {}\r\n",
        operation.method,
        operation.path,
        endpoint.host_header,
        body.len()
    );
    if !operation.diagnostics {
        request.push_str(&format!(
            "X-ROCK-Snapshot-Protocol-Version: {PROTOCOL_VERSION}\r\nX-Request-ID: {request_id}\r\nX-Deadline-Unix-Ms: {deadline_unix_ms}\r\n"
        ));
    }
    if operation.body.is_some() {
        request.push_str("Content-Type: application/json\r\n");
    }
    request.push_str("\r\n");
    stream
        .write_all(request.as_bytes())
        .and_then(|_| stream.write_all(body))
        .and_then(|_| stream.flush())
        .map_err(|error| format!("cannot send request: {error}"))?;
    let mut response = Vec::new();
    stream
        .take((MAX_RESPONSE_BYTES + 1) as u64)
        .read_to_end(&mut response)
        .map_err(|error| format!("cannot read response: {error}"))?;
    if response.len() > MAX_RESPONSE_BYTES {
        return Err("response exceeds 32 MiB".into());
    }
    parse_http_response(&response)
}

fn parse_http_response(payload: &[u8]) -> Result<HttpResponse, String> {
    let header_end = find_bytes(payload, b"\r\n\r\n")
        .ok_or_else(|| "server returned an incomplete HTTP response".to_owned())?;
    let header = std::str::from_utf8(&payload[..header_end])
        .map_err(|_| "server returned non-UTF-8 HTTP headers".to_owned())?;
    let mut lines = header.split("\r\n");
    let status_line = lines
        .next()
        .ok_or_else(|| "HTTP status line is absent".to_owned())?;
    let mut status_parts = status_line.split_whitespace();
    let version = status_parts.next().unwrap_or_default();
    let status = status_parts
        .next()
        .ok_or_else(|| "HTTP status code is absent".to_owned())?
        .parse::<u16>()
        .map_err(|_| "HTTP status code is invalid".to_owned())?;
    if !version.starts_with("HTTP/1.") {
        return Err("server returned an unsupported HTTP version".into());
    }
    let mut content_length = None;
    let mut chunked = false;
    for line in lines {
        let Some((name, value)) = line.split_once(':') else {
            return Err("server returned a malformed HTTP header".into());
        };
        if name.eq_ignore_ascii_case("content-length") {
            content_length = Some(
                value
                    .trim()
                    .parse::<usize>()
                    .map_err(|_| "server returned an invalid Content-Length".to_owned())?,
            );
        }
        if name.eq_ignore_ascii_case("transfer-encoding")
            && value.trim().eq_ignore_ascii_case("chunked")
        {
            chunked = true;
        }
    }
    let raw_body = &payload[header_end + 4..];
    let body = if chunked {
        decode_chunked(raw_body)?
    } else if let Some(length) = content_length {
        if raw_body.len() != length {
            return Err("response body differs from Content-Length".into());
        }
        raw_body.to_vec()
    } else {
        raw_body.to_vec()
    };
    Ok(HttpResponse { status, body })
}

fn decode_chunked(mut payload: &[u8]) -> Result<Vec<u8>, String> {
    let mut decoded = Vec::new();
    loop {
        let line_end = find_bytes(payload, b"\r\n")
            .ok_or_else(|| "chunk size line is incomplete".to_owned())?;
        let size_text = std::str::from_utf8(&payload[..line_end])
            .map_err(|_| "chunk size is not text".to_owned())?;
        let size = usize::from_str_radix(size_text.split(';').next().unwrap_or_default(), 16)
            .map_err(|_| "chunk size is invalid".to_owned())?;
        payload = &payload[line_end + 2..];
        if size == 0 {
            return Ok(decoded);
        }
        if payload.len() < size + 2 || &payload[size..size + 2] != b"\r\n" {
            return Err("chunk body is incomplete".into());
        }
        decoded.extend_from_slice(&payload[..size]);
        if decoded.len() > MAX_RESPONSE_BYTES {
            return Err("decoded response exceeds 32 MiB".into());
        }
        payload = &payload[size + 2..];
    }
}

fn find_bytes(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}

fn render_control_response(
    response: &HttpResponse,
    request_id: &str,
    compact: bool,
) -> Result<u8, String> {
    let reply: Value = serde_json::from_slice(&response.body).map_err(|error| {
        format!(
            "snapd returned non-protocol JSON (HTTP {}): {error}",
            response.status
        )
    })?;
    let object = reply
        .as_object()
        .ok_or_else(|| "snapd reply is not a JSON object".to_owned())?;
    let actual_request_id = object
        .get("request_id")
        .and_then(Value::as_str)
        .ok_or_else(|| "snapd reply omitted request_id".to_owned())?;
    if actual_request_id != request_id {
        return Err(format!(
            "snapd reply request_id mismatch: expected {request_id:?}, got {actual_request_id:?}"
        ));
    }
    let code = object
        .get("code")
        .and_then(Value::as_str)
        .ok_or_else(|| "snapd reply omitted code".to_owned())?;
    if code == "OK" && !(200..300).contains(&response.status) {
        return Err(format!(
            "snapd returned HTTP {} with code=OK",
            response.status
        ));
    }
    let output = object.get("result").unwrap_or(&reply);
    if compact {
        println!(
            "{}",
            serde_json::to_string(output).map_err(|error| error.to_string())?
        );
    } else {
        println!(
            "{}",
            serde_json::to_string_pretty(output).map_err(|error| error.to_string())?
        );
    }
    if code == "OK" {
        Ok(0)
    } else {
        if let Some(message) = object.get("error_message").and_then(Value::as_str) {
            eprintln!("snapd rejected request: code={code} message={message}");
        }
        Ok(4)
    }
}

fn print_bytes(payload: &[u8]) -> Result<(), String> {
    io::stdout()
        .write_all(payload)
        .map_err(|error| format!("cannot write output: {error}"))
}

fn print_help() {
    println!(
        r#"Direct command-line client for a single rock-snapd daemon.

USAGE:
  rock-snapd-cli [GLOBAL OPTIONS] COMMAND

GLOBAL OPTIONS:
  --host URL          Daemon URL (default: http://127.0.0.1:8080)
  --timeout SECONDS   Request timeout and server deadline (default: 30)
  --request-id ID     Optional tracing identity
  --compact           Print compact JSON
  -h, --help          Show this help
  -V, --version       Show the version

COMMANDS:
  health
  metrics
  base-image resolve BODY
  base-image create BODY
  sandbox snapshot SANDBOX_ID BODY
  sandbox fork SANDBOX_ID BODY
  sandbox start SNAPSHOT_ID BODY
  sandbox status SANDBOX_ID
  sandbox plugger SANDBOX_ID ACTION [BODY]
  sandbox stop SANDBOX_ID BODY
  sandbox delete SANDBOX_ID BODY
  snapshot status SNAPSHOT_ID
  snapshot import-oci BODY
  snapshot finalize SNAPSHOT_ID BODY
  snapshot check-usage SNAPSHOT_ID BODY
  snapshot abort-delete-fence SNAPSHOT_ID BODY
  snapshot invalidate SNAPSHOT_ID BODY
  p2p availability SNAPSHOT_ID SPEC_CHECKSUM
  p2p read-range SNAPSHOT_ID BODY --output FILE
  raw METHOD PATH [BODY]

BODY is inline JSON, @path/to/request.json, or - for stdin. Requests use a
random request ID for correlation unless --request-id is given.
Exit status 4 means snapd returned a non-OK protocol result or HTTP status."#
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn full_oci_import_uses_a_distinct_snapshot_route() {
        let body = r#"{"oci_ref":"registry.example/team/snapshot@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}"#;
        let operation =
            parse_operation(&["snapshot", "import-oci", body].map(String::from)).unwrap();
        assert_eq!(operation.method, "POST");
        assert_eq!(operation.path, "/v1/snapd/snapshots/import-oci");
        assert_eq!(
            serde_json::from_slice::<Value>(&operation.body.unwrap()).unwrap(),
            serde_json::from_str::<Value>(body).unwrap()
        );
        assert!(parse_operation(&["snapshot", "import-oci"].map(String::from)).is_err());
    }

    #[test]
    fn plugger_commands_use_the_native_daemon_route() {
        let operation = parse_operation(
            &[
                "sandbox",
                "plugger",
                "sandbox-1",
                "configure",
                r#"{"upstream_url":"http://relay/inference"}"#,
            ]
            .map(String::from),
        )
        .unwrap();
        assert_eq!(operation.method, "POST");
        assert_eq!(operation.path, "/v1/snapd/sandboxes/sandbox-1/plugger");
        assert_eq!(
            serde_json::from_slice::<Value>(&operation.body.unwrap()).unwrap(),
            serde_json::json!({"action":"configure","payload":{"upstream_url":"http://relay/inference"}})
        );
        let state =
            parse_operation(&["sandbox", "plugger", "sandbox-1", "state"].map(String::from))
                .unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&state.body.unwrap()).unwrap(),
            serde_json::json!({"action":"state","payload":{}})
        );
        assert!(parse_operation(
            &["sandbox", "plugger", "sandbox-1", "exec", "{}"].map(String::from)
        )
        .is_err());
    }

    #[test]
    fn plugger_guest_failure_is_a_failed_cli_operation() {
        for (status, expected) in [(200, 0), (204, 0), (409, 4), (503, 4)] {
            let response = HttpResponse {
                status: 200,
                body: serde_json::to_vec(&serde_json::json!({
                    "request_id":"request-1", "code":"OK", "result":{"status":status,"body":{}}
                }))
                .unwrap(),
            };
            assert_eq!(plugger_result_exit_code(&response), expected);
        }
        assert_eq!(
            plugger_result_exit_code(&HttpResponse {
                status: 200,
                body: b"{}".to_vec()
            }),
            4
        );
    }
}
