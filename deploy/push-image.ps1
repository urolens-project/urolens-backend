<#
=============================================================================
push-image.ps1  -  build the Docker image and push it to GitHub Container Registry

What it does : builds the backend image from the repo Dockerfile, tags it
               ghcr.io/urolens-project/urolens-backend:<Tag>, and pushes it.
               Refuses the tag "latest". Warns if your current commit is not
               the commit that git tag <Tag> points to.
When to run  : every time you cut a release (after you create the git tag),
               BEFORE deploy-tag.ps1. Runs on your own machine (needs Docker).
AWS equivalent: `docker build` + `docker tag` + `docker push` to an ECR repo
               (after `aws ecr get-login-password | docker login`). GHCR is
               simply a different registry; Azure pulls from it later.

Usage        : pwsh deploy/push-image.ps1 -Tag v1.1.0 [-Login] [-DryRun]
  -Login       also run `docker login ghcr.io` first (prompts for a GitHub
               username and a PAT with write:packages; the PAT is not echoed).
  -DryRun      print every command, run nothing.
=============================================================================
#>
param(
    [Parameter(Mandatory)] [string]$Tag,
    [switch]$Login,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\_common.ps1"

$fullImageName  = "${ImageRepository}:$Tag"
$localImageName = "urolens-backend:$Tag"

Write-Step 1 "Check the tag is allowed ('latest' is refused)"
Assert-ValidTag -Tag $Tag
Write-Host "  Tag '$Tag' is OK. Image will be: $fullImageName"

Write-Step 2 "Compare your current commit with git tag '$Tag'"
# Read-only git commands: show the commit you are on, and the commit the tag points to.
$headCommit = Invoke-External -Executable 'git' -Arguments @('-C', $RepositoryRoot, 'rev-parse', 'HEAD') -CaptureOutput -DryRun:$DryRun
$tagCommit  = Invoke-External -Executable 'git' -Arguments @('-C', $RepositoryRoot, 'rev-list', '-n', '1', $Tag) -CaptureOutput -DryRun:$DryRun
if (-not $DryRun) {
    if ("$tagCommit" -match 'fatal|unknown revision|ambiguous') {
        Write-Warning "Git tag '$Tag' does not exist. You are building an image for a tag that has no matching release."
    }
    elseif ("$headCommit".Trim() -ne "$tagCommit".Trim()) {
        Write-Warning "HEAD ($headCommit) is NOT the commit git tag '$Tag' points to ($tagCommit). The image will contain your current files, not the tagged release."
    }
    else {
        Write-Host '  HEAD matches the tag. Good.'
    }
}

if ($Login) {
    Write-Step 3 'Log in to ghcr.io'
    if ($DryRun) {
        Write-Host '  (dry run: would prompt for GitHub username and PAT with write:packages)' -ForegroundColor Yellow
        $githubUser  = '<github-user>'
        $githubToken = '<hidden>'
    }
    else {
        $githubUser  = Read-Host -Prompt 'GitHub username'
        $githubToken = Read-SecretText -PromptText 'GitHub PAT with write:packages (hidden)'
    }
    # Logs Docker in to GitHub's registry. The token goes in through stdin so it
    # never shows in the process list. AWS: `aws ecr get-login-password | docker login`.
    Write-Host "  > docker login ghcr.io -u $githubUser --password-stdin   (token piped in, hidden)" -ForegroundColor DarkGray
    if ($DryRun) {
        Write-Host '    (dry run: not executed)' -ForegroundColor Yellow
    }
    else {
        $githubToken | docker login ghcr.io -u $githubUser --password-stdin
        if ($LASTEXITCODE -ne 0) { throw 'docker login failed.' }
    }
}
else {
    Write-Step 3 'Skip login (pass -Login if you are not already logged in to ghcr.io)'
}

Write-Step 4 'Build the image (linux/amd64, because the Azure B1 plan is x86)'
# Builds an image from the Dockerfile in the repo root. AWS: the same `docker build` you'd do before pushing to ECR.
Invoke-External -Executable 'docker' -Arguments @('build', '--platform', 'linux/amd64', '-t', $localImageName, $RepositoryRoot) -DryRun:$DryRun

Write-Step 5 'Tag the image with the GHCR name'
# Adds a second name to the same image so Docker knows which registry to push to. AWS: `docker tag` to the ECR URI.
Invoke-External -Executable 'docker' -Arguments @('tag', $localImageName, $fullImageName) -DryRun:$DryRun

Write-Step 6 'Push the image to GHCR'
# Uploads the image layers to ghcr.io. AWS: `docker push <account>.dkr.ecr.<region>.amazonaws.com/repo:tag`.
Invoke-External -Executable 'docker' -Arguments @('push', $fullImageName) -DryRun:$DryRun

Write-Host ''
Write-Host "Done. Pushed $fullImageName" -ForegroundColor Green
Write-Host 'Next: make sure the package is readable by the read-only PAT you use in provision.ps1.'
