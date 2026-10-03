# Backfill full years into S3. Usage: .\scripts\backfill.ps1 -Bucket my-bucket [-From 2010] [-To 2025]
param(
    [Parameter(Mandatory = $true)][string]$Bucket,
    [int]$From = 2010,
    [int]$To = (Get-Date).Year,
    [string]$Python = ".\venv\Scripts\python.exe"
)

$failed = @()
for ($year = $From; $year -le $To; $year++) {
    Write-Host "Processing year: $year" -ForegroundColor Green
    & $Python -m scripts.run_local --year $year --bucket $Bucket
    if ($LASTEXITCODE -ne 0) { $failed += $year; Write-Warning "Execution failed for year $year" }
}
if ($failed.Count -gt 0) { Write-Error "Failed years: $($failed -join ', ')"; exit 1 }
