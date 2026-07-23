param(
    [Parameter(Mandatory = $true)]
    [string]$Workbook,
    [int]$TimeoutSec = 240,
    [switch]$DryRun,
    [switch]$DeElevatedRun,
    [switch]$IsolatedInstance
)

$ErrorActionPreference = 'Stop'
$Workbook = [System.IO.Path]::GetFullPath($Workbook)
if (-not (Test-Path -LiteralPath $Workbook)) {
    Write-Output "MISSING_WORKBOOK $Workbook"
    exit 3
}
if ($DryRun) {
    Write-Output "DRY_RUN workbook=$Workbook timeout=$TimeoutSec"
    exit 0
}

# The S&P COM Add-in and its remembered WebView2 login are registered in the
# interactive user's medium-integrity profile.  An elevated Terminal/server
# would otherwise launch elevated Excel, where the per-user ribbon/session can
# disappear.  Re-dispatch only this refresh through a unique, temporary,
# interactive Limited task and relay its output/exit code to the caller.
$isElevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if ($isElevated -and $DeElevatedRun) {
    Write-Output 'DEELEVATION_FAILED child task is still elevated'
    exit 3
}
if ($isElevated) {
    $runToken = [Guid]::NewGuid().ToString('N')
    $taskName = "Fincept LPG DeElevated $runToken"
    $tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    $cmdPath = Join-Path $tempRoot ("fincept-lpg-deelevated-$runToken.cmd")
    $logPath = Join-Path $tempRoot ("fincept-lpg-deelevated-$runToken.log")
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $taskRegistered = $false
    $workbookWriteTimeBefore = (Get-Item -LiteralPath $Workbook).LastWriteTimeUtc.Ticks
    try {
        Write-Output 'ELEVATED_SHELL_DETECTED redispatching as the interactive user at Limited integrity'
        $isolatedArgument = if ($IsolatedInstance) { ' -IsolatedInstance' } else { '' }
        @(
            '@echo off',
            'setlocal',
            ('powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath +
                '" -Workbook "' + $Workbook + '" -TimeoutSec ' + $TimeoutSec +
                ' -DeElevatedRun' + $isolatedArgument + ' > "' + $logPath + '" 2>&1'),
            'set "FINCEPT_LPG_EXIT=%ERRORLEVEL%"',
            ('echo EXIT=%FINCEPT_LPG_EXIT%>>"' + $logPath + '"'),
            'exit /b %FINCEPT_LPG_EXIT%'
        ) | Set-Content -LiteralPath $cmdPath -Encoding ASCII

        $action = New-ScheduledTaskAction -Execute $env:ComSpec -Argument "/d /c `"`"$cmdPath`"`""
        $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1)
        $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (
            New-TimeSpan -Seconds ([Math]::Max(180, $TimeoutSec + 90))
        ) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
            -Principal $principal -Settings $settings -Description `
            'Temporary Limited-integrity S&P Excel refresh launched from an elevated Fincept process.' `
            -Force | Out-Null
        $taskRegistered = $true
        Start-ScheduledTask -TaskName $taskName

        $deadline = (Get-Date).AddSeconds([Math]::Max(180, $TimeoutSec + 75))
        $exitMatch = $null
        $workbookUpdated = $false
        $lastUpdatedWriteTime = 0
        $stableUpdatePasses = 0
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 2
            if (Test-Path -LiteralPath $logPath) {
                $exitMatch = Select-String -LiteralPath $logPath -Pattern '^EXIT=(-?\d+)$' |
                    Select-Object -Last 1
                if ($exitMatch) { break }
            }
            if ($IsolatedInstance) {
                $currentWriteTime = (Get-Item -LiteralPath $Workbook).LastWriteTimeUtc.Ticks
                if ($currentWriteTime -gt $workbookWriteTimeBefore) {
                    if ($currentWriteTime -eq $lastUpdatedWriteTime) {
                        $stableUpdatePasses++
                    } else {
                        $lastUpdatedWriteTime = $currentWriteTime
                        $stableUpdatePasses = 0
                    }
                    if ($stableUpdatePasses -ge 2) {
                        $workbookUpdated = $true
                        break
                    }
                }
            }
        }
        if (-not $exitMatch -and -not $workbookUpdated) {
            Write-Output 'DEELEVATED_RUN_TIMEOUT no exit marker was returned'
            exit 3
        }
        if (Test-Path -LiteralPath $logPath) {
            Get-Content -LiteralPath $logPath | Where-Object { $_ -notmatch '^EXIT=-?\d+$' } |
                ForEach-Object { Write-Output $_ }
        }
        if ($workbookUpdated) {
            Write-Output 'DEELEVATED_WORKBOOK_UPDATED saved workbook will be externally validated'
            exit 0
        }
        exit [int]$exitMatch.Matches[0].Groups[1].Value
    } catch {
        Write-Output ("DEELEVATED_RUN_FAILED " + $_.Exception.Message)
        exit 3
    } finally {
        if ($taskRegistered) {
            Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
        }
        Remove-Item -LiteralPath $cmdPath, $logPath -Force -ErrorAction SilentlyContinue
    }
}

