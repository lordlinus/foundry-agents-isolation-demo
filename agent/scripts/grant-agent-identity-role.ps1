# Post-deploy hook (Windows): grants the hosted agent's managed identity the
# Azure AI User role on the Foundry account.
$ErrorActionPreference = "Stop"

foreach ($v in 'AZURE_SUBSCRIPTION_ID','AZURE_RESOURCE_GROUP','AZURE_AI_ACCOUNT_NAME','AZURE_AI_PROJECT_NAME') {
    if (-not (Get-Item "Env:$v" -ErrorAction SilentlyContinue)) {
        throw "$v is required"
    }
}

$AgentName = if ($env:AGENT_NAME) { $env:AGENT_NAME } else { "docs-helper-agent" }
$IdentityDisplayName = "$($env:AZURE_AI_ACCOUNT_NAME)-$($env:AZURE_AI_PROJECT_NAME)-$AgentName-AgentIdentity"
$Scope = "/subscriptions/$($env:AZURE_SUBSCRIPTION_ID)/resourceGroups/$($env:AZURE_RESOURCE_GROUP)/providers/Microsoft.CognitiveServices/accounts/$($env:AZURE_AI_ACCOUNT_NAME)"

# Roles the agent's microvm identity needs:
#  - Azure AI User: read/write its own session storage, call agent APIs
#  - Cognitive Services OpenAI User: invoke model deployments
$Roles = @("Azure AI User", "Cognitive Services OpenAI User")

Write-Host "[postdeploy] Looking up agent identity: $IdentityDisplayName"

$PrincipalId = $null
for ($i = 1; $i -le 12; $i++) {
    $PrincipalId = az ad sp list --display-name "$IdentityDisplayName" --query "[0].id" -o tsv 2>$null
    if ($PrincipalId -and $PrincipalId -ne "null") { break }
    Write-Host "[postdeploy] Identity not yet visible, retrying ($i/12)..."
    Start-Sleep -Seconds 5
}

if (-not $PrincipalId -or $PrincipalId -eq "null") {
    Write-Warning "[postdeploy] Agent identity '$IdentityDisplayName' not found after retries. Skipping."
    exit 0
}

foreach ($Role in $Roles) {
    Write-Host "[postdeploy] Granting '$Role' to $PrincipalId on $Scope"
    $err = az role assignment create `
        --assignee-object-id $PrincipalId `
        --assignee-principal-type ServicePrincipal `
        --role $Role `
        --scope $Scope `
        -o none 2>&1
    if ($LASTEXITCODE -ne 0) {
        if ($err -match "RoleAssignmentExists") {
            Write-Host "[postdeploy]   already exists — skipping."
        } else {
            Write-Error $err
            exit 1
        }
    } else {
        Write-Host "[postdeploy]   created."
    }
}
Write-Host "[postdeploy] Done."
