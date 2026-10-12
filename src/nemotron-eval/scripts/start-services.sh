#!/bin/bash
# Lifecycle hook (postCreate and postStart), run as root after Workbench login.
# Starts the Workbench MCP server and regenerates the workspace context as the
# app user, so both see that user's wb credentials.
set -o nounset
set -o pipefail

readonly USER_NAME="${1:?Usage: start-services.sh <username> <user-home-dir>}"
readonly USER_HOME_DIR="${2:?Usage: start-services.sh <username> <user-home-dir>}"

sudo -H -u "${USER_NAME}" /opt/wb-mcp-server/start-server.sh || echo "wb-mcp-server failed to start" >&2

# Context generation waits for wb to be ready; run it in the background so a
# slow workspace doesn't hold up startup. The app picks the file up when it appears.
nohup sudo -H -u "${USER_NAME}" /opt/llm-context/run-context-generator.sh "${USER_HOME_DIR}" \
    > /tmp/llm-context.log 2>&1 < /dev/null &
echo "Started wb-mcp-server and workspace context generation for ${USER_NAME}"
