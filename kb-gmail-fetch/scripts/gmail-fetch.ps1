#Requires -Version 3.0
<#
.SYNOPSIS
  Deterministic fetch of the PERSONAL Gmail mailbox into .raw/gmail/.

.DESCRIPTION
  Companion to kb-m365-fetch. That skill reads the corporate Microsoft 365
  mailbox through Microsoft Graph; this one reads a personal @gmail.com mailbox
  through the Google Workspace CLI (`gws`). They are deliberately separate
  skills: the two mailboxes hold different material (congress invitations, CFP
  calls, community threads arrive at the personal address, not corporate), and
  they authenticate against different identity providers, so one cannot stand
  in for the other.

  Reads kb-config.yaml (section `gmail`) at the vault root.

.PARAMETER Config
  Path to kb-config.yaml (default: vault root).

.PARAMETER Query
  Raw Gmail search query override, e.g. "from:rootedcon after:2026/01/01".
  When set, config keywords are ignored.

.PARAMETER Days
  Override the look-back window in days.

.PARAMETER Limit
  Override the maximum number of messages to inspect.

.PARAMETER DryRun
  Count matches and print them without writing anything to .raw/.

.EXAMPLE
  pwsh -File .agents/skills/kb-gmail-fetch/scripts/gmail-fetch.ps1
  pwsh -File .agents/skills/kb-gmail-fetch/scripts/gmail-fetch.ps1 -Query "rootedcon" -DryRun
