/**
 * Runner script builder — generates the self-contained bash script for DinD compose execution.
 *
 * The generated script handles:
 *   1. dockerd startup and readiness check
 *   2. Materialization of compose YAML and service scripts via heredocs
 *   3. OSS artifact downloads
 *   4. Init container execution (serial, fail-fast)
 *   5. docker compose up and main container wait
 *   6. Result collection and cleanup
 *
 * Matches Python rock.sdk.job.compose.script_builder.
 */

import type { ComposeJobConfig } from '../config_compose';
import { buildComposeYaml } from './yaml_builder';
import { shellQuote } from '../../utils/shell';

function _q(s: string): string {
  return shellQuote(s);
}

function _heredocMarker(prefix: string, value: string): string {
  return `${prefix}_${value.replace(/[^A-Za-z0-9_]/g, '_').toUpperCase()}_EOF`;
}

function _scriptPath(filename: string): string {
  return `"$SCRIPTS_DIR/"${_q(filename)}`;
}

function _ossObjectPath(key: string): string {
  return `"oss://$OSS_BUCKET/"${_q(key)}`;
}

function _volumeArg(source: string, target: string, readOnly: boolean = false): string {
  return `-v ${_q(`${source}:${target}${readOnly ? ':ro' : ''}`)}`;
}

function _defaultVolumeArgs(): string[] {
  return [
    _volumeArg('/tmp/shared', '/tmp/shared'),
    _volumeArg('/tmp/output', '/tmp/output'),
  ];
}

export function buildRunnerScript(config: ComposeJobConfig): string {
  const [composeYaml, scripts] = buildComposeYaml(config);
  const mainService = config.services.find((s) => s.is_main);
  const mainName = mainService?.name ?? 'main';

  const sections = [
    _sectionHeader(config),
    _sectionMaterialize(composeYaml, scripts),
    _sectionStartDockerd(),
    _sectionDownloadArtifacts(config),
    _sectionInitContainers(config),
    _sectionComposeUp(),
    _sectionWaitMain(mainName),
    _sectionCollectResults(),
    _sectionExit(),
  ];

  return sections.join('\n');
}

// ---------------------------------------------------------------------------
// Section: Header
// ---------------------------------------------------------------------------

function _sectionHeader(config: ComposeJobConfig): string {
  const jobName = config.job_name ?? 'default';
  const timeout = config.timeout;
  const callbackUrl = config.callback_url ?? '';
  return `#!/bin/bash
set -uo pipefail

JOB_ID=${_q(jobName)}
WORKSPACE="/workspace"
SCRIPTS_DIR="$WORKSPACE/scripts"
COMPOSE_FILE="$WORKSPACE/docker-compose.yaml"
LOG_DIR="/data/logs/user-defined/compose-$JOB_ID"
EXIT_CODE=0
TIMEOUT=${timeout}
CALLBACK_URL=${_q(callbackUrl)}

mkdir -p "$LOG_DIR" "$SCRIPTS_DIR" /tmp/shared /tmp/output

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_DIR/runner.log"; }

collect_container_logs() {
    log "Collecting container logs..."
    for svc in $(docker compose -f "$COMPOSE_FILE" ps --services 2>/dev/null); do
        docker compose -f "$COMPOSE_FILE" logs --no-color "$svc" > "$LOG_DIR/$svc.log" 2>&1 || true
    done
}

send_callback() {
    local status="$1"
    local exit_code="\${2:-0}"
    if [ -z "$CALLBACK_URL" ]; then return 0; fi
    curl -sf -X PATCH "$CALLBACK_URL/jobs/$JOB_ID/status" \\
        -H "Content-Type: application/json" \\
        -d "{\\"status\\":\\"$status\\",\\"exit_code\\":$exit_code}" \\
        --max-time 30 --retry 2 || true
}

cleanup() {
    local code=$?
    if [ "$EXIT_CODE" -eq 0 ] && [ "$code" -ne 0 ]; then EXIT_CODE=$code; fi
    log "Cleanup starting (exit_code=$EXIT_CODE)..."
    collect_container_logs
    send_callback "Failed" "$EXIT_CODE"
    docker compose -f "$COMPOSE_FILE" down --timeout 30 --volumes 2>/dev/null || true
    log "Cleanup complete."
}
trap cleanup EXIT`;
}

