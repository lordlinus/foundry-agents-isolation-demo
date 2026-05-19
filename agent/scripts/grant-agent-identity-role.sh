#!/usr/bin/env bash
# Post-deploy hook: grants the hosted agent's managed identity the Azure AI User role
# on the Foundry account so it can read/write its own session storage.
#
# The agent identity is created by `azd deploy` (after provisioning), so this can't
# live in bicep — it has to run after registration.
#
# Identity naming convention (as of azd `azure.ai.agents` extension v0.1.x):
#   <ai-account>-<ai-project>-<agent-name>-AgentIdentity
set -euo pipefail

: "${AZURE_SUBSCRIPTION_ID:?AZURE_SUBSCRIPTION_ID is required}"
: "${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
: "${AZURE_AI_ACCOUNT_NAME:?AZURE_AI_ACCOUNT_NAME is required}"
: "${AZURE_AI_PROJECT_NAME:?AZURE_AI_PROJECT_NAME is required}"

AGENT_NAME="${AGENT_NAME:-docs-helper-agent}"
IDENTITY_DISPLAY_NAME="${AZURE_AI_ACCOUNT_NAME}-${AZURE_AI_PROJECT_NAME}-${AGENT_NAME}-AgentIdentity"
SCOPE="/subscriptions/${AZURE_SUBSCRIPTION_ID}/resourceGroups/${AZURE_RESOURCE_GROUP}/providers/Microsoft.CognitiveServices/accounts/${AZURE_AI_ACCOUNT_NAME}"

# Roles the agent's microvm identity needs (referenced by GUID — immutable
# across renames; display-name lookup occasionally fails in some tenants):
#  - Azure AI User                       53ca6127-db72-4b80-b1b0-d745d6d5456d
#  - Cognitive Services OpenAI User      5e0bd9bd-7b17-4742-d0bb-3c438a228a0d
declare -A ROLES=(
  ["Azure AI User"]="53ca6127-db72-4b80-b1b0-d745d6d5456d"
  ["Cognitive Services OpenAI User"]="5e0bd9bd-7b17-4742-d0bb-3c438a228a0d"
)

echo "[postdeploy] Looking up agent identity: ${IDENTITY_DISPLAY_NAME}"

PRINCIPAL_ID=""
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
  PRINCIPAL_ID="$(az ad sp list --display-name "${IDENTITY_DISPLAY_NAME}" --query "[0].id" -o tsv 2>/dev/null || true)"
  if [ -n "${PRINCIPAL_ID}" ] && [ "${PRINCIPAL_ID}" != "null" ]; then
    break
  fi
  echo "[postdeploy] Identity not yet visible, retrying (${i}/12)..."
  sleep 5
done

if [ -z "${PRINCIPAL_ID}" ] || [ "${PRINCIPAL_ID}" = "null" ]; then
  echo "[postdeploy] WARNING: Agent identity '${IDENTITY_DISPLAY_NAME}' not found after retries."
  echo "[postdeploy] Skipping role assignments. You may need to grant them manually."
  exit 0
fi

for ROLE_NAME in "${!ROLES[@]}"; do
  ROLE_ID="${ROLES[$ROLE_NAME]}"
  ROLE_DEF="/subscriptions/${AZURE_SUBSCRIPTION_ID}/providers/Microsoft.Authorization/roleDefinitions/${ROLE_ID}"
  echo "[postdeploy] Granting '${ROLE_NAME}' (${ROLE_ID}) to ${PRINCIPAL_ID} on ${SCOPE}"
  if az role assignment create \
      --assignee-object-id "${PRINCIPAL_ID}" \
      --assignee-principal-type ServicePrincipal \
      --role "${ROLE_ID}" \
      --scope "${SCOPE}" \
      -o none 2>/tmp/azroleerr.txt; then
    echo "[postdeploy]   created."
  else
    if grep -q "RoleAssignmentExists" /tmp/azroleerr.txt 2>/dev/null; then
      echo "[postdeploy]   already exists — skipping."
    else
      echo "[postdeploy]   WARNING: could not (re)assign '${ROLE_NAME}':"
      sed 's/^/[postdeploy]     /' /tmp/azroleerr.txt
      echo "[postdeploy]   Continuing — prior deploys may have already granted this role."
    fi
  fi
done

echo "[postdeploy] Done."
