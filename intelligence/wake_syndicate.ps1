# wake_syndicate.ps1 -- Syndicate TC Intelligence Gate
# Polls triggers/ every 500ms. Three code paths (priority order):
#
#   1. pending_signal.json    -- 6-agent panel flow (existing)
#   2. {name}_signal.json     -- Agent flow: TC decides BUY/PASS as the agent
#   3. {name}_postmortem.json -- Postmortem flow: TC extracts a lesson
#
# Agent flow output : triggers/{name}_decision.json  (read by main.py)
# Postmortem output : triggers/{name}_lesson.json    (read by update_memory.py)
#
# Usage: Run once at Syndicate startup (via start_syndicate.bat).

param(
    [string]$SyndicateRoot = (Split-Path -Parent $PSScriptRoot)
)

$TriggersDir            = Join-Path $SyndicateRoot "triggers"
$IntelDir               = Join-Path $SyndicateRoot "intelligence"
$PromptsDir             = Join-Path $SyndicateRoot "tools\prompts"
$PanelPromptPath        = Join-Path $IntelDir "prompts\panel_prompt.txt"
$BaseDecisionPromptPath = Join-Path $PromptsDir "base_decision_prompt.txt"
$PostmortemPromptPath   = Join-Path $PromptsDir "postmortem_prompt.txt"
$PendingPath            = Join-Path $TriggersDir "pending_signal.json"
$AnalysisPath           = Join-Path $IntelDir "tc_analysis.txt"
$ParseScriptPath        = Join-Path $IntelDir "parse_decision.py"

# Ensure triggers dir exists
if (-not (Test-Path $TriggersDir)) {
    New-Item -ItemType Directory -Path $TriggersDir | Out-Null
}

# Load all three prompt templates at startup -- fail fast if any missing
foreach ($P in @($PanelPromptPath, $BaseDecisionPromptPath, $PostmortemPromptPath)) {
    if (-not (Test-Path $P)) {
        Write-Error ("[Syndicate Gate] Prompt not found: " + $P)
        exit 1
    }
}

$PanelPrompt        = Get-Content $PanelPromptPath        -Raw -Encoding UTF8
$BaseDecisionPrompt = Get-Content $BaseDecisionPromptPath -Raw -Encoding UTF8
$PostmortemPrompt   = Get-Content $PostmortemPromptPath   -Raw -Encoding UTF8

Write-Host "[Syndicate Gate] Started. Watching $TriggersDir for signals..."
Write-Host "[Syndicate Gate] Root: $SyndicateRoot"

# ---------------------------------------------------------------------------
# Helper: post a message to Syndicate Telegram channel (fire-and-forget)
# ---------------------------------------------------------------------------
function Post-Telegram {
    param([string]$Message, [string]$Emoji = "🎯")
    $Token  = "8660314239:AAEwRmlfwR5GzYanGmKpFR51208gfLrrqLw"
    $ChatId = "1560090178"
    $Url    = "https://api.telegram.org/bot$Token/sendMessage"
    try {
        $Body = @{ chat_id = $ChatId; text = "$Emoji SYNDICATE | $Message" } | ConvertTo-Json -Compress
        Invoke-RestMethod -Uri $Url -Method Post -Body $Body -ContentType "application/json" -TimeoutSec 5 | Out-Null
    } catch { }
}

# ---------------------------------------------------------------------------
# Helper: post a message to Syndicate Discord channel (fire-and-forget)
# ---------------------------------------------------------------------------
function Post-Discord {
    param([string]$Message, [string]$Emoji = "🎯")
    $Webhook = "https://discordapp.com/api/webhooks/1492550062298108056/J3F6tVddXjPd6YMDBdiFpzU53c4bxjBrY7v99PIXiEuNcHFBQC-9Rhh7fzXvLK1ZxCMO"
    try {
        $Body = @{ content = "$Emoji **SYNDICATE** | $Message" } | ConvertTo-Json -Compress
        Invoke-RestMethod -Uri $Webhook -Method Post -Body $Body -ContentType "application/json" -TimeoutSec 5 | Out-Null
    } catch { }
}

# ---------------------------------------------------------------------------
# Helper: extract TC response text from Claude --output-format json envelope
# ---------------------------------------------------------------------------
function Get-TcText {
    param([string[]]$ClaudeOutput)
    $Text = $null
    foreach ($Line in $ClaudeOutput) {
        $Line = "$Line".Trim()
        if ($Line.StartsWith("{")) {
            try {
                $Obj = $Line | ConvertFrom-Json
                $resultProp = $Obj.PSObject.Properties["result"]
                if ($Obj.type -eq "result" -and $resultProp) {
                    $Text = $Obj.result
                }
            } catch { }
        }
    }
    if (-not $Text) {
        $Text = ($ClaudeOutput | Out-String).Trim()
        Write-Warning "[Syndicate Gate] Could not parse Claude JSON envelope -- using raw output"
    }
    return $Text
}