// ---------------------------------------------------------------------------
// Section: Materialize
// ---------------------------------------------------------------------------

function _sectionMaterialize(composeYaml: string, scripts: Record<string, string>): string {
  const parts: string[] = ['log "Materializing compose files..."'];
  parts.push(`cat > "$COMPOSE_FILE" << 'COMPOSE_EOF'\n${composeYaml}COMPOSE_EOF`);

  for (const [filename, content] of Object.entries(scripts)) {
    const safeMarker = _heredocMarker('SCRIPT', filename);
    parts.push(`cat > ${_scriptPath(filename)} << '${safeMarker}'\n${content}\n${safeMarker}`);
    parts.push(`chmod +x ${_scriptPath(filename)}`);
  }

  return parts.join('\n');
}

// ---------------------------------------------------------------------------
// Section: Start dockerd
// ---------------------------------------------------------------------------

function _sectionStartDockerd(): string {
  return `
# ── Start dockerd ──────────────────────────────────────────────────
log "Starting dockerd..."
if ! pgrep -x dockerd > /dev/null; then
    nohup dockerd --host unix:///var/run/docker.sock --host tcp://127.0.0.1:2375 --tls=false > /var/log/dockerd.log 2>&1 &
fi

DOCKERD_TIMEOUT=120
for i in $(seq 1 $DOCKERD_TIMEOUT); do
    if docker info > /dev/null 2>&1; then
        log "dockerd ready after \${i}s"
        break
    fi
    if [ "$i" -eq "$DOCKERD_TIMEOUT" ]; then
        log "ERROR: dockerd failed to start within \${DOCKERD_TIMEOUT}s"
        EXIT_CODE=90
        exit 90
    fi
    sleep 1
done`;
}

// ---------------------------------------------------------------------------
// Section: Download OSS artifacts
// ---------------------------------------------------------------------------

function _sectionDownloadArtifacts(config: ComposeJobConfig): string {
  if (!config.oss_artifacts || config.oss_artifacts.length === 0) {
    return '# No OSS artifacts to download';
  }

  const lines: string[] = ['log "Downloading OSS artifacts..."'];
  for (const artifact of config.oss_artifacts) {
    const target = artifact.target_path;
    const key = artifact.oss_key;
    const name = artifact.name;
    lines.push(`log ${_q(`  Downloading ${name}...`)}`);
    lines.push(`mkdir -p ${_q(target)}`);
    if (artifact.archive) {
      const localArchive = `/tmp/${name}.tar.gz`;
      lines.push(
        `ossutil cp ${_ossObjectPath(key)} ${_q(localArchive)} && ` +
        `tar -xzf ${_q(localArchive)} -C ${_q(target)} && ` +
        `rm -f ${_q(localArchive)} || ` +
        `log ${_q(`WARN: Failed to download artifact ${name}`)}`
      );
    } else {
      const targetFile = `${target}/${name}`;
      lines.push(
        `ossutil cp ${_ossObjectPath(key)} ${_q(targetFile)} || ` +
        `log ${_q(`WARN: Failed to download artifact ${name}`)}`
      );
    }
  }
  return lines.join('\n');
}

function _buildInitContainerCommand(
  ic: NonNullable<ComposeJobConfig['init_containers']>[number],
  index: number,
  lines: string[]
): string {
  const volArgs = _defaultVolumeArgs();
  for (const vm of ic.volume_mounts) {
    volArgs.push(_volumeArg(vm.name, vm.mount_path, vm.read_only));
  }

  const base = `docker run --rm --network host ${volArgs.join(' ')}`;
  if (ic.script) {
    const scriptFile = `init_${index}.sh`;
    const marker = _heredocMarker('INIT', `${index}_${ic.name}`);
    lines.push(`cat > ${_scriptPath(scriptFile)} << '${marker}'\n${ic.script}\n${marker}`);
    lines.push(`chmod +x ${_scriptPath(scriptFile)}`);
    return `${base} -v "$SCRIPTS_DIR:/tmp/run:ro" ${_q(ic.image)} bash ${_q(`/tmp/run/${scriptFile}`)}`;
  }

  if (ic.command) {
    const cmdParts = [...ic.command, ...(ic.args ?? [])].map(_q).join(' ');
    return `${base} ${_q(ic.image)} ${cmdParts}`;
  }

  return `${base} ${_q(ic.image)}`;
}

