<#
=============================================================================
init-uat-db.ps1  -  initialise an EMPTY UAT Supabase database

What it does : 1) copies live-schema-local.sql to a temp file, removing the line
                  `CREATE SCHEMA public;` (Supabase already has that schema)
               2) loads it into UAT with psql (postgres:17 container, ON_ERROR_STOP=1).
                  Refuses if UAT already has tables in public, unless -Force
               3) alembic stamp 0048 -> alembic upgrade head -> alembic current
                  (expects 0053), using the release image
               4) verifies: lists public tables, sessions + qc_reviews exist, 0 rows
               5) explains what migrations 0041 + 0043 (skipped by the stamp) did to
                  Supabase Storage, and how to recreate that by hand
                  (-ApplyStorage runs the equivalent SQL for you)
               6) prints how the first users are created in this codebase
When to run  : once, on a brand-new UAT Supabase project, after push-image.ps1
               and before provision.ps1. Never point it at the live database.
AWS equivalent: restoring a schema snapshot into a fresh RDS instance, then
               marking the migration tool's version table as "already applied".

Usage        : pwsh deploy/init-uat-db.ps1 -Tag v1.1.0 [-Force] [-ApplyStorage] [-DryRun]
=============================================================================
#>
param(
    [Parameter(Mandatory)] [string]$Tag,
    [switch]$Force,
    [switch]$ApplyStorage,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\_common.ps1"

$fullImageName   = "${ImageRepository}:$Tag"
$schemaDumpPath  = Join-Path $RepositoryRoot 'live-schema-local.sql'
$stampRevision   = '0048'
$expectedHead    = '0053'
$tempFolder      = Join-Path ([System.IO.Path]::GetTempPath()) ("urolens-uat-init-" + [guid]::NewGuid().ToString('N'))
$tempSqlName     = 'schema.sql'
$tempSqlPath     = Join-Path $tempFolder $tempSqlName

# Bucket name comes from settings.env (same value the app uses), default "microscopy".
$settingsValues  = Read-EnvFile -Path $SettingsFilePath
$bucketName      = if ($settingsValues['SUPABASE_IMAGE_BUCKET']) { $settingsValues['SUPABASE_IMAGE_BUCKET'] } else { 'microscopy' }
if ($bucketName -notmatch '^[A-Za-z0-9_-]+$') { throw "Unsafe bucket name '$bucketName'." }

# Runs psql inside a postgres:17 container. The URL is passed by variable NAME so it never shows on screen.
function Invoke-UatPsql {
    # Runs psql against UAT. $Sql = inline SQL (-c); $File = file inside /work (-f). Returns output lines.
    param([string]$Sql, [string]$File, [switch]$Quiet)
    $arguments = @('run', '--rm', '-e', 'UAT_PG_URL')
    if ($File) { $arguments += @('-v', "${tempFolder}:/work:ro") }
    # "sh -c" lets psql read the URL from the environment variable inside the container.
    if ($File) {
        $inner = 'psql --no-psqlrc -v ON_ERROR_STOP=1 -d "$UAT_PG_URL" -f "/work/' + $File + '"'
    }
    else {
        $inner = 'psql --no-psqlrc -v ON_ERROR_STOP=1 -At -d "$UAT_PG_URL" -c "' + ($Sql -replace '"', '\"') + '"'
    }
    $arguments += @('postgres:17', 'sh', '-c', $inner)
    if ($Quiet) {
        return Invoke-External -Executable 'docker' -Arguments $arguments -DryRun:$DryRun -CaptureOutput
    }
    return Invoke-External -Executable 'docker' -Arguments $arguments -DryRun:$DryRun
}

Write-Step 1 'Check inputs and prepare a temp copy of the schema (CREATE SCHEMA public; removed)'
Assert-ValidTag -Tag $Tag
Write-Host "  Image          : $fullImageName"
Write-Host "  Schema source  : $schemaDumpPath"
if (-not (Test-Path $schemaDumpPath)) { throw "Schema file not found: $schemaDumpPath" }
Write-Host "  Storage bucket : $bucketName"

$throwawayJwtKey = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
$throwawayFernetKey = [Convert]::ToBase64String([byte[]](1..32 | ForEach-Object { Get-Random -Maximum 256 })).Replace('+','-').Replace('/','_')

try {
    $allLines     = Get-Content -LiteralPath $schemaDumpPath
    $keptLines    = $allLines | Where-Object { $_.Trim() -ne 'CREATE SCHEMA public;' }
    $removedCount = $allLines.Count - @($keptLines).Count
    $restrictKept = @($keptLines | Where-Object { $_ -match '^\\(un)?restrict ' }).Count
    Write-Host "  Lines removed  : $removedCount (expected 1); \restrict/\unrestrict lines kept: $restrictKept"
    if ($removedCount -ne 1) { throw "Expected to remove exactly 1 'CREATE SCHEMA public;' line, removed $removedCount." }
    if ($DryRun) {
        Write-Host "  (dry run: temp file NOT written; would be $tempSqlPath)" -ForegroundColor Yellow
    }
    else {
        New-Item -ItemType Directory -Force -Path $tempFolder | Out-Null
        [System.IO.File]::WriteAllLines($tempSqlPath, [string[]]$keptLines, (New-Object System.Text.UTF8Encoding($false)))
    }

    Write-Step 2 'Ask for the UAT database URL (hidden) - NOT the live one'
    if ($DryRun) {
        Write-Host '  (dry run: would prompt; using a placeholder)' -ForegroundColor Yellow
        $uatDatabaseUrl = 'postgresql://<user>:<hidden>@<uat-host>:5432/postgres'
    }
    else {
        $uatDatabaseUrl = Read-SecretText -PromptText 'UAT DATABASE_URL (Supabase session pooler, hidden)'
        if (-not $uatDatabaseUrl) { throw 'No URL entered.' }
    }
    $psqlUrl = $uatDatabaseUrl -replace '^postgresql\+[a-z0-9_]+://', 'postgresql://'

    # Env vars are read by NAME by docker. The last three are throwaway: the app
    # settings refuse to load without them, alembic never uses them.
    $env:UAT_PG_URL           = $psqlUrl
    $env:DATABASE_URL         = $uatDatabaseUrl
    $env:JWT_SIGNING_KEY      = $throwawayJwtKey
    $env:SUPABASE_URL         = 'https://migration-placeholder.invalid'
    $env:SUPABASE_SERVICE_KEY = 'migration-placeholder-not-used'
    $env:ENCRYPTION_KEY       = $throwawayFernetKey
    $env:DEDUP_HASH_KEY       = $throwawayJwtKey

    Write-Step 3 'Safety check: UAT public schema must have no tables (other than alembic_version)'
    $countSql = "select count(*) from information_schema.tables where table_schema='public' and table_type='BASE TABLE' and table_name <> 'alembic_version'"
    $countOutput = Invoke-UatPsql -Sql $countSql -Quiet
    if ($DryRun) {
        Write-Host '  (dry run: would refuse here if the count is above 0 and -Force was not given)' -ForegroundColor Yellow
    }
    else {
        $existingTables = [int](($countOutput | Where-Object { $_ -match '^\d+$' } | Select-Object -First 1))
        Write-Host "  Existing tables in public: $existingTables"
        if ($existingTables -gt 0 -and -not $Force) {
            throw "UAT already has $existingTables table(s) in public. Refusing. Use -Force only if you are sure (the load will probably fail on 'already exists')."
        }
    }

    Write-Step 4 'Load the schema into UAT (psql, ON_ERROR_STOP=1)'
    Invoke-UatPsql -File $tempSqlName

    Write-Step 5 "alembic stamp $stampRevision (marks 0001-$stampRevision as already applied, runs none of them)"
    $alembicBase = @('run', '--rm', '-e', 'DATABASE_URL', '-e', 'JWT_SIGNING_KEY', '-e', 'SUPABASE_URL', '-e', 'SUPABASE_SERVICE_KEY', '-e', 'ENCRYPTION_KEY', '-e', 'DEDUP_HASH_KEY', $fullImageName, 'alembic')
    Invoke-External -Executable 'docker' -Arguments ($alembicBase + @('stamp', $stampRevision)) -DryRun:$DryRun

    Write-Step 6 'alembic upgrade head (applies 0049 onward)'
    Invoke-External -Executable 'docker' -Arguments ($alembicBase + @('upgrade', 'head')) -DryRun:$DryRun

    Write-Step 7 "alembic current (expect $expectedHead)"
    $currentOutput = Invoke-External -Executable 'docker' -Arguments ($alembicBase + @('current')) -DryRun:$DryRun -CaptureOutput
    if (-not $DryRun) {
        $currentOutput | ForEach-Object { Write-Host "  $_" }
        if (-not ($currentOutput -match $expectedHead)) { throw "alembic current did not report $expectedHead." }
        Write-Host "  Revision is $expectedHead" -ForegroundColor Green
    }

    Write-Step 8 'Verify: public tables, sessions + qc_reviews present, every table empty'
    $rowCountSql = "select table_name || '|' || (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from public.%I', table_name), false, true, '')))[1]::text from information_schema.tables where table_schema='public' and table_type='BASE TABLE' order by table_name"
    $tableOutput = Invoke-UatPsql -Sql $rowCountSql -Quiet
    if ($DryRun) {
        Write-Host '  (dry run: would list tables with row counts and fail if sessions/qc_reviews are missing or any table except alembic_version has rows)' -ForegroundColor Yellow
    }
    else {
        $tableRows = @($tableOutput | Where-Object { $_ -match '^[a-z_0-9]+\|\d+$' } | ForEach-Object {
            $parts = $_ -split '\|'
            [pscustomobject]@{ Table = $parts[0]; Rows = [int]$parts[1] }
        })
        $tableRows | Format-Table -AutoSize | Out-String | Write-Host
        Write-Host "  Tables: $($tableRows.Count)"
        foreach ($mustExist in @('sessions', 'qc_reviews')) {
            if (-not ($tableRows.Table -contains $mustExist)) { throw "Missing expected table: $mustExist" }
        }
        $nonEmpty = @($tableRows | Where-Object { $_.Table -ne 'alembic_version' -and $_.Rows -ne 0 })
        if ($nonEmpty.Count -gt 0) { throw "Tables with rows: $($nonEmpty.Table -join ', ')" }
        Write-Host '  sessions and qc_reviews exist; all data tables have 0 rows.' -ForegroundColor Green
    }

    Write-Step 9 'Storage bucket (migrations 0041 + 0043 were skipped by the stamp)'
    Write-Host @"
  What they did to Supabase Storage (bucket = SUPABASE_IMAGE_BUCKET, default 'microscopy'):
    0041  UPDATE storage.buckets SET public = false,
          allowed_mime_types = ['image/jpeg','image/png']          (bucket PRIVATE)
    0043  UPDATE storage.buckets SET file_size_limit = 10485760     (10 MiB)
  Both are plain SQL on storage.buckets, and both are UPDATEs: they do nothing if the bucket does not exist.

  Manual dashboard steps (UAT project -> Storage):
    1. New bucket, name: $bucketName
    2. Public bucket: OFF (private)
    3. Restrict file size: ON, 10 MB   (10 MiB = 10485760 bytes)
    4. Restrict MIME types: ON, 'image/jpeg' and 'image/png'
    5. Save. (No storage policies are needed: the backend uses the service-role key.)
"@
    if ($ApplyStorage) {
        Write-Host '  -ApplyStorage given: creating/updating the bucket with SQL.' -ForegroundColor Cyan
        $storageSql = "insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types) values ('$bucketName', '$bucketName', false, 10485760, array['image/jpeg','image/png']) on conflict (id) do update set public = false, file_size_limit = 10485760, allowed_mime_types = array['image/jpeg','image/png']"
        Invoke-UatPsql -Sql $storageSql | Out-Null
        $bucketCheck = Invoke-UatPsql -Sql "select id, public, file_size_limit, allowed_mime_types from storage.buckets where id='$bucketName'" -Quiet
        if (-not $DryRun) { $bucketCheck | ForEach-Object { Write-Host "  bucket row: $_" } }
    }
    else {
        Write-Host '  (Re-run with -ApplyStorage to have this script do it. Not applied now.)' -ForegroundColor Yellow
    }

    Write-Step 10 'First users (instructions only - nothing is created here)'
    Write-Host @"
  There is NO endpoint and NO admin-creation script for staff users. The only mechanism is the
  seed script seed/seed_users.py, which inserts rows into public.users through the Supabase API
  (supabase client: needs SUPABASE_URL + SUPABASE_SERVICE_KEY of UAT in a local .env).
  It creates test accounts with the password 'password123' (administrator, supervisor, medtech,
  receptionist, physician, patient, locked_receptionist) - do NOT use those passwords in UAT as-is.

  Recommended for UAT: change the passwords in SEED_USERS (or delete the test entries you do not want),
  and run from the repo root with a .env pointing at UAT:
      python seed/seed_users.py
  Alternative, pure SQL: insert into public.users (username, hashed_password, role) values
  ('<name>', '<bcrypt hash>', 'ADMINISTRATOR'|'SUPERVISOR'|'MEDTECH'|'RECEPTIONIST');
  make the hash with:  python -c "from src.core.auth_service import hashPassword; print(hashPassword('<pw>'))"
  Roles are stored upper-case. Patient logins are also rows in public.users (role PATIENT).
"@
}
finally {
    Remove-Item Env:\UAT_PG_URL, Env:\DATABASE_URL, Env:\JWT_SIGNING_KEY, Env:\SUPABASE_URL, Env:\SUPABASE_SERVICE_KEY, Env:\ENCRYPTION_KEY, Env:\DEDUP_HASH_KEY -ErrorAction SilentlyContinue
    if (Test-Path $tempFolder) { Remove-Item -Recurse -Force $tempFolder -ErrorAction SilentlyContinue }
}

Write-Host ''
Write-Host 'Done.' -ForegroundColor Green
