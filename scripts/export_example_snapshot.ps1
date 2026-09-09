# Export the fictional, editable example slide using desktop PowerPoint on Windows.
# Run from any directory: powershell -File scripts/export_example_snapshot.ps1
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$deckPath = Join-Path $projectRoot 'docs\examples\treasurer-snapshot.pptx'
$imagePath = Join-Path $projectRoot 'docs\screenshots\treasurer-snapshot.png'
$powerPoint = $null
$deck = $null
$startedEmpty = $false
try {
    $powerPoint = New-Object -ComObject PowerPoint.Application
    $startedEmpty = $powerPoint.Presentations.Count -eq 0
    $deck = $powerPoint.Presentations.Open($deckPath, $true, $false, $false)
    if ($deck.Slides.Count -ne 1) { throw 'Expected one fictional example slide.' }
    $deck.Slides.Item(1).Export($imagePath, 'PNG', 1600, 900)
    Write-Output "Exported $imagePath"
}
finally {
    if ($null -ne $deck) {
        $deck.Close()
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($deck)
    }
    if ($null -ne $powerPoint) {
        if ($startedEmpty -and $powerPoint.Presentations.Count -eq 0) { $powerPoint.Quit() }
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($powerPoint)
    }
}