// ---------------------------------------------------------------------------
// Section: Init containers
// ---------------------------------------------------------------------------

function _sectionInitContainers(config: ComposeJobConfig): string {
  if (!config.init_containers || config.init_containers.length === 0) {
    return '# No init containers';
  }

  const lines: string[] = ['log "Running init containers..."'];
  for (const [index, ic] of config.init_containers.entries()) {
    const name = ic.name;
    const command = _buildInitContainerCommand(ic, index, lines);

    lines.push(`log ${_q(`  Running init container: ${name}`)}`);
    lines.push(`if ! ${command}; then`);
    lines.push(`    log ${_q(`ERROR: Init container ${name} failed`)}`);
    lines.push(`    EXIT_CODE=92`);
    lines.push(`    exit 92`);
    lines.push(`fi`);
  }

  return lines.join('\n');
}

// ---------------------------------------------------------------------------
// Section: Compose up
// ---------------------------------------------------------------------------

function _sectionComposeUp(): string {
  return `
# ── Pull and start compose ─────────────────────────────────────────
log "Pulling images..."
docker compose -f "$COMPOSE_FILE" pull --quiet 2>/dev/null || log "WARN: docker compose pull failed (continuing)"

log "Starting compose services..."
if ! docker compose -f "$COMPOSE_FILE" up -d; then
    log "ERROR: docker compose up failed"
    EXIT_CODE=91
    exit 91
fi

send_callback "Running" 0

# Stream logs in background
for svc in $(docker compose -f "$COMPOSE_FILE" ps --services 2>/dev/null); do
    docker compose -f "$COMPOSE_FILE" logs -f --no-color "$svc" >> "$LOG_DIR/$svc.log" 2>&1 &
done`;
}

// ---------------------------------------------------------------------------
// Section: Wait for main container
// ---------------------------------------------------------------------------

function _sectionWaitMain(mainServiceName: string): string {
  const container = `compose-${mainServiceName}-1`;
  return `
# ── Wait for main container ────────────────────────────────────────
MAIN_CONTAINER=${_q(container)}
log ${_q(`Waiting for main container (${mainServiceName}) to exit...`)}
EXIT_CODE=$(docker wait "$MAIN_CONTAINER" 2>/dev/null || echo 1)
log "Main container exited with code: $EXIT_CODE"`;
}

// ---------------------------------------------------------------------------
// Section: Collect results
// ---------------------------------------------------------------------------

function _sectionCollectResults(): string {
  return `
# ── Collect results ────────────────────────────────────────────────
log "Collecting results..."
if [ -d /tmp/output ]; then
    cp -r /tmp/output/* "$LOG_DIR/" 2>/dev/null || true
fi

# Write result.json for SDK collect()
cat > "$LOG_DIR/result.json" << RESULT_EOF
{
    "task_name": "$JOB_ID",
    "exit_code": $EXIT_CODE,
    "finished_at": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
}
RESULT_EOF

# Override cleanup callback with success if exit_code == 0
if [ "$EXIT_CODE" -eq 0 ]; then
    send_callback "Succeeded" 0
    # Remove the EXIT trap's Failed callback
    trap - EXIT
    collect_container_logs
    docker compose -f "$COMPOSE_FILE" down --timeout 30 --volumes 2>/dev/null || true
    log "Job completed successfully."
fi`;
}

// ---------------------------------------------------------------------------
// Section: Exit
// ---------------------------------------------------------------------------

function _sectionExit(): string {
  return `
# ── Exit ───────────────────────────────────────────────────────────
exit "$EXIT_CODE"`;
}
