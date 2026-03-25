#!/bin/bash
# =============================================================================
# FC E2E Test Runner - HTTP Protocol
# =============================================================================
#
# Tests the deployed FC function via HTTP trigger
#
# Usage:
#   ./run_e2e_http_tests.sh                    # Auto-detect URL from s info
#   FC_URL=https://xxx.fcapp.run ./run_e2e...  # Use custom URL
#

# Don't exit on error - we want to continue testing
# set -e

# =============================================================================
# Get FC URL
# =============================================================================
# Priority: FC_URL env var > s info command

get_fc_url() {
    # 1. Check environment variable
    if [ -n "$FC_URL" ]; then
        echo "$FC_URL"
        return 0
    fi

    # 2. Try to get from s info (requires s CLI and deployed function)
    local runtime_dir="$(dirname "$0")/runtime"
    if [ -d "$runtime_dir" ] && command -v s &> /dev/null; then
        # Use temp file to avoid pipe and potential resource issues
        local tmp_file=$(mktemp)
        local url=""
        # Save s info output to temp file
        if (cd "$runtime_dir" && s info --output json > "$tmp_file" 2>/dev/null); then
            # Extract URL using grep/sed (avoids creating extra python process)
            url=$(grep -o '"system_url"[[:space:]]*:[[:space:]]*"[^"]*"' "$tmp_file" 2>/dev/null | \
                  sed 's/.*"system_url"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/' | head -1)
        fi
        rm -f "$tmp_file"
        if [ -n "$url" ]; then
            echo "$url"
            return 0
        fi
    fi

    # 3. Failed to get URL
    echo "ERROR: Could not determine FC URL" >&2
    echo "Set FC_URL environment variable or ensure s CLI is available with deployed function" >&2
    exit 1
}

FC_URL=$(get_fc_url)
SESSION_ID="e2e-http-test-$(date +%s)"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

PASS=0
FAIL=0
SKIP=0

echo "========================================"
echo "FC E2E Test Runner (HTTP Protocol)"
echo "========================================"
echo "FC URL: $FC_URL"
echo "Session ID: $SESSION_ID"
echo ""

# Test function
test_case() {
    local test_id=$1
    local test_name=$2
    local test_cmd=$3
    local expected=$4

    echo -n "[$test_id] $test_name... "

    result=$(eval "$test_cmd" 2>&1)
    exit_code=$?

    if [ $exit_code -eq 0 ] && echo "$result" | grep -q "$expected"; then
        echo -e "${GREEN}PASSED${NC}"
        ((PASS++))
    else
        echo -e "${RED}FAILED${NC}"
        echo "  Expected: $expected"
        echo "  Got: $result"
        ((FAIL++))
    fi
}

# =============================================================================
# E2E-HTTP-01: Health Check
# =============================================================================

echo "--- E2E-HTTP-01: Health Check ---"

test_case "E2E-HTTP-01" "Health check (is_alive)" \
    "curl -s '$FC_URL/is_alive'" \
    '"is_alive": true'

echo ""

# =============================================================================
# E2E-HTTP-02: Session Lifecycle
# =============================================================================

echo "--- E2E-HTTP-02: Session Lifecycle ---"

test_case "E2E-HTTP-02a" "Create session" \
    "curl -s -X POST '$FC_URL/create_session' -H 'Content-Type: application/json' -H 'x-rock-session-id: $SESSION_ID' -d '{\"session_type\": \"bash\", \"session\": \"$SESSION_ID\"}'" \
    'session_type'

test_case "E2E-HTTP-02b" "Run echo command" \
    "curl -s -X POST '$FC_URL/run_in_session' -H 'Content-Type: application/json' -H 'x-rock-session-id: $SESSION_ID' -d '{\"action_type\": \"bash\", \"session\": \"$SESSION_ID\", \"command\": \"echo e2e-test\"}'" \
    'e2e-test'

echo ""

# =============================================================================
# E2E-HTTP-03: Command Execution
# =============================================================================

echo "--- E2E-HTTP-03: Command Execution ---"

test_case "E2E-HTTP-03a" "Command with pipe" \
    "curl -s -X POST '$FC_URL/run_in_session' -H 'Content-Type: application/json' -H 'x-rock-session-id: $SESSION_ID' -d '{\"action_type\": \"bash\", \"session\": \"$SESSION_ID\", \"command\": \"echo hello world | wc -w\"}'" \
    '2'

