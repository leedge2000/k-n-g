$ErrorActionPreference = "Stop"
try {
    [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
    [Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
    $langs = [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages
    Write-Output "OCR_OK count=$($langs.Count)"
    foreach ($l in $langs) {
        Write-Output ("LANG: " + $l.LanguageTag + " | " + $l.DisplayName)
    }
}
catch {
    Write-Output ("OCR_FAIL " + $_.Exception.Message)
}
