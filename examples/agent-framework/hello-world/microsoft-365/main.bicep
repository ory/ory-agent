// Hosts the Microsoft 365 custom engine agent on Azure Container Apps behind an Azure Bot,
// with its model served by a Microsoft Foundry project. One user-assigned managed identity is
// the bot identity, the image pull identity, and the Foundry caller, so no Microsoft or model
// credential is created or stored. deploy.py supplies runtimeSecrets from memory; never pass
// them on a command line or in a parameters file.

targetScope = 'resourceGroup'

@description('Lowercase prefix for resource names.')
@minLength(3)
@maxLength(16)
param namePrefix string

@description('Azure region for regional resources.')
param location string = resourceGroup().location

@description('Existing Azure Container Registry in this resource group that holds the Agent image.')
param registryName string

@description('Immutable image reference: <login server>/<repository>@sha256:<digest>.')
param image string

@description('Unique per release so the new revision always loads the current secrets.')
param revisionSuffix string

@description('Foundry model deployed for the Agent.')
param modelName string = 'gpt-5.4-mini'

@description('Model version. Pin it so a deployment never changes model behavior implicitly.')
param modelVersion string = '2026-03-17'

@description('Model deployment type. DataZoneStandard keeps processing in the region\'s data zone and has separate quota.')
@allowed([
  'GlobalStandard'
  'DataZoneStandard'
])
param modelSku string = 'GlobalStandard'

@description('Model deployment capacity in thousands of tokens per minute.')
param modelCapacity int = 10

@secure()
@description('Agent Security runtime values from the deployment package.')
param runtimeSecrets object

var suffix = uniqueString(resourceGroup().id, namePrefix)
var secretEnvironment = [
  'ORY_PROJECT_URL'
  'ORY_AGENT_SECURITY_URL'
  'ORY_AGENT_TYPE'
  'ORY_AGENT_CREDENTIAL_METHOD'
  'ORY_AGENT_API_KEY'
]
var secretEnvironmentReferences = [
  for name in secretEnvironment: {
    name: name
    secretRef: toLower(replace(name, '_', '-'))
  }
]
var acrPullRoleDefinitionId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var foundryUserRoleDefinitionId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'
var foundryName = '${namePrefix}-${suffix}'
var foundryProjectName = 'agent'

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-agent'
  location: location
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, acrPullRoleDefinitionId)
  scope: registry
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleDefinitionId)
  }
}

// Entra ID is the only way to call the model: local (key) authentication is disabled.
resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: foundryName
  location: location
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: foundryName
    allowProjectManagement: true
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundry
  name: foundryProjectName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {}
}

// Foundry accepts one child operation per account at a time; serialize after the project.
resource model 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  name: modelName
  dependsOn: [
    foundryProject
  ]
  sku: {
    name: modelSku
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

// Foundry User (formerly Azure AI User) on the project lets the Agent call its models and nothing at the account level.
resource foundryUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryProject.id, identity.id, foundryUserRoleDefinitionId)
  scope: foundryProject
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryUserRoleDefinitionId)
  }
}

var foundryProjectEndpoint = 'https://${foundry.properties.customSubDomainName}.services.ai.azure.com/api/projects/${foundryProject.name}'

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${namePrefix}-logs'
  location: location
  properties: {
    retentionInDays: 30
    sku: {
      name: 'PerGB2018'
    }
  }
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${namePrefix}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${namePrefix}-agent'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  dependsOn: [
    acrPull
    foundryUser
  ]
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 3978
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: identity.id
        }
      ]
      secrets: [
        for name in secretEnvironment: {
          name: toLower(replace(name, '_', '-'))
          // The linter cannot trace secure values through a loop over a @secure() object.
          #disable-next-line use-secure-value-for-secure-inputs
          value: runtimeSecrets[name]
        }
      ]
    }
    template: {
      revisionSuffix: revisionSuffix
      containers: [
        {
          name: 'agent'
          image: image
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: concat(
            secretEnvironmentReferences,
            [
              {
                name: 'FOUNDRY_PROJECT_ENDPOINT'
                value: foundryProjectEndpoint
              }
              {
                name: 'FOUNDRY_MODEL'
                value: model.name
              }
              // DefaultAzureCredential uses only this managed identity in the container.
              {
                name: 'AZURE_TOKEN_CREDENTIALS'
                value: 'ManagedIdentityCredential'
              }
              {
                name: 'AZURE_CLIENT_ID'
                value: identity.properties.clientId
              }
              {
                name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__AUTHTYPE'
                value: 'UserManagedIdentity'
              }
              {
                name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID'
                value: identity.properties.clientId
              }
              {
                name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID'
                value: tenant().tenantId
              }
            ]
          )
          probes: [
            {
              type: 'Readiness'
              httpGet: {
                path: '/healthz'
                port: 3978
              }
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 3978
              }
            }
          ]
        }
      ]
      // Conversation state is in memory; keep one warm replica for channel response deadlines.
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

resource bot 'Microsoft.BotService/botServices@2022-09-15' = {
  name: '${namePrefix}-${suffix}'
  location: 'global'
  kind: 'azurebot'
  sku: {
    name: 'F0'
  }
  properties: {
    displayName: 'Ory Agent Security hello world'
    endpoint: 'https://${app.properties.configuration.ingress.fqdn}/api/messages'
    msaAppId: identity.properties.clientId
    msaAppType: 'UserAssignedMSI'
    msaAppMSIResourceId: identity.id
    msaAppTenantId: tenant().tenantId
  }
}

resource teams 'Microsoft.BotService/botServices/channels@2022-09-15' = {
  parent: bot
  name: 'MsTeamsChannel'
  location: 'global'
  properties: {
    channelName: 'MsTeamsChannel'
    properties: {
      isEnabled: true
    }
  }
}

// deploy.py uses Direct Line for the headless activation probe through the same Azure Bot.
resource directLine 'Microsoft.BotService/botServices/channels@2022-09-15' = {
  parent: bot
  name: 'DirectLineChannel'
  location: 'global'
  properties: {
    channelName: 'DirectLineChannel'
    properties: {
      sites: [
        {
          siteName: 'ory-activation-probe'
          isEnabled: true
          isV1Enabled: false
          isV3Enabled: true
          isSecureSiteEnabled: false
          isBlockUserUploadEnabled: true
        }
      ]
    }
  }
}

output containerAppName string = app.name
output endpoint string = 'https://${app.properties.configuration.ingress.fqdn}'
output botName string = bot.name
output botAppId string = identity.properties.clientId
output foundryProjectEndpoint string = foundryProjectEndpoint
