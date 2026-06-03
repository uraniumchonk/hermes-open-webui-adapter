#!/bin/bash
# Register the Hermes Tool Filter Pipeline with Open WebUI
# Usage: ./register_pipeline.sh [open_webui_base_url]

OPENWEBUI_URL="${1:-http://localhost:30010}"
PIPELINE_URL="http://localhost:9099"

echo "Registering Hermes Tool Filter Pipeline..."
echo "  Open WebUI: ${OPENWEBUI_URL}"
echo "  Pipeline:   ${PIPELINE_URL}"

RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "${OPENWEBUI_URL}/api/pipelines/add" \
  -H "Content-Type: application/json" \
  -d "{
    \"url\": \"${PIPELINE_URL}\",
    \"name\": \"Hermes Tool Call → Details Card Converter\",
    \"url_name\": \"hermes_tool_details_converter\"
  }")

HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | head -n -1)

echo ""
echo "HTTP Status: ${HTTP_CODE}"
echo "Response: ${BODY}"

if [ "$HTTP_CODE" = "200" ]; then
    echo ""
    echo "✅ Pipeline registered successfully!"
    echo ""
    echo "Next step: Go to Open WebUI Settings → Pipelines"
    echo "Enable the 'Hermes Tool Call → Details Card Converter' filter"
    echo "Set type to 'filter' and mode to 'outlet'"
else
    echo ""
    echo "❌ Failed to register pipeline"
fi