# ---------------------------------------------------------------------------
# Helper: extract first complete JSON object using brace-depth counting.
# Handles nested objects correctly; stops at the first balanced closing brace.
# ---------------------------------------------------------------------------
function Get-FirstJson {
    param([string]$Text)
    $Depth = 0
    $Start = -1
    for ($i = 0; $i -lt $Text.Length; $i++) {
        $Ch = $Text[$i]
        if ($Ch -eq "{") {
            if ($Depth -eq 0) { $Start = $i }
            $Depth++
        } elseif ($Ch -eq "}") {
            $Depth--
            if ($Depth -eq 0 -and $Start -ge 0) {
                return $Text.Substring($Start, $i - $Start + 1)
            }
        }
    }
    return $null
}

# ---------------------------------------------------------------------------
# Helper: safely convert a value to JSON; returns "[]" if null/missing.
# Prevents prompt injection of literal "null" when a field is absent.
# ---------------------------------------------------------------------------
function ConvertTo-JsonSafe {
    param($Value, [string]$Default = "[]")
    if ($null -eq $Value) { return $Default }
    return $Value | ConvertTo-Json -Depth 10 -Compress
}

# ---------------------------------------------------------------------------
# Helper: invoke TC claude CLI, return extracted response text or $null
# ---------------------------------------------------------------------------
function Invoke-TC {
    param([string]$Prompt, [string]$Label)
    Write-Host ("[Syndicate Gate] Invoking TC for " + $Label + " (15-30s)...")
    $Output = $null
    try {
        $Output = & claude --print --output-format json -p $Prompt 2>&1
    } catch {
        Write-Warning ("[Syndicate Gate] claude CLI failed for " + $Label + ": $_")
        return $null
    }
    return Get-TcText -ClaudeOutput $Output
}

# ---------------------------------------------------------------------------
# Code path 2: Agent signal flow -- {name}_signal.json
#
# TC is prompted as the specific agent (ACE/AXIOM/DIAMOND) and responds with:
#   { "decision": "BUY|PASS", "side": "yes|no", "conviction": 1-5, ... }
# Written to triggers/{name}_decision.json for main.py to act on.
# ---------------------------------------------------------------------------
function Invoke-AgentSignal {
    param([System.IO.FileInfo]$File)

    $SignalPath = $File.FullName
    $AgentName  = ($File.Name -replace "_signal\.json$", "").ToUpper()

    Write-Host ("[Syndicate Gate] Agent signal: " + $File.Name + " | agent=" + $AgentName)

    # Read and parse signal file
    $Raw = $null
    $Obj = $null
    try {
        $Raw = Get-Content $SignalPath -Raw -Encoding UTF8
        $Obj = $Raw | ConvertFrom-Json
    } catch {
        Write-Warning ("[Syndicate Gate] Cannot read agent signal " + $File.Name + ": $_")
        Remove-Item $SignalPath -Force -ErrorAction SilentlyContinue
        return
    }

    # Check expiry before calling TC
    $ExpiresAt = $null
    try {
        $ExpiresAt = [DateTime]::Parse($Obj.expires_at).ToUniversalTime()
    } catch {
        Write-Warning "[Syndicate Gate] Cannot parse expires_at in agent signal -- dropping"
        Remove-Item $SignalPath -Force -ErrorAction SilentlyContinue
        return
    }
    if ([DateTime]::UtcNow -gt $ExpiresAt) {
        Write-Host ("[Syndicate Gate] Agent signal expired (" + $AgentName + ") -- dropping")
        Remove-Item $SignalPath -Force -ErrorAction SilentlyContinue
        return
    }

    # Inject memory and signal into base_decision_prompt.txt
    $MemoryRulesJson  = ConvertTo-JsonSafe $Obj.memory_rules
    $RecentTradesJson = ConvertTo-JsonSafe $Obj.recent_trades
    $SignalJson       = ConvertTo-JsonSafe $Obj.signal -Default "{}"

    $Prompt = $BaseDecisionPrompt
    $Prompt = $Prompt.Replace("{AGENT_NAME}", $AgentName)
    $Prompt = $Prompt.Replace("{MEMORY_RULES_INJECTED_HERE}", $MemoryRulesJson)
    $Prompt = $Prompt.Replace("{RECENT_TRADES_INJECTED_HERE}", $RecentTradesJson)
    $Prompt = $Prompt.Replace("{SIGNAL_JSON_INJECTED_HERE}", $SignalJson)

    # Call TC
    $TcText = Invoke-TC -Prompt $Prompt -Label $AgentName
    if ([string]::IsNullOrWhiteSpace($TcText)) {
        Write-Warning ("[Syndicate Gate] TC empty response for agent " + $AgentName + " -- dropping")
        Remove-Item $SignalPath -Force -ErrorAction SilentlyContinue
        return
    }

    # Extract the JSON decision TC responded with
    $DecisionJson = Get-FirstJson -Text $TcText
    if (-not $DecisionJson) {
        Write-Warning ("[Syndicate Gate] No JSON in TC response for " + $AgentName + " -- dropping")
        Set-Content -Path $AnalysisPath -Value $TcText -Encoding UTF8
        Remove-Item $SignalPath -Force -ErrorAction SilentlyContinue
        return
    }

    # Write decision file for main.py
    $DecisionPath = Join-Path $TriggersDir ($AgentName.ToLower() + "_decision.json")
    try {
        Set-Content -Path $DecisionPath -Value $DecisionJson -Encoding UTF8
        Write-Host ("[Syndicate Gate] Decision written: " + ($AgentName.ToLower() + "_decision.json"))
        $DecisionObj = $DecisionJson | ConvertFrom-Json -ErrorAction SilentlyContinue
        $DecisionStr = if ($DecisionObj -and $DecisionObj.PSObject.Properties["decision"]) { $DecisionObj.decision } else { "?" }
        $SignalTicker = if ($Obj.signal -and $Obj.signal.PSObject.Properties["ticker"]) { $Obj.signal.ticker } else { "?" }
        Post-Discord "TC Decision: $AgentName $SignalTicker → $DecisionStr"
        Post-Telegram "TC: $AgentName $SignalTicker → $DecisionStr"
    } catch {
        Write-Warning ("[Syndicate Gate] Failed to write decision file: $_")
    }

    # Audit trail
    Set-Content -Path $AnalysisPath -Value $TcText -Encoding UTF8

    Remove-Item $SignalPath -Force -ErrorAction SilentlyContinue
    Write-Host ("[Syndicate Gate] Agent flow done for " + $AgentName + ".")
}

