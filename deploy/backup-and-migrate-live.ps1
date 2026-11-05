<#
=============================================================================
backup-and-migrate-live.ps1  -  back up the LIVE database, then run Alembic migrations

What it does : 1) asks for the live DATABASE_URL (hidden)
               2) dumps the database to $HOME\urolens-backups\<timestamp>.sql
                  using pg_dump from the postgres:17 image (no local install)
               3) checks the dump file exists and is not empty
               4) makes you type  MIGRATE LIVE  to continue
               5) runs `alembic upgrade head` using the release image, then
                  `alembic current` so you can see the resulting revision
               Stops at the first error.
When to run  : once per release that contains database migrations, BEFORE
               deploy-tag.ps1. The database stays on Supabase, so this is
               independent of Azure. Needs Docker and pull access to the image.
AWS equivalent: taking an RDS snapshot / pg_dump before a schema change, then
               running the migration as a one-off ECS task (`aws ecs run-task`).

Usage        : pwsh deploy/backup-and-migrate-live.ps1 -Tag v1.1.0 [-DryRun]
=============================================================================
#>
param(
    [Parameter(Mandatory)] [string]$Tag,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\_common.ps1"

$fullImageName  = "${ImageRepository}:$Tag"
$backupFolder   = Join-Path $HOME 'urolens-backups'
$timestamp      = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupFileName = "$timestamp.sql"
$backupFilePath = Join-Path $backupFolder $backupFileName

Write-Step 1 'Check the tag and create the backup folder'
Assert-ValidTag -Tag $Tag
Write-Host "  Image to migrate with : $fullImageName"
Write-Host "  Backup file will be   : $backupFilePath"
if (-not $DryRun) {
    New-Item -ItemType Directory -Force -Path $backupFolder | Out-Null
}

Write-Step 2 'Ask for the LIVE database URL (hidden)'
if ($DryRun) {
    Write-Host '  (dry run: would prompt; using a placeholder)' -ForegroundColor Yellow
    $liveDatabaseUrl = 'postgresql://<user>:<hidden>@<host>:5432/postgres'
}
else {
    $liveDatabaseUrl = Read-SecretText -PromptText 'LIVE DATABASE_URL (Supabase session pooler, hidden)'
}
# pg_dump only understands postgresql://. The app may use postgresql+psycopg2:// -
# strip the "+driver" part for the dump only.
$pgDumpUrl = $liveDatabaseUrl -replace '^postgresql\+[a-z0-9_]+://', 'postgresql://'

# Alembic loads the app settings, which refuse to start without these three
# values. Migrations never use them, so throwaway values are enough.
$throwawayJwtKey = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
$throwawayFernetKey = [Convert]::ToBase64String([byte[]](1..32 | ForEach-Object { Get-Random -Maximum 256 })).Replace('+','-').Replace('/','_')

try {
    # Docker reads these from the environment by NAME ("-e NAME" with no value),
    # so the secret never appears in the command line or on screen.
    $env:LIVE_DATABASE_URL    = $pgDumpUrl
    $env:DATABASE_URL         = $liveDatabaseUrl
    $env:JWT_SIGNING_KEY      = $throwawayJwtKey
    $env:SUPABASE_URL         = 'https://migration-placeholder.invalid'
    $env:SUPABASE_SERVICE_KEY = 'migration-placeholder-not-used'
    $env:ENCRYPTION_KEY       = $throwawayFernetKey
    $env:DEDUP_HASH_KEY       = $throwawayJwtKey

    Write-Step 3 'Dump the live database with pg_dump (postgres:17 container)'
    # Runs the pg_dump tool from the official postgres:17 image, writes a plain-SQL
    # backup into the mounted backup folder, then the container deletes itself.
    # AWS: like `aws rds create-db-snapshot`, except this is a portable SQL file you keep locally.
    $dumpCommandInsideContainer = 'pg_dump "$LIVE_DATABASE_URL" --format=plain --schema=public --file="/backup/' + $backupFileName + '"'
    $dumpArguments = @(
        'run', '--rm',
        '-e', 'LIVE_DATABASE_URL',
        '-v', "${backupFolder}:/backup",
        'postgres:17',
        'sh', '-c', $dumpCommandInsideContainer
    )
    Invoke-External -Executable 'docker' -Arguments $dumpArguments -DryRun:$DryRun

    Write-Step 4 'Verify the backup file exists and is not empty'
    if ($DryRun) {
        Write-Host '  (dry run: would check the file exists, size > 0, and ends with the pg_dump completion line)' -ForegroundColor Yellow
    }
    else {
        if (-not (Test-Path $backupFilePath)) {
            throw "Backup file was not created: $backupFilePath"
        }
        $backupSizeBytes = (Get-Item $backupFilePath).Length
        if ($backupSizeBytes -le 0) {
            throw "Backup file is empty: $backupFilePath"
        }
        $lastLines = Get-Content $backupFilePath -Tail 5
        if (-not ($lastLines -match 'PostgreSQL database dump complete')) {
            throw 'Backup file does not end with the "dump complete" marker. It may be truncated. Stopping.'
        }
        Write-Host ("  Backup OK: {0:N0} bytes" -f $backupSizeBytes) -ForegroundColor Green
    }

    Write-Step 5 'Final safety gate'
    if ($DryRun) {
        Write-Host '  (dry run: would require you to type MIGRATE LIVE)' -ForegroundColor Yellow
    }
    else {
        Write-Host 'This will change the LIVE database schema.' -ForegroundColor Yellow
        $typedConfirmation = Read-Host -Prompt 'Type MIGRATE LIVE to continue'
        if ($typedConfirmation -cne 'MIGRATE LIVE') {
            throw 'Confirmation text did not match. Nothing was migrated.'
        }
    }

    Write-Step 6 'Run: alembic upgrade head (inside the release image)'
    # Starts a throwaway container from the release image and applies the migrations
    # to the live database. AWS: a one-off ECS/Fargate task running the migration command.
    $upgradeArguments = @(
        'run', '--rm',
        '-e', 'DATABASE_URL', '-e', 'JWT_SIGNING_KEY', '-e', 'SUPABASE_URL', '-e', 'SUPABASE_SERVICE_KEY', '-e', 'ENCRYPTION_KEY', '-e', 'DEDUP_HASH_KEY',
        $fullImageName,
        'alembic', 'upgrade', 'head'
    )
    Invoke-External -Executable 'docker' -Arguments $upgradeArguments -DryRun:$DryRun

    Write-Step 7 'Show the current revision: alembic current'
    # Prints which migration the live database is now on. It should be the "head" revision.
    $currentArguments = @(
        'run', '--rm',
        '-e', 'DATABASE_URL', '-e', 'JWT_SIGNING_KEY', '-e', 'SUPABASE_URL', '-e', 'SUPABASE_SERVICE_KEY', '-e', 'ENCRYPTION_KEY', '-e', 'DEDUP_HASH_KEY',
        $fullImageName,
        'alembic', 'current'
    )
    Invoke-External -Executable 'docker' -Arguments $currentArguments -DryRun:$DryRun
}
finally {
    # Always wipe the secrets from this process, even after an error.
    Remove-Item Env:\LIVE_DATABASE_URL, Env:\DATABASE_URL, Env:\JWT_SIGNING_KEY, Env:\SUPABASE_URL, Env:\SUPABASE_SERVICE_KEY, Env:\ENCRYPTION_KEY, Env:\DEDUP_HASH_KEY -ErrorAction SilentlyContinue
}

Write-Host ''
Write-Host "Done. Backup kept at $backupFilePath" -ForegroundColor Green
