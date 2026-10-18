// azure/main.bicep
//
// Provisions the Azure footprint for india-wireless-toolkit:
//   - Azure Container Registry (holds both service images; apps pull with managed identity, no admin user)
//   - Linux App Service Plan
//   - Two Web Apps for Containers (analytics-api / Sanic, news-api / Blacksheep), each with a
//     system-assigned identity, health check, TLS 1.2+, FTPS disabled
//   - Azure Cache for Redis (TLS, used by both services)
//   - Key Vault holding the Redis key and the API key; the apps read them through Key Vault
//     references instead of plain-text app settings
//   - Log Analytics + Application Insights (APPLICATIONINSIGHTS_CONNECTION_STRING is set on both apps)
//   - Storage Account + File Share, mounted into both Web Apps for persistent
//     charts/reports/news_archive.db (container filesystems are ephemeral)
//
// Deploy with:
//   az deployment group create \
//     --resource-group <your-rg> \
//     --template-file azure/main.bicep \
//     --parameters appNamePrefix=iwtoolkit apiKey=<long-random-string>
//
// NOTE: this template was updated alongside v2.1 but could not be compiled in the environment it
// was written in. Run `az bicep build --file azure/main.bicep` (or `az deployment group what-if`)
// before the first deployment. After deployment, restart both apps once so the Key Vault
// references resolve (azure/deploy.sh does this). See azure/README.md for the walkthrough.

@description('Short, globally-unique prefix used to derive resource names (lowercase alphanumeric).')
param appNamePrefix string

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('App Service Plan SKU. B1 is a low-cost starting point; scale up for production.')
param appServicePlanSku string = 'B1'

@description('Azure Cache for Redis SKU.')
@allowed(['Basic', 'Standard'])
param redisSku string = 'Basic'

@description('Azure Cache for Redis capacity (0 = C0/250MB, smallest tier).')
param redisCapacity int = 0

@description('Container image tag both apps run. CI/deploy.sh deploy immutable commit-SHA tags; "latest" is only for the first bootstrap.')
param imageTag string = 'latest'

@secure()
@description('Clients must send this in the X-API-Key header (stored in Key Vault, never in app settings).')
param apiKey string

var acrName = '${appNamePrefix}acr${uniqueString(resourceGroup().id)}'
var planName = '${appNamePrefix}-plan'
var analyticsApiName = '${appNamePrefix}-analytics-api'
var newsApiName = '${appNamePrefix}-news-api'
var redisName = '${appNamePrefix}-redis'
var storageAccountName = toLower('${appNamePrefix}st${uniqueString(resourceGroup().id)}')
var fileShareName = 'toolkit-data'
var keyVaultName = take('${appNamePrefix}kv${uniqueString(resourceGroup().id)}', 24)
var logAnalyticsName = '${appNamePrefix}-logs'
var appInsightsName = '${appNamePrefix}-insights'

// Built-in role definition IDs
var acrPullRoleId = '7f951dda-4ed3-4967-97b7-0a0d4a3de3ab'
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
  }
}

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageAccountName
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

resource fileServices 'Microsoft.Storage/storageAccounts/fileServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
}

resource fileShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-01-01' = {
  parent: fileServices
  name: fileShareName
  properties: {
    shareQuota: 20
  }
}

resource redis 'Microsoft.Cache/redis@2023-08-01' = {
  name: redisName
  location: location
  properties: {
    sku: {
      name: redisSku
      family: 'C'
      capacity: redisCapacity
    }
    enableNonSslPort: false
    minimumTlsVersion: '1.2'
  }
}

// --- Observability -----------------------------------------------------------

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: logAnalyticsName
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

// --- Secrets -------------------------------------------------------------------

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
  }
}

resource redisPasswordSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'redis-password'
  properties: {
    value: redis.listKeys().primaryKey
  }
}

resource apiKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'api-key'
  properties: {
    value: apiKey
  }
}

// Versionless references: rotating a secret in Key Vault is picked up without redeploying.
var redisPasswordRef = '@Microsoft.KeyVault(SecretUri=${keyVault.properties.vaultUri}secrets/redis-password/)'
var apiKeyRef = '@Microsoft.KeyVault(SecretUri=${keyVault.properties.vaultUri}secrets/api-key/)'

