# Syndicate TC Intelligence Gate - PowerShell Signal Watcher
# Polls triggers/pending_signal.json every 500ms.
# When a signal appears, invokes the TC panel, writes tc_analysis.txt,
# calls parse_decision.py to write decision.json, then cleans up.
#
# Usage: Run once at Syndicate startup (manually or via start_syndicate.bat).
# Does NOT need to restart between signals - loops indefinitely.

param(
    [string]$SyndicateRoot = (Split-Path -Parent $PSScriptRoot)
)

$TriggersDir     = Join-Path $SyndicateRoot "triggers"
$IntelDir        = Join-Path $SyndicateRoot "intelligence"
$PanelPromptPath = Join-Path $IntelDir "prompts\panel_prompt.txt"
$PendingPath     = Join-Path $TriggersDir "pending_signal.json"
$AnalysisPath    = Join-Path $IntelDir "tc_analysis.txt"
$ParseScriptPath = Join-Path $IntelDir "parse_decision.py"

# Ensure triggers dir exists
if (-not (Test-Path $TriggersDir)) {
    New-Item -ItemType Directory -Path $TriggersDir | Out-Null
}

# Load system prompt once at startup
if (-not (Test-Path $PanelPromptPath)) {
    Write-Error "[Syndicate Gate] panel_prompt.txt not found at $PanelPromptPath"
    exit 1
}
$SystemPrompt = Get-Content $PanelPromptPath -Raw -Encoding UTF8

Write-Host "[Syndicate Gate] Started. Watching $TriggersDir for signals..."
Write-Host "[Syndicate Gate] Root: $SyndicateRoot"

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
while ($true) {
    Start-Sleep -Milliseconds 500

    if (-not (Test-Path $PendingPath)) {
        continue
    }

    # Read signal
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
        Write-Warning "[Syndicate Gate] Invalid JSON in pending_signal.json - dropping"
        Remove-Item $PendingPath -Force -ErrorAction SilentlyContinue
        continue
    }

    # Check expiry BEFORE calling TC
    $ExpiresAt = $null
    try {
        $ExpiresAt = [DateTime]::Parse($SignalObj.expires_at).ToUniversalTime()
    } catch {
        Write-Warning "[Syndicate Gate] Could not parse expires_at - dropping"
        Remove-Item $PendingPath -Force -ErrorAction SilentlyContinue
        continue
    }

    $Now = [DateTime]::UtcNow
    if ($Now -gt $ExpiresAt) {
        $Ticker = $SignalObj.signal.ticker
        Write-Host ("[Syndicate Gate] Signal expired (" + $Ticker + ") - dropping without TC call")
        Remove-Item $PendingPath -Force -ErrorAction SilentlyContinue
        continue
    }

    $Tier   = $SignalObj.signal.conviction_tier
    $Ticker = $SignalObj.signal.ticker
    $Class  = $SignalObj.signal.contract_class
    Write-Host ("[Syndicate Gate] Signal received: " + $Ticker + " | " + $Tier + " | " + $Class + " | expires " + $SignalObj.expires_at)

    # Build combined prompt (system prompt + signal JSON)
    $FullPrompt = $SystemPrompt + "`n`n---SIGNAL---`n`n" + $SignalRaw

    # Invoke TC panel
    Write-Host "[Syndicate Gate] Invoking TC panel (15-30s)..."
    $ClaudeOutput = $null
    try {
        $ClaudeOutput = & claude --print --output-format json -p $FullPrompt 2>&1
    } catch {
        Write-Warning "[Syndicate Gate] claude CLI call failed: $_"
        Remove-Item $PendingPath -Force -ErrorAction SilentlyContinue
        continue
    }

    # Extract text from JSON envelope
    # claude --print --output-format json streams objects; last one with type=result has the text
    $TcText = $null
    foreach ($Line in $ClaudeOutput) {
        $Line = "$Line".Trim()
        if ($Line.StartsWith("{")) {
            try {
                $Obj = $Line | ConvertFrom-Json
                $resultProp = $Obj.PSObject.Properties["result"]
                if ($Obj.type -eq "result" -and $resultProp) {
                    $TcText = $Obj.result
                }
            } catch { }
        }
    }

    # Fallback: treat entire output as raw text
    if (-not $TcText) {
        $TcText = ($ClaudeOutput | Out-String).Trim()
        Write-Warning "[Syndicate Gate] Could not parse JSON envelope - using raw output"
    }

    if ([string]::IsNullOrWhiteSpace($TcText)) {
        Write-Warning "[Syndicate Gate] TC returned empty response - dropping signal"
        Remove-Item $PendingPath -Force -ErrorAction SilentlyContinue
        continue
    }

    # Write TC analysis to file
    Set-Content -Path $AnalysisPath -Value $TcText -Encoding UTF8
    Write-Host ("[Syndicate Gate] TC analysis written (" + $TcText.Length + " chars). Running parser...")

    # Parse decision (reads tc_analysis.txt + pending_signal.json, writes decision.json)
    try {
        & python $ParseScriptPath $PendingPath 2>&1 | Write-Host
    } catch {
        Write-Warning "[Syndicate Gate] parse_decision.py failed: $_"
    }

    # Clean up pending signal after parse_decision.py has read it
    Remove-Item $PendingPath -Force -ErrorAction SilentlyContinue

    Write-Host ("[Syndicate Gate] Done. Decision written for " + $Ticker + ".")
}