# ---------------------------------------------------------------------------
# Code path 3: Postmortem flow -- {name}_postmortem.json
#
# TC is given the outcome and current rules and responds with:
#   { "lesson": "...", "new_rule": "...", "modify_rule_index": null|int,
#     "modified_rule": null|str }
# Written to triggers/{name}_lesson.json for update_memory.py.
# ---------------------------------------------------------------------------
function Invoke-Postmortem {
    param([System.IO.FileInfo]$File)

    $PostmortemPath = $File.FullName
    $AgentName      = ($File.Name -replace "_postmortem\.json$", "").ToUpper()

    Write-Host ("[Syndicate Gate] Postmortem: " + $File.Name + " | agent=" + $AgentName)

    # Read and parse postmortem file
    $Raw = $null
    $Obj = $null
    try {
        $Raw = Get-Content $PostmortemPath -Raw -Encoding UTF8
        $Obj = $Raw | ConvertFrom-Json
    } catch {
        Write-Warning ("[Syndicate Gate] Cannot read postmortem " + $File.Name + ": $_")
        Remove-Item $PostmortemPath -Force -ErrorAction SilentlyContinue
        return
    }

    # Inject outcome and memory rules into postmortem_prompt.txt
    $OutcomeJson     = ConvertTo-JsonSafe $Obj.outcome      -Default "{}"
    $MemoryRulesJson = ConvertTo-JsonSafe $Obj.memory_rules

    $Prompt = $PostmortemPrompt
    $Prompt = $Prompt.Replace("{AGENT_NAME}", $AgentName)
    $Prompt = $Prompt.Replace("{OUTCOME_JSON}", $OutcomeJson)
    $Prompt = $Prompt.Replace("{MEMORY_RULES}", $MemoryRulesJson)

    # Call TC
    $Label  = "postmortem-" + $AgentName
    $TcText = Invoke-TC -Prompt $Prompt -Label $Label
    if ([string]::IsNullOrWhiteSpace($TcText)) {
        Write-Warning ("[Syndicate Gate] TC empty response for postmortem " + $AgentName + " -- dropping")
        Remove-Item $PostmortemPath -Force -ErrorAction SilentlyContinue
        return
    }

    # Extract JSON lesson
    $LessonJson = Get-FirstJson -Text $TcText
    if (-not $LessonJson) {
        Write-Warning ("[Syndicate Gate] No JSON in TC postmortem response for " + $AgentName + " -- dropping")
        Remove-Item $PostmortemPath -Force -ErrorAction SilentlyContinue
        return
    }

    # Write lesson file then immediately apply it via update_memory.py
    $LessonPath       = Join-Path $TriggersDir ($AgentName.ToLower() + "_lesson.json")
    $UpdateMemoryPath = Join-Path $IntelDir "update_memory.py"
    try {
        Set-Content -Path $LessonPath -Value $LessonJson -Encoding UTF8
        Write-Host ("[Syndicate Gate] Lesson written: " + ($AgentName.ToLower() + "_lesson.json"))
    } catch {
        Write-Warning ("[Syndicate Gate] Failed to write lesson file: $_")
        Remove-Item $PostmortemPath -Force -ErrorAction SilentlyContinue
        return
    }

    # update_memory.py reads the lesson, updates memory/{name}.json, deletes lesson file
    try {
        & python $UpdateMemoryPath $LessonPath 2>&1 | Write-Host
    } catch {
        Write-Warning ("[Syndicate Gate] update_memory.py failed: $_")
    }

    Remove-Item $PostmortemPath -Force -ErrorAction SilentlyContinue
    Write-Host ("[Syndicate Gate] Postmortem done for " + $AgentName + ".")
}