// --- App Service ------------------------------------------------------------------

resource plan 'Microsoft.Web/serverfarms@2023-01-01' = {
  name: planName
  location: location
  kind: 'linux'
  sku: { name: appServicePlanSku }
  properties: {
    reserved: true
  }
}

var commonAppSettings = [
  { name: 'WEBSITES_ENABLE_APP_SERVICE_STORAGE', value: 'false' }
  { name: 'DOCKER_REGISTRY_SERVER_URL', value: 'https://${acr.properties.loginServer}' }
  { name: 'REDIS_HOST', value: redis.properties.hostName }
  { name: 'REDIS_PORT', value: '6380' }
  { name: 'REDIS_SSL', value: 'true' }
  { name: 'REDIS_PASSWORD', value: redisPasswordRef }
  { name: 'API_KEY', value: apiKeyRef }
  { name: 'API_REQUIRE_KEY', value: 'true' }
  { name: 'TRUST_PROXY', value: 'true' }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
  { name: 'AZURE_CHARTS_DIR', value: '/mnt/toolkit-data/charts' }
]

// --- Analytics API (Sanic) --------------------------------------------------

resource analyticsApi 'Microsoft.Web/sites@2023-01-01' = {
  name: analyticsApiName
  location: location
  kind: 'app,linux,container'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'DOCKER|${acr.properties.loginServer}/india-wireless-analytics-api:${imageTag}'
      acrUseManagedIdentityCreds: true
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      http20Enabled: true
      healthCheckPath: '/health'
      appSettings: concat(commonAppSettings, [
        { name: 'WEBSITES_PORT', value: '8001' }
      ])
      azureStorageAccounts: {
        toolkitdata: {
          type: 'AzureFiles'
          accountName: storageAccount.name
          shareName: fileShareName
          mountPath: '/mnt/toolkit-data'
          accessKey: storageAccount.listKeys().keys[0].value
        }
      }
    }
  }
}

// --- News/Report API (Blacksheep) -------------------------------------------

resource newsApi 'Microsoft.Web/sites@2023-01-01' = {
  name: newsApiName
  location: location
  kind: 'app,linux,container'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'DOCKER|${acr.properties.loginServer}/india-wireless-news-api:${imageTag}'
      acrUseManagedIdentityCreds: true
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      http20Enabled: true
      healthCheckPath: '/health'
      appSettings: concat(commonAppSettings, [
        { name: 'WEBSITES_PORT', value: '8002' }
        { name: 'AZURE_REPORTS_DIR', value: '/mnt/toolkit-data/reports' }
        { name: 'AZURE_NEWS_DB_PATH', value: '/mnt/toolkit-data/news_archive.db' }
        // Set these manually after deployment (kept out of source control/IaC state; for
        // production prefer Key Vault references): ALERT_WEBHOOK_URL, SMTP_HOST, SMTP_PORT,
        // SMTP_FROM_ADDR, SMTP_TO_ADDR, SMTP_USER, SMTP_PASSWORD, ANTHROPIC_API_KEY
      ])
      azureStorageAccounts: {
        toolkitdata: {
          type: 'AzureFiles'
          accountName: storageAccount.name
          shareName: fileShareName
          mountPath: '/mnt/toolkit-data'
          accessKey: storageAccount.listKeys().keys[0].value
        }
      }
    }
  }
}

// --- Identity-based access (replaces the ACR admin password and plain-text secrets) ----

resource analyticsAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, analyticsApi.id, acrPullRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: analyticsApi.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource newsAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, newsApi.id, acrPullRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: newsApi.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource analyticsKeyVaultAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, analyticsApi.id, keyVaultSecretsUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: analyticsApi.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource newsKeyVaultAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, newsApi.id, keyVaultSecretsUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: newsApi.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output acrLoginServer string = acr.properties.loginServer
output analyticsApiUrl string = 'https://${analyticsApi.properties.defaultHostName}'
output newsApiUrl string = 'https://${newsApi.properties.defaultHostName}'
output redisHostName string = redis.properties.hostName
output keyVaultName string = keyVault.name
output storageAccountName string = storageAccount.name
output fileShareName string = fileShareName
