// azure/main.bicep
//
// Provisions the Azure footprint for india-wireless-toolkit:
//   - Azure Container Registry (holds both service images)
//   - Linux App Service Plan
//   - Two Web Apps for Containers (analytics-api / Sanic, news-api / Blacksheep)
//   - Azure Cache for Redis (TLS, used by both services)
//   - Storage Account + File Share, mounted into both Web Apps for
//     persistent charts/reports/news_archive.db (container filesystems
//     are ephemeral, so this survives restarts/redeploys)
//
// Deploy with:
//   az deployment group create \
//     --resource-group <your-rg> \
//     --template-file azure/main.bicep \
//     --parameters appNamePrefix=iwtoolkit
//
// After deployment, build+push images and set the SMTP/webhook secrets —
// see azure/README.md for the full walkthrough.

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

var acrName = '${appNamePrefix}acr${uniqueString(resourceGroup().id)}'
var planName = '${appNamePrefix}-plan'
var analyticsApiName = '${appNamePrefix}-analytics-api'
var newsApiName = '${appNamePrefix}-news-api'
var redisName = '${appNamePrefix}-redis'
var storageAccountName = toLower('${appNamePrefix}st${uniqueString(resourceGroup().id)}')
var fileShareName = 'toolkit-data'

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: true
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

resource plan 'Microsoft.Web/serverfarms@2023-01-01' = {
  name: planName
  location: location
  kind: 'linux'
  sku: { name: appServicePlanSku }
  properties: {
    reserved: true
  }
}

// --- Analytics API (Sanic) --------------------------------------------------

resource analyticsApi 'Microsoft.Web/sites@2023-01-01' = {
  name: analyticsApiName
  location: location
  kind: 'app,linux,container'
  properties: {
    serverFarmId: plan.id
    siteConfig: {
      linuxFxVersion: 'DOCKER|${acr.properties.loginServer}/india-wireless-analytics-api:latest'
      appSettings: [
        { name: 'WEBSITES_ENABLE_APP_SERVICE_STORAGE', value: 'false' }
        { name: 'DOCKER_REGISTRY_SERVER_URL', value: 'https://${acr.properties.loginServer}' }
        { name: 'DOCKER_REGISTRY_SERVER_USERNAME', value: acr.listCredentials().username }
        { name: 'DOCKER_REGISTRY_SERVER_PASSWORD', value: acr.listCredentials().passwords[0].value }
        { name: 'WEBSITES_PORT', value: '8001' }
        { name: 'REDIS_HOST', value: redis.properties.hostName }
        { name: 'REDIS_PORT', value: '6380' }
        { name: 'REDIS_SSL', value: 'true' }
        { name: 'REDIS_PASSWORD', value: redis.listKeys().primaryKey }
        { name: 'AZURE_CHARTS_DIR', value: '/mnt/toolkit-data/charts' }
      ]
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
    httpsOnly: true
  }
}

// --- News/Report API (Blacksheep) -------------------------------------------

resource newsApi 'Microsoft.Web/sites@2023-01-01' = {
  name: newsApiName
  location: location
  kind: 'app,linux,container'
  properties: {
    serverFarmId: plan.id
    siteConfig: {
      linuxFxVersion: 'DOCKER|${acr.properties.loginServer}/india-wireless-news-api:latest'
      appSettings: [
        { name: 'WEBSITES_ENABLE_APP_SERVICE_STORAGE', value: 'false' }
        { name: 'DOCKER_REGISTRY_SERVER_URL', value: 'https://${acr.properties.loginServer}' }
        { name: 'DOCKER_REGISTRY_SERVER_USERNAME', value: acr.listCredentials().username }
        { name: 'DOCKER_REGISTRY_SERVER_PASSWORD', value: acr.listCredentials().passwords[0].value }
        { name: 'WEBSITES_PORT', value: '8002' }
        { name: 'REDIS_HOST', value: redis.properties.hostName }
        { name: 'REDIS_PORT', value: '6380' }
        { name: 'REDIS_SSL', value: 'true' }
        { name: 'REDIS_PASSWORD', value: redis.listKeys().primaryKey }
        { name: 'AZURE_CHARTS_DIR', value: '/mnt/toolkit-data/charts' }
        { name: 'AZURE_REPORTS_DIR', value: '/mnt/toolkit-data/reports' }
        { name: 'AZURE_NEWS_DB_PATH', value: '/mnt/toolkit-data/news_archive.db' }
        // Set these manually after deployment (kept out of source control/IaC state):
        // ALERT_WEBHOOK_URL, SMTP_HOST, SMTP_PORT, SMTP_FROM_ADDR, SMTP_TO_ADDR,
        // SMTP_USER, SMTP_PASSWORD — see azure/README.md.
      ]
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
    httpsOnly: true
  }
}

output acrLoginServer string = acr.properties.loginServer
output analyticsApiUrl string = 'https://${analyticsApi.properties.defaultHostName}'
output newsApiUrl string = 'https://${newsApi.properties.defaultHostName}'
output redisHostName string = redis.properties.hostName
output storageAccountName string = storageAccount.name
output fileShareName string = fileShareName