# Daily scheduled refreshes never attach to or take focus from a workbook the
# user already has open. Specialized history/curve/MOC probes may explicitly
# request a separate /x Excel process; the ROT lookup below binds only that PID
# and cleanup only terminates the process launched by this script.
$existingExcel = @(Get-Process -Name EXCEL -ErrorAction SilentlyContinue)
if ($existingExcel.Count -gt 0 -and -not $IsolatedInstance) {
    Write-Output 'DEFERRED_EXCEL_ALREADY_OPEN'
    exit 4
}
if ($existingExcel.Count -gt 0) {
    Write-Output ("EXISTING_EXCEL_DETECTED count=" + $existingExcel.Count + " launching_isolated_instance")
}

try {
    $addinKey = 'HKCU:\Software\Microsoft\Office\Excel\Addins\SPGlobal_Platts_Excel_AddIn.AddinModule'
    if (Test-Path $addinKey) {
        Set-ItemProperty -Path $addinKey -Name LoadBehavior -Value 3 -Type DWord
    }
} catch {
    Write-Output ("ADDIN_ENABLE_WARNING " + $_.Exception.Message)
}

$excel = $null
$book = $null
$process = $null
$saved = $false
$stage = 'initializing'

function Release-ComObject($value) {
    if ($null -ne $value) {
        try { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($value) } catch {}
    }
}

function ConvertTo-UnsignedHResult([int]$value) {
    if ($value -lt 0) { return [uint32]([int64]$value + 4294967296) }
    return [uint32]$value
}

function Invoke-WithComRetry {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$Operation,
        [int]$Attempts = 20,
        [int]$DelayMilliseconds = 750
    )
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try { return & $Operation } catch {
            $exception = $_.Exception
            $hresults = New-Object System.Collections.Generic.List[uint32]
            while ($null -ne $exception) {
                [void]$hresults.Add((ConvertTo-UnsignedHResult $exception.HResult))
                $exception = $exception.InnerException
            }
            $message = $_.Exception.ToString()
            $retryable = @($hresults | Where-Object { $_ -in @(0x80010001, 0x8001010A, 0x800AC472) }).Count -gt 0
            if (-not $retryable) {
                $retryable = $message -match 'RPC_E_CALL_REJECTED|RPC_E_SERVERCALL_RETRYLATER|Call was rejected by callee'
            }
            if (-not $retryable -or $attempt -eq $Attempts) { throw }
            Start-Sleep -Milliseconds $DelayMilliseconds
        }
    }
}

function Close-OwnedExcel {
    param([bool]$SaveChanges)
    if ($null -ne $book) {
        try { $book.Close($SaveChanges) | Out-Null } catch {}
    }
    if ($null -ne $excel) {
        try { $excel.DisplayAlerts = $false } catch {}
        try { $excel.Quit() } catch {}
    }
    Release-ComObject $book
    Release-ComObject $excel
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
    if ($null -ne $process) {
        try { $process | Wait-Process -Timeout 15 -ErrorAction SilentlyContinue } catch {}
        $owned = Get-Process -Id $process.Id -ErrorAction SilentlyContinue
        if ($owned) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
    }
}

