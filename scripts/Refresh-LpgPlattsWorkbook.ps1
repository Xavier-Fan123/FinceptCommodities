param(
    [Parameter(Mandatory = $true)]
    [string]$Workbook,
    [int]$TimeoutSec = 240,
    [switch]$DryRun
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

# Never attach to or take focus from a workbook the user already has open.
# Scheduled tasks repeat every 15 minutes for two hours, so exit 4 is a safe
# defer signal rather than a failure or an invitation to kill Excel.
if (Get-Process -Name EXCEL -ErrorAction SilentlyContinue) {
    Write-Output 'DEFERRED_EXCEL_ALREADY_OPEN'
    exit 4
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
    # manual calculation, so it can safely be opened before COM attaches.
    $stage = 'launching_excel'
    $process = Start-Process -FilePath 'excel.exe' -ArgumentList "`"$Workbook`"" -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds 15
    for ($attempt = 0; $attempt -lt 20 -and $null -eq $excel; $attempt++) {
        try { $excel = [Runtime.InteropServices.Marshal]::GetActiveObject('Excel.Application') }
        catch { Start-Sleep -Seconds 2 }
    }
    if ($null -eq $excel) { throw 'Excel did not register in the Running Object Table' }
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
