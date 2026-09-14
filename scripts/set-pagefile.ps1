# Run elevated. Moves Windows from an auto-managed (~3 GB) page file to a fixed-start page file on C:
# (the only NVMe SSD). Takes effect after the next reboot. Does not reboot.
$ErrorActionPreference = 'Stop'
$log = 'D:\Projects\agent-harness\scripts\set-pagefile.log'
try {
    $cs = Get-CimInstance Win32_ComputerSystem
    if ($cs.AutomaticManagedPagefile) {
        Set-CimInstance -InputObject $cs -Property @{ AutomaticManagedPagefile = $false }
    }
    $existing = Get-CimInstance Win32_PageFileSetting | Where-Object Name -like 'C:\pagefile.sys'
    if (-not $existing) {
        New-CimInstance -ClassName Win32_PageFileSetting -Property @{ Name = 'C:\pagefile.sys' } | Out-Null
        $existing = Get-CimInstance Win32_PageFileSetting | Where-Object Name -like 'C:\pagefile.sys'
    }
    # 32 GB up front lifts the commit limit to ~64 GB; allowed to grow to 64 GB if ever needed.
    Set-CimInstance -InputObject $existing -Property @{ InitialSize = [uint32]32768; MaximumSize = [uint32]65536 }
    $after = Get-CimInstance Win32_PageFileSetting | Select-Object Name, InitialSize, MaximumSize
    "OK $(Get-Date -Format s) automatic=$((Get-CimInstance Win32_ComputerSystem).AutomaticManagedPagefile) $($after | ConvertTo-Json -Compress)" | Out-File $log -Encoding utf8
} catch {
    "ERROR $(Get-Date -Format s) $_" | Out-File $log -Encoding utf8
    exit 1
}
