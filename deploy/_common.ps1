# =============================================================================
# _common.ps1  -  shared helpers for the deploy scripts (dot-sourced, not run directly)
#
# What it does : defines the project constants (names, image) and a few small
#                helper functions so every script prints steps and runs commands
#                the same way.
# When to run  : never directly. Each script loads it with:  . "$PSScriptRoot\_common.ps1"
# AWS equivalent: none - think of it as a tiny shared "utils" module for your
#                CloudFormation/CDK deploy scripts.
# =============================================================================

# ---- Project constants (change here, every script follows) ------------------
$ResourceGroupName = 'rg-urolens'          # AWS: like a CloudFormation stack / tag group that holds related resources
$AzureLocation     = 'southeastasia'       # AWS: Region (ap-southeast-1 Singapore is the closest match)
$AppServicePlanName = 'plan-urolens-b1'    # AWS: the EC2 capacity behind Elastic Beanstalk / App Runner
$WebAppName        = 'urolens-api'         # AWS: the Elastic Beanstalk environment / App Runner service
$ImageRepository   = 'ghcr.io/urolens-project/urolens-backend'   # AWS: like an ECR repo URI (but on GitHub)
$SecretsFilePath   = Join-Path $PSScriptRoot 'secrets.local.env'  # gitignored
$SettingsFilePath  = Join-Path $PSScriptRoot 'settings.env'       # non-secret, safe to commit
$RepositoryRoot    = Split-Path $PSScriptRoot -Parent

# ---- Prints "Step N: ..." so you can follow the run -------------------------
function Write-Step {
    param(
        [int]$StepNumber,
        [string]$Message
    )
    Write-Host ''
    Write-Host "Step ${StepNumber}: $Message" -ForegroundColor Cyan
}

# ---- Stops the tag "latest" (and bad characters) from being used ------------
function Assert-ValidTag {
    param([string]$Tag)

    if ($Tag -ieq 'latest') {
        throw "Refusing tag 'latest'. Use an explicit, immutable tag such as v1.1.0 so you always know what is running."
    }
    if ($Tag -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$') {
        throw "Tag '$Tag' is not a valid Docker tag (letters, digits, '_', '.', '-')."
    }
}

# ---- Runs one external command (az, docker, git) ----------------------------
# In dry-run mode it only PRINTS the command. Any text listed in MaskValues is
# replaced with ******** in what is printed, so secrets never appear on screen.
# Returns the command's output lines when -CaptureOutput is used.
function Invoke-External {
    param(
        [Parameter(Mandatory)] [string]$Executable,
        [Parameter(Mandatory)] [string[]]$Arguments,
        [switch]$DryRun,
        [switch]$CaptureOutput,
        [string[]]$MaskValues = @()
    )

    # Build the text we show on screen, hiding any secret values.
    $displayText = "$Executable " + ($Arguments -join ' ')
    foreach ($secretValue in $MaskValues) {
        if ($secretValue) {
            $displayText = $displayText.Replace($secretValue, '********')
        }
    }
    Write-Host "  > $displayText" -ForegroundColor DarkGray

    if ($DryRun) {
        Write-Host '    (dry run: not executed)' -ForegroundColor Yellow
        return $null
    }

    # Each element of $Arguments must reach the program as exactly ONE argument
    # (e.g. the whole `sh -c` script). pwsh 7.3+: 'Standard' passing does this correctly.
    # Windows PowerShell 5.1 does not quote embedded spaces/quotes, so quote them by hand.
    $nativeArguments = $Arguments
    if ($PSVersionTable.PSVersion -ge [version]'7.3') {
        $PSNativeCommandArgumentPassing = 'Standard'
    }
    else {
        $nativeArguments = @($Arguments | ForEach-Object {
            if ($_ -match '[\s"]' -or $_ -eq '') {
                '"' + (($_ -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
            }
            else { $_ }
        })
    }

    if ($CaptureOutput) {
        $outputLines = & $Executable @nativeArguments 2>&1
    }
    else {
        & $Executable @nativeArguments
        $outputLines = $null
    }
    $exitCode = $LASTEXITCODE

    if ($exitCode -ne 0) {
        throw "Command failed with exit code ${exitCode}: $Executable $($Arguments[0]) ..."
    }
    return $outputLines
}

# ---- Asks "does this exist?" by running a command and checking it succeeded --
# Returns $true / $false. In dry-run mode it prints the command and returns
# $false (so the dry run shows the "create" path).
function Test-ExternalSucceeds {
    param(
        [Parameter(Mandatory)] [string]$Executable,
        [Parameter(Mandatory)] [string[]]$Arguments,
        [switch]$DryRun
    )

    Write-Host "  > $Executable $($Arguments -join ' ')" -ForegroundColor DarkGray
    if ($DryRun) {
        Write-Host '    (dry run: not executed, assuming "does not exist yet")' -ForegroundColor Yellow
        return $false
    }
    & $Executable @Arguments *> $null
    return ($LASTEXITCODE -eq 0)
}

# ---- Reads KEY=VALUE lines from a file into a hashtable ---------------------
# Skips blank lines and lines starting with '#'. Only the FIRST '=' splits.
function Read-EnvFile {
    param([string]$Path)

    $values = @{}
    if (-not (Test-Path $Path)) {
        return $values
    }
    foreach ($line in Get-Content $Path) {
        $trimmedLine = $line.Trim()
        if ($trimmedLine -eq '' -or $trimmedLine.StartsWith('#')) {
            continue
        }
        $equalsIndex = $trimmedLine.IndexOf('=')
        if ($equalsIndex -lt 1) {
            continue
        }
        $key   = $trimmedLine.Substring(0, $equalsIndex).Trim()
        $value = $trimmedLine.Substring($equalsIndex + 1).Trim()
        $values[$key] = $value
    }
    return $values
}

# ---- Asks for a secret without echoing it, returns it as plain text ---------
function Read-SecretText {
    param([string]$PromptText)

    $secureValue = Read-Host -Prompt $PromptText -AsSecureString
    $plainValue  = [System.Net.NetworkCredential]::new('', $secureValue).Password
    return $plainValue
}

# ---- Gets a secret: from secrets.local.env if present, otherwise prompts ----
function Get-SecretValue {
    param(
        [string]$Name,
        [string]$PromptText,
        [hashtable]$FileValues
    )

    if ($FileValues.ContainsKey($Name) -and $FileValues[$Name]) {
        Write-Host "  $Name : taken from deploy/secrets.local.env" -ForegroundColor DarkGray
        return $FileValues[$Name]
    }
    return Read-SecretText -PromptText $PromptText
}
