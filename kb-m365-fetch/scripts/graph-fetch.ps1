#Requires -Version 7.0
<#
.SYNOPSIS
  Unified deterministic fetch of project emails, chats, Teams channels, attachments, and SharePoint files.
.DESCRIPTION
  Uses Microsoft Graph API with delegated access (interactive browser login).
  Reads configuration from kb-config.json (section .m365) at the vault root.
  Each module can be enabled/disabled:
    emails, chats, teams_channels, attachments, chat_attachments, transcripts, sharepoint

  CONSENT NOTE — two modules need Entra ID admin consent and are OFF by default:
    teams_channels -> ChannelMessage.Read.All
    transcripts    -> OnlineMeetingTranscript.Read.All
  Their scopes are only requested when the module is enabled, because adding an
  admin-consent scope to the sign-in makes the ENTIRE consent fail for a
  non-admin user, taking the working modules down with it.

  PREREQUISITES — install Microsoft Graph modules first (one-time):
    Install-Module Microsoft.Graph.Authentication -Scope CurrentUser -Force
    Install-Module Microsoft.Graph.Users -Scope CurrentUser -Force
    Install-Module Microsoft.Graph.Mail -Scope CurrentUser -Force
    Install-Module Microsoft.Graph.Teams -Scope CurrentUser -Force
    Install-Module Microsoft.Graph.Groups -Scope CurrentUser -Force
    Install-Module Microsoft.Graph.Sites -Scope CurrentUser -Force

  Or install the full meta-module:
    Install-Module Microsoft.Graph -Scope CurrentUser -Force

.PARAMETER Config
  Path to config JSON (default: kb-config.json at the vault root, falling back to a legacy m365-config.json).
.PARAMETER Hours
  Override time window in hours (default: from config).
.PARAMETER Top
  Override max emails to scan (default: from config).
.PARAMETER ChatLimit
  Override max chats to scan (default: from config).
.PARAMETER Modules
  Override which modules to run (comma-separated: emails,chats,teams_channels,
  attachments,chat_attachments,transcripts,sharepoint).
  Default: all enabled modules from config.
.EXAMPLE
  pwsh -File scripts/graph-fetch.ps1
  pwsh -File scripts/graph-fetch.ps1 -Hours 72 -Top 200
  pwsh -File scripts/graph-fetch.ps1 -Modules emails,chats
  pwsh -File scripts/graph-fetch.ps1 -Modules chats,chat_attachments
  pwsh -File scripts/graph-fetch.ps1 -Modules chats,transcripts   # needs admin consent
#>

param(
    [string]$Config,
    [int]$Hours = -1,
    [int]$Top = -1,
    [int]$ChatLimit = -1,
    [string]$Modules = ""
)

$ErrorActionPreference = "Stop"

# --- Resolve paths ---
# This script lives inside a skill, so its depth below the repo root depends on
# where the skill is installed. Walk up looking for the markers instead of
# counting levels, which breaks the moment the skill moves.
# Both wiki/ AND .git are required: there is a skill directory named "wiki"
# under .agents/skills/, so the wiki/ marker alone resolves to the skills
# directory instead of the repo root.
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = $ScriptDir
function Test-RepoRoot($p) {
    return (Test-Path (Join-Path $p "wiki")) -and (Test-Path (Join-Path $p ".git"))
}
while ($RepoRoot -and -not (Test-RepoRoot $RepoRoot)) {
    $parent = Split-Path -Parent $RepoRoot
    if ($parent -eq $RepoRoot) { break }
    $RepoRoot = $parent
}
if (-not (Test-RepoRoot $RepoRoot)) {
    Write-Host "ERROR: Repo root not found: no ancestor has both wiki/ and .git" -ForegroundColor Red
    exit 1
}

# --- Load config ---
# Preferred: kb-config.yaml at the vault root, with project-wide settings under
# .project and this pipeline's settings under .m365. JSON is still accepted so
# consumer projects migrate at their own pace, as is the legacy flat
# m365-config.json (project_name/keywords at top level). -Config always wins.
$Candidates = @(
    (Join-Path $RepoRoot "kb-config.yaml"),
    (Join-Path $RepoRoot "kb-config.yml"),
    (Join-Path $RepoRoot "kb-config.json"),
    (Join-Path $RepoRoot "m365-config.json")
)

if (-not $Config) {
    $Config = $Candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $Config) {
        Write-Host "ERROR: No config found. Looked for:" -ForegroundColor Red
        $Candidates | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }
        Write-Host "Copy kb-config.example.yaml from the kb-skills repo and fill it in." -ForegroundColor Yellow
        exit 1
    }
}
if (-not (Test-Path $Config)) {
    Write-Host "ERROR: Config file not found: $Config" -ForegroundColor Red
    exit 1
}

# PowerShell has no native YAML parser, so powershell-yaml is installed on
# demand — the same treatment the Microsoft.Graph modules get further down.
# It has to happen HERE, before the config is read, not with the others.
if ($Config -match '\.ya?ml$') {
    if (-not (Get-Module -ListAvailable -Name powershell-yaml)) {
        Write-Host "Installing missing module: powershell-yaml" -ForegroundColor Yellow
        Install-Module powershell-yaml -Scope CurrentUser -Force -AllowClobber
    }
    Import-Module powershell-yaml -ErrorAction Stop
    # ConvertFrom-Yaml yields Hashtables, but the rest of this script enumerates
    # config keys via .PSObject.Properties — which on a Hashtable returns Count,
    # Keys and Values instead of the config keys, silently emptying the module
    # set. Round-trip through JSON so both formats produce PSCustomObjects.
    $Raw = Get-Content $Config -Raw | ConvertFrom-Yaml | ConvertTo-Json -Depth 20 | ConvertFrom-Json
} else {
    $Raw = Get-Content $Config -Raw | ConvertFrom-Json
}

