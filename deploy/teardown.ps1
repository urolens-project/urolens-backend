<#
=============================================================================
teardown.ps1  -  delete the web app and the App Service plan

What it does : deletes web app urolens-api, then the plan. You must type the
               app name to confirm. The resource group is left in place (empty)
               and the Supabase database is NOT touched.
When to run  : when you want to stop paying for B1 (about the cost of one small
               VM, billed 24/7 while the plan exists, even if the app is stopped).
AWS equivalent: `aws cloudformation delete-stack` / terminating the Elastic
               Beanstalk environment.

Usage        : pwsh deploy/teardown.ps1 [-DryRun]
=============================================================================
#>
param(
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\_common.ps1"

Write-Step 1 'Confirm what will be deleted'
Write-Host "  Web app : $WebAppName" -ForegroundColor Yellow
Write-Host "  Plan    : $AppServicePlanName" -ForegroundColor Yellow
Write-Host "  In group: $ResourceGroupName (the group itself is kept)"
if ($DryRun) {
    Write-Host "  (dry run: would require you to type '$WebAppName')" -ForegroundColor Yellow
}
else {
    $typedName = Read-Host -Prompt "Type the app name ($WebAppName) to confirm deletion"
    if ($typedName -cne $WebAppName) {
        throw 'Name did not match. Nothing was deleted.'
    }
}

Write-Step 2 'Delete the web app'
# Removes the app and its URL. AWS: terminating the Elastic Beanstalk environment / deleting the App Runner service.
Invoke-External -Executable 'az' -Arguments @('webapp', 'delete', '--name', $WebAppName, '--resource-group', $ResourceGroupName) -DryRun:$DryRun

Write-Step 3 'Delete the App Service plan'
# Removes the rented server capacity, which stops the billing. --yes skips az's own prompt (we already confirmed).
# AWS: terminating the EC2 instances / Auto Scaling group.
Invoke-External -Executable 'az' -Arguments @('appservice', 'plan', 'delete', '--name', $AppServicePlanName, '--resource-group', $ResourceGroupName, '--yes') -DryRun:$DryRun

Write-Host ''
Write-Host 'Done. The empty resource group remains; delete it in the portal if you want it gone too.' -ForegroundColor Green