test_case "E2E-HTTP-03b" "Environment variable persistence" \
    "curl -s -X POST '$FC_URL/run_in_session' -H 'Content-Type: application/json' -H 'x-rock-session-id: $SESSION_ID' -d '{\"action_type\": \"bash\", \"session\": \"$SESSION_ID\", \"command\": \"export TEST_VAR=e2e_value\"}'" \
    'exit_code.*0'

test_case "E2E-HTTP-03c" "Check environment variable" \
    "curl -s -X POST '$FC_URL/run_in_session' -H 'Content-Type: application/json' -H 'x-rock-session-id: $SESSION_ID' -d '{\"action_type\": \"bash\", \"session\": \"$SESSION_ID\", \"command\": \"echo \$TEST_VAR\"}'" \
    'e2e_value'

test_case "E2E-HTTP-03d" "Working directory change" \
    "curl -s -X POST '$FC_URL/run_in_session' -H 'Content-Type: application/json' -H 'x-rock-session-id: $SESSION_ID' -d '{\"action_type\": \"bash\", \"session\": \"$SESSION_ID\", \"command\": \"cd /tmp && pwd\"}'" \
    '/tmp'

test_case "E2E-HTTP-03e" "Verify directory persistence" \
    "curl -s -X POST '$FC_URL/run_in_session' -H 'Content-Type: application/json' -H 'x-rock-session-id: $SESSION_ID' -d '{\"action_type\": \"bash\", \"session\": \"$SESSION_ID\", \"command\": \"pwd\"}'" \
    '/tmp'

echo ""

# =============================================================================
# E2E-HTTP-04: File Operations
# =============================================================================

echo "--- E2E-HTTP-04: File Operations ---"

test_case "E2E-HTTP-04a" "Write file" \
    "curl -s -X POST '$FC_URL/write_file' -H 'Content-Type: application/json' -H 'x-rock-session-id: $SESSION_ID' -d '{\"path\": \"/tmp/e2e_test_file.txt\", \"content\": \"E2E test content 12345\"}'" \
    'success.*true'

test_case "E2E-HTTP-04b" "Read file" \
    "curl -s -X POST '$FC_URL/read_file' -H 'Content-Type: application/json' -H 'x-rock-session-id: $SESSION_ID' -d '{\"path\": \"/tmp/e2e_test_file.txt\"}'" \
    'E2E test content 12345'

echo ""

# =============================================================================
# E2E-HTTP-05: Direct Execute (no session)
# =============================================================================

echo "--- E2E-HTTP-05: Direct Execute ---"

test_case "E2E-HTTP-05a" "Execute without session" \
    "curl -s -X POST '$FC_URL/execute' -H 'Content-Type: application/json' -d '{\"command\": \"echo direct execute test\", \"timeout\": 30, \"shell\": true}'" \
    'direct execute test\|stdout'

echo ""

# =============================================================================
# E2E-HTTP-06: Error Handling
# =============================================================================

echo "--- E2E-HTTP-06: Error Handling ---"

test_case "E2E-HTTP-06a" "Read nonexistent file" \
    "curl -s -X POST '$FC_URL/read_file' -H 'Content-Type: application/json' -H 'x-rock-session-id: $SESSION_ID' -d '{\"path\": \"/tmp/nonexistent_file_xyz.txt\"}'" \
    'error\|success.*false'

echo ""

# =============================================================================
# E2E-HTTP-07: Close Session
# =============================================================================

echo "--- E2E-HTTP-07: Session Cleanup ---"

test_case "E2E-HTTP-07a" "Close session" \
    "curl -s -X POST '$FC_URL/close_session' -H 'Content-Type: application/json' -H 'x-rock-session-id: $SESSION_ID' -d '{\"session\": \"$SESSION_ID\", \"session_type\": \"bash\"}'" \
    'session_type.*bash'

echo ""

# =============================================================================
# Summary
# =============================================================================

echo "========================================"
echo "Test Summary"
echo "========================================"
echo -e "Passed: ${GREEN}$PASS${NC}"
echo -e "Failed: ${RED}$FAIL${NC}"
echo -e "Skipped: ${YELLOW}$SKIP${NC}"
echo "Total: $((PASS + FAIL + SKIP))"
echo ""

if [ $FAIL -eq 0 ]; then
    echo -e "${GREEN}All tests passed!${NC}"
    exit 0
else
    echo -e "${RED}Some tests failed!${NC}"
    exit 1
fi