if ($null -ne $Raw.m365) {
    # kb-config.json shape
    $Cfg = $Raw.m365
    if ($null -ne $Raw.project) {
        $ProjectName = $Raw.project.name
        $Keywords = $Raw.project.keywords
    }
    if ($Cfg.PSObject.Properties.Name -contains "enabled" -and -not $Cfg.enabled) {
        Write-Host "m365.enabled is false in kb-config.json — nothing to fetch." -ForegroundColor Yellow
        Write-Host "Set it to true once the modules are configured." -ForegroundColor Yellow
        exit 1
    }
} else {
    # legacy m365-config.json shape
    $Cfg = $Raw
    $ProjectName = $Raw.project_name
    $Keywords = $Raw.keywords
}

if (-not $ProjectName) {
    Write-Host "ERROR: project name missing from $Config" -ForegroundColor Red
    exit 1
}
$nl = [Environment]::NewLine

# Override config with CLI params
if ($Hours -ge 0) { $Cfg.window.hours = $Hours }
if ($Top -ge 0) { $Cfg.window.top = $Top }
if ($ChatLimit -ge 0) { $Cfg.window.chat_limit = $ChatLimit }

# Determine which modules to run
$ModuleSet = @{}
if ($Modules) {
    foreach ($m in $Modules -split ",") { $ModuleSet[$m.Trim()] = $true }
} else {
    foreach ($prop in $Cfg.modules.PSObject.Properties) {
        if ($prop.Value) { $ModuleSet[$prop.Name] = $true }
    }
}

# --- Resolve output paths ---
# Config paths must be relative to the vault root. Absolute values and `..`
# traversal are rejected: Python's pathlib lets an absolute value replace the
# root entirely while PowerShell's Join-Path does not, so without this check one
# config file would mean two different things depending on which pipeline read it.
function Resolve-VaultPath($root, $value, $key) {
    if ([System.IO.Path]::IsPathRooted($value) -or $value.StartsWith("~")) {
        Write-Host "ERROR: kb-config.json $key must be relative to the vault root, got: $value" -ForegroundColor Red
        exit 1
    }
    $full = [System.IO.Path]::GetFullPath((Join-Path $root $value))
    $rootFull = [System.IO.Path]::GetFullPath($root).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
    if ($full -ne $rootFull -and -not $full.StartsWith($rootFull + [System.IO.Path]::DirectorySeparatorChar)) {
        Write-Host "ERROR: kb-config.json $key escapes the vault root, got: $value" -ForegroundColor Red
        exit 1
    }
    return $full
}

$OutputDir = if ($Cfg.output.dir) { Resolve-VaultPath $RepoRoot $Cfg.output.dir "m365.output.dir" } else { Join-Path $RepoRoot ".raw/msoffice" }
$AttachDir = if ($Cfg.output.attachments_dir) { Resolve-VaultPath $RepoRoot $Cfg.output.attachments_dir "m365.output.attachments_dir" } else { Join-Path $OutputDir "attachments" }
$SharePointDir = if ($Cfg.output.sharepoint_dir) { Resolve-VaultPath $RepoRoot $Cfg.output.sharepoint_dir "m365.output.sharepoint_dir" } else { Join-Path $RepoRoot ".raw/sharepoint" }
$ChatAttachDir = if ($Cfg.output.chat_attachments_dir) { Resolve-VaultPath $RepoRoot $Cfg.output.chat_attachments_dir "m365.output.chat_attachments_dir" } else { Join-Path $OutputDir "chat-attachments" }
$TranscriptDir = if ($Cfg.output.transcripts_dir) { Resolve-VaultPath $RepoRoot $Cfg.output.transcripts_dir "m365.output.transcripts_dir" } else { Join-Path $OutputDir "transcripts" }
if (-not (Test-Path $OutputDir)) { New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null }
if ($ModuleSet["attachments"] -and -not (Test-Path $AttachDir)) { New-Item -ItemType Directory -Path $AttachDir -Force | Out-Null }
if ($ModuleSet["chat_attachments"] -and -not (Test-Path $ChatAttachDir)) { New-Item -ItemType Directory -Path $ChatAttachDir -Force | Out-Null }
if ($ModuleSet["transcripts"] -and -not (Test-Path $TranscriptDir)) { New-Item -ItemType Directory -Path $TranscriptDir -Force | Out-Null }

$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$OutputFile = Join-Path $OutputDir "graph-fetch-$Timestamp.md"
$Since = (Get-Date).AddHours(-$Cfg.window.hours)
$SinceIso = $Since.ToString("yyyy-MM-ddTHH:mm:ssZ")

# --- Ensure required modules are installed ---
$RequiredModules = @(
    "Microsoft.Graph.Authentication",
    "Microsoft.Graph.Users",
    "Microsoft.Graph.Mail",
    "Microsoft.Graph.Teams",
    "Microsoft.Graph.Groups"
)
if ($ModuleSet["sharepoint"]) { $RequiredModules += "Microsoft.Graph.Sites" }

$MissingModules = @()
foreach ($mod in $RequiredModules) {
    if (-not (Get-Module -ListAvailable -Name $mod)) {
        $MissingModules += $mod
    }
}
if ($MissingModules.Count -gt 0) {
    Write-Host "Installing missing modules: $($MissingModules -join ', ')" -ForegroundColor Yellow
    foreach ($mod in $MissingModules) {
        Install-Module $mod -Scope CurrentUser -Force -AllowClobber
    }
    Write-Host "Modules installed." -ForegroundColor Green
}
foreach ($mod in $RequiredModules) {
    Import-Module $mod -ErrorAction SilentlyContinue
}

