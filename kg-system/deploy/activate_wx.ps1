Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32 {
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(int idAttach, int idAttachTo, bool fAttach);
    [DllImport("kernel32.dll")] public static extern int GetWindowThreadProcessId(IntPtr hWnd, out int lpdwProcessId);
    [DllImport("kernel32.dll")] public static extern int GetCurrentThreadId();
}
"@

# 找微信主窗口（不是 WeChatAppEx 子进程）
$wechatMain = Get-Process | Where-Object { $_.ProcessName -eq 'Weixin' -and $_.MainWindowTitle -ne '' } | Select-Object -First 1
if (-not $wechatMain) { Write-Host "No WeChat main window found"; exit }

$hWnd = $wechatMain.MainWindowHandle
$procId = $wechatMain.Id
Write-Host "WeChat PID=$procId HWND=$hWnd Title='$($wechatMain.MainWindowTitle)'"

# Force foreground
$thread = [Win32]::GetWindowThreadProcessId($hWnd, [ref]$procId)
[Win32]::AttachThreadInput([Win32]::GetCurrentThreadId(), $thread, $true) | Out-Null
[Win32]::ShowWindow($hWnd, 9)  # SW_RESTORE
[Win32]::SetForegroundWindow($hWnd) | Out-Null
[Win32]::AttachThreadInput([Win32]::GetCurrentThreadId(), $thread, $false) | Out-Null

Start-Sleep -Milliseconds 500

$fg = [Win32]::GetForegroundWindow()
Write-Host "Foreground HWND=$fg WeChat HWND=$hWnd Match=$($fg -eq $hWnd)"