function Stop-IsolatedExcelAfterSave {
    # A completed Save is the durability boundary for disposable specialized
    # workbooks. Graceful Workbook.Close/Quit can make this Add-in recalculate
    # volatile UDFs during shutdown and hang after the data is already on disk.
    # Release COM references, then terminate only the exact process we launched.
    Release-ComObject $book
    Release-ComObject $excel
    $script:book = $null
    $script:excel = $null
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
    Start-Sleep -Seconds 3
    if ($null -ne $process) {
        $owned = Get-Process -Id $process.Id -ErrorAction SilentlyContinue
        if ($owned) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
    }
}

function Get-ResolutionCounts {
    param($WorkbookObject)
    $queryResolved = 0
    $discoveryResolved = 0
    $queryCompleted = 0
    $queryTotal = 0
    $errorDetails = New-Object System.Collections.Generic.List[string]
    $discoveryNames = @(
        'datasets','md_catalog','md_current_schema','md_history_schema',
        'md_correction_schema','md_lpg_metadata','fc_catalog','fc_curve_schema',
        'fc_pivot_schema','ewmd_catalog','ewmd_trade_schema'
    )
    foreach ($sheet in $WorkbookObject.Worksheets) {
        $name = [string]$sheet.Name
        if (($name -notmatch '^q\d+_') -and ($discoveryNames -notcontains $name)) { continue }
        if ($name -match '^q\d+_') { $queryTotal++ }
        $used = $sheet.UsedRange
        $values = $used.Value2
        $numeric = 0
        $meaningful = 0
        $errors = 0
        if ($values -is [Array]) {
            foreach ($value in $values) {
                $isErrorNumber = (($value -is [double] -or $value -is [int]) -and [double]$value -lt -1000000)
                if ($isErrorNumber) {
                    $errors++
                    continue
                }
                if ($value -is [double] -or $value -is [int]) { $numeric++ }
                if ($null -ne $value -and ([string]$value).Trim()) {
                    $text = ([string]$value).Trim()
                    if ($text -match '^#(VALUE!|N/A|REF!|NAME\?|NUM!|NULL!|DIV/0!)$') { $errors++ }
                    elseif ($text -notmatch '^=') { $meaningful++ }
                }
            }
        } elseif ($null -ne $values) {
            $text = ([string]$values).Trim()
            $isErrorNumber = (($values -is [double] -or $values -is [int]) -and [double]$values -lt -1000000)
            if ($isErrorNumber) { $errors = 1 }
            elseif ($values -is [double] -or $values -is [int]) { $numeric = 1; $meaningful = 1 }
            elseif ($text -and $text -notmatch '^#' -and $text -notmatch '^=') { $meaningful = 1 }
        }
        if ($name -match '^q\d+_' -and $numeric -ge 2) { $queryResolved++ }
        if ($name -match '^q\d+_' -and ($numeric -ge 2 -or $errors -gt 0)) { $queryCompleted++ }
        if (($discoveryNames -contains $name) -and $meaningful -ge 2 -and $errors -eq 0) {
            $discoveryResolved++
        }
        if ($errors -gt 0 -and $errorDetails.Count -lt 25) {
            try {
                $errorText = ([string]$sheet.Range('A2').Text).Trim()
                if ($errorText) { [void]$errorDetails.Add("$name=$errorText") }
            } catch {}
        }
        Release-ComObject $used
        Release-ComObject $sheet
    }
    return @(
        $queryResolved, $discoveryResolved, ($errorDetails -join '; '),
        $queryCompleted, $queryTotal
    )
}