# --- Build scopes based on enabled modules ---
# An admin-consent scope in this list makes the whole sign-in fail for a non-admin
# user, so admin-only scopes are added only when their module is explicitly enabled.
$Scopes = @("Mail.Read", "User.Read")
if ($ModuleSet["chats"] -or $ModuleSet["teams_channels"]) { $Scopes += "Group.Read.All" }
if ($ModuleSet["chats"]) { $Scopes += "Chat.Read"; $Scopes += "ChatMessage.Read" }
if ($ModuleSet["sharepoint"]) { $Scopes += "Sites.Read.All" }
# Chat file attachments are fetched from /shares/{id}/driveItem/content.
#
# WHY NOT Files.Read.All: per the Graph permissions reference, delegated
# Files.Read.All does NOT require admin consent — but this tenant restricts user
# consent, so consent is a fixed admin-approved SET of scopes. Requesting anything
# outside that set triggers the admin approval wall even for user-level permissions.
# Files.Read.All is outside the set; Sites.Read.All is inside it (proven working
# by the sharepoint module). Chat files live in the sender's OneDrive for Business,
# which is a SharePoint site collection, so Sites.Read.All can cover them.
# If it doesn't, the download fails per-file and the run continues.
if ($ModuleSet["chat_attachments"]) { $Scopes += "Sites.Read.All" }
# teams_channels needs ChannelMessage.Read.All (admin consent) — not requested.

# Admin-consent scopes are kept OUT of $Scopes so that a refused consent can be
# retried without them. Requesting them inline would make one optional module
# take the whole run down at sign-in.
$AdminScopes = @()
$AdminModules = @()
if ($ModuleSet["transcripts"]) {
    $AdminScopes += @("OnlineMeetings.Read", "OnlineMeetingTranscript.Read.All")
    $AdminModules += "transcripts"
}

# Safe here: these are strings, which compare by value. Never use -Unique on
# Graph objects — see the chat dedup note below.
$Scopes = @($Scopes | Select-Object -Unique)
$AllScopes = @(@($Scopes) + @($AdminScopes) | Select-Object -Unique)

if ($AdminModules.Count -gt 0) {
    Write-Host "NOTE: module(s) '$($AdminModules -join ', ')' need Entra ID admin consent." -ForegroundColor Yellow
    Write-Host "      If consent is refused, they are dropped and the rest still runs." -ForegroundColor Yellow
}

# --- Connect ---
Write-Host "Connecting to Microsoft Graph..." -ForegroundColor Cyan
Write-Host "  Project: $ProjectName" -ForegroundColor DarkGray
Write-Host "  Config:  $Config" -ForegroundColor DarkGray
Write-Host "  Modules: $($ModuleSet.Keys -join ', ')" -ForegroundColor DarkGray
Write-Host "  Window:  last $($Cfg.window.hours) hours (since $SinceIso)" -ForegroundColor DarkGray
Write-Host "  Scopes:  $($AllScopes -join ', ')" -ForegroundColor DarkGray
try {
    Connect-MgGraph -Scopes $AllScopes -NoWelcome -ErrorAction Stop
} catch {
    # No admin scopes were in play, so the failure is not about consent.
    if ($AdminScopes.Count -eq 0) { throw }
    Write-Host ""
    Write-Host "Sign-in failed while requesting admin-consent scopes:" -ForegroundColor DarkYellow
    Write-Host "  $($AdminScopes -join ', ')" -ForegroundColor DarkYellow
    Write-Host "Dropping module(s): $($AdminModules -join ', ') and retrying without them." -ForegroundColor DarkYellow
    Write-Host "Ask an Entra ID admin to consent to those scopes if you need them." -ForegroundColor DarkYellow
    Write-Host ""
    foreach ($m in $AdminModules) { $ModuleSet.Remove($m) }
    Connect-MgGraph -Scopes $Scopes -NoWelcome -ErrorAction Stop
}
$Me = Get-MgContext
Write-Host "Connected as: $($Me.Account)" -ForegroundColor Green
Write-Host "  Active modules: $($ModuleSet.Keys -join ', ')" -ForegroundColor DarkGray

# --- Helpers ---
function Test-ProjectMatch {
    param([string]$Text)
    if (-not $Text) { return $false }
    $lower = $Text.ToLower()
    foreach ($kw in $Keywords) {
        if ($lower -match [regex]::Escape($kw)) { return $true }
    }
    return $false
}

function Get-SafeFileName {
    param([string]$Name, [string]$Fallback = "unnamed")
    if (-not $Name) { $Name = $Fallback }
    $safe = $Name -replace '[^\w\.\-]', '_'
    if ($safe.Length -gt 120) { $safe = $safe.Substring(0, 120) }
    return $safe
}

# Encodes a sharing URL into the share ID accepted by /shares/{id}.
# Format: "u!" + base64(url), padding stripped, '+'->'-', '/'->'_'.
function ConvertTo-GraphShareId {
    param([string]$Url)
    $b64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($Url))
    return "u!" + ($b64.TrimEnd('=').Replace('+', '-').Replace('/', '_'))
}

function Get-UniquePath {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return $Path }
    $dir = Split-Path -Parent $Path
    $base = [System.IO.Path]::GetFileNameWithoutExtension($Path)
    $ext = [System.IO.Path]::GetExtension($Path)
    $i = 2
    while (Test-Path (Join-Path $dir "$base($i)$ext")) { $i++ }
    return (Join-Path $dir "$base($i)$ext")
}

