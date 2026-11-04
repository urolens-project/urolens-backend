<#
=============================================================================
set-config.ps1  -  push app settings and secrets to the web app

What it does : reads non-secret settings from deploy/settings.env, collects the
               secrets (from deploy/secrets.local.env if present, otherwise
               hidden prompts), and applies all of them as app settings.
               Azure restarts the app when settings change.
               Secrets: DATABASE_URL, JWT_SIGNING_KEY, ENCRYPTION_KEY,
               DEDUP_HASH_KEY, SUPABASE_SERVICE_KEY.
When to run  : after provision.ps1, and again whenever a setting or secret
               changes. Settings you do not mention are left alone.
AWS equivalent: setting environment variables on the Elastic Beanstalk
               environment / ECS task definition, with secrets coming from
               Secrets Manager or SSM Parameter Store. (Here the secrets are
               stored as encrypted app settings instead; there is no Key Vault
               in this setup.)

Usage        : pwsh deploy/set-config.ps1 [-DryRun]
=============================================================================
#>
param(
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\_common.ps1"

# Keys that are allowed to be empty in settings.env.
$keysAllowedToBeEmpty = @('CORS_ALLOWED_ORIGINS')

Write-Step 1 'Read non-secret settings from deploy/settings.env'
if (-not (Test-Path $SettingsFilePath)) {
    throw "Missing $SettingsFilePath"
}
$plainSettings = Read-EnvFile -Path $SettingsFilePath
foreach ($settingName in $plainSettings.Keys | Sort-Object) {
    Write-Host "  $settingName = $($plainSettings[$settingName])"
}
foreach ($settingName in $plainSettings.Keys) {
    $isEmpty = [string]::IsNullOrWhiteSpace($plainSettings[$settingName])
    if ($isEmpty -and ($keysAllowedToBeEmpty -notcontains $settingName)) {
        if ($DryRun) {
            Write-Warning "settings.env: '$settingName' is empty. A real run would stop here until you fill it in."
        }
        else {
            throw "settings.env: '$settingName' is empty. Fill it in first."
        }
    }
}

Write-Step 2 'Collect secrets (secrets.local.env, else hidden prompts)'
$secretsFromFile = Read-EnvFile -Path $SecretsFilePath
$secretNames = @('DATABASE_URL', 'JWT_SIGNING_KEY', 'ENCRYPTION_KEY', 'DEDUP_HASH_KEY', 'SUPABASE_SERVICE_KEY')
$secretPrompts = @{
    'DATABASE_URL'         = 'DATABASE_URL (Supabase SESSION POOLER, port 5432)'
    'JWT_SIGNING_KEY'      = 'JWT_SIGNING_KEY (long random string)'
    'ENCRYPTION_KEY'       = 'ENCRYPTION_KEY - WARNING: must EXACTLY equal the LIVE value, or existing encrypted patient data becomes unreadable'
    'DEDUP_HASH_KEY'       = 'DEDUP_HASH_KEY - WARNING: must EXACTLY equal the LIVE value, or duplicate-patient detection breaks'
    'SUPABASE_SERVICE_KEY' = 'SUPABASE_SERVICE_KEY (service-role key)'
}
$secretValues = @{}
foreach ($secretName in $secretNames) {
    if ($DryRun) {
        Write-Host "  $secretName : (dry run: would load from file or prompt)" -ForegroundColor Yellow
        $secretValues[$secretName] = "<hidden-$secretName>"
    }
    else {
        $secretValues[$secretName] = Get-SecretValue -Name $secretName -PromptText $secretPrompts[$secretName] -FileValues $secretsFromFile
        if ([string]::IsNullOrWhiteSpace($secretValues[$secretName])) {
            throw "$secretName was empty."
        }
    }
}

Write-Step 3 'Build a temporary settings file (so special characters survive)'
# Passing values like postgresql://user:p&ss@host straight on the command line breaks on
# Windows (& and | are special). A JSON file avoids that. The file is deleted right after.
$allSettings = @()
foreach ($settingName in $plainSettings.Keys | Sort-Object) {
    $allSettings += [ordered]@{ name = $settingName; value = $plainSettings[$settingName]; slotSetting = $false }
}
foreach ($secretName in $secretNames) {
    $allSettings += [ordered]@{ name = $secretName; value = $secretValues[$secretName]; slotSetting = $false }
}
$temporarySettingsFile = Join-Path ([System.IO.Path]::GetTempPath()) ("urolens-settings-{0}.json" -f [guid]::NewGuid().ToString('N'))
Write-Host "  Settings to apply: $(($allSettings | ForEach-Object { $_.name }) -join ', ')"
Write-Host '  (secret values are never printed)'

try {
    if (-not $DryRun) {
        $allSettings | ConvertTo-Json -Depth 3 | Set-Content -Path $temporarySettingsFile -Encoding utf8
    }

    Write-Step 4 'Apply the settings to the web app'
    # Sets environment variables for the container; the app restarts to pick them up.
    # Values are stored encrypted at rest by Azure and shown to the app as env vars.
    # AWS: `aws elasticbeanstalk update-environment --option-settings` or an ECS task definition's environment/secrets.
    Invoke-External -Executable 'az' -DryRun:$DryRun -Arguments @(
        'webapp', 'config', 'appsettings', 'set',
        '--name', $WebAppName,
        '--resource-group', $ResourceGroupName,
        '--settings', "@$temporarySettingsFile",
        '--output', 'none'
    )
}
finally {
    # Always remove the file that briefly held the secrets.
    Remove-Item $temporarySettingsFile -ErrorAction SilentlyContinue
}

Write-Host ''
Write-Host 'Done. Next: pwsh deploy/smoke.ps1  (allow a few minutes for the first start).' -ForegroundColor Green
