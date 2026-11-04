<#
=============================================================================
provision.ps1  -  create the Azure resources (safe to re-run)

What it does : creates, only if missing:
                 1. resource group  rg-urolens (southeastasia)
                 2. Linux App Service plan, SKU B1, 1 instance
                 3. web app urolens-api running your GHCR image
               then applies the hardening settings: Always On, HTTPS only,
               minimum TLS 1.2, FTPS disabled, health check path "/",
               container logging on, 1 instance.
               Does NOT set app settings or secrets (that is set-config.ps1).
When to run  : once, first. Re-run any time to repair drift; it will not
               duplicate anything. Needs `az login` already done and an
               image tag that is already pushed (push-image.ps1).
AWS equivalent: creating a CloudFormation stack for: a tag group (resource
               group), an EC2-backed capacity pool (App Service plan, like the
               instances behind Elastic Beanstalk), and the app itself
               (Elastic Beanstalk environment / App Runner service) pulling
               from a private registry (like ECR credentials).

Usage        : pwsh deploy/provision.ps1 -Tag v1.1.0 [-DryRun]
  Prompts (hidden) for the GHCR username and a READ-ONLY PAT (read:packages).
=============================================================================
#>
param(
    [Parameter(Mandatory)] [string]$Tag,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\_common.ps1"

$fullImageName = "${ImageRepository}:$Tag"

Write-Step 1 'Check the tag and note the az version'
Assert-ValidTag -Tag $Tag
Write-Host "  Image: $fullImageName"
# Shows which Azure CLI you have, so flag problems can be traced to a version. AWS: `aws --version`.
Invoke-External -Executable 'az' -Arguments @('version', '--output', 'tsv') -DryRun:$DryRun

Write-Step 2 'Ask for GHCR credentials (read-only PAT, hidden)'
if ($DryRun) {
    Write-Host '  (dry run: would prompt for username and PAT with read:packages)' -ForegroundColor Yellow
    $registryUser  = '<github-user>'
    $registryToken = '<hidden>'
}
else {
    $registryUser  = Read-Host -Prompt 'GitHub username that owns the read-only PAT'
    $registryToken = Read-SecretText -PromptText 'GitHub PAT with read:packages ONLY (hidden)'
}

Write-Step 3 "Resource group '$ResourceGroupName'"
# A resource group is a folder that holds related resources and can be deleted as a unit.
# `group create` is already idempotent (re-running just returns the existing group).
# AWS: closest to a CloudFormation stack plus a tag; no direct single resource.
Invoke-External -Executable 'az' -Arguments @('group', 'create', '--name', $ResourceGroupName, '--location', $AzureLocation, '--output', 'none') -DryRun:$DryRun

Write-Step 4 "App Service plan '$AppServicePlanName' (Linux, B1, 1 instance)"
# The plan is the rented server capacity (CPU/RAM) your web app runs on. You pay for the
# plan 24/7 whether or not the app is busy. AWS: the EC2 instance(s) behind Elastic Beanstalk.
$planAlreadyExists = Test-ExternalSucceeds -Executable 'az' -Arguments @('appservice', 'plan', 'show', '--name', $AppServicePlanName, '--resource-group', $ResourceGroupName) -DryRun:$DryRun
if ($planAlreadyExists) {
    Write-Host '  Plan already exists. Making sure it is still 1 worker.'
    # Sets the number of instances on an existing plan. AWS: Auto Scaling group desired capacity = 1.
    Invoke-External -Executable 'az' -Arguments @('appservice', 'plan', 'update', '--name', $AppServicePlanName, '--resource-group', $ResourceGroupName, '--number-of-workers', '1', '--output', 'none') -DryRun:$DryRun
}
else {
    Write-Host '  Plan not found. Creating it.'
    # Creates the plan: --is-linux = Linux hosts (needed for containers), --sku B1 = Basic tier size 1.
    # AWS: choosing the EC2 instance type + count for Elastic Beanstalk.
    Invoke-External -Executable 'az' -Arguments @('appservice', 'plan', 'create', '--name', $AppServicePlanName, '--resource-group', $ResourceGroupName, '--location', $AzureLocation, '--is-linux', '--sku', 'B1', '--number-of-workers', '1', '--output', 'none') -DryRun:$DryRun
}

Write-Step 5 "Web app '$WebAppName' from GHCR"
# The web app is your running container plus its URL (urolens-api.azurewebsites.net).
# The name must be unique across ALL of Azure. AWS: an Elastic Beanstalk environment / App Runner service.
$appAlreadyExists = Test-ExternalSucceeds -Executable 'az' -Arguments @('webapp', 'show', '--name', $WebAppName, '--resource-group', $ResourceGroupName) -DryRun:$DryRun
if (-not $appAlreadyExists) {
    Write-Host '  Web app not found. Creating it.'
    # Creates the app on the plan, pointing at the GHCR image with the read-only PAT so Azure can pull it.
    # AWS: creating the service and giving it ECR pull permission (here it's a username + token instead of an IAM role).
    Invoke-External -Executable 'az' -MaskValues @($registryToken) -DryRun:$DryRun -Arguments @(
        'webapp', 'create',
        '--name', $WebAppName,
        '--resource-group', $ResourceGroupName,
        '--plan', $AppServicePlanName,
        '--container-image-name', ($fullImageName -replace '^ghcr\.io/', ''),   # `webapp create` wants NO registry host when --container-registry-url is given
        '--container-registry-url', 'https://ghcr.io',
        '--container-registry-user', $registryUser,
        '--container-registry-password', $registryToken,
        '--output', 'none'
    )
}
else {
    Write-Host '  Web app already exists. Refreshing its registry credentials and image (handles PAT rotation).'
    # Re-applies image + registry login on an existing app. AWS: updating the task definition / image source.
    Invoke-External -Executable 'az' -MaskValues @($registryToken) -DryRun:$DryRun -Arguments @(
        'webapp', 'config', 'container', 'set',
        '--name', $WebAppName,
        '--resource-group', $ResourceGroupName,
        '--container-image-name', $fullImageName,
        '--container-registry-url', 'https://ghcr.io',
        '--container-registry-user', $registryUser,
        '--container-registry-password', $registryToken,
        '--output', 'none'
    )
}

Write-Step 6 'Always On, TLS 1.2, FTPS disabled, 1 instance, health check "/"'
# One `config set` call that applies the main site settings:
#   --always-on true            keep the container loaded (otherwise Azure unloads it after idle time). AWS: no equivalent needed; EB is always on.
#   --min-tls-version 1.2       reject older TLS. AWS: a TLS security policy on the load balancer listener.
#   --ftps-state Disabled       turn off FTP deploys (we deploy by image only). AWS: no equivalent; EB has no FTP.
#   --number-of-workers 1       one instance of the app. AWS: Auto Scaling min = max = 1.
#   --generic-configurations    JSON file with healthCheckPath "/". AWS: the target-group health check path.
# The JSON goes through a temp file because quotes inside JSON get mangled when passed straight to az on Windows.
$healthCheckFile = Join-Path ([System.IO.Path]::GetTempPath()) 'urolens-healthcheck.json'
try {
    if (-not $DryRun) {
        Set-Content -Path $healthCheckFile -Value '{"healthCheckPath": "/"}' -Encoding utf8
    }
    Invoke-External -Executable 'az' -DryRun:$DryRun -Arguments @(
        'webapp', 'config', 'set',
        '--name', $WebAppName,
        '--resource-group', $ResourceGroupName,
        '--always-on', 'true',
        '--min-tls-version', '1.2',
        '--ftps-state', 'Disabled',
        '--number-of-workers', '1',
        '--generic-configurations', "@$healthCheckFile",
        '--output', 'none'
    )
}
finally {
    Remove-Item $healthCheckFile -ErrorAction SilentlyContinue
}

Write-Step 7 'HTTPS only'
# Redirects http:// to https://. AWS: an ALB listener rule redirecting port 80 to 443.
Invoke-External -Executable 'az' -Arguments @('webapp', 'update', '--name', $WebAppName, '--resource-group', $ResourceGroupName, '--https-only', 'true', '--output', 'none') -DryRun:$DryRun

Write-Step 8 'Container logging to the app filesystem'
# Sends the container's stdout/stderr to Azure so `az webapp log tail` can show it.
# AWS: the awslogs log driver sending to CloudWatch Logs (here it is simpler: a log file on the app).
Invoke-External -Executable 'az' -Arguments @('webapp', 'log', 'config', '--name', $WebAppName, '--resource-group', $ResourceGroupName, '--docker-container-logging', 'filesystem', '--output', 'none') -DryRun:$DryRun

Write-Host ''
Write-Host 'Done. Next: pwsh deploy/set-config.ps1  (the app will not be healthy until settings and secrets exist).' -ForegroundColor Green