# --- 1. Emails ---
$projectEmails = @()
if ($ModuleSet["emails"]) {
    Write-Host ""
    Write-Host "=== Emails ===" -ForegroundColor Cyan
    try {
        $allEmails = Get-MgUserMessage -UserId $Me.Account -All -Property subject,receivedDateTime,from,bodyPreview,hasAttachments,id -ErrorAction Stop | Select-Object -First $Cfg.window.top
        foreach ($e in $allEmails) {
            $combined = "$($e.Subject) $($e.BodyPreview)"
            if (Test-ProjectMatch -Text $combined) {
                $projectEmails += $e
            }
        }
        Write-Host "  Scanned $($allEmails.Count) emails, $($projectEmails.Count) match project keywords" -ForegroundColor Green
    } catch {
        Write-Host "  Emails skipped: $($_.Exception.Message.Substring(0, [Math]::Min(200, $_.Exception.Message.Length)))" -ForegroundColor DarkYellow
    }
}

# --- 2. Email attachments ---
$downloadedAttachments = @()
if ($ModuleSet["attachments"] -and $projectEmails.Count -gt 0) {
    Write-Host ""
    Write-Host "=== Email Attachments ===" -ForegroundColor Cyan
    foreach ($e in $projectEmails) {
        if ($e.HasAttachments) {
            try {
                $atts = Get-MgUserMessageAttachment -UserId $Me.Account -MessageId $e.Id -All
                foreach ($att in $atts) {
                    if ($att.Size -gt 0 -and $att.ContentBytes) {
                        $safeName = $att.Name -replace '[^\w\.\-]', '_'
                        $attPath = Join-Path $AttachDir "$Timestamp-$safeName"
                        $bytes = [Convert]::FromBase64String($att.ContentBytes)
                        [System.IO.File]::WriteAllBytes($attPath, $bytes)
                        $downloadedAttachments += [PSCustomObject]@{
                            EmailSubject = $e.Subject
                            Name = $att.Name
                            Size = $att.Size
                            Path = $attPath
                        }
                        Write-Host "    Downloaded: $safeName ($($att.Size) bytes)" -ForegroundColor DarkGray
                    }
                }
            } catch {
                Write-Host "    Warning: could not fetch attachments for: $($e.Subject)" -ForegroundColor DarkYellow
            }
        }
    }
    Write-Host "  Downloaded $($downloadedAttachments.Count) attachments" -ForegroundColor Green
}

# --- 3. Chat messages ---
$projectChats = @()
# Chats that matched the project, carried over to the transcripts module below.
$matchedChats = @{}
if ($ModuleSet["chats"]) {
    Write-Host ""
    Write-Host "=== Chat Messages ===" -ForegroundColor Cyan
    try {
        $allChats = Get-MgChat -All -ErrorAction Stop
        $topicChats = $allChats | Where-Object { $_.Topic -and (Test-ProjectMatch -Text $_.Topic) }
        $recentChats = $allChats | Sort-Object LastMessageDateTime -Descending | Select-Object -First $Cfg.window.chat_limit
        # NEVER pipe Graph objects through `Select-Object -Unique`: it dedups by the
        # object's string representation, and these all stringify to their type name,
        # so any number of distinct chats collapses to a single item. The -notin
        # filter above already guarantees uniqueness, so no dedup step is needed.
        $extraChats = @($recentChats | Where-Object { $_.Id -notin $topicChats.Id })
        $chatsToScan = @($topicChats) + $extraChats
        Write-Host "  Scanning $($chatsToScan.Count) chats (topic-matched: $($topicChats.Count) + recent: $($extraChats.Count))" -ForegroundColor DarkGray

        $scanned = 0
        foreach ($chat in $chatsToScan) {
            $scanned++
            if ($scanned % 10 -eq 0) { Write-Host "    Progress: $scanned/$($chatsToScan.Count)..." -ForegroundColor DarkGray }
            $chatTopic = if ($chat.Topic) { $chat.Topic } else { "(1:1 or group)" }
            if ($chat.Topic -and (Test-ProjectMatch -Text $chat.Topic)) { $matchedChats[$chat.Id] = $chatTopic }
            try {
                $messages = Get-MgChatMessage -ChatId $chat.Id -Top 20 -ErrorAction Stop
            } catch { continue }
            foreach ($msg in $messages) {
                $msgDate = [datetime]$msg.CreatedDateTime
                if ($msgDate -lt $Since) { continue }
                $msgText = if ($msg.Body -and $msg.Body.Content) { $msg.Body.Content -replace '<[^>]+>', ' ' } else { "" }
                $combined = "$chatTopic $msgText"
                if (Test-ProjectMatch -Text $combined) {
                    $from = if ($msg.From -and $msg.From.User) { $msg.From.User.DisplayName } else { "unknown" }
                    $matchedChats[$chat.Id] = $chatTopic
                    $projectChats += [PSCustomObject]@{
                        ChatId = $chat.Id
                        ChatTopic = $chatTopic
                        CreatedDateTime = $msg.CreatedDateTime
                        From = $from
                        Body = $msgText
                        Attachments = @($msg.Attachments)
                    }
                }
            }
        }
        Write-Host "  Found $($projectChats.Count) project-relevant chat messages" -ForegroundColor Green
    } catch {
        Write-Host "  Chats not available: $($_.Exception.Message.Substring(0, [Math]::Min(200, $_.Exception.Message.Length)))" -ForegroundColor DarkYellow
    }
}

