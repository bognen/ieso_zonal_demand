# PowerShell script to run ieso_zonal_demand_to_s3.py for years 2010 to 2025

$pythonPath = ".\venv\Scripts\python.exe"

for ($year = 2010; $year -le 2025; $year++) {
    Write-Host "Processing year: $year" -ForegroundColor Green
    & $pythonPath ieso_zonal_demand_to_s3.py --year $year --bucket com.dsa.ieso-project --prefix ieso/load/zonal_hourly --normalize-long-format
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Execution failed for year $year"
    }
}
