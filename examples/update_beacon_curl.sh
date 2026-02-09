#!/bin/bash
#
# Simple one-liner to update beacon comment via HTTP POST API
#
# Usage:
#   ./update_beacon_curl.sh "Your new comment"
#   ./update_beacon_curl.sh "Updated status" --tx    # Send beacon immediately
#
# Configuration:
WEBUI_URL="${WEBUI_URL:-http://localhost:10002}"
PASSWORD="${WEBUI_PASSWORD:-your_password_here}"

# Check if password is still default
if [ "$PASSWORD" = "your_password_here" ]; then
    echo "ERROR: Please set WEBUI_PASSWORD environment variable or edit this script!"
    echo ""
    echo "Examples:"
    echo "  export WEBUI_PASSWORD=your_secure_password"
    echo "  ./update_beacon_curl.sh \"New status\""
    echo ""
    echo "Or:"
    echo "  WEBUI_PASSWORD=your_password ./update_beacon_curl.sh \"New status\""
    exit 1
fi

# Check if comment provided
if [ -z "$1" ]; then
    echo "Usage: $0 <comment> [--tx]"
    echo ""
    echo "Examples:"
    echo "  $0 \"Testing from shell\""
    echo "  $0 \"Live update!\" --tx    # Send beacon immediately"
    exit 1
fi

COMMENT="$1"
TX="false"

# Check for --tx flag
if [ "$2" = "--tx" ]; then
    TX="true"
fi

# Execute curl and capture response
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "$WEBUI_URL/api/beacon/comment" \
  -H "Content-Type: application/json" \
  -d "{\"password\":\"$PASSWORD\",\"comment\":\"$COMMENT\",\"tx\":$TX}" 2>&1)

# Check if curl itself failed (connection error)
if [ $? -ne 0 ]; then
    echo "✗ Connection error: Could not reach $WEBUI_URL"
    echo ""
    echo "Possible causes:"
    echo "  - Web UI is not running"
    echo "  - Wrong URL (current: $WEBUI_URL)"
    echo "  - Firewall blocking connection"
    echo ""
    echo "Check that the console is running and web UI is enabled."
    exit 1
fi

# Extract HTTP status code (last line) and JSON body (all but last line)
HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
JSON_BODY=$(echo "$RESPONSE" | head -n-1)

# Pretty print JSON
echo "$JSON_BODY" | jq '.' 2>/dev/null || echo "$JSON_BODY"

# Check HTTP status code
if [ "$HTTP_CODE" != "200" ]; then
    echo ""
    echo "✗ HTTP Error $HTTP_CODE"

    # Parse error message from JSON if available
    ERROR_MSG=$(echo "$JSON_BODY" | jq -r '.error // empty' 2>/dev/null)
    if [ -n "$ERROR_MSG" ]; then
        echo "   $ERROR_MSG"

        # Provide helpful hints based on error
        if echo "$ERROR_MSG" | grep -q "Invalid password"; then
            echo ""
            echo "Hint: Check that WEBUI_PASSWORD matches the console setting"
        elif echo "$ERROR_MSG" | grep -q "disabled"; then
            echo ""
            echo "Hint: Set password in console: WEBUI_PASSWORD your_password"
        fi
    fi
    exit 1
fi

# Check JSON response for success field
SUCCESS=$(echo "$JSON_BODY" | jq -r '.success // false' 2>/dev/null)

if [ "$SUCCESS" = "true" ]; then
    echo ""
    echo "✓ Beacon comment updated successfully!"

    # Show if beacon was transmitted
    BEACON_SENT=$(echo "$JSON_BODY" | jq -r '.beacon_sent // false' 2>/dev/null)
    if [ "$BEACON_SENT" = "true" ]; then
        echo "  (Beacon transmitted immediately)"
    fi

    # Show if comment was truncated
    TRUNCATED=$(echo "$JSON_BODY" | jq -r '.truncated // false' 2>/dev/null)
    if [ "$TRUNCATED" = "true" ]; then
        echo "  (Comment was truncated to 43 characters)"
    fi
else
    echo ""
    echo "✗ Update failed"
    ERROR_MSG=$(echo "$JSON_BODY" | jq -r '.error // "Unknown error"' 2>/dev/null)
    echo "   $ERROR_MSG"
    exit 1
fi
