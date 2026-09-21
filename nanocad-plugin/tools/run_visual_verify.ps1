param(
    [Parameter(Mandatory = $true)]
    [string]$Python,
    [Parameter(Mandatory = $true)]
    [string]$Script,
    [Parameter(Mandatory = $true)]
    [string]$NanoCad,
    [Parameter(Mandatory = $true)]
    [string]$Plugin,
    [Parameter(Mandatory = $true)]
    [string]$Drawing,
    [Parameter(Mandatory = $true)]
    [string]$Screenshot,
    [Parameter(Mandatory = $true)]
    [string]$Status
)

$ErrorActionPreference = "Stop"
$native = @'
using System;
using System.Runtime.InteropServices;
public static class DesktopProcess {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct STARTUPINFO {
        public int cb;
        public string lpReserved;
        public string lpDesktop;
        public string lpTitle;
        public int dwX;
        public int dwY;
        public int dwXSize;
        public int dwYSize;
        public int dwXCountChars;
        public int dwYCountChars;
        public int dwFillAttribute;
        public int dwFlags;
        public short wShowWindow;
        public short cbReserved2;
        public IntPtr lpReserved2;
        public IntPtr hStdInput;
        public IntPtr hStdOutput;
        public IntPtr hStdError;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct PROCESS_INFORMATION {
        public IntPtr hProcess;
        public IntPtr hThread;
        public int dwProcessId;
        public int dwThreadId;
    }
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr CreateDesktop(string name, IntPtr device, IntPtr devmode, int flags, uint access, IntPtr security);
    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool CloseDesktop(IntPtr desktop);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool CreateProcess(
        string applicationName, string commandLine, IntPtr processAttributes,
        IntPtr threadAttributes, bool inheritHandles, uint creationFlags,
        IntPtr environment, string currentDirectory, ref STARTUPINFO startupInfo,
        out PROCESS_INFORMATION processInformation);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool GetExitCodeProcess(IntPtr process, out uint exitCode);
    [DllImport("kernel32.dll")]
    public static extern bool CloseHandle(IntPtr handle);
}
'@
Add-Type $native -ErrorAction Stop

$desktopName = "GreenAIVerify_$PID"
$genericAll = 0x10000000
$desktop = [DesktopProcess]::CreateDesktop($desktopName, [IntPtr]::Zero, [IntPtr]::Zero, 0, $genericAll, [IntPtr]::Zero)
if ($desktop -eq [IntPtr]::Zero) {
    throw "CreateDesktop failed: $([Runtime.InteropServices.Marshal]::GetLastWin32Error())"
}

function Quote-Arg([string]$value) {
    return '"' + $value.Replace('"', '\"') + '"'
}

$commandLine = @(
    (Quote-Arg $Python),
    (Quote-Arg $Script),
    (Quote-Arg $NanoCad),
    (Quote-Arg $Plugin),
    (Quote-Arg $Drawing),
    (Quote-Arg $Screenshot),
    (Quote-Arg $Status)
) -join ' '
$startup = New-Object DesktopProcess+STARTUPINFO
$startup.cb = [Runtime.InteropServices.Marshal]::SizeOf([type][DesktopProcess+STARTUPINFO])
$startup.lpDesktop = "WinSta0\$desktopName"
$processInfo = New-Object DesktopProcess+PROCESS_INFORMATION

try {
    $created = [DesktopProcess]::CreateProcess(
        $Python,
        $commandLine,
        [IntPtr]::Zero,
        [IntPtr]::Zero,
        $false,
        0,
        [IntPtr]::Zero,
        (Split-Path -Parent $Script),
        [ref]$startup,
        [ref]$processInfo
    )
    if (-not $created) {
        throw "CreateProcess failed: $([Runtime.InteropServices.Marshal]::GetLastWin32Error())"
    }
    [void][DesktopProcess]::WaitForSingleObject($processInfo.hProcess, 180000)
    $exitCode = 0
    [void][DesktopProcess]::GetExitCodeProcess($processInfo.hProcess, [ref]$exitCode)
    if ($exitCode -ne 0) {
        if (Test-Path -LiteralPath $Status) {
            Get-Content -LiteralPath $Status -Raw
        }
        throw "Visual verifier failed with exit code $exitCode"
    }
    Get-Content -LiteralPath $Status -Raw
}
finally {
    if ($processInfo.hThread -ne [IntPtr]::Zero) { [void][DesktopProcess]::CloseHandle($processInfo.hThread) }
    if ($processInfo.hProcess -ne [IntPtr]::Zero) { [void][DesktopProcess]::CloseHandle($processInfo.hProcess) }
    [void][DesktopProcess]::CloseDesktop($desktop)
}