try {
    # A normal Excel process is required; creating Excel.Application directly
    # does not load the official Add-in. The generated workbook itself requests
    # manual calculation, so it can safely be opened before COM attaches.  Keep
    # Excel visible during startup: the Add-in restores its remembered login in
    # a normal desktop window before automation attaches.  Hiding Excel at
    # process creation makes the Add-in take its COM-automation startup path and
    # leaves the ribbon signed out even when its UDF token is still valid.
    $stage = 'launching_excel'
    $excelArguments = @()
    if ($IsolatedInstance) { $excelArguments += '/x' }
    $excelArguments += "`"$Workbook`""
    $process = Start-Process -FilePath 'excel.exe' -ArgumentList $excelArguments -PassThru
    Start-Sleep -Seconds 20
    # WPS Office preloads an et.exe COM automation server (wps.exe /prometheus /et
    # -> et.exe /Automation -Embedding) that also answers to the 'Excel.Application'
    # ROT moniker. A plain GetActiveObject then binds to WPS - which has no Platts
    # Add-in, so every UDF calculates to #NAME? while the real Excel sits untouched
    # (this silently broke the refresh from 2026-07-17 08:50 onward). Enumerate the
    # Running Object Table instead and accept only a COM object whose window belongs
    # to the excel.exe process launched above.
    Add-Type -ReferencedAssemblies 'Microsoft.CSharp', 'System.Core' -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

public static class ExcelRotFinder
{
    [DllImport("ole32.dll")]
    private static extern int GetRunningObjectTable(uint reserved, out IRunningObjectTable prot);

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);

    public static object GetApplicationForProcess(int processId)
    {
        IRunningObjectTable rot;
        if (GetRunningObjectTable(0, out rot) != 0 || rot == null) return null;
        IEnumMoniker enumMoniker;
        rot.EnumRunning(out enumMoniker);
        if (enumMoniker == null) return null;
        enumMoniker.Reset();
        var monikers = new IMoniker[1];
        while (enumMoniker.Next(1, monikers, IntPtr.Zero) == 0)
        {
            object comObject = null;
            try { rot.GetObject(monikers[0], out comObject); } catch { comObject = null; }
            if (comObject == null) continue;
            dynamic application = null;
            try { application = ((dynamic)comObject).Application; }
            catch { try { application = (dynamic)comObject; } catch { application = null; } }
            if (application == null) continue;
            try
            {
                long hwnd = (long)application.Hwnd;
                uint ownerPid;
                GetWindowThreadProcessId(new IntPtr(hwnd), out ownerPid);
                if (ownerPid == (uint)processId) return application;
            }
            catch { }
        }
        return null;
    }
}
'@
    $windowActivator = New-Object -ComObject WScript.Shell
    for ($attempt = 0; $attempt -lt 20 -and $null -eq $excel; $attempt++) {
        foreach ($windowTitle in @([System.IO.Path]::GetFileNameWithoutExtension($Workbook), 'Excel')) {
            try { [void]$windowActivator.AppActivate($windowTitle) } catch {}
        }
        Start-Sleep -Milliseconds 750
        try { $excel = [ExcelRotFinder]::GetApplicationForProcess($process.Id) }
        catch { $excel = $null }
        if ($null -eq $excel) { Start-Sleep -Seconds 2 }
    }
    if ($null -eq $excel) { throw 'The launched Excel process did not register in the Running Object Table' }
    $stage = 'configuring_excel'
    Invoke-WithComRetry {
        $excel.DisplayAlerts = $false
        $excel.Calculation = -4135 # xlCalculationManual
        $excel.Visible = $true
        try { $excel.WindowState = 2 } catch {} # xlMinimized
    } | Out-Null

    $stage = 'binding_workbook'
    Invoke-WithComRetry {
        foreach ($candidate in $excel.Workbooks) {
            try {
                if ([System.IO.Path]::GetFullPath([string]$candidate.FullName) -ieq $Workbook) {
                    $script:book = $candidate
                    break
                }
            } catch {}
        }
    } -Attempts 40 -DelayMilliseconds 1000 | Out-Null
    if ($null -eq $book) {
        # Excel can publish its Application object in the ROT before the
        # command-line workbook is added to Workbooks.  Open it explicitly on
        # the same COM object so calculation, saving, and cleanup own one
        # deterministic instance.
        try { $book = Invoke-WithComRetry { $excel.Workbooks.Open($Workbook, 0, $false) } } catch {
            $openNames = @(
                foreach ($candidate in $excel.Workbooks) {
                    try { [string]$candidate.FullName } catch {}
                }
            ) -join '; '
            throw "Target workbook could not be bound to Excel; open=$openNames; $($_.Exception.Message)"
        }
    }

    # Calculate each isolated UDF exactly once. Automatic calculation causes
    # the async Platts XLL to redispatch completed requests indefinitely.
    $stage = 'dispatching_queries'
    $querySheets = @(
        foreach ($sheet in $book.Worksheets) {
            try {
                if ([string]$sheet.Name -match '^q\d+_') { [string]$sheet.Name }
            } finally {
                Release-ComObject $sheet
            }
        }
    )
    foreach ($sheetName in $querySheets) {
        Invoke-WithComRetry {
            $sheet = $book.Worksheets.Item($sheetName)
            try { $sheet.Range('A2').Calculate() } finally { Release-ComObject $sheet }
        } -Attempts 60 -DelayMilliseconds 1000 | Out-Null
    }
    Write-Output ("RECALC_STARTED queries=" + $querySheets.Count)

    if ($IsolatedInstance) {
        # The Add-in writes large spilled ranges asynchronously. Reading
        # UsedRange while FillSheet is still committing rows can make this
        # Add-in build throw DISP_E_BADINDEX and close Excel even though the
        # returned data is valid. Specialized workbooks are disposable and
        # validated by Python after save, so give each isolated query a quiet
        # settle window and persist without touching its result range here.
        $stage = 'waiting_for_isolated_results'
        $settleSeconds = [Math]::Min(
            [Math]::Max(45, 30 + (15 * $querySheets.Count)),
            [Math]::Max(45, $TimeoutSec)
        )
        $settleWatch = [Diagnostics.Stopwatch]::StartNew()
        while ($settleWatch.Elapsed.TotalSeconds -lt $settleSeconds) {
            Start-Sleep -Seconds 5
            if (-not (Get-Process -Id $process.Id -ErrorAction SilentlyContinue)) {
                throw 'The isolated Excel process exited before the validation workbook was saved'
            }
        }
        $stage = 'saving_for_external_validation'
        $mtimeBeforeSave = (Get-Item -LiteralPath $Workbook).LastWriteTimeUtc.Ticks
        try {
            Invoke-WithComRetry {
                try { $excel.CalculateBeforeSave = $false } catch {}
                $runtime = $book.Worksheets.Item('_runtime_status')
                try {
                    $runtime.Cells.Item(2, 2).Value2 = 'success'
                    $runtime.Cells.Item(3, 2).Value2 = [DateTime]::UtcNow.ToString('o')
                    $runtime.Cells.Item(4, 2).Value2 = 0
                    $runtime.Cells.Item(5, 2).Value2 = 0
                    $runtime.Cells.Item(6, 2).Value2 = 'Saved after isolated Add-in calculation for external validation.'
                } finally {
                    Release-ComObject $runtime
                }
                $book.Save()
            } -Attempts 3 -DelayMilliseconds 500 | Out-Null
        } catch {
            $mtimeAfterSave = (Get-Item -LiteralPath $Workbook).LastWriteTimeUtc.Ticks
            $owned = Get-Process -Id $process.Id -ErrorAction SilentlyContinue
            if (-not $owned -and $mtimeAfterSave -gt $mtimeBeforeSave) {
                $saved = $true
                Write-Output (
                    "SAVED_AFTER_ADDIN_EXIT queries=" + $querySheets.Count +
                    " settle=" + [int]$settleWatch.Elapsed.TotalSeconds
                )
                $script:book = $null
                $script:excel = $null
                [GC]::Collect()
                [GC]::WaitForPendingFinalizers()
                exit 0
            }
            throw
        }
        $saved = $true
        Write-Output (
            "SAVED_FOR_EXTERNAL_VALIDATION queries=" + $querySheets.Count +
            " settle=" + [int]$settleWatch.Elapsed.TotalSeconds
        )
        Stop-IsolatedExcelAfterSave
        exit 0
    }

    $watch = [Diagnostics.Stopwatch]::StartNew()
    $counts = @(0, 0, '', 0, 0)
    $lastSignature = ''
    $stablePasses = 0
    while ($watch.Elapsed.TotalSeconds -lt [Math]::Max(60, $TimeoutSec)) {
        Start-Sleep -Seconds 5
        # CalculateUntilAsyncQueriesDone does not return for this Add-in's
        # custom async UDFs.  Poll only the cells the Add-in writes.
        # Leave the XLL alone while its one-shot async results spill to cells.
        if ($watch.Elapsed.TotalSeconds -ge 45) {
            $stage = 'reading_results'
            try {
                $counts = @(Invoke-WithComRetry {
                    Get-ResolutionCounts $book
                } -Attempts 45 -DelayMilliseconds 1000)
            } catch {
                $hresult = $_.Exception.HResult -band 0xffffffff
                if ($hresult -in @(0x80010001, 0x8001010A, 0x800AC472)) { continue }
                throw
            }
            $signature = "$($counts[0])|$($counts[1])|$($counts[2])|$($counts[3])|$($counts[4])"
            if ($signature -eq $lastSignature) { $stablePasses++ } else { $stablePasses = 0 }
            $lastSignature = $signature
            $hasResolved = ($counts[0] -gt 0 -or $counts[1] -gt 0)
            $allQueriesCompleted = ($counts[4] -gt 0 -and $counts[3] -ge $counts[4])
            if ($hasResolved -and ($allQueriesCompleted -or $stablePasses -ge 3)) { break }
            if ($hasResolved -and $excel.CalculationState -eq 0) { break }
            # A stable terminal formula error is also a completed query.  Do
            # not hold the automation-owned Excel instance until the full timeout when
            # every isolated sheet has already returned the same error.
            if ($allQueriesCompleted -and $stablePasses -ge 3) { break }
        }
    }

    if ($counts[0] -eq 0 -and $counts[1] -eq 0) {
        Write-Output ("DATA_NOT_RESOLVED session_or_addin_failure; workbook_not_saved errors=" + $counts[2])
        Close-OwnedExcel $false
        exit 2
    }

    $stage = 'saving_workbook'
    Invoke-WithComRetry {
        $runtime = $book.Worksheets.Item('_runtime_status')
        $runtime.Cells.Item(2, 2).Value2 = 'success'
        $runtime.Cells.Item(3, 2).Value2 = [DateTime]::UtcNow.ToString('o')
        $runtime.Cells.Item(4, 2).Value2 = [int]$counts[0]
        $runtime.Cells.Item(5, 2).Value2 = [int]$counts[1]
        $runtime.Cells.Item(6, 2).Value2 = 'Calculated through the signed-in official S&P Global Energy Excel Add-in.'
        Release-ComObject $runtime
        $book.Save()
    } -Attempts 30 -DelayMilliseconds 1000 | Out-Null
    $saved = $true
    Write-Output ("SAVED_OK resolved_queries=" + $counts[0] + " completed_queries=" + $counts[3] + "/" + $counts[4] + " resolved_discovery=" + $counts[1] + " elapsed=" + [int]$watch.Elapsed.TotalSeconds)
    Close-OwnedExcel $true
    exit 0
} catch {
    $hresult = ConvertTo-UnsignedHResult $_.Exception.HResult
    Write-Output ("EXCEL_AUTOMATION_FAILED stage=" + $stage + " hresult=0x" + $hresult.ToString('X8') + " " + $_.Exception.Message)
    Close-OwnedExcel $saved
    exit 3
}
