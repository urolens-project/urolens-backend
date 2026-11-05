<#
=============================================================================
create-user.ps1  -  create ONE staff user (any role) in a database

What it does : 1) asks for role, username, password (hidden, twice, min 12 chars)
                  and the target DATABASE_URL (hidden)
               2) runs scripts/create_user.py inside the release image
                  (docker run; secrets are passed to the container by variable
                  NAME, so they never appear on screen or in the command line)
               3) the script hashes the password (bcrypt, via the app's own
                  hashPassword), refuses a duplicate username, inserts the row,
                  and prints the new user id + role only
When to run  : after init-uat-db.ps1 on a fresh database, or whenever a staff
               account is needed. Point it at the database you mean to change.
AWS equivalent: a one-off ECS task / Lambda that seeds the first Cognito-less
               admin row.

Notes        : the users table has no encrypted columns, so no real
               ENCRYPTION_KEY / DEDUP_HASH_KEY is needed - throwaway values are
               passed only because the app settings refuse to load without them.
               The script file is mounted read-only into the container (the
               image does not contain scripts/).
               Local database on the host? use host.docker.internal in the URL.

Usage        : pwsh deploy/create-user.ps1 -Tag v1.1.0 [-DryRun]
=============================================================================
#>
param(
    [Parameter(Mandatory)] [string]$Tag,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\_common.ps1"

$fullImageName = "${ImageRepository}:$Tag"
$scriptsFolder = Join-Path $RepositoryRoot 'scripts'
$scriptFile    = Join-Path $scriptsFolder 'create_user.py'
$allowedRoles  = @('ADMINISTRATOR', 'SUPERVISOR', 'MEDTECH', 'RECEPTIONIST', 'PHYSICIAN')

Write-Step 1 'Check inputs'
Assert-ValidTag -Tag $Tag
if (-not (Test-Path $scriptFile)) { throw "Script not found: $scriptFile" }
Write-Host "  Image  : $fullImageName"
Write-Host "  Script : $scriptFile (mounted read-only)"

# Throwaway values: the app settings refuse to load without these; the users table uses none of them.
$throwawayJwtKey    = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
$throwawayFernetKey = [Convert]::ToBase64String([byte[]](1..32 | ForEach-Object { Get-Random -Maximum 256 })).Replace('+','-').Replace('/','_')

try {
    Write-Step 2 'Ask for the new user (password and database URL are hidden)'
    if ($DryRun) {
        Write-Host '  (dry run: would prompt; using placeholders)' -ForegroundColor Yellow
        $role = 'MEDTECH'; $username = '<username>'; $password = '<hidden>'
        $databaseUrl = 'postgresql://<user>:<hidden>@<host>:5432/postgres'
    }
    else {
        $role = (Read-Host -Prompt ("Role (" + ($allowedRoles -join '|') + ")")).Trim().ToUpper()
        if ($allowedRoles -notcontains $role) { throw "Role must be one of: $($allowedRoles -join ', ')." }
        $username = Read-Host -Prompt 'Username'
        if (-not $username -or $username -ne $username.Trim() -or $username.Length -gt 80) {
            throw 'Username must be 1-80 characters with no leading/trailing spaces.'
        }
        $password = Read-SecretText -PromptText 'Password (hidden, min 12 chars)'
        $passwordAgain = Read-SecretText -PromptText 'Repeat password'
        if ($password -ne $passwordAgain) { throw 'Passwords do not match.' }
        if ($password.Length -lt 12) { throw 'Password must be at least 12 characters.' }
        $databaseUrl = Read-SecretText -PromptText 'DATABASE_URL (hidden)'
        if (-not $databaseUrl) { throw 'No URL entered.' }
    }

    # Read by NAME by docker (-e NAME with no value), so nothing secret is on the command line.
    $env:CREATE_USER_ROLE     = $role
    $env:CREATE_USER_NAME     = $username
    $env:CREATE_USER_PASSWORD = $password
    $env:DATABASE_URL         = $databaseUrl
    $env:JWT_SIGNING_KEY      = $throwawayJwtKey
    $env:SUPABASE_URL         = 'https://migration-placeholder.invalid'
    $env:SUPABASE_SERVICE_KEY = 'migration-placeholder-not-used'
    $env:ENCRYPTION_KEY       = $throwawayFernetKey
    $env:DEDUP_HASH_KEY       = $throwawayJwtKey

    Write-Step 3 'Create the user inside the image'
    $arguments = @('run', '--rm',
        '-e', 'CREATE_USER_ROLE', '-e', 'CREATE_USER_NAME', '-e', 'CREATE_USER_PASSWORD', '-e', 'DATABASE_URL',
        '-e', 'JWT_SIGNING_KEY', '-e', 'SUPABASE_URL', '-e', 'SUPABASE_SERVICE_KEY', '-e', 'ENCRYPTION_KEY', '-e', 'DEDUP_HASH_KEY',
        '-e', 'PYTHONPATH=/app',
        '-v', "${scriptsFolder}:/tools:ro",
        $fullImageName, 'python', '/tools/create_user.py')
    Invoke-External -Executable 'docker' -Arguments $arguments -DryRun:$DryRun
}
finally {
    Remove-Item Env:\CREATE_USER_ROLE, Env:\CREATE_USER_NAME, Env:\CREATE_USER_PASSWORD, Env:\DATABASE_URL, Env:\JWT_SIGNING_KEY, Env:\SUPABASE_URL, Env:\SUPABASE_SERVICE_KEY, Env:\ENCRYPTION_KEY, Env:\DEDUP_HASH_KEY -ErrorAction SilentlyContinue
}

Write-Host ''
Write-Host 'Done.' -ForegroundColor Green