# --- 3b. Chat file attachments ---
# Files posted in a chat are not inline bytes: the attachment carries
# contentType "reference" plus a contentUrl pointing at OneDrive/SharePoint.
# The bytes come from /shares/{shareId}/driveItem/content.
$chatAttachments = @()
$skippedChatAttachments = @()
if ($ModuleSet["chat_attachments"]) {
    Write-Host ""
    Write-Host "=== Chat Attachments ===" -ForegroundColor Cyan
    Write-Host "  NOTE: using Sites.Read.All, not Files.Read.All (see scope comments)." -ForegroundColor DarkGray
    Write-Host "        Files outside SharePoint/OneDrive reach will fail per-file, not abort the run." -ForegroundColor DarkGray
    if ($projectChats.Count -eq 0) {
        Write-Host "  No project chat messages to inspect (is the 'chats' module enabled?)" -ForegroundColor DarkYellow
    } else {
        $seenUrls = @{}
        foreach ($msg in $projectChats) {
            foreach ($att in $msg.Attachments) {
                if (-not $att) { continue }
                if ($att.ContentType -ne "reference" -or -not $att.ContentUrl) {
                    $skippedChatAttachments += [PSCustomObject]@{
                        ChatTopic = $msg.ChatTopic
                        Name = if ($att.Name) { $att.Name } else { "(unnamed)" }
                        ContentType = $att.ContentType
                    }
                    continue
                }
                if ($seenUrls[$att.ContentUrl]) { continue }
                $seenUrls[$att.ContentUrl] = $true

                $safeName = Get-SafeFileName -Name $att.Name -Fallback "chat-attachment"
                $localPath = Get-UniquePath -Path (Join-Path $ChatAttachDir $safeName)
                try {
                    $shareId = ConvertTo-GraphShareId -Url $att.ContentUrl
                    Invoke-MgGraphRequest -Method GET `
                        -Uri "https://graph.microsoft.com/v1.0/shares/$shareId/driveItem/content" `
                        -OutputFilePath $localPath -ErrorAction Stop
                    $size = (Get-Item $localPath).Length
                    $chatAttachments += [PSCustomObject]@{
                        ChatTopic = $msg.ChatTopic
                        From = $msg.From
                        Date = $msg.CreatedDateTime
                        Name = $att.Name
                        Size = $size
                        ContentUrl = $att.ContentUrl
                        LocalPath = $localPath
                    }
                    Write-Host "    Downloaded: $safeName ($size bytes)" -ForegroundColor DarkGray
                } catch {
                    if (Test-Path $localPath) { Remove-Item $localPath -Force -ErrorAction SilentlyContinue }
                    Write-Host "    Failed: $($att.Name) - $($_.Exception.Message.Substring(0, [Math]::Min(120, $_.Exception.Message.Length)))" -ForegroundColor DarkYellow
                    $skippedChatAttachments += [PSCustomObject]@{
                        ChatTopic = $msg.ChatTopic
                        Name = $att.Name
                        ContentType = "reference (download failed)"
                    }
                }
            }
        }
        Write-Host "  Downloaded $($chatAttachments.Count) chat attachments ($($skippedChatAttachments.Count) skipped)" -ForegroundColor Green
    }
}

