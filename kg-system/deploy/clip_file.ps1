Add-Type -AssemblyName System.Windows.Forms
$files = New-Object System.Collections.Specialized.StringCollection
$files.Add('c:\粮油知识图谱\kg-system\mobile-screenshots.html') | Out-Null
[System.Windows.Forms.Clipboard]::SetFileDropList($files)
Write-Host 'OK - file copied to clipboard'
