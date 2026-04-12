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
$ExitPromptPath         = Join-Path $PromptsDir "exit_prompt.txt"
$PendingPath            = Join-Path $TriggersDir "pending_signal.json"
$AnalysisPath           = Join-Path $IntelDir "tc_analysis.txt"
$ParseScriptPath        = Join-Path $IntelDir "parse_decision.py"

# Ensure triggers dir exists
if (-not (Test-Path $TriggersDir)) {
    New-Item -ItemType Directory -Path $TriggersDir | Out-Null
}

# Load all three prompt templates at startup -- fail fast if any missing
foreach ($P in @($PanelPromptPath, $BaseDecisionPromptPath, $PostmortemPromptPath, $ExitPromptPath)) {
    if (-not (Test-Path $P)) {
        Write-Error ("[Syndicate Gate] Prompt not found: " + $P)
        exit 1
    }
}

$PanelPrompt        = Get-Content $PanelPromptPath        -Raw -Encoding UTF8
$BaseDecisionPrompt = Get-Content $BaseDecisionPromptPath -Raw -Encoding UTF8
$PostmortemPrompt   = Get-Content $PostmortemPromptPath   -Raw -Encoding UTF8
$ExitPrompt         = Get-Content $ExitPromptPath         -Raw -Encoding UTF8

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
        # Write prompt to temp file — avoids Windows CLI arg length limit / newline truncation
        $TmpFile = [System.IO.Path]::GetTempFileName()
        [System.IO.File]::WriteAllText($TmpFile, $Prompt, [System.Text.Encoding]::UTF8)
        $Output = Get-Content $TmpFile -Raw | & claude --print --output-format json 2>&1
        Remove-Item $TmpFile -Force -ErrorAction SilentlyContinue
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

    # ── Debate: cross-agent context for HIGH_CONVICTION signals ──────────────
    # Check if any other agent has evaluated the same ticker in the last 5 min.
    # Inject their signal reasoning as "Other agent views" so TC has cross-agent context.
    $OtherAgentViews = ""
    $SignalTicker2 = if ($Obj.signal -and $Obj.signal.PSObject.Properties["ticker"]) { $Obj.signal.ticker } else { "" }
    $ConvictionTier = if ($Obj.signal -and $Obj.signal.PSObject.Properties["conviction_tier"]) { $Obj.signal.conviction_tier } else { "" }
    if ($ConvictionTier -eq "HIGH_CONVICTION" -and $SignalTicker2 -ne "") {
        $FiveMinAgo = (Get-Date).AddMinutes(-5)
        $OtherSignalFiles = Get-ChildItem -Path $TriggersDir -Filter "*_signal.json" `
            -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -ne $File.Name -and $_.LastWriteTime -ge $FiveMinAgo }
        $OtherViews = @()
        foreach ($OtherFile in $OtherSignalFiles) {
            try {
                $OtherRaw = Get-Content $OtherFile.FullName -Raw -Encoding UTF8
                $OtherObj = $OtherRaw | ConvertFrom-Json
                $OtherTicker = if ($OtherObj.signal -and $OtherObj.signal.PSObject.Properties["ticker"]) { $OtherObj.signal.ticker } else { "" }
                if ($OtherTicker -eq $SignalTicker2) {
                    $OtherAgent = ($OtherFile.Name -replace "_signal\.json$", "").ToUpper()
                    $OtherSide  = if ($OtherObj.signal -and $OtherObj.signal.PSObject.Properties["side"]) { $OtherObj.signal.side } else { "?" }
                    $OtherEdge  = if ($OtherObj.signal -and $OtherObj.signal.PSObject.Properties["edge_pct"]) { $OtherObj.signal.edge_pct } else { "?" }
                    $OtherReas  = if ($OtherObj.signal -and $OtherObj.signal.PSObject.Properties["reasoning"]) { $OtherObj.signal.reasoning } else { "no reasoning" }
                    $OtherViews += "$OtherAgent says $OtherSide (edge=$OtherEdge%): $OtherReas"
                }
            } catch { }
        }
        if ($OtherViews.Count -gt 0) {
            $OtherAgentViews = "`n`n============================================================`nOTHER AGENT VIEWS ON $SignalTicker2 (last 5 min)`n============================================================`n" + ($OtherViews -join "`n")
        }
    }

    # ── SAGE briefing + ECHO warning for this signal ─────────────────────────
    $SignalYesPrice = if ($Obj.signal -and $Obj.signal.PSObject.Properties["entry_price"]) { $Obj.signal.entry_price } else { 0.5 }
    $SignalClass2   = if ($Obj.signal -and $Obj.signal.PSObject.Properties["contract_class"]) { $Obj.signal.contract_class } else { "SCALP" }

    $SageBriefing = "SAGE: Unavailable."
    $EchoWarning  = "ECHO: Unavailable."
    try {
        $SagePy = Join-Path $SyndicateRoot "tools\get_sage_briefing.py"
        if (Test-Path $SagePy) {
            $SageBriefing = (& python $SagePy --ticker $SignalTicker2 --yes_price $SignalYesPrice --class_ $SignalClass2 2>$null) -join ""
        }
    } catch { }
    try {
        $EchoPy = Join-Path $SyndicateRoot "tools\get_echo_warning.py"
        if (Test-Path $EchoPy) {
            $EchoWarning = (& python $EchoPy --ticker $SignalTicker2 --agent $AgentName --yes_price $SignalYesPrice 2>$null) -join ""
        }
    } catch { }

    $Prompt = $BaseDecisionPrompt
    $Prompt = $Prompt.Replace("{AGENT_NAME}", $AgentName)
    $Prompt = $Prompt.Replace("{ECHO_WARNING}", $EchoWarning)
    $Prompt = $Prompt.Replace("{SAGE_BRIEFING}", $SageBriefing)
    $Prompt = $Prompt.Replace("{MEMORY_RULES_INJECTED_HERE}", $MemoryRulesJson)
    $Prompt = $Prompt.Replace("{RECENT_TRADES_INJECTED_HERE}", $RecentTradesJson)
    $Prompt = $Prompt.Replace("{SIGNAL_JSON_INJECTED_HERE}", $SignalJson)
    if ($OtherAgentViews -ne "") {
        $Prompt = $Prompt + $OtherAgentViews
    }

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
    # Inject signal fields — TC may echo the other side; always use ours.
    $SignalTicker    = if ($Obj.signal -and $Obj.signal.PSObject.Properties["ticker"])         { $Obj.signal.ticker }         else { "?" }
    $SignalClass     = if ($Obj.signal -and $Obj.signal.PSObject.Properties["contract_class"]) { $Obj.signal.contract_class } else { "SWING" }
    $SignalAgentName = if ($Obj.signal -and $Obj.signal.PSObject.Properties["agent_name"])     { $Obj.signal.agent_name }     else { $AgentName }
    $SignalStopPrice = if ($Obj.signal -and $Obj.signal.PSObject.Properties["stop_price"])     { $Obj.signal.stop_price }     else { $null }
    $SignalEdgePct   = if ($Obj.signal -and $Obj.signal.PSObject.Properties["edge_pct"])       { $Obj.signal.edge_pct }       else { 0.0 }
    try {
        $DecisionParsed = $DecisionJson | ConvertFrom-Json -ErrorAction SilentlyContinue
        if ($DecisionParsed) {
            $DecisionParsed | Add-Member -NotePropertyName "ticker"         -NotePropertyValue $SignalTicker    -Force
            $DecisionParsed | Add-Member -NotePropertyName "contract_class" -NotePropertyValue $SignalClass     -Force
            $DecisionParsed | Add-Member -NotePropertyName "agent_name"     -NotePropertyValue $SignalAgentName -Force
            $DecisionParsed | Add-Member -NotePropertyName "edge_pct"       -NotePropertyValue $SignalEdgePct   -Force
            if ($null -ne $SignalStopPrice) {
                $DecisionParsed | Add-Member -NotePropertyName "stop_price" -NotePropertyValue $SignalStopPrice -Force
            }
            $DecisionJson = $DecisionParsed | ConvertTo-Json -Depth 10 -Compress
        }
    } catch { }

    $DecisionPath = Join-Path $TriggersDir ($AgentName.ToLower() + "_decision.json")
    try {
        Set-Content -Path $DecisionPath -Value $DecisionJson -Encoding UTF8
        Write-Host ("[Syndicate Gate] Decision written: " + ($AgentName.ToLower() + "_decision.json"))
        $DecisionObj = $DecisionJson | ConvertFrom-Json -ErrorAction SilentlyContinue
        $DecisionStr = if ($DecisionObj -and $DecisionObj.PSObject.Properties["decision"]) { $DecisionObj.decision } else { "?" }
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
# Code path 4: Exit review -- {name}_exit.json
#
# Agent calls build_exit_signal() and writes triggers/{name}_exit.json.
# TC reviews position context and decides EXIT or HOLD.
# Written to triggers/{name}_exit_decision.json for scalper to act on.
# ---------------------------------------------------------------------------
function Invoke-ExitReview {
    param([System.IO.FileInfo]$File)

    $ExitPath  = $File.FullName
    $AgentName = ($File.Name -replace "_exit\.json$", "").ToUpper()

    Write-Host ("[Syndicate Gate] Exit review: " + $File.Name + " | agent=" + $AgentName)

    $Raw = $null
    $Obj = $null
    try {
        $Raw = Get-Content $ExitPath -Raw -Encoding UTF8
        $Obj = $Raw | ConvertFrom-Json
    } catch {
        Write-Warning ("[Syndicate Gate] Cannot read exit file " + $File.Name + ": $_")
        Remove-Item $ExitPath -Force -ErrorAction SilentlyContinue
        return
    }

    # Check expiry
    $ExpiresAt = $null
    try { $ExpiresAt = [DateTime]::Parse($Obj.expires_at).ToUniversalTime() } catch { }
    if ($ExpiresAt -and [DateTime]::UtcNow -gt $ExpiresAt) {
        Write-Host ("[Syndicate Gate] Exit review expired (" + $AgentName + ") -- dropping")
        Remove-Item $ExitPath -Force -ErrorAction SilentlyContinue
        return
    }

    $PositionJson = ConvertTo-JsonSafe $Obj.position  -Default "{}"
    $MarketJson   = ConvertTo-JsonSafe $Obj.market     -Default "{}"
    $GameState    = if ($Obj.game_state) { $Obj.game_state } else { "no live game data" }
    $EntryReason  = if ($Obj.entry_reasoning) { $Obj.entry_reasoning } else { "not recorded" }
    $MemRulesJson = ConvertTo-JsonSafe $Obj.memory_rules

    $Prompt = $ExitPrompt
    $Prompt = $Prompt.Replace("{AGENT_NAME}",      $AgentName)
    $Prompt = $Prompt.Replace("{POSITION_CONTEXT}", $PositionJson)
    $Prompt = $Prompt.Replace("{MARKET_CONTEXT}",   $MarketJson)
    $Prompt = $Prompt.Replace("{GAME_STATE}",       $GameState)
    $Prompt = $Prompt.Replace("{ENTRY_REASONING}",  $EntryReason)
    $Prompt = $Prompt.Replace("{MEMORY_RULES}",     $MemRulesJson)

    $TcText = Invoke-TC -Prompt $Prompt -Label ("exit:" + $AgentName)
    if ([string]::IsNullOrWhiteSpace($TcText)) {
        Write-Warning ("[Syndicate Gate] TC empty response for exit " + $AgentName + " -- dropping")
        Remove-Item $ExitPath -Force -ErrorAction SilentlyContinue
        return
    }

    $DecisionJson = Get-FirstJson -Text $TcText
    if (-not $DecisionJson) {
        Write-Warning ("[Syndicate Gate] No JSON in TC exit response for " + $AgentName)
        Remove-Item $ExitPath -Force -ErrorAction SilentlyContinue
        return
    }

    # Inject ticker from position context
    $Ticker = if ($Obj.position -and $Obj.position.PSObject.Properties["ticker"]) { $Obj.position.ticker } else { "?" }
    try {
        $DecisionParsed = $DecisionJson | ConvertFrom-Json -ErrorAction SilentlyContinue
        if ($DecisionParsed) {
            $DecisionParsed | Add-Member -NotePropertyName "ticker" -NotePropertyValue $Ticker -Force
            $DecisionParsed | Add-Member -NotePropertyName "agent"  -NotePropertyValue $AgentName -Force
            $DecisionJson = $DecisionParsed | ConvertTo-Json -Depth 10 -Compress
        }
    } catch { }

    $DecisionPath = Join-Path $TriggersDir ($AgentName.ToLower() + "_exit_decision.json")
    try {
        Set-Content -Path $DecisionPath -Value $DecisionJson -Encoding UTF8
        $DecisionObj = $DecisionJson | ConvertFrom-Json -ErrorAction SilentlyContinue
        $DecStr = if ($DecisionObj -and $DecisionObj.PSObject.Properties["decision"]) { $DecisionObj.decision } else { "?" }
        Write-Host ("[Syndicate Gate] Exit decision: " + $AgentName + " " + $Ticker + " → " + $DecStr)
        Post-Telegram "EXIT REVIEW: $AgentName $Ticker → $DecStr"
    } catch {
        Write-Warning ("[Syndicate Gate] Failed to write exit decision: $_")
    }

    Set-Content -Path $AnalysisPath -Value $TcText -Encoding UTF8
    Remove-Item $ExitPath -Force -ErrorAction SilentlyContinue
    Write-Host ("[Syndicate Gate] Exit review done for " + $AgentName + ".")
}

# ---------------------------------------------------------------------------
# Main loop -- priority: panel > agent_signal > postmortem > exit_review
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

        # Inject SAGE + ECHO into panel prompt
        $PanelYesPrice = if ($SignalObj.signal -and $SignalObj.signal.PSObject.Properties["entry_price"]) { $SignalObj.signal.entry_price } else { 0.5 }
        $PanelClass    = if ($SignalObj.signal -and $SignalObj.signal.PSObject.Properties["contract_class"]) { $SignalObj.signal.contract_class } else { "SCALP" }
        $PanelAgent    = if ($SignalObj.signal -and $SignalObj.signal.PSObject.Properties["agent_name"]) { $SignalObj.signal.agent_name } else { "unknown" }

        $PanelSage = "SAGE: Unavailable."
        $PanelEcho = "ECHO: Unavailable."
        try {
            $SagePy = Join-Path $SyndicateRoot "tools\get_sage_briefing.py"
            if (Test-Path $SagePy) {
                $PanelSage = (& python $SagePy --ticker $Ticker --yes_price $PanelYesPrice --class_ $PanelClass 2>$null) -join ""
            }
        } catch { }
        try {
            $EchoPy = Join-Path $SyndicateRoot "tools\get_echo_warning.py"
            if (Test-Path $EchoPy) {
                $PanelEcho = (& python $EchoPy --ticker $Ticker --agent $PanelAgent --yes_price $PanelYesPrice 2>$null) -join ""
            }
        } catch { }

        $PanelFull = $PanelPrompt
        $PanelFull = $PanelFull.Replace("{ECHO_WARNING}", $PanelEcho)
        $PanelFull = $PanelFull.Replace("{SAGE_BRIEFING}", $PanelSage)
        $FullPrompt = $PanelFull + "`n`n---SIGNAL---`n`n" + $SignalRaw
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

    # ── Code path 4: Exit review -- {name}_exit.json ──────────────────────
    $ExitFiles = Get-ChildItem -Path $TriggersDir -Filter "*_exit.json" `
        -ErrorAction SilentlyContinue
    if ($ExitFiles) {
        Invoke-ExitReview -File $ExitFiles[0]
        continue
    }
}