#>
param(
    [string]$Config = "",
    [string]$Query = "",
    [int]$Days = -1,
    [int]$Limit = -1,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# --- Resolve vault root ---
function Test-RepoRoot($p) {
    return (Test-Path (Join-Path $p "kb-config.yaml")) -or
           (Test-Path (Join-Path $p "kb-config.yml")) -or
           (Test-Path (Join-Path $p ".agents/skills"))
}
$RepoRoot = $PSScriptRoot
while ($RepoRoot -and -not (Test-RepoRoot $RepoRoot)) {
    $parent = Split-Path $RepoRoot -Parent
    if ($parent -eq $RepoRoot) { break }
    $RepoRoot = $parent
}
if (-not (Test-RepoRoot $RepoRoot)) {
    Write-Host "ERROR: could not locate the vault root (no kb-config.yaml / .agents/skills upward)." -ForegroundColor Red
    exit 1
}

# --- Check gws ---
if (-not (Get-Command gws -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: gws not on PATH." -ForegroundColor Red
    Write-Host "  brew install googleworkspace-cli   # or: see github.com/googleworkspace/cli" -ForegroundColor Yellow
    exit 2
}

# --- Load config ---
$Candidates = @(
    (Join-Path $RepoRoot "kb-config.yaml"),
    (Join-Path $RepoRoot "kb-config.yml")
)
if (-not $Config) {
    $Config = $Candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $Config) {
        Write-Host "ERROR: no kb-config.yaml at $RepoRoot" -ForegroundColor Red
        exit 1
    }
}
if (-not (Test-Path $Config)) {
    Write-Host "ERROR: config not found: $Config" -ForegroundColor Red
    exit 1
}
# ConvertFrom-Yaml yields Hashtables; round-trip through JSON so property
# enumeration behaves like the rest of the scripts in this vault.
if (-not (Get-Module -ListAvailable -Name powershell-yaml)) {
    Write-Host "Installing missing module: powershell-yaml" -ForegroundColor Yellow
    Install-Module powershell-yaml -Scope CurrentUser -Force -AllowClobber
}
Import-Module powershell-yaml -ErrorAction Stop

function Get-Setting($obj, $names, $default) {
    foreach ($n in $names) {
        $cur = $obj
        $ok = $true
        foreach ($part in ($n -split '\.')) {
            if ($null -eq $cur -or -not ($cur.PSObject.Properties.Name -contains $part)) { $ok = $false; break }
            $cur = $cur.$part
        }
        if ($ok -and $null -ne $cur) { return $cur }
    }
    return $default
}

try {
    $Raw = Get-Content $Config -Raw | ConvertFrom-Yaml | ConvertTo-Json -Depth 20 | ConvertFrom-Json
} catch {
    Write-Host "ERROR: could not parse $Config : $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

$G = $Raw.gmail
if ($null -eq $G) {
    Write-Host "gmail section missing from kb-config.yaml — nothing to fetch." -ForegroundColor Yellow
    Write-Host "Add a 'gmail:' section (see SKILL.md)." -ForegroundColor Yellow
    exit 1
}
if ($G.PSObject.Properties.Name -contains "enabled" -and -not $G.enabled) {
    Write-Host "gmail.enabled is false in kb-config.yaml — nothing to fetch." -ForegroundColor Yellow
    exit 1
}

$ProjectName = if ($Raw.project -and $Raw.project.name) { $Raw.project.name } else { "personal" }
$Keywords = @()
if ($G.keywords) { $Keywords = @($G.keywords) }
elseif ($Raw.project -and $Raw.project.keywords) { $Keywords = @($Raw.project.keywords) }

$WindowDays = if ($Days -ge 0) { $Days } else { [int](Get-Setting $G @("window.days") 365) }
$MaxMessages = if ($Limit -ge 0) { $Limit } else { [int](Get-Setting $G @("window.max_messages","window.top") 300) }
$PageLimit = [int](Get-Setting $G @("window.page_limit") 10)
$BodyMode = [string](Get-Setting $G @("body") "snippet")
$OutRel = [string](Get-Setting $G @("output.dir") ".raw/gmail")
$OutDir = Join-Path $RepoRoot $OutRel

if (-not $Query -and $Keywords.Count -eq 0) {
    Write-Host "ERROR: no query and no keywords — a full-mailbox scan is never what you want." -ForegroundColor Red
    exit 3
}

$Since = (Get-Date).AddDays(-$WindowDays)
$SinceGmail = $Since.ToString("yyyy/MM/dd")

# Notification digests (LinkedIn, Glassdoor, aggregators) quote every keyword in
# their bodies, so a keyword-only query returns them all and buries the real
# mail. Negations are part of the Gmail query, not a post-filter, so they also
# keep the page budget for messages that matter.
$Excludes = @()
if ($G.exclude -and $G.exclude.senders) { $Excludes = @($G.exclude.senders) }

if (-not $Query) {
    # YAML drops the quotes around a multi-word value, so "black hat" arrives as
    # `black hat` and OR-ing it unquoted breaks the grouping — Gmail then parses
    # `OR black hat OR ...` as bare terms and returns resultSizeEstimate 0 with
    # no error. Re-quote anything containing a space.
    $orKw = ($Keywords | ForEach-Object {
        if ($_ -match '\s') { '"' + ($_ -replace '"','') + '"' } else { $_ }
    }) -join ' OR '
    $Query = "($orKw) after:$SinceGmail"
} elseif ($Query -notmatch 'after:|before:') {
    $Query = "$Query after:$SinceGmail"
}
foreach ($ex in $Excludes) {
    if ($Query -notmatch [regex]::Escape("-from:$ex")) { $Query = "$Query -from:$ex" }
}

Write-Host "=== Gmail Fetch ===" -ForegroundColor Cyan
Write-Host "  Vault:    $RepoRoot" -ForegroundColor DarkGray
Write-Host "  Project:  $ProjectName" -ForegroundColor DarkGray
Write-Host "  Query:    $Query" -ForegroundColor DarkGray
Write-Host "  Window:   last $WindowDays days (since $SinceGmail)" -ForegroundColor DarkGray
Write-Host "  Limit:    $MaxMessages messages" -ForegroundColor DarkGray
Write-Host "  Body:     $BodyMode" -ForegroundColor DarkGray

# --- Identity check ---
# gws writes a "Using keyring backend" banner to STDERR, never to stdout, so
# only stdout is parsed. Keep stderr for diagnostics when a call fails.
function Invoke-Gws {
    param([string[]]$GwsArgs)
    $out = & gws @GwsArgs 2>$TempErr
    if ($LASTEXITCODE -ne 0) {
        $err = if (Test-Path $TempErr) { Get-Content $TempErr -Raw } else { "" }
        throw "gws exit $LASTEXITCODE : $($err.Trim())"
    }
    return $out
}
$TempErr = Join-Path ([System.IO.Path]::GetTempPath()) "gws-stderr-$PID.log"

try {
    $profile = Invoke-Gws @("gmail","users","getProfile","--params",'{"userId":"me"}',"--format","json") | ConvertFrom-Json
} catch {
    Write-Host "  gws is not authenticated: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "  Run: gws auth login" -ForegroundColor Yellow
    exit 2
}
Write-Host "  Identity: $($profile.emailAddress) ($($profile.messagesTotal) messages, $($profile.threadsTotal) threads)" -ForegroundColor Green

# --- List matching messages ---
$listParams = @{ userId = "me"; q = $Query; maxResults = [Math]::Min($MaxMessages, 500) } | ConvertTo-Json -Compress
try {
    $rawPages = Invoke-Gws @("gmail","users","messages","list","--params",$listParams,"--page-all","--page-limit","$PageLimit","--format","json")
} catch {
    Write-Host "  List failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

$ids = New-Object System.Collections.Generic.List[string]
foreach ($line in @($rawPages)) {
    if (-not $line -or -not $line.Trim()) { continue }
    try { $page = $line | ConvertFrom-Json } catch { continue }
    if ($page.messages) {
        foreach ($m in $page.messages) {
            if ($ids.Count -ge $MaxMessages) { break }
            if (-not $ids.Contains($m.id)) { $ids.Add($m.id) }
        }
    }
    if ($ids.Count -ge $MaxMessages) { break }
}
Write-Host "  Matched:  $($ids.Count) messages (capped at $MaxMessages)" -ForegroundColor Green

if ($ids.Count -eq 0) {
    Write-Host "  Nothing to write. Widen the window or change the query." -ForegroundColor Yellow
    exit 0
}

# --- Dedup ledger: never rewrite a message already captured ---
$CacheDir = Join-Path $RepoRoot ".cache/gmail"
$LedgerPath = Join-Path $CacheDir "seen.json"
$seen = @{}
if (Test-Path $LedgerPath) {
    try {
        $obj = Get-Content $LedgerPath -Raw | ConvertFrom-Json
        foreach ($p in $obj.PSObject.Properties) { $seen[$p.Name] = $p.Value }
    } catch { $seen = @{} }
}

$fresh = @($ids | Where-Object { -not $seen.ContainsKey($_) })
Write-Host "  Already captured: $($ids.Count - $fresh.Count) | new: $($fresh.Count)" -ForegroundColor DarkGray

if ($fresh.Count -eq 0) {
    Write-Host "  All matches were captured in a previous run. Nothing new to write." -ForegroundColor Yellow
    exit 0
}

# --- Fetch each message ---
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$records = @()
$n = 0
foreach ($id in $fresh) {
    $n++
    if ($n % 25 -eq 0) { Write-Host "    Progress: $n/$($fresh.Count)..." -ForegroundColor DarkGray }
    $fmt = if ($BodyMode -eq "full") { "full" } else { "metadata" }
    $getParams = @{ userId = "me"; id = $id; format = $fmt } | ConvertTo-Json -Compress
    if ($fmt -eq "metadata") {
        $getParams = @{ userId = "me"; id = $id; format = "metadata"; metadataHeaders = @("From","To","Subject","Date") } | ConvertTo-Json -Compress
    }
    try {
        $msg = Invoke-Gws @("gmail","users","messages","get","--params",$getParams,"--format","json") | ConvertFrom-Json
    } catch {
        Write-Host "    Warning: skipped $id : $($_.Exception.Message)" -ForegroundColor DarkYellow
        continue
    }

    $headers = @{}
    if ($msg.payload -and $msg.payload.headers) {
        foreach ($h in $msg.payload.headers) { $headers[$h.name] = $h.value }
    }

    $bodyText = ""
    if ($fmt -eq "full") {
        # Prefer text/plain; fall back to stripped HTML. Bodies are base64url.
        $plain = $null; $html = $null
        $stack = New-Object System.Collections.Stack
        if ($msg.payload) { $stack.Push($msg.payload) }
        while ($stack.Count -gt 0) {
            $part = $stack.Pop()
            if ($part.body -and $part.body.data) {
                $b64 = $part.body.data -replace '-','+' -replace '_','/'
                switch ($b64.Length % 4) { 2 { $b64 += '==' } 3 { $b64 += '=' } }
                try {
                    $decoded = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64))
                    if ($part.mimeType -eq "text/plain" -and -not $plain) { $plain = $decoded }
                    elseif ($part.mimeType -eq "text/html" -and -not $html) { $html = $decoded }
                } catch { }
            }
            if ($part.parts) { foreach ($p in $part.parts) { $stack.Push($p) } }
        }
        if ($plain) { $bodyText = $plain }
        elseif ($html) {
            # Strip the parts that carry no prose before flattening tags: an
            # HTML-only newsletter yields its whole stylesheet as text otherwise.
            $bodyText = $html -replace '(?is)<(script|style|head)\b.*?</\1>', ' '
            $bodyText = $bodyText -replace '(?s)<!--.*?-->', ' '
            $bodyText = $bodyText -replace '(?i)<br\s*/?>', "`n"
            $bodyText = $bodyText -replace '(?i)</(p|div|tr|li|h[1-6])>', "`n"
            $bodyText = $bodyText -replace '<[^>]+>', ' '
            $bodyText = $bodyText -replace '&nbsp;', ' ' -replace '&amp;', '&' `
                                  -replace '&lt;', '<' -replace '&gt;', '>' `
                                  -replace '&quot;', '"' -replace '&#39;', "'"
            $bodyText = $bodyText -replace '[ \t]+', ' ' -replace '(\r?\n\s*){3,}', "`n`n"
            $bodyText = $bodyText.Trim()
        }
    }

    $records += [PSCustomObject]@{
        Id       = $id
        ThreadId = $msg.threadId
        Subject  = if ($headers["Subject"]) { $headers["Subject"] } else { "(no subject)" }
        From     = $headers["From"]
        To       = $headers["To"]
        Date     = if ($headers["Date"]) { $headers["Date"] } else { $msg.internalDate }
        Labels   = @($msg.labelIds) -join ","
        Snippet  = if ($msg.snippet) { $msg.snippet } else { "" }
        Body     = $bodyText
    }
    Start-Sleep -Milliseconds 120   # stay inside the per-user Gmail quota
}
Write-Host "  Fetched:  $($records.Count) messages" -ForegroundColor Green

# --- Dry run stops here ---
if ($DryRun) {
    Write-Host ""
    Write-Host "=== DRY RUN (nothing written) ===" -ForegroundColor Yellow
    foreach ($r in $records) {
        Write-Host ("  {0} | {1} | {2}" -f $r.Date, $r.From, $r.Subject)
    }
    exit 0
}

# --- Write markdown ---
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
$OutFile = Join-Path $OutDir "gmail-fetch-$timestamp.md"

$lines = @()
$lines += "# Gmail Fetch $timestamp"
$lines += ""
$lines += "**Project:** $ProjectName"
$lines += "**Fetched:** $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
$lines += "**Account:** $($profile.emailAddress)"
$lines += "**Query:** ``$Query``"
$lines += "**Window:** last $WindowDays days (since $SinceGmail)"
$lines += "**Body mode:** $BodyMode"
$lines += "**Matched:** $($ids.Count) | **New in this run:** $($records.Count)"
$lines += ""
$lines += "> Source: personal Gmail mailbox via the Google Workspace CLI (``gws``)."
$lines += "> Corporate Microsoft 365 mail is a different mailbox, captured by ``kb-m365-fetch``."
$lines += ""
$lines += "---"
$lines += ""
$lines += "## Messages ($($records.Count))"
$lines += ""
foreach ($r in $records) {
    $lines += "### $($r.Subject)"
    $lines += "- **From:** $($r.From)"
    if ($r.To) { $lines += "- **To:** $($r.To)" }
    $lines += "- **Date:** $($r.Date)"
    $lines += "- **Labels:** $($r.Labels)"
    $lines += "- **Message-Id:** ``$($r.Id)``"
    if ($r.Snippet) {
        $snip = ($r.Snippet -replace "`r?`n", " ")
        $lines += "- **Snippet:** $snip"
    }
    if ($r.Body) {
        $lines += ""
        $lines += '```text'
        $lines += $r.Body.Trim()
        $lines += '```'
    }
    $lines += ""
}

Set-Content -Path $OutFile -Value ($lines -join "`n") -Encoding UTF8

# --- Update ledger ---
if (-not (Test-Path $CacheDir)) { New-Item -ItemType Directory -Path $CacheDir -Force | Out-Null }
foreach ($r in $records) { $seen[$r.Id] = $timestamp }
($seen | ConvertTo-Json -Depth 5) | Set-Content -Path $LedgerPath -Encoding UTF8

$sizeKb = [Math]::Round((Get-Item $OutFile).Length / 1KB, 1)
Write-Host ""
Write-Host "=== Summary ===" -ForegroundColor Green
Write-Host "  Wrote:    $($OutFile -replace [regex]::Escape($RepoRoot + "/"), '') ($sizeKb KB)"
Write-Host "  Messages: $($records.Count) new of $($ids.Count) matched"
Write-Host "  Ledger:   $($LedgerPath -replace [regex]::Escape($RepoRoot + "/"), '') ($($seen.Count) ids)"
Write-Host ""
Write-Host "Next: hand off to wiki-ingest — do not edit what this script wrote." -ForegroundColor Cyan
