targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Environment name used for resource naming (azd env).')
param environmentName string

@description('Azure region for new resources.')
param location string

@description('Resource group name.')
param resourceGroupName string = 'rg-foundry-agents-demo'

@description('Existing Foundry / Azure AI Services account resource ID (the parent of the project endpoint).')
param foundryAccountResourceId string

@description('Foundry project endpoint, e.g. https://<account>.services.ai.azure.com/api/projects/<project>')
param foundryProjectEndpoint string

@description('Hosted agent name.')
param foundryAgentName string = 'docs-helper-agent'

@description('Foundry API version.')
param foundryApiVersion string = '2025-11-15-preview'

@description('Foundry-Features preview header value.')
param foundryFeatures string = 'HostedAgents=V1Preview,AgentEndpoints=V1Preview'

@description('App Insights resource name (created in same RG).')
param appInsightsName string = 'appi-foundry-agents-demo'

var tags = {
  'azd-env-name': environmentName
  project: 'foundry-agents-isolation-demo'
}

var foundryRgName = split(foundryAccountResourceId, '/')[4]
var foundryAccountName = split(foundryAccountResourceId, '/')[8]

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module workload 'workload.bicep' = {
  scope: rg
  name: 'workload'
  params: {
    location: location
    environmentName: environmentName
    tags: tags
    foundryProjectEndpoint: foundryProjectEndpoint
    foundryAgentName: foundryAgentName
    foundryApiVersion: foundryApiVersion
    foundryFeatures: foundryFeatures
    appInsightsName: appInsightsName
  }
}

// Grant the Container App's managed identity access to call the Foundry account.
// Role: "Azure AI User" — runtime access to AI projects / agents.
var azureAiUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'
// Belt-and-braces: "Cognitive Services User" for any older data-plane checks.
var cogSvcUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'

module roleAssignAi 'role-assignment.bicep' = {
  scope: resourceGroup(foundryRgName)
  name: 'role-foundry-aiuser'
  params: {
    foundryAccountName: foundryAccountName
    principalId: workload.outputs.identityPrincipalId
    roleDefinitionId: azureAiUserRoleId
  }
}

module roleAssignCog 'role-assignment.bicep' = {
  scope: resourceGroup(foundryRgName)
  name: 'role-foundry-coguser'
  params: {
    foundryAccountName: foundryAccountName
    principalId: workload.outputs.identityPrincipalId
    roleDefinitionId: cogSvcUserRoleId
  }
}

output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = rg.name
output SERVICE_API_NAME string = workload.outputs.containerAppName
output SERVICE_API_FQDN string = workload.outputs.containerAppFqdn
output SERVICE_API_URI string = 'https://${workload.outputs.containerAppFqdn}'
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = workload.outputs.acrLoginServer
output AZURE_CONTAINER_REGISTRY_NAME string = workload.outputs.acrName
output AZURE_CONTAINER_ENVIRONMENT_NAME string = workload.outputs.envName
output MANAGED_IDENTITY_PRINCIPAL_ID string = workload.outputs.identityPrincipalId
output MANAGED_IDENTITY_CLIENT_ID string = workload.outputs.identityClientId
output FOUNDRY_PROJECT_ENDPOINT string = foundryProjectEndpoint
output FOUNDRY_AGENT_NAME string = foundryAgentName
