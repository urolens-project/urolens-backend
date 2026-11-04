<#
=============================================================================
deploy-tag.ps1  -  point the web app at a different image tag and restart

What it does : switches the web app's container image to
               ghcr.io/urolens-project/urolens-backend:<Tag>, then restarts it.
               Refuses "latest". Registry credentials set by provision.ps1 are kept.
When to run  : each release, after push-image.ps1 (and after
               backup-and-migrate-live.ps1 if the release has migrations).
               To roll back, run it again with the previous tag.
AWS equivalent: registering a new ECS task definition revision with the new
               image tag and updating the service (or `eb deploy` of a new
               version), forcing a new deployment.

Usage        : pwsh deploy/deploy-tag.ps1 -Tag v1.1.0 [-DryRun]
=============================================================================
#>
param(
    [Parameter(Mandatory)] [string]$Tag,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\_common.ps1"

$fullImageName = "${ImageRepository}:$Tag"

Write-Step 1 'Check the tag is allowed'
Assert-ValidTag -Tag $Tag
Write-Host "  New image: $fullImageName"

Write-Step 2 'Switch the web app to the new image'
# Changes which container image the app runs. AWS: updating the task definition's image.
Invoke-External -Executable 'az' -DryRun:$DryRun -Arguments @(
    'webapp', 'config', 'container', 'set',
    '--name', $WebAppName,
    '--resource-group', $ResourceGroupName,
    '--container-image-name', $fullImageName,
    '--output', 'none'
)

Write-Step 3 'Restart the web app'
# Stops and starts the container so it pulls the image fresh. AWS: forcing a new ECS deployment.
Invoke-External -Executable 'az' -Arguments @('webapp', 'restart', '--name', $WebAppName, '--resource-group', $ResourceGroupName) -DryRun:$DryRun

Write-Host ''
Write-Host "Done. $WebAppName is restarting on $fullImageName." -ForegroundColor Green
Write-Host 'Next: pwsh deploy/smoke.ps1  (the first start can take several minutes while the AI model loads).'