# --- 3c. Meeting transcripts ---
# A meeting chat exposes onlineMeetingInfo.joinWebUrl; the meeting is resolved by
# filtering /me/onlineMeetings on that URL, then its transcripts are pulled as VTT.
$transcripts = @()
if ($ModuleSet["transcripts"]) {
    Write-Host ""
    Write-Host "=== Meeting Transcripts ===" -ForegroundColor Cyan
    if ($matchedChats.Count -eq 0) {
        Write-Host "  No project chats matched (is the 'chats' module enabled?)" -ForegroundColor DarkYellow
    } else {
        $meetingChats = 0
        foreach ($chatId in $matchedChats.Keys) {
            $chatTopic = $matchedChats[$chatId]
            try {
                $chatDetail = Invoke-MgGraphRequest -Method GET `
                    -Uri "https://graph.microsoft.com/v1.0/me/chats/$($chatId)?`$select=id,topic,chatType,onlineMeetingInfo" -ErrorAction Stop
            } catch { continue }

            $joinUrl = $chatDetail.onlineMeetingInfo.joinWebUrl
            if (-not $joinUrl) { continue }
            $meetingChats++

            try {
                $mtgResp = Invoke-MgGraphRequest -Method GET `
                    -Uri "https://graph.microsoft.com/v1.0/me/onlineMeetings?`$filter=JoinWebUrl eq '$joinUrl'" -ErrorAction Stop
            } catch {
                Write-Host "    Could not resolve meeting for: $chatTopic" -ForegroundColor DarkYellow
                continue
            }
            $meeting = if ($mtgResp.value) { $mtgResp.value[0] } else { $null }
            if (-not $meeting) { continue }

            try {
                $trResp = Invoke-MgGraphRequest -Method GET `
                    -Uri "https://graph.microsoft.com/v1.0/me/onlineMeetings/$($meeting.id)/transcripts" -ErrorAction Stop
            } catch {
                $emsg = $_.Exception.Message
                if ($emsg -match "GraphAccessToTranscriptsDisabled") {
                    Write-Host "  Transcripts are disabled tenant-wide by the administrator." -ForegroundColor DarkYellow
                    Write-Host "  Skipping meeting transcripts." -ForegroundColor DarkYellow
                    break
                }
                Write-Host "    No transcript access for: $chatTopic" -ForegroundColor DarkYellow
                continue
            }

            foreach ($tr in $trResp.value) {
                $created = [datetime]$tr.createdDateTime
                if ($created -lt $Since) { continue }
                $stamp = $created.ToString("yyyyMMdd-HHmmss")
                $safeTopic = Get-SafeFileName -Name $chatTopic -Fallback "meeting"
                $localPath = Get-UniquePath -Path (Join-Path $TranscriptDir "$stamp-$safeTopic.vtt")
                try {
                    Invoke-MgGraphRequest -Method GET `
                        -Uri "https://graph.microsoft.com/v1.0/me/onlineMeetings/$($meeting.id)/transcripts/$($tr.id)/content?`$format=text/vtt" `
                        -OutputFilePath $localPath -ErrorAction Stop
                    $size = (Get-Item $localPath).Length
                    $transcripts += [PSCustomObject]@{
                        ChatTopic = $chatTopic
                        MeetingId = $meeting.id
                        TranscriptId = $tr.id
                        Created = $tr.createdDateTime
                        Size = $size
                        LocalPath = $localPath
                    }
                    Write-Host "    Downloaded transcript: $stamp-$safeTopic.vtt ($size bytes)" -ForegroundColor DarkGray
                } catch {
                    if (Test-Path $localPath) { Remove-Item $localPath -Force -ErrorAction SilentlyContinue }
                    Write-Host "    Transcript download failed for: $chatTopic" -ForegroundColor DarkYellow
                }
            }
        }
        Write-Host "  Downloaded $($transcripts.Count) transcripts from $meetingChats meeting chats" -ForegroundColor Green
    }
}

# --- 4. Teams channel messages ---
$projectChannelMsgs = @()
if ($ModuleSet["teams_channels"]) {
    Write-Host ""
    Write-Host "=== Teams Channel Messages ===" -ForegroundColor Cyan
    try {
        $teams = Get-MgGroup -All | Where-Object {
            $_.ResourceProvisioningOptions -contains "Team" -and
            (Test-ProjectMatch -Text $_.DisplayName)
        }
        Write-Host "  Found $($teams.Count) project teams" -ForegroundColor DarkGray
        foreach ($team in $teams) {
            try { $channels = Get-MgTeamChannel -TeamId $team.Id -All -ErrorAction Stop } catch { continue }
            foreach ($channel in $channels) {
                try { $messages = Get-MgTeamChannelMessage -TeamId $team.Id -ChannelId $channel.Id -Top 50 -ErrorAction Stop } catch { continue }
                foreach ($msg in $messages) {
                    $msgDate = [datetime]$msg.CreatedDateTime
                    if ($msgDate -lt $Since) { continue }
                    $msgText = if ($msg.Body -and $msg.Body.Content) { $msg.Body.Content -replace '<[^>]+>', ' ' } else { "" }
                    $from = if ($msg.From -and $msg.From.User) { $msg.From.User.DisplayName } else { "unknown" }
                    $projectChannelMsgs += [PSCustomObject]@{
                        Team = $team.DisplayName
                        Channel = $channel.DisplayName
                        CreatedDateTime = $msg.CreatedDateTime
                        From = $from
                        Body = $msgText
                    }
                }
            }
        }
        Write-Host "  Found $($projectChannelMsgs.Count) project-relevant channel messages" -ForegroundColor Green
    } catch {
        Write-Host "  Teams channels require admin consent (ChannelMessage.Read.All)" -ForegroundColor DarkYellow
        Write-Host "  Skipping Teams channel messages" -ForegroundColor DarkYellow
    }
}

# --- 5. SharePoint files ---
$script:sharePointFiles = @()
if ($ModuleSet["sharepoint"] -and $Cfg.sharepoint.site_host) {
    Write-Host ""
    Write-Host "=== SharePoint Files ===" -ForegroundColor Cyan
    $spCfg = $Cfg.sharepoint
    try {
        $siteUri = "$($spCfg.site_host):$($spCfg.site_path):"
        Write-Host "  Resolving site: $($spCfg.site_host)$($spCfg.site_path)" -ForegroundColor DarkGray
        try {
            $site = Get-MgSite -SiteId $siteUri -ErrorAction Stop
        } catch {
            $siteResp = Invoke-MgGraphRequest -Method GET -Uri "https://graph.microsoft.com/v1.0/sites/$siteUri" -ErrorAction Stop
            $site = [PSCustomObject]@{ Id = $siteResp.id; DisplayName = $siteResp.displayName; WebUrl = $siteResp.webUrl }
        }
        Write-Host "  Site found: $($site.DisplayName)" -ForegroundColor Green

        try {
            $drive = Get-MgSiteDrive -SiteId $site.Id -ErrorAction Stop | Select-Object -First 1
        } catch {
            $driveResp = Invoke-MgGraphRequest -Method GET -Uri "https://graph.microsoft.com/v1.0/sites/$($site.Id)/drive" -ErrorAction Stop
            $drive = [PSCustomObject]@{ Id = $driveResp.id; Name = $driveResp.name }
        }
        Write-Host "  Drive: $($drive.Name)" -ForegroundColor DarkGray

        if (-not (Test-Path $SharePointDir)) { New-Item -ItemType Directory -Path $SharePointDir -Force | Out-Null }

        function Invoke-WalkSharePointFolder {
            param([string]$DriveId, [string]$FolderItemId, [string]$RelativePath, [int]$Depth = 0, [int]$MaxDepth = 3)
            if ($Depth -gt $MaxDepth) { return }
            try {
                $items = Get-MgDriveItemChild -DriveId $DriveId -DriveItemId $FolderItemId -ErrorAction Stop
            } catch {
                $itemsResp = Invoke-MgGraphRequest -Method GET -Uri "https://graph.microsoft.com/v1.0/drives/$DriveId/items/$FolderItemId/children" -ErrorAction SilentlyContinue
                if (-not $itemsResp -or -not $itemsResp.value) { return }
                $items = $itemsResp.value
            }
            foreach ($item in $items) {
                $itemRelPath = if ($RelativePath) { "$RelativePath/$($item.Name)" } else { $item.Name }
                $isFolder = $null -ne $item.Folder -and $null -ne $item.Folder.ChildCount
                $isFile = $item.File -and $item.File.MimeType
                if (-not $isFile -and -not $isFolder -and $item.Name -match '\.(xlsx|xls|docx|doc|pptx|ppt|pdf|csv|txt|md|png|jpg|jpeg|gif|zip|7z)$') {
                    $isFile = $true
                }
                if ($isFolder) {
                    Invoke-WalkSharePointFolder -DriveId $DriveId -FolderItemId $item.Id -RelativePath $itemRelPath -Depth ($Depth + 1) -MaxDepth $MaxDepth
                } elseif ($isFile) {
                    $localSubDir = if ($RelativePath) { Join-Path $SharePointDir (Split-Path -Parent $itemRelPath) } else { $SharePointDir }
                    if (-not (Test-Path $localSubDir)) { New-Item -ItemType Directory -Path $localSubDir -Force | Out-Null }
                    $localPath = Join-Path $localSubDir $item.Name
                    try {
                        Get-MgDriveItemContent -DriveId $DriveId -DriveItemId $item.Id -OutFile $localPath -ErrorAction Stop
                    } catch {
                        try { Invoke-MgGraphRequest -Method GET -Uri "https://graph.microsoft.com/v1.0/drives/$DriveId/items/$($item.Id)/content" -OutputFilePath $localPath -ErrorAction Stop }
                        catch { continue }
                    }
                    $script:sharePointFiles += [PSCustomObject]@{
                        Name = $item.Name; Size = $item.Size; SharePointPath = $itemRelPath; LocalPath = $localPath; LastModified = $item.LastModifiedDateTime
                    }
                    Write-Host "    Downloaded: $($item.Name) ($($item.Size) bytes)" -ForegroundColor DarkGray
                }
            }
        }

        $encodedFolderPath = "root:/$($spCfg.folder_path)"
        try {
            $targetItem = Get-MgDriveItem -DriveId $drive.Id -DriveItemId $encodedFolderPath -ErrorAction Stop
        } catch {
            $rootItems = Get-MgDriveItemChild -DriveId $drive.Id -DriveItemId "root" -ErrorAction Stop
            $targetItem = $rootItems | Where-Object { $_.Folder -and $_.Name -eq $spCfg.folder_path } | Select-Object -First 1
            if (-not $targetItem) { throw "Could not find folder: $($spCfg.folder_path)" }
        }
        Invoke-WalkSharePointFolder -DriveId $drive.Id -FolderItemId $targetItem.Id -RelativePath "" -Depth 0 -MaxDepth $spCfg.max_depth
        Write-Host "  Downloaded $($script:sharePointFiles.Count) files from SharePoint" -ForegroundColor Green
    } catch {
        Write-Host "  SharePoint skipped: $($_.Exception.Message.Substring(0, [Math]::Min(200, $_.Exception.Message.Length)))" -ForegroundColor DarkYellow
    }
}

# --- 6. Build markdown ---
Write-Host ""
Write-Host "=== Building output ===" -ForegroundColor Cyan

$lines = @()
$lines += "# Graph Fetch $Timestamp"
$lines += ""
$lines += "**Project:** $ProjectName"
$lines += "**Fetched:** $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
$lines += "**User:** $($Me.Account)"
$lines += "**Window:** last $($Cfg.window.hours) hours (since $SinceIso)"
$lines += "**Keywords:** $($Keywords -join ', ')"
$lines += "**Modules:** $($ModuleSet.Keys -join ', ')"
$lines += ""
$lines += "---"
$lines += ""

if ($ModuleSet["emails"]) {
    $lines += "## Emails ($($projectEmails.Count))"
    $lines += ""
    foreach ($e in $projectEmails) {
        $subject = if ($e.Subject) { $e.Subject } else { "(no subject)" }
        $received = if ($e.ReceivedDateTime) { $e.ReceivedDateTime } else { "" }
        $sender = if ($e.From -and $e.From.EmailAddress) { $e.From.EmailAddress.Address } else { "unknown" }
        $preview = if ($e.BodyPreview) { ($e.BodyPreview.Substring(0, [Math]::Min(300, $e.BodyPreview.Length)) -replace $nl, " ") } else { "" }
        $hasAtt = if ($e.HasAttachments) { "Yes" } else { "No" }
        $lines += "### $subject"
        $lines += "- **From:** $sender"
        $lines += "- **Received:** $received"
        $lines += "- **Attachments:** $hasAtt"
        $lines += "- **Preview:** $preview"
        $lines += ""
    }
    $lines += "---"
    $lines += ""
}

if ($ModuleSet["chats"]) {
    $lines += "## Chat Messages ($($projectChats.Count))"
    $lines += ""
    foreach ($msg in $projectChats) {
        $bodyShort = if ($msg.Body) { ($msg.Body.Substring(0, [Math]::Min(500, $msg.Body.Length)) -replace $nl, " ") } else { "" }
        $lines += "### $($msg.ChatTopic)"
        $lines += "- **From:** $($msg.From)"
        $lines += "- **Date:** $($msg.CreatedDateTime)"
        if ($msg.Attachments -and $msg.Attachments.Count -gt 0) {
            $attNames = ($msg.Attachments | ForEach-Object { if ($_.Name) { $_.Name } else { $_.ContentType } }) -join ", "
            $lines += "- **Attachments:** $attNames"
        }
        $lines += "- **Body:** $bodyShort"
        $lines += ""
    }
    $lines += "---"
    $lines += ""
}

if ($ModuleSet["chat_attachments"]) {
    $lines += "## Chat Attachments ($($chatAttachments.Count))"
    $lines += ""
    foreach ($att in $chatAttachments) {
        $relLocal = $att.LocalPath -replace [regex]::Escape($RepoRoot + "/"), ""
        $sizeFormatted = if ($att.Size -ge 1MB) { "{0:N2} MB" -f ($att.Size / 1MB) } elseif ($att.Size -ge 1KB) { "{0:N1} KB" -f ($att.Size / 1KB) } else { "$($att.Size) bytes" }
        $lines += "### $($att.Name)"
        $lines += "- **Chat:** $($att.ChatTopic)"
        $lines += "- **From:** $($att.From)"
        $lines += "- **Date:** $($att.Date)"
        $lines += "- **Size:** $sizeFormatted"
        $lines += "- **Local path:** $relLocal"
        $lines += ""
    }
    if ($skippedChatAttachments.Count -gt 0) {
        $lines += "### Skipped ($($skippedChatAttachments.Count))"
        $lines += ""
        $lines += "Not downloadable as files (cards, code snippets, quoted messages, or failed fetches)."
        $lines += ""
        foreach ($s in $skippedChatAttachments) {
            $lines += "- **$($s.Name)** - ``$($s.ContentType)`` - in: $($s.ChatTopic)"
        }
        $lines += ""
    }
    $lines += "---"
    $lines += ""
}

if ($ModuleSet["transcripts"]) {
    $lines += "## Meeting Transcripts ($($transcripts.Count))"
    $lines += ""
    if ($transcripts.Count -gt 0) {
        foreach ($t in $transcripts) {
            $relLocal = $t.LocalPath -replace [regex]::Escape($RepoRoot + "/"), ""
            $sizeFormatted = if ($t.Size -ge 1KB) { "{0:N1} KB" -f ($t.Size / 1KB) } else { "$($t.Size) bytes" }
            $lines += "### $($t.ChatTopic)"
            $lines += "- **Created:** $($t.Created)"
            $lines += "- **Size:** $sizeFormatted"
            $lines += "- **Local path:** $relLocal"
            $lines += ""
        }
    } else {
        $lines += "No transcripts retrieved."
        $lines += ""
        $lines += "Requires the OnlineMeetingTranscript.Read.All delegated scope (Entra ID admin"
        $lines += "consent) and tenant-level Graph access to transcripts to be enabled."
        $lines += ""
    }
    $lines += "---"
    $lines += ""
}

if ($ModuleSet["teams_channels"]) {
    if ($projectChannelMsgs.Count -gt 0) {
        $lines += "## Teams Channel Messages ($($projectChannelMsgs.Count))"
        $lines += ""
        foreach ($msg in $projectChannelMsgs) {
            $bodyShort = if ($msg.Body) { ($msg.Body.Substring(0, [Math]::Min(500, $msg.Body.Length)) -replace $nl, " ") } else { "" }
            $lines += "### $($msg.Team) > $($msg.Channel)"
            $lines += "- **From:** $($msg.From)"
            $lines += "- **Date:** $($msg.CreatedDateTime)"
            $lines += "- **Body:** $bodyShort"
            $lines += ""
        }
    } else {
        $lines += "## Teams Channel Messages (skipped - requires admin consent)"
        $lines += ""
        $lines += "ChannelMessage.Read.All scope needs Entra ID admin consent."
        $lines += "Use m365-copilot.py for Teams channel content."
        $lines += ""
    }
    $lines += "---"
    $lines += ""
}

if ($ModuleSet["sharepoint"]) {
    $lines += "## SharePoint Files ($($script:sharePointFiles.Count))"
    $lines += ""
    if ($script:sharePointFiles.Count -gt 0) {
        $lines += "**Source:** $($Cfg.sharepoint.site_host)$($Cfg.sharepoint.site_path) > $($Cfg.sharepoint.folder_path)"
        $lines += ""
        foreach ($f in $script:sharePointFiles) {
            $relLocal = $f.LocalPath -replace [regex]::Escape($RepoRoot + "/"), ""
            $sizeFormatted = if ($f.Size -ge 1MB) { "{0:N2} MB" -f ($f.Size / 1MB) } elseif ($f.Size -ge 1KB) { "{0:N1} KB" -f ($f.Size / 1KB) } else { "$($f.Size) bytes" }
            $lines += "### $($f.Name)"
            $lines += "- **Size:** $sizeFormatted"
            $lines += "- **SharePoint path:** $($f.SharePointPath)"
            $lines += "- **Local path:** $relLocal"
            $lines += "- **Last modified:** $($f.LastModified)"
            $lines += ""
        }
    } else {
        $lines += "No files downloaded."
        $lines += ""
    }
    $lines += "---"
    $lines += ""
}

if ($ModuleSet["attachments"]) {
    $lines += "## Downloaded Attachments ($($downloadedAttachments.Count))"
    $lines += ""
    foreach ($att in $downloadedAttachments) {
        $relPath = $att.Path -replace [regex]::Escape($RepoRoot + "/"), ""
        $lines += "- **$($att.Name)** ($($att.Size) bytes) - from: $($att.EmailSubject)"
        $lines += "  - Path: $relPath"
        $lines += ""
    }
}

$output = $lines -join $nl
$output | Out-File -FilePath $OutputFile -Encoding utf8 -NoNewline

Write-Host ""
Write-Host "=== Summary ===" -ForegroundColor Green
if ($ModuleSet["emails"])        { Write-Host "  Emails:        $($projectEmails.Count)" -ForegroundColor White }
if ($ModuleSet["chats"])         { Write-Host "  Chat msgs:     $($projectChats.Count)" -ForegroundColor White }
if ($ModuleSet["chat_attachments"]) { Write-Host "  Chat files:    $($chatAttachments.Count) ($($skippedChatAttachments.Count) skipped)" -ForegroundColor White }
if ($ModuleSet["transcripts"])   { Write-Host "  Transcripts:   $($transcripts.Count)" -ForegroundColor White }
if ($ModuleSet["teams_channels"]) { Write-Host "  Channel msgs:  $($projectChannelMsgs.Count)" -ForegroundColor White }
if ($ModuleSet["sharepoint"])    { Write-Host "  SharePoint:    $($script:sharePointFiles.Count) files" -ForegroundColor White }
if ($ModuleSet["attachments"])   { Write-Host "  Attachments:   $($downloadedAttachments.Count)" -ForegroundColor White }
Write-Host "  Output:        $OutputFile" -ForegroundColor Yellow
