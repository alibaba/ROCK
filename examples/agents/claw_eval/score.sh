#!/bin/bash
# Parse claw-eval run.log and extract scores to scores.json
set -eo pipefail

LOG_FILE="/data/logs/user-defined/run.log"
OUT_FILE="/data/logs/user-defined/scores.json"

if [ ! -f "$LOG_FILE" ]; then
    echo "ERROR: $LOG_FILE not found"
    exit 1
fi

TEXT=$(cat "$LOG_FILE")

get_float() { echo "$TEXT" | grep -oP "$1:\s+\K[\d.]+" | tail -1; }

TASK_SCORE=$(get_float "task_score")
COMPLETION=$(get_float "completion")
ROBUSTNESS=$(get_float "robustness")
COMMUNICATION=$(get_float "communication")
SAFETY=$(get_float "safety")
WALL_TIME=$(echo "$TEXT" | grep -oP 'wall=\K[\d.]+' | tail -1)
PASSED=$(echo "$TEXT" | grep -oP 'passed:\s+\K(True|False)' | tail -1)
TOKENS=$(echo "$TEXT" | grep -oP 'tokens=\K\d+' | tail -1)
IN_TOKENS=$(echo "$TEXT" | grep -oP '\(\K\d+(?=in/)' | tail -1)
OUT_TOKENS=$(echo "$TEXT" | grep -oP 'in/\K\d+(?=out)' | tail -1)

# Convert passed to boolean
if [ "$PASSED" = "True" ]; then
    PASSED_JSON="true"
else
    PASSED_JSON="false"
fi

cat > "$OUT_FILE" << EOF
{
  "task_score": ${TASK_SCORE:-null},
  "completion": ${COMPLETION:-null},
  "robustness": ${ROBUSTNESS:-null},
  "communication": ${COMMUNICATION:-null},
  "safety": ${SAFETY:-null},
  "passed": ${PASSED_JSON:-null},
  "wall_time_s": ${WALL_TIME:-null},
  "total_tokens": ${TOKENS:-null},
  "input_tokens": ${IN_TOKENS:-null},
  "output_tokens": ${OUT_TOKENS:-null}
}
EOF

cat "$OUT_FILE"