# ---------------------------------------------------------------------------
# Main loop -- priority: panel > agent_signal > postmortem
# ---------------------------------------------------------------------------
while ($true) {
    Start-Sleep -Milliseconds 500

    # ── Code path 1: Panel flow (pending_signal.json) ─────────────────────────
    if (Test-Path $PendingPath) {
        $SignalRaw = $null
        try {
            $SignalRaw = Get-Content $PendingPath -Raw -Encoding UTF8
        } catch {
            Write-Warning "[Syndicate Gate] Could not read pending_signal.json: $_"
            continue
        }

        $SignalObj = $null
        try {
            $SignalObj = $SignalRaw | ConvertFrom-Json
        } catch {
            Write-Warning "[Syndicate Gate] Invalid JSON in pending_signal.json -- dropping"
            Remove-Item $PendingPath -Force -ErrorAction SilentlyContinue
            continue
        }

        $ExpiresAt = $null
        try {
            $ExpiresAt = [DateTime]::Parse($SignalObj.expires_at).ToUniversalTime()
        } catch {
            Write-Warning "[Syndicate Gate] Could not parse expires_at in panel signal -- dropping"
            Remove-Item $PendingPath -Force -ErrorAction SilentlyContinue
            continue
        }

        if ([DateTime]::UtcNow -gt $ExpiresAt) {
            $Ticker = $SignalObj.signal.ticker
            Write-Host ("[Syndicate Gate] Panel signal expired (" + $Ticker + ") -- dropping")
            Remove-Item $PendingPath -Force -ErrorAction SilentlyContinue
            continue
        }

        $Tier   = $SignalObj.signal.conviction_tier
        $Ticker = $SignalObj.signal.ticker
        $Class  = $SignalObj.signal.contract_class
        Write-Host ("[Syndicate Gate] Panel signal: " + $Ticker + " | " + $Tier + " | " + $Class + " | expires " + $SignalObj.expires_at)

        $FullPrompt = $PanelPrompt + "`n`n---SIGNAL---`n`n" + $SignalRaw
        $TcText = Invoke-TC -Prompt $FullPrompt -Label ("panel:" + $Ticker)

        if ([string]::IsNullOrWhiteSpace($TcText)) {
            Write-Warning "[Syndicate Gate] TC empty response for panel -- dropping"
            Remove-Item $PendingPath -Force -ErrorAction SilentlyContinue
            continue
        }

        Set-Content -Path $AnalysisPath -Value $TcText -Encoding UTF8
        Write-Host ("[Syndicate Gate] TC analysis written (" + $TcText.Length + " chars). Running parser...")

        try {
            & python $ParseScriptPath $PendingPath 2>&1 | Write-Host
        } catch {
            Write-Warning "[Syndicate Gate] parse_decision.py failed: $_"
        }

        Remove-Item $PendingPath -Force -ErrorAction SilentlyContinue
        Write-Host ("[Syndicate Gate] Panel flow done for " + $Ticker + ".")
        continue
    }

    # ── Code path 2: Agent signal flow -- {name}_signal.json ─────────────────
    $AgentSignalFiles = Get-ChildItem -Path $TriggersDir -Filter "*_signal.json" `
        -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne "pending_signal.json" }
    if ($AgentSignalFiles) {
        Invoke-AgentSignal -File $AgentSignalFiles[0]
        continue
    }

    # ── Code path 3: Postmortem flow -- {name}_postmortem.json ───────────────
    $PostmortemFiles = Get-ChildItem -Path $TriggersDir -Filter "*_postmortem.json" `
        -ErrorAction SilentlyContinue
    if ($PostmortemFiles) {
        Invoke-Postmortem -File $PostmortemFiles[0]
        continue
    }
}
