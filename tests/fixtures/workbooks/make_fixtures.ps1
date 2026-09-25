# Regenerates the cross-format workbook fixtures with Microsoft Excel (COM).
# The files are committed; this script is the record of how they were made.
# One workbook, saved by Excel itself in four formats, so every backend is
# tested against genuine bytes rather than a library's idea of the format.
#
#   pwsh tests/fixtures/workbooks/make_fixtures.ps1
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$excel = New-Object -ComObject Excel.Application
$excel.DisplayAlerts = $false
try {
    $book = $excel.Workbooks.Add()
    while ($book.Worksheets.Count -lt 2) { [void]$book.Worksheets.Add([Type]::Missing, $book.Worksheets.Item($book.Worksheets.Count)) }
    while ($book.Worksheets.Count -gt 2) { $book.Worksheets.Item($book.Worksheets.Count).Delete() }

    $animals = $book.Worksheets.Item(1)
    $animals.Name = "animals"
    # Row 1 is left blank on purpose: the header is not always the first row.
    $rows = @(
        @("animal_id", "genotype", "weight_g", "dob", "score", "note"),
        @("A001", "WT", 21.5, "2024-01-03", "=C3*2", "ok"),
        @("A002", "KO", 19, "2024-01-04", 38, $null),
        @("A003", "NA", $null, "2024-01-05", "NA", "x")
    )
    for ($r = 0; $r -lt $rows.Count; $r++) {
        for ($c = 0; $c -lt $rows[$r].Count; $c++) {
            $value = $rows[$r][$c]
            if ($null -eq $value) { continue }
            $cell = $animals.Cells.Item($r + 2, $c + 1)
            if ($c -eq 3 -and $r -gt 0) { $cell.Formula = [string][DateTime]::Parse($value).ToOADate(); $cell.NumberFormat = "yyyy-mm-dd" }
            # Entered as typed text, exactly as a person would: Excel itself
            # decides that "19" is a number and "=C3*2" is a formula.
            else { $cell.Formula = [string]$value }
        }
    }

    $notes = $book.Worksheets.Item(2)
    $notes.Name = "notes"
    $notes.Cells.Item(1, 1).Formula = "key"
    $notes.Cells.Item(1, 2).Formula = "value"
    $notes.Cells.Item(2, 1).Formula = "protocol"
    $notes.Cells.Item(2, 2).Formula = "P-1"
    $notes.Visible = 0  # xlSheetHidden

    # xlOpenXMLWorkbook=51, xlExcel8 (BIFF8)=56, xlExcel12 (binary)=50, xlOpenDocumentSpreadsheet=60
    $targets = @{ "animals.xlsx" = 51; "animals.xls" = 56; "animals.xlsb" = 50; "animals.ods" = 60 }
    foreach ($name in $targets.Keys) {
        $path = Join-Path $here $name
        if (Test-Path $path) { Remove-Item $path }
        $book.SaveAs($path, $targets[$name])
    }
    $book.Close($false)
} finally {
    $excel.Quit()
    [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($excel)
}
