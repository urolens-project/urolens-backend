<#
=============================================================================
smoke.ps1  -  quick pass/fail check that the deployed API is up

What it does : sends GET /  and  GET /health/ready  to the API and reports
               PASS or FAIL for each. /health/ready is 200 only when the
               database answers AND the AI model is loaded. Retries for a
               while because the container takes time to start.
               Exit code 0 = all passed, 1 = something failed.
When to run  : after deploy-tag.ps1 or set-config.ps1, and after the custom
               domain is set up (use -Domain api.urolens.tech).
AWS equivalent: curl against the load balancer URL / a Route 53 health check
               or a CodeDeploy validation hook.

Usage        : pwsh deploy/smoke.ps1 [-Domain api.urolens.tech] [-Attempts 10] [-DelaySeconds 30] [-DryRun]
  Without -Domain it asks Azure for the app's default *.azurewebsites.net name.
=============================================================================
#>
param(
    [string]$Domain,
    [int]$Attempts = 10,
    [int]$DelaySeconds = 30,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\_common.ps1"

Write-Step 1 'Work out which host to test'
if ($Domain) {
    $hostName = $Domain
    Write-Host "  Using custom domain: $hostName"
}
else {
    # Asks Azure for the app's public hostname (urolens-api.azurewebsites.net or similar).
    # AWS: reading the load balancer DNS name from `describe-load-balancers`.
    $hostLines = Invoke-External -Executable 'az' -CaptureOutput -DryRun:$DryRun -Arguments @(
        'webapp', 'show', '--name', $WebAppName, '--resource-group', $ResourceGroupName,
        '--query', 'defaultHostName', '--output', 'tsv'
    )
    $hostName = if ($DryRun) { "$WebAppName.azurewebsites.net" } else { "$hostLines".Trim() }
    Write-Host "  Using default host: $hostName"
}
$baseUrl = "https://$hostName"

# Checks one URL, retrying until it returns HTTP 200 or attempts run out.
function Test-Endpoint {
    param([string]$Path)

    $url = "$baseUrl$Path"
    Write-Host "  > GET $url" -ForegroundColor DarkGray
    if ($DryRun) {
        Write-Host '    (dry run: not sent)' -ForegroundColor Yellow
        return $true
    }
    for ($attemptNumber = 1; $attemptNumber -le $Attempts; $attemptNumber++) {
        try {
            $response = Invoke-WebRequest -Uri $url -Method Get -TimeoutSec 30 -SkipHttpErrorCheck
            if ($response.StatusCode -eq 200) {
                Write-Host "    PASS  $Path  HTTP 200  $($response.Content)" -ForegroundColor Green
                return $true
            }
            Write-Host "    attempt ${attemptNumber}/${Attempts}: HTTP $($response.StatusCode) $($response.Content)"
        }
        catch {
            Write-Host "    attempt ${attemptNumber}/${Attempts}: $($_.Exception.Message)"
        }
        if ($attemptNumber -lt $Attempts) {
            Start-Sleep -Seconds $DelaySeconds
        }
    }
    Write-Host "    FAIL  $Path" -ForegroundColor Red
    return $false
}

Write-Step 2 'GET /  (is the web server answering?)'
$rootPassed = Test-Endpoint -Path '/'

Write-Step 3 'GET /health/ready  (database reachable + AI model loaded?)'
$readyPassed = Test-Endpoint -Path '/health/ready'

Write-Host ''
if ($rootPassed -and $readyPassed) {
    Write-Host 'SMOKE TEST: PASS' -ForegroundColor Green
    exit 0
}
Write-Host 'SMOKE TEST: FAIL' -ForegroundColor Red
Write-Host "Look at the logs:  az webapp log tail --name $WebAppName --resource-group $ResourceGroupName"
exit 1
