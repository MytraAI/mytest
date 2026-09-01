<#
.SYNOPSIS
    Prepares a Windows stand box for unattended test runs: never sleeps, never powers
    down its I/O, and answers SSH by mDNS name.

.DESCRIPTION
    Idempotent - every step reads current state and changes only what is wrong, so
    re-running is cheap and safe. Nothing in here pins a machine identity: no static
    address, no rename. Boxes are reached as <hostname>.local.

    Power     display / sleep / hibernate / unattended-sleep -> never, lid close -> do
              nothing, USB selective suspend / PCIe ASPM / disk spindown / wireless
              power saving / Energy Saver off, per-device "turn off to save power"
              cleared, screen saver off, the automatic lock off (-KeepAutoLock to keep
              it), and a boot-time task holding a wake lock.
    SSH       OpenSSH.Server installed, sshd Automatic with restart-on-failure,
              DefaultShell -> pwsh, optional public key for -TargetUser.
    Network   inbound TCP 22 open on the Private and Domain profiles (not Public),
              inbound UDP 5353 for mDNS, connection profiles moved off Public.

    Reach a box by whatever name DHCP registered for it in DNS. The mDNS/.local path
    works only when client and box share a subnet - it cannot cross a router.

.PARAMETER TargetUser
    Local account the public key is installed for. Not the account running the script -
    UAC elevation can change that, so this is always explicit.

.PARAMETER PublicKey
    An SSH public key, either the key text or a path to a .pub file. If omitted the
    script prompts; a blank answer skips key setup and leaves password auth as the only
    way in.

.PARAMETER InstallWakeLockTaskOnly
    Register the boot-time wake lock task and do nothing else.

.PARAMETER KeepAwake
    After setup, block in the foreground holding a wake lock until Ctrl+C. The boot task
    already covers reboots; this is for watching a specific run.

.PARAMETER InstallBonjour
    If the built-in mDNS responder does not answer, attempt to install Apple's Bonjour.

.PARAMETER KeepAutoLock
    Leave the 'Interactive logon: Machine inactivity limit' policy alone. That policy locks
    the console after N minutes of no input regardless of any power or screen saver
    setting, and this script disables it by default: a stand behind a lock screen keeps
    running its tests but loses the operator dashboard and every on-screen GUI, which is
    the thing the rest of this script exists to prevent.

    Pass this on a box that must keep locking. The cost of the default is that the box
    stays signed in unattended, and the stand account is a local administrator.

.PARAMETER ResultsShareOnly
    Establish access to the results share and register the mirror task, and do nothing
    else. This is the repair an operator is told to run when a run's prompt reports that
    finished runs are not reaching the share - after a reimage, most likely.

.PARAMETER ResultsShareCredential
    Credentials for the results share. Prompted for if omitted. Never stored in this
    file: it goes into the machine's SMB credential store and nowhere else.

.PARAMETER ResultsShareUnc
    The server share holding the results tree. The mirror writes under
    <share>\TestResults\MytestResults.

.PARAMETER RepoPath
    The mytest checkout the mirror task runs from. Defaults to the checkout this script
    is in.

.PARAMETER MirrorIntervalMinutes
    How often the mirror looks for finished runs to copy.

.PARAMETER PowerOnly
    Apply the power and wake-lock work only; leave SSH, firewall and mDNS alone.

.PARAMETER SshOnly
    Apply the SSH, firewall and mDNS work only; leave power settings alone.

.PARAMETER DisableHibernation
    Also run 'powercfg /hibernate off'. Frees hiberfil.sys but disables Fast Startup.

.PARAMETER MaxProcessor
    Pin minimum processor state to 100% and disable idle states. Runs hot; use only on a
    well-ventilated box that stays plugged in.

.PARAMETER AllDevices
    Clear the device power-management checkbox for every device that exposes it, not just
    USB and network adapters.

.PARAMETER SshPort
    Port for sshd and the firewall rule. Default 22.

.PARAMETER InstallTimeoutMinutes
    Ceiling on the OpenSSH capability install. Default 30.

.PARAMETER StallMinutes
    Abandon the install wait after this long with no measurable servicing progress.
    Default 5. The install itself is never cancelled, only the waiting.

.PARAMETER PollSeconds
    Heartbeat interval while waiting on the capability install. Default 30.

.EXAMPLE
    .\Setup-StandBox.ps1

.EXAMPLE
    .\Setup-StandBox.ps1 -PublicKey ~\Desktop\id_ed25519.pub -WhatIf

.NOTES
    Power scheme changes revert with:  powercfg -restoredefaultschemes
    (that resets every scheme to Windows defaults, not only what this touched).
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TargetUser            = 'seitteam',
    [string]$PublicKey,
    [int]   $SshPort               = 22,
    [int]   $InstallTimeoutMinutes = 30,
    [int]   $StallMinutes          = 5,
    [int]   $PollSeconds           = 30,
    [switch]$PowerOnly,
    [switch]$SshOnly,
    [switch]$InstallWakeLockTaskOnly,
    [switch]$ResultsShareOnly,
    [pscredential]$ResultsShareCredential,
    [string]$ResultsShareUnc        = '\\nas.mytra.co\SEIT',
    [string]$RepoPath,
    [int]   $MirrorIntervalMinutes  = 5,
    [switch]$InstallBonjour,
    [switch]$KeepAutoLock,
    [switch]$DisableHibernation,
    [switch]$MaxProcessor,
    [switch]$AllDevices,
    [switch]$KeepAwake
)

$ErrorActionPreference = 'Stop'

# PowerShell 7.4 makes a non-zero exit from a native command a terminating error when
# $ErrorActionPreference is 'Stop'. This script checks exit codes itself and carries on,
# so that behaviour has to be off. Assigning it on Windows PowerShell 5.1, where the
# variable does not exist, is harmless.
$PSNativeCommandUseErrorActionPreference = $false

if ($PowerOnly -and $SshOnly) { throw '-PowerOnly and -SshOnly are mutually exclusive.' }

$script:Notes    = [System.Collections.Generic.List[string]]::new()
$script:SshdOk   = $false
$script:KeyOk    = $false
$script:MdnsOk   = $false
$script:WakeTask   = $false
$script:MirrorTask = $false

$WakeDir      = Join-Path $env:ProgramData 'StandBox'
$WakeScript   = Join-Path $WakeDir 'Hold-Wake.ps1'
$WakeTaskName = 'StandBox-WakeLock'
$MirrorTaskName = 'StandBox-ResultsMirror'

#region output helpers -------------------------------------------------------

function Write-Step { param([string]$m) Write-Host "`n=== $m" -ForegroundColor Cyan }
function Write-Ok   { param([string]$m) Write-Host "  [ ok ] $m"  -ForegroundColor Green }
function Write-Fix  { param([string]$m) Write-Host "  [fixed] $m" -ForegroundColor Yellow }
function Write-Skip { param([string]$m) Write-Host "  [skip] $m"  -ForegroundColor DarkGray }
function Write-Note { param([string]$m) Write-Host "  [warn] $m"  -ForegroundColor Magenta; $script:Notes.Add($m) }

function Invoke-Native {
    # Runs an external command and returns its exit code and combined output.
    #
    # $ErrorActionPreference must be relaxed for the call: under 'Stop', Windows
    # PowerShell turns anything a native command writes to stderr into a terminating
    # NativeCommandError as soon as 2>&1 merges the streams. powercfg writes to stderr
    # for every setting a machine does not support, so without this the graceful
    # "[skip] not supported here" path would throw instead.
    param(
        [Parameter(Mandatory)][string]$File,
        [string[]]$Arguments = @()
    )
    # $LASTEXITCODE is not cleared when a command fails to launch, so without this an
    # executable that does not exist would silently inherit the previous command's exit
    # code - reporting success for something that never ran.
    if (-not (Get-Command $File -CommandType Application -ErrorAction SilentlyContinue)) {
        return [pscustomobject]@{ ExitCode = -1; Output = "'$File' was not found" }
    }

    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $File @Arguments 2>&1 | Out-String
        [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = $out }
    } finally {
        $ErrorActionPreference = $prev
    }
}

#endregion

#region elevation ------------------------------------------------------------

$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'Elevation required - relaunching as administrator...' -ForegroundColor Yellow
    $psExe   = (Get-Process -Id $PID).Path
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"")
    foreach ($sw in 'PowerOnly','SshOnly','InstallWakeLockTaskOnly','ResultsShareOnly','InstallBonjour',
                    'KeepAutoLock','DisableHibernation','MaxProcessor','AllDevices','KeepAwake') {
        if ($PSBoundParameters[$sw]) { $argList += "-$sw" }
    }
    foreach ($p in 'TargetUser','PublicKey','SshPort','InstallTimeoutMinutes','StallMinutes','PollSeconds',
                   'ResultsShareUnc','RepoPath','MirrorIntervalMinutes') {
        if ($PSBoundParameters.ContainsKey($p)) { $argList += @("-$p", "`"$($PSBoundParameters[$p])`"") }
    }
    # Without these two, `.\Setup-StandBox.ps1 -WhatIf` run unelevated would relaunch
    # without -WhatIf and make every change for real.
    # A pscredential cannot be passed on a command line, so it does not survive this.
    # Said out loud, because otherwise the elevated window just asks again and the
    # person who already supplied it has no idea why.
    if ($PSBoundParameters.ContainsKey('ResultsShareCredential')) {
        Write-Host '  -ResultsShareCredential cannot cross elevation - the elevated window will prompt.' -ForegroundColor Yellow
    }
    if ($WhatIfPreference)                   { $argList += '-WhatIf' }
    if ($PSBoundParameters.ContainsKey('Verbose')) { $argList += '-Verbose' }

    # -NoExit: the elevated window is a new console, and its summary is the whole point
    # of the run. Without this it closes the instant the script ends.
    $argList = @('-NoExit') + $argList

    try {
        Start-Process -FilePath $psExe -ArgumentList $argList -Verb RunAs
    } catch {
        Write-Host 'Elevation was declined - nothing has been changed.' -ForegroundColor Red
    }
    return
}

Write-Host ("Setup-StandBox - {0} - {1}" -f $env:COMPUTERNAME, (Get-Date -Format 'yyyy-MM-dd HH:mm')) -ForegroundColor White

#endregion

#region power ----------------------------------------------------------------

# Power setting GUIDs. Aliases such as SUB_SLEEP are absent on some SKUs, so raw
# GUIDs are used throughout.
$SUB = @{
    Sleep       = '238c9fa8-0aad-41ed-83f4-97be242c8f20'
    Video       = '7516b95f-f776-4464-8c53-06167f40cc99'
    Disk        = '0012ee47-9041-4b5d-9b77-535fba8b1442'
    Usb         = '2a737441-1930-4402-8d77-b2bebba308a3'
    Buttons     = '4f971e89-eebd-4455-a8de-9e59040e7347'
    PciExpress  = '501a4d13-42af-4429-9fd1-a8218c268e20'
    Processor   = '54533251-82be-4824-96c1-47b60b740d00'
    Wireless    = '19cbb8fa-5279-450e-9fac-8a3d5fedd0c1'
    Multimedia  = '9596fb26-9850-41fd-ac3e-f7c3c00afd4b'
    EnergySaver = 'de830923-a562-41af-a086-e3a2c6bad2da'
}

function Set-PowerValue {
    # Writes one setting to the active scheme on both AC and battery. The set of
    # supported settings varies by machine, so an unsupported one is reported and
    # skipped rather than thrown.
    param(
        [Parameter(Mandatory)][string]$Label,
        [Parameter(Mandatory)][string]$SubGroup,
        [Parameter(Mandatory)][string]$Setting,
        [Parameter(Mandatory)][int]   $Value
    )
    if (-not $PSCmdlet.ShouldProcess($Label, 'set power value')) { return }

    $ok = $true
    foreach ($rail in 'ac', 'dc') {
        $r = Invoke-Native powercfg.exe @("/set${rail}valueindex", 'SCHEME_CURRENT', $SubGroup, $Setting, "$Value")
        if ($r.ExitCode -ne 0) { $ok = $false }
    }
    if ($ok) { Write-Ok $Label } else { Write-Skip "$Label - not supported here" }
}

function Set-NeverSleep {
    Write-Step "Power scheme: $((& powercfg.exe /getactivescheme) -join ' ')"

    Set-PowerValue 'sleep after: never'              $SUB.Sleep       '29f6c1db-86da-48c5-9fdb-f2b67b1f44da' 0
    Set-PowerValue 'hibernate after: never'          $SUB.Sleep       '9d7815a6-7ee4-497e-8888-515a05f02364' 0
    Set-PowerValue 'unattended sleep timeout: never' $SUB.Sleep       '7bc4a2f9-d8fc-4469-b07b-33eb785aaca0' 0
    Set-PowerValue 'hybrid sleep: off'               $SUB.Sleep       '94ac6d29-73ce-41a6-809f-6363ba21b47e' 0
    Set-PowerValue 'turn off display: never'         $SUB.Video       '3c0bc021-c8a8-4e07-a973-6b14cbcb2b7e' 0
    Set-PowerValue 'console lock display off: never' $SUB.Video       '8ec4b3a5-6868-48c2-be75-4f3044be88a7' 0
    Set-PowerValue 'adaptive brightness: off'        $SUB.Video       'fbd9aa66-9553-4097-ba44-ed6e9d65eab8' 0
    Set-PowerValue 'turn off hard disk: never'       $SUB.Disk        '6738e2c4-e8a5-4a42-b16a-e040e769756e' 0
    Set-PowerValue 'USB selective suspend: off'      $SUB.Usb         '48e6b7a6-50f5-4782-a5d4-53bb8f07e226' 0
    Set-PowerValue 'USB 3 link power mgmt: off'      $SUB.Usb         'd4e98f31-5ffe-4ce1-be31-1b38b384c009' 0
    Set-PowerValue 'hub selective suspend: 0'        $SUB.Usb         '0853a681-27c8-4100-a2fd-82013e970683' 0
    Set-PowerValue 'lid close: do nothing'           $SUB.Buttons     '5ca83367-6e45-459f-a27b-476b1d01c936' 0
    Set-PowerValue 'sleep button: do nothing'        $SUB.Buttons     '96996bc0-ad50-47ec-923b-6f41874dd9eb' 0
    Set-PowerValue 'PCIe link power mgmt: off'       $SUB.PciExpress  'ee12f906-d277-404b-b6da-e5fa1a576df5' 0
    Set-PowerValue 'wireless adapter: max perf'      $SUB.Wireless    '12bbebe6-58d6-4636-95bb-3217ef867c1a' 0
    Set-PowerValue 'media sharing: prevent idling'   $SUB.Multimedia  '03680956-93bc-4294-bba6-4e0f09bb717f' 1
    Set-PowerValue 'Energy Saver threshold: never'   $SUB.EnergySaver 'e69653ca-cf7f-4f05-aa73-cb833fa90ad4' 0

    if ($MaxProcessor) {
        Set-PowerValue 'min processor state: 100%'   $SUB.Processor '893dee8e-2bef-41e0-89c6-b55d0929964c' 100
        Set-PowerValue 'processor idle states: off'  $SUB.Processor '5d76a2ca-e8c0-402f-a133-2158492d58ad' 1
    }

    # setac/setdcvalueindex only take effect once the scheme is re-activated.
    if ($PSCmdlet.ShouldProcess('active scheme', 'reactivate')) {
        $null = Invoke-Native powercfg.exe @('/setactive', 'SCHEME_CURRENT')
        foreach ($t in 'standby-timeout','monitor-timeout','disk-timeout','hibernate-timeout') {
            $null = Invoke-Native powercfg.exe @('/change', "$t-ac", '0')
            $null = Invoke-Native powercfg.exe @('/change', "$t-dc", '0')
        }
    }

    if ($DisableHibernation -and $PSCmdlet.ShouldProcess('hibernation', 'disable')) {
        $r = Invoke-Native powercfg.exe @('/hibernate', 'off')
        if ($r.ExitCode -eq 0) { Write-Fix 'hibernation off (Fast Startup off with it)' }
        else                     { Write-Skip 'could not disable hibernation' }
    }
}

function Get-PowerManagedDevice {
    # Devices exposing "Allow the computer to turn off this device to save power",
    # joined to their PnP entry for class filtering and readable names.
    $wmi = Get-CimInstance -Namespace root\wmi -ClassName MSPower_DeviceEnable -ErrorAction SilentlyContinue
    if (-not $wmi) { return @() }

    $pnp = @{}
    foreach ($d in Get-PnpDevice -ErrorAction SilentlyContinue) { $pnp[$d.InstanceId.ToUpperInvariant()] = $d }

    foreach ($w in $wmi) {
        # MSPower instance names are the PnP instance id with a _<n> suffix.
        $dev  = $pnp[($w.InstanceName -replace '_\d+$', '').ToUpperInvariant()]
        $name = if ($dev -and $dev.FriendlyName) { $dev.FriendlyName } else { $w.InstanceName }
        [pscustomobject]@{ Wmi = $w; Device = $dev; Class = $dev.Class; Name = $name }
    }
}

function Disable-DevicePowerSaving {
    Write-Step "Device 'turn off to save power' checkboxes"

    $targets = Get-PowerManagedDevice
    if (-not $AllDevices) { $targets = $targets | Where-Object { $_.Class -in @('USB','Net') } }
    if (-not $targets) { Write-Skip 'no power-manageable devices reported'; return }

    foreach ($t in $targets) {
        if (-not $t.Wmi.Enable) { Write-Ok "already off: $($t.Name)"; continue }
        if (-not $PSCmdlet.ShouldProcess($t.Name, 'clear power management')) { continue }
        try {
            Set-CimInstance -InputObject $t.Wmi -Property @{ Enable = $false } -ErrorAction Stop
            Write-Fix $t.Name
        } catch {
            Write-Note "$($t.Name): $($_.Exception.Message)"
        }
    }

    if (Get-Command Set-NetAdapterPowerManagement -ErrorAction SilentlyContinue) {
        foreach ($a in (Get-NetAdapter -Physical -ErrorAction SilentlyContinue)) {
            try {
                Set-NetAdapterPowerManagement -Name $a.Name -DeviceSleepOnDisconnect Disabled -ErrorAction Stop
                Write-Fix "sleep-on-disconnect off: $($a.Name)"
            } catch {
                Write-Verbose "$($a.Name): $($_.Exception.Message)"
            }
        }
    }
}

function Disable-ScreenSaver {
    Write-Step 'Screen saver'
    $desktop = 'HKCU:\Control Panel\Desktop'
    try {
        Set-ItemProperty -Path $desktop -Name 'ScreenSaveActive'    -Value '0' -Type String
        Set-ItemProperty -Path $desktop -Name 'ScreenSaveTimeOut'   -Value '0' -Type String
        Set-ItemProperty -Path $desktop -Name 'ScreenSaverIsSecure' -Value '0' -Type String
        # The registry values alone do not take effect until the next sign-in.
        $null = Invoke-Native rundll32.exe @('user32.dll,UpdatePerUserSystemParameters')
        Write-Fix 'disabled for the elevating account'
        Write-Skip "under UAC this is HKCU of whoever elevated, not necessarily $TargetUser"
    } catch {
        Write-Note "screen saver: $($_.Exception.Message)"
    }
}

#endregion

#region wake lock ------------------------------------------------------------

# ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED, and ES_CONTINUOUS alone to
# release. Written in decimal on purpose: Windows PowerShell 5.1 parses 0x80000000 as a
# negative Int32 while PowerShell 7 parses it as Int64, and the negative value will not
# cast to the uint32 the API takes.
$WakeFlags    = [uint32]2147483651
$WakeRelease  = [uint32]2147483648

function Set-AutoLockPolicy {
    # 'Interactive logon: Machine inactivity limit'. Locks the workstation after N seconds
    # of no input, independently of screen saver and power settings - so a box with every
    # timeout set to never still ends up behind a lock screen, which no amount of powercfg
    # work prevents.
    #
    # DISABLED BY DEFAULT, AND IT DID NOT USED TO BE. This needed an opt-in -DisableAutoLock
    # and without it the function only printed a warning, so both stands were provisioned
    # and then went on locking themselves for weeks - the warning scrolls past in a run
    # this long, and the box behaves exactly as though the script had never been run. A
    # setting that has to be remembered separately from the run that configures everything
    # else is one nobody remembers.
    #
    # NOTHING ELSE MANAGES THIS VALUE, so writing the registry is enough and no policy
    # tooling is needed. Checked on both stands: they are in a workgroup, there is no
    # local policy template, and the setting is Not Defined in the local security
    # database - and a value written here survived a gpupdate /force on SEIT-LT-2. Beware
    # `secedit /export` without `/db`, which reports the live registry back and so looks
    # like a second, independent source agreeing with it.
    $key = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
    $cur = (Get-ItemProperty $key -Name InactivityTimeoutSecs -ErrorAction SilentlyContinue).InactivityTimeoutSecs

    Write-Step 'Automatic lock (machine inactivity limit)'

    if ($null -eq $cur -or $cur -eq 0) { Write-Ok 'no automatic lock configured'; return }

    # Rounded, because this is a duration in a sentence: a box shipped with a limit that is
    # not a whole number of minutes prints 14.8333333333333 otherwise.
    $mins = [math]::Round($cur / 60, 1)
    if ($KeepAutoLock) {
        Write-Note "this box locks itself after $mins min of no input and -KeepAutoLock was passed, so it was left alone - no power setting prevents that lock."
        return
    }

    if (-not $PSCmdlet.ShouldProcess('InactivityTimeoutSecs', 'set to 0')) { return }

    # Guarded like Disable-ScreenSaver above, and for a reason that is new: this script runs
    # under $ErrorActionPreference = 'Stop', so an unhandled failure here terminates the run
    # before SSH, the firewall and the results mirror are ever set up. That was survivable
    # while the write only happened for somebody who passed a switch. Now that it happens on
    # every run, one refused write would take the whole provisioning down with it.
    try {
        Set-ItemProperty -Path $key -Name InactivityTimeoutSecs -Value 0 -Type DWord
    } catch {
        Write-Note "automatic lock: $($_.Exception.Message)"
        return
    }

    # Read back rather than assumed. This is the one setting on these boxes whose failure
    # mode was months of silence, so it reports what the machine holds, not what was sent.
    $after = (Get-ItemProperty $key -Name InactivityTimeoutSecs -ErrorAction SilentlyContinue).InactivityTimeoutSecs
    if ($null -eq $after) {
        # Deliberately not folded into the check below. $null -ne 0 is TRUE in PowerShell,
        # so one test would blame an enforcing policy for a read that merely failed, and
        # print a blank where the number goes.
        Write-Note 'set the automatic lock to 0 but could not read the value back to confirm it.'
        return
    }
    if ($after -ne 0) {
        Write-Note "tried to disable the automatic lock but it still reads $after s - something else is enforcing it. Look for a domain policy or a management agent."
        return
    }

    Write-Fix "automatic lock disabled (was $mins min) - applies from the next sign-in"
    Write-Skip 'the console then stays unlocked: anyone with physical access has a live session as this account'
}

function Get-WakeLock {
    if (-not ('Win32.Power' -as [type])) {
        Add-Type -Namespace Win32 -Name Power -MemberDefinition '[System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)] public static extern uint SetThreadExecutionState(uint esFlags);'
    }
    [void][Win32.Power]::SetThreadExecutionState($WakeFlags)
}

function Install-WakeLockTask {
    Write-Step 'Boot-time wake lock task'

    # A session-0 task can assert ES_SYSTEM_REQUIRED reliably; ES_DISPLAY_REQUIRED
    # from session 0 does not reliably keep the panel lit. The powercfg display
    # timeout above is what actually does that.
    $body = @'
# Holds a system wake lock for the life of the machine. Registered by Setup-StandBox.ps1.
Add-Type -Namespace Win32 -Name Power -MemberDefinition '[System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)] public static extern uint SetThreadExecutionState(uint esFlags);'
while ($true) {
    [void][Win32.Power]::SetThreadExecutionState([uint32]2147483651)
    Start-Sleep -Seconds 60
}
'@

    if (-not $PSCmdlet.ShouldProcess($WakeTaskName, 'register scheduled task')) { return }

    try {
        if (-not (Test-Path $WakeDir)) { New-Item -ItemType Directory -Path $WakeDir -Force | Out-Null }
        # This script is executed as SYSTEM at every boot. ProgramData's inherited ACL
        # lets standard users create files in subfolders, so the permissions are pinned
        # explicitly: anyone who could replace this file would get SYSTEM at startup.
        $null = Invoke-Native icacls.exe @($WakeDir, '/inheritance:r',
                                           '/grant', '*S-1-5-18:(OI)(CI)F',
                                           '/grant', '*S-1-5-32-544:(OI)(CI)F',
                                           '/grant', '*S-1-5-32-545:(OI)(CI)RX')
        Set-Content -Path $WakeScript -Value $body -Encoding ascii
        Write-Fix "wrote $WakeScript (SYSTEM/Administrators write, Users read-only)"

        $exe = (Get-Command powershell.exe).Source
        $act = New-ScheduledTaskAction -Execute $exe `
                 -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$WakeScript`""
        $trg = New-ScheduledTaskTrigger -AtStartup
        $pri = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                 -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

        Register-ScheduledTask -TaskName $WakeTaskName -Action $act -Trigger $trg `
            -Principal $pri -Settings $set -Force | Out-Null
        Start-ScheduledTask -TaskName $WakeTaskName -ErrorAction SilentlyContinue
        Write-Fix "task '$WakeTaskName' registered at startup and started now"
        $script:WakeTask = $true
    } catch {
        Write-Note "wake lock task: $($_.Exception.Message)"
    }
}

function Install-ResultsMirror {
    Write-Step 'Results share and mirror task'

    # Idempotent, like the rest of this script: a box where the share already works
    # is left alone. That matters more here than elsewhere - remapping means removing
    # the existing mapping first, and if the new credential turns out to be wrong the
    # box ends up worse off than before the "repair". It also stops a full provisioning
    # run prompting for share credentials every time.
    Write-Host "  checking $ResultsShareUnc (up to ~20s if it is unreachable)..." -ForegroundColor DarkGray
    if (-not $ResultsShareCredential -and (Test-Path $ResultsShareUnc -ErrorAction SilentlyContinue)) {
        Write-Ok "$ResultsShareUnc already reachable"
    } else {
        Install-ResultsShareMapping
    }

    Install-ResultsMirrorTask
}


function Install-ResultsShareMapping {
    # A machine-wide SMB mapping, not `net use` and not `cmdkey`. Both of those are
    # per-user: a credential stored by whoever ran this script is invisible to any other
    # account, and this box's operator account is not necessarily the account that
    # provisioned it. A global mapping is visible to every session on the machine,
    # including a service, so the mirror keeps working whoever ends up running it.
    $cred = $ResultsShareCredential
    if (-not $cred) {
        # -WhatIf is documented as a true dry run, and a prompt is not nothing: it
        # stops the run dead waiting for a person who only asked what would happen.
        if ($WhatIfPreference) {
            Write-Skip "would prompt for credentials and map $ResultsShareUnc"
            return
        }
        # Get-Credential over SSH does not fail, it blocks forever - there is a console
        # to write to and nothing to read from. Refused up front with the same advice
        # this would otherwise never get to give. $env:SSH_CONNECTION is how the sshd
        # section decides the same thing.
        if ($env:SSH_CONNECTION -or $env:SSH_CLIENT) {
            Write-Note "over SSH with no -ResultsShareCredential - cannot prompt for one here without hanging. Run this at the console, or pass -ResultsShareCredential."
            return
        }
        # Prompted, never stored in this file or in the repo.
        try {
            $cred = Get-Credential -Message "Credentials for $ResultsShareUnc"
        } catch {
            Write-Note "could not prompt for credentials for $ResultsShareUnc ($($_.Exception.Message)) - pass -ResultsShareCredential"
            return
        }
    }
    if (-not $cred) { Write-Note "no credential given - $ResultsShareUnc not mapped"; return }

    if ($PSCmdlet.ShouldProcess($ResultsShareUnc, 'map for every session on this machine')) {
        try {
            $existing = Get-SmbGlobalMapping -RemotePath $ResultsShareUnc -ErrorAction SilentlyContinue
            if ($existing) {
                Remove-SmbGlobalMapping -RemotePath $ResultsShareUnc -Force -ErrorAction Stop
            }
            New-SmbGlobalMapping -RemotePath $ResultsShareUnc -Credential $cred `
                -Persistent $true -ErrorAction Stop | Out-Null
            Write-Fix "$ResultsShareUnc mapped for every session"
        } catch {
            # Older builds have no SmbGlobalMapping at all. cmdkey is the fallback and is
            # per-user, so it only serves a mirror running as this same account - which is
            # how the task below is registered, so it is a real fallback and not a stub.
            Write-Note "global mapping failed ($($_.Exception.Message)) - falling back to a per-user credential"
            $server = ([uri]$ResultsShareUnc.Replace('\', '/')).Host
            # cmdkey takes the password as an argument and offers nothing better, so it is
            # briefly visible to anything listing processes on this box. Acceptable for a
            # fallback on a stand box; it is why the global mapping is tried first.
            $fallback = Invoke-Native cmdkey.exe @("/add:$server",
                                                   "/user:$($cred.UserName)",
                                                   "/pass:$($cred.GetNetworkCredential().Password)")
            if ($fallback.ExitCode -eq 0) {
                Write-Fix "credential for $server stored for $env:USERNAME"
            } else {
                Write-Note "cmdkey also failed: $($fallback.Output.Trim())"
            }
        }
    }
}


function Install-ResultsMirrorTask {
    # Where the mirror runs from, and with what. Defaults to the checkout this script
    # lives in, which is the checkout somebody just pulled onto the box.
    $repo = if ($RepoPath) { $RepoPath } else { Split-Path -Parent $PSScriptRoot }
    if (-not (Test-Path (Join-Path $repo 'tools\mirror_results.py'))) {
        # Says what was actually looked for. "No checkout here" is wrong and
        # misleading when there is one, on a branch that predates the mirror.
        Write-Note "$repo has no tools\mirror_results.py - wrong path, or a checkout that predates the mirror. Task not registered."
        return
    }
    # pythonw, so a pass every few minutes does not flash a console window on a stand's
    # screen. The venv's copy if there is one, since that is where this project's
    # dependencies are.
    $python = Join-Path $repo '.venv\Scripts\pythonw.exe'
    if (-not (Test-Path $python)) { $python = 'pythonw.exe' }

    if (-not $PSCmdlet.ShouldProcess($MirrorTaskName, 'register scheduled task')) { return }

    try {
        $act = New-ScheduledTaskAction -Execute $python `
                 -Argument '-m tools.mirror_results' -WorkingDirectory $repo

        # Two triggers rather than one with a repetition attached: at logon so a rebooted
        # box starts mirroring as soon as somebody is on it, and a repeating one so it
        # keeps looking. Overlap between them is harmless - see IgnoreNew below.
        # No -RepetitionDuration: omitting it is what means "repeat indefinitely".
        # The documented-looking alternatives do not survive registration - measured on
        # a stand box, both [TimeSpan]::MaxValue and ::Zero build a trigger object
        # happily and are then rejected by Register-ScheduledTask with "value ...
        # incorrectly formatted or out of range", taking the whole task with them.
        $triggers = @(
            New-ScheduledTaskTrigger -AtLogOn -User $TargetUser
            New-ScheduledTaskTrigger -Once -At (Get-Date) `
              -RepetitionInterval (New-TimeSpan -Minutes $MirrorIntervalMinutes)
        )

        # Interactive, as the stand account: the mirror reads the engine's heartbeat to
        # find where runs are being written, and that file lives in the running user's
        # temp directory. A SYSTEM task resolves a different temp directory, a different
        # home, and would need the output directory hardcoded here instead - which is
        # exactly the second copy of "where results live" that reading the heartbeat
        # exists to avoid. Interactive also needs no stored password.
        $pri = New-ScheduledTaskPrincipal -UserId $TargetUser -LogonType Interactive -RunLevel Limited

        # IgnoreNew: a 24 h endurance run is gigabytes, and a copy can still be going when
        # the next tick fires. ExecutionTimeLimit is hours and not zero, unlike the wake
        # lock's: this is a short-lived pass, so one that is still alive after that long is
        # blocked on a dead server and should be killed rather than left holding the slot.
        $set = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
                 -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
                 -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
                 -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)

        Register-ScheduledTask -TaskName $MirrorTaskName -Action $act `
            -Trigger $triggers -Principal $pri -Settings $set -Force | Out-Null
        Start-ScheduledTask -TaskName $MirrorTaskName -ErrorAction SilentlyContinue
        Write-Fix "task '$MirrorTaskName' registered every $MirrorIntervalMinutes min as $TargetUser, and started now"
        $script:MirrorTask = $true
    } catch {
        Write-Note "results mirror task: $($_.Exception.Message)"
    }
}


function Invoke-HoldAwake {
    Write-Step 'Holding this session awake (Ctrl+C to release)'
    try {
        while ($true) {
            Get-WakeLock
            Write-Host ("`r  wake lock held - {0} " -f (Get-Date -Format 'HH:mm:ss')) -NoNewline -ForegroundColor DarkGray
            Start-Sleep -Seconds 30
        }
    } finally {
        # Guarded: if Add-Type itself failed, this cleanup would throw and mask the real
        # error on the way out.
        if ('Win32.Power' -as [type]) {
            [void][Win32.Power]::SetThreadExecutionState($WakeRelease)
            Write-Host "`n  wake lock released." -ForegroundColor DarkGray
        }
    }
}

#endregion

#region openssh --------------------------------------------------------------

function Wait-CapabilityInstall {
    <#
      Add-WindowsCapability pulls Features on Demand from Windows Update and can run
      for many minutes with no output. Runs it as a job and reports a heartbeat,
      treating CBS.log growth or TrustedInstaller CPU time as proof of progress.

      Returns $true only if the job completed. On stall or timeout it returns $false
      WITHOUT cancelling anything: the work runs inside TrustedInstaller, Stop-Job
      only kills the wrapper, and killing TiWorker mid-servicing risks corrupting the
      component store. Waiting is abandoned; the install is not.
    #>
    param([Parameter(Mandatory)][string]$Name)

    $cbs = Join-Path $env:SystemRoot 'Logs\CBS\CBS.log'
    function Get-ServicingPulse {
        $len = 0
        try { $len = (Get-Item $cbs -ErrorAction Stop).Length } catch { }
        $cpu = (Get-Process -Name TiWorker, TrustedInstaller -ErrorAction SilentlyContinue |
                Measure-Object -Property CPU -Sum).Sum
        "$len/$cpu"
    }

    # Windows PowerShell explicitly, as a process rather than a job. Add-WindowsCapability
    # comes from the DISM module, which is a .NET Framework binary module: under
    # PowerShell 7 it is only reachable through the WinPSCompatSession shim, and that is
    # an extra failure mode to carry into a 30-minute unattended install. A process also
    # survives independently of this session, which is what "abandon the wait, not the
    # install" requires.
    $winPs   = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $outFile = Join-Path $env:TEMP 'standbox-capability.out'
    $errFile = Join-Path $env:TEMP 'standbox-capability.err'
    Remove-Item $outFile, $errFile -ErrorAction SilentlyContinue

    if (-not (Test-Path $winPs)) {
        Write-Note "Windows PowerShell not found at $winPs - cannot run the capability install"
        return $false
    }

    # -NoNewWindow, not -WindowStyle: only the former reliably shares a parameter set
    # with the redirection switches. Both streams go to files, so nothing reaches the
    # console either way.
    $proc = Start-Process -FilePath $winPs -PassThru -NoNewWindow `
              -RedirectStandardOutput $outFile -RedirectStandardError $errFile `
              -ArgumentList @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                              '-Command', "Add-WindowsCapability -Online -Name '$Name' -ErrorAction Stop")

    $started  = Get-Date
    $deadline = $started.AddMinutes($InstallTimeoutMinutes)
    $lastPulse = Get-ServicingPulse
    $lastMoved = Get-Date

    Write-Host "  installing $Name - up to $InstallTimeoutMinutes min, heartbeat every ${PollSeconds}s" -ForegroundColor DarkGray

    while ($true) {
        Start-Sleep -Seconds $PollSeconds
        $elapsed = [int]((Get-Date) - $started).TotalMinutes

        if ($proc.HasExited) { break }

        $pulse = Get-ServicingPulse
        if ($pulse -ne $lastPulse) { $lastPulse = $pulse; $lastMoved = Get-Date }
        $stalledFor = [int]((Get-Date) - $lastMoved).TotalMinutes

        $mark = if ($stalledFor -ge 1) { "no progress for ${stalledFor}m" } else { 'working' }
        Write-Host ("  [{0,3} min] {1}" -f $elapsed, $mark) -ForegroundColor DarkGray

        if ($stalledFor -ge $StallMinutes) {
            Write-Note "install showed no progress for $StallMinutes min - abandoning the wait at $elapsed min"
            return $false
        }
        if ((Get-Date) -gt $deadline) {
            Write-Note "install exceeded $InstallTimeoutMinutes min - abandoning the wait"
            return $false
        }
    }

    if ($proc.ExitCode -eq 0) {
        Remove-Item $outFile, $errFile -ErrorAction SilentlyContinue
        Write-Fix "installed $Name"
        return $true
    }

    $err = ''
    if (Test-Path $errFile) { $err = (Get-Content $errFile -Raw).Trim() }
    if (-not $err -and (Test-Path $outFile)) { $err = (Get-Content $outFile -Raw).Trim() }
    Write-Note "install exited $($proc.ExitCode): $err"
    return $false
}

function Install-Sshd {
    Write-Step 'OpenSSH server'

    $svc = Get-Service -Name sshd -ErrorAction SilentlyContinue
    if ($svc) {
        Write-Ok 'sshd service already present'
    }
    else {
        $cap = Get-WindowsCapability -Online -ErrorAction SilentlyContinue |
               Where-Object Name -like 'OpenSSH.Server*' | Select-Object -First 1
        if (-not $cap) {
            Write-Note 'this Windows image offers no OpenSSH.Server capability - install it by hand (winget install Microsoft.OpenSSH.Beta, or the "OpenSSH Server" optional feature)'
            return $false
        }
        if ($cap.State -ne 'Installed') {
            if (-not $PSCmdlet.ShouldProcess($cap.Name, 'Add-WindowsCapability')) { return $false }
            if (-not (Wait-CapabilityInstall -Name $cap.Name)) {
                Write-Note 'TrustedInstaller may still be working - do NOT kill TiWorker. Check with: Get-WindowsCapability -Online -Name OpenSSH.Server*  /  Get-Content C:\Windows\Logs\CBS\CBS.log -Tail 30 -Wait'
                Write-Note "re-run this script once it settles: $PSCommandPath"
                return $false
            }
        }
        # Service registration trails the capability install by a few seconds, so a
        # single check straight after it reports a false failure.
        $svc = $null
        foreach ($attempt in 1..6) {
            $svc = Get-Service -Name sshd -ErrorAction SilentlyContinue
            if ($svc) { break }
            Start-Sleep -Seconds 5
        }
        if (-not $svc) { Write-Note 'sshd service still missing 30s after install - a reboot may be needed'; return $false }
    }

    if ($svc.StartType -ne 'Automatic') {
        Set-Service -Name sshd -StartupType Automatic
        Write-Fix 'sshd startup type -> Automatic'
    } else { Write-Ok 'sshd starts automatically' }

    # Auto-start covers reboots; failure actions cover a crash mid-run.
    $r = Invoke-Native sc.exe @('failure', 'sshd', 'reset=', '86400', 'actions=', 'restart/5000/restart/10000/restart/30000')
    if ($r.ExitCode -eq 0) { Write-Ok 'sshd restarts automatically on failure' }
    else { Write-Note 'could not set sshd failure actions' }

    $agent = Get-Service ssh-agent -ErrorAction SilentlyContinue
    if ($agent -and $agent.StartType -eq 'Disabled') {
        Set-Service -Name ssh-agent -StartupType Manual
        Write-Fix 'ssh-agent startup type -> Manual'
    }

    if ($SshPort -ne 22) {
        $cfg = Join-Path $env:ProgramData 'ssh\sshd_config'
        if (Test-Path $cfg) {
            $body = Get-Content $cfg -Raw
            if ($body -notmatch "(?m)^\s*Port\s+$SshPort\s*$") {
                $body = $body -replace '(?m)^\s*#?\s*Port\s+\d+\s*$', ''
                Set-Content -Path $cfg -Value ("Port $SshPort`r`n" + $body.TrimStart()) -Encoding ascii
                Write-Fix "sshd_config Port -> $SshPort"
            } else { Write-Ok "sshd_config already listens on $SshPort" }
        }
    }

    # This script leaves password auth as the way in, so check rather than assume:
    # a previous run, or a hardening baseline, may have turned it off.
    $cfgPath = Join-Path $env:ProgramData 'ssh\sshd_config'
    if (Test-Path $cfgPath) {
        $cfgBody = Get-Content $cfgPath -Raw
        if ($cfgBody -match '(?m)^\s*PasswordAuthentication\s+no\b') {
            Write-Note "sshd_config sets PasswordAuthentication no, so password logins will be refused. If that is not intended, edit $cfgPath and restart sshd."
        } else {
            Write-Ok 'password authentication is enabled'
        }
    }

    Set-DefaultShell

    if ((Get-Service sshd).Status -ne 'Running') {
        Start-Service sshd
        Write-Fix 'sshd started'
    }
    elseif ($env:SSH_CONNECTION -or $env:SSH_CLIENT) {
        # Stopping the service takes its per-connection child processes with it, which
        # means restarting sshd from inside an SSH session drops that very session
        # mid-run. Re-running this script remotely is the normal case, so refuse.
        Write-Note 'sshd is already running and this script is running over SSH - NOT restarting it, because that would kill this session. Config changes (DefaultShell, Port) need: Restart-Service sshd, run from the console or as a scheduled task.'
    }
    else {
        Restart-Service sshd
        Write-Fix 'sshd restarted to pick up config'
    }

    return $true
}

function Set-DefaultShell {
    $pwsh = (Get-Command pwsh.exe -ErrorAction SilentlyContinue).Source
    if (-not $pwsh) { $pwsh = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe' }

    $key = 'HKLM:\SOFTWARE\OpenSSH'
    if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
    $current = (Get-ItemProperty -Path $key -Name DefaultShell -ErrorAction SilentlyContinue).DefaultShell

    if ($current -eq $pwsh) {
        Write-Ok "DefaultShell already $pwsh"
    }
    elseif ($PSCmdlet.ShouldProcess($pwsh, 'set sshd DefaultShell')) {
        Set-ItemProperty -Path $key -Name DefaultShell -Value $pwsh -Type String
        # Without this, sshd passes /c to a shell that expects -c and every
        # `ssh box '<command>'` invocation fails.
        Set-ItemProperty -Path $key -Name DefaultShellCommandOption -Value '-c' -Type String
        Write-Fix "DefaultShell -> $pwsh (command option -c)"
    }

    # A non-cmd DefaultShell breaks scp/sftp when the shell emits a banner or profile
    # output, because that contaminates the transfer stream. A real round trip can
    # only be tested from a client, so check the failure mode instead: the shell must
    # print nothing for a trivial command.
    # Deliberately WITHOUT -NoProfile: sshd does not pass it either, so a noisy profile
    # is exactly the failure being tested for and -NoProfile would hide it.
    $probe = Invoke-Native $pwsh @('-NonInteractive', '-c', 'exit')
    if ($probe.ExitCode -eq -1) {
        Write-Note "the configured default shell does not exist: $pwsh"
    }
    elseif ($probe.Output.Trim()) {
        Write-Note "the default shell prints output on startup, which corrupts scp/sftp transfers: $($probe.Output.Trim())"
    } else {
        Write-Ok 'default shell is silent on startup (scp/sftp stream should be clean)'
    }

    if (-not (Test-Path (Join-Path $env:ProgramFiles 'OpenSSH\sftp-server.exe')) -and
        -not (Test-Path (Join-Path $env:SystemRoot 'System32\OpenSSH\sftp-server.exe'))) {
        Write-Note 'sftp-server.exe not found - scp/sftp will not work even though ssh does'
    }
}

#endregion

#region authorized key -------------------------------------------------------

function Resolve-PublicKey {
    # Accepts key text or a path to a .pub file; prompts if neither was supplied.
    param([string]$Value)

    # A key blob contains characters that are illegal in a path, and Test-Path raises a
    # terminating ArgumentException on some of them rather than simply returning false.
    if ($Value) {
        try {
            if (Test-Path -LiteralPath $Value -PathType Leaf -ErrorAction SilentlyContinue) {
                $Value = (Get-Content -LiteralPath $Value -Raw)
            }
        } catch {
            Write-Verbose "not a usable path, treating as key text: $($_.Exception.Message)"
        }
    }
    if (-not $Value -and $WhatIfPreference) {
        Write-Skip 'would prompt for a public key (suppressed by -WhatIf)'
        return $null
    }
    # Read-Host has nothing to read from under a scheduled task, or from
    # `ssh box '...'` where no tty is allocated, and would block the run forever.
    if (-not $Value -and (-not [Environment]::UserInteractive -or [Console]::IsInputRedirected)) {
        Write-Skip 'no -PublicKey given and this session has no console - skipping key setup rather than blocking on a prompt'
        return $null
    }
    if (-not $Value) {
        Write-Host "  paste the operator's public key (the contents of ~/.ssh/id_ed25519.pub on their" -ForegroundColor DarkGray
        Write-Host '  own machine), or press Enter to skip and leave password auth as the only way in:' -ForegroundColor DarkGray
        $Value = Read-Host '  key'
    }

    $Value = ($Value -replace '\r?\n', ' ').Trim()
    if (-not $Value) { return $null }

    $valid = '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp\d+|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)\s+[A-Za-z0-9+/=]+(\s+\S.*)?$'
    if ($Value -notmatch $valid) {
        Write-Note 'that does not look like an SSH public key (expected e.g. "ssh-ed25519 AAAA... user@host") - skipping key setup'
        return $null
    }
    return $Value
}

function Test-LocalAdminMember {
    # $true, $false, or $null when membership genuinely could not be determined.
    #
    # Get-LocalGroupMember throws on machines where any member SID fails to resolve
    # (deleted local accounts, orphaned domain or AAD entries) - a long-standing bug,
    # and it takes the whole group listing down with it. The fallback matters because
    # guessing wrong here is not cosmetic: administrators_authorized_keys authorises a
    # key for EVERY administrator account, so a standard user's key placed there would
    # let that key log in as any admin.
    param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][string]$Sid)

    try {
        $members = Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction Stop
        return [bool]($members | Where-Object { $_.SID.Value -eq $Sid })
    } catch {
        Write-Skip "Get-LocalGroupMember failed - falling back to net localgroup ($($_.Exception.Message.Trim()))"
    }

    # 'Administrators' is localized on non-English Windows, so the name is resolved from
    # the well-known SID rather than hard-coded.
    try {
        $adminName = ([Security.Principal.SecurityIdentifier]'S-1-5-32-544').Translate(
                        [Security.Principal.NTAccount]).Value -replace '^.*\\', ''
    } catch {
        $adminName = 'Administrators'
    }

    $r = Invoke-Native net.exe @('localgroup', $adminName)
    if ($r.ExitCode -ne 0) { return $null }
    foreach ($line in ($r.Output -split '\r?\n')) {
        if ($line.Trim() -eq $Name) { return $true }
    }
    return $false
}

function Add-AuthorizedKey {
    param([Parameter(Mandatory)][string]$Key)

    Write-Step "Authorized key for '$TargetUser'"

    $acct = Get-LocalUser -Name $TargetUser -ErrorAction SilentlyContinue
    if (-not $acct) {
        Write-Note "no local user '$TargetUser' on this machine - note that Microsoft-account and domain logins are not local users and will not be found here. Create the account, or pass -TargetUser <name>, and re-run; everything else above is already applied."
        return $false
    }
    $sid = $acct.SID.Value

    # Deliberately NOT $env:USERNAME: under UAC the elevated process may be running as
    # a different administrator than the account being configured.
    $isAdmin = Test-LocalAdminMember -Name $TargetUser -Sid $sid
    if ($null -eq $isAdmin) {
        Write-Note "could not determine whether '$TargetUser' is an administrator, so no key was written. The two key files are not interchangeable - one would be silently ignored, the other would authorise this key for every admin account. Check by hand: Get-LocalGroupMember -SID S-1-5-32-544"
        return $false
    }

    if ($isAdmin) {
        # sshd's stock config has `Match Group administrators` pointing here, so for an
        # admin account a key in ~/.ssh/authorized_keys is read but never honoured.
        $path = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
        Write-Ok "'$TargetUser' is an administrator - using administrators_authorized_keys"
    }
    else {
        $prof = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$sid" `
                    -Name ProfileImagePath -ErrorAction SilentlyContinue).ProfileImagePath
        if (-not $prof) {
            $prof = Join-Path $env:SystemDrive "Users\$TargetUser"
            Write-Note "'$TargetUser' has never signed in, so no profile exists yet - assuming $prof"
        }
        $path = Join-Path $prof '.ssh\authorized_keys'
        Write-Ok "'$TargetUser' is a standard user - using $path"
    }

    if (-not $PSCmdlet.ShouldProcess($path, 'append public key')) { return $false }

    $dir = Split-Path $path -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }

    $existing = if (Test-Path $path) { @(Get-Content $path) } else { @() }
    # Compare on type+blob so the same key with a different trailing comment is not
    # appended twice.
    $blob = ($Key -split '\s+')[1]
    if ($existing | Where-Object { ($_ -split '\s+')[1] -eq $blob }) {
        Write-Ok 'key already authorized'
    }
    else {
        # Add-Content inserts no separator of its own, so if the file's last line has
        # no trailing newline this key would be concatenated onto it and both would be
        # unusable.
        if ($existing.Count -and ((Get-Content $path -Raw) -notmatch '\n$')) {
            Add-Content -Path $path -Value '' -Encoding ascii
        }
        # ascii, not utf8: Windows PowerShell 5.1 writes a BOM on utf8 and sshd
        # rejects the whole file because of it.
        Add-Content -Path $path -Value $Key -Encoding ascii
        Write-Fix "key appended ($(($existing.Count) + 1) key(s) now authorized)"
    }

    if ($isAdmin) {
        # sshd refuses this file unless only SYSTEM and Administrators can write it.
        $null = Invoke-Native icacls.exe @($path, '/inheritance:r', '/grant', '*S-1-5-18:F', '/grant', '*S-1-5-32-544:F')
        Write-Fix 'ACL tightened to SYSTEM + Administrators'
    }
    else {
        $null = Invoke-Native icacls.exe @($path, '/inheritance:r', '/grant', '*S-1-5-18:F', '/grant', "*$($sid):F")
        Write-Fix "ACL tightened to SYSTEM + $TargetUser"
    }
    return $true
}

#endregion

#region firewall and mDNS ----------------------------------------------------

function Set-ProfilesPrivate {
    Write-Step 'Network connection profiles'
    $profiles = Get-NetConnectionProfile -ErrorAction SilentlyContinue
    if (-not $profiles) { Write-Note 'no active network connection profile - is the box on a network?'; return }

    foreach ($p in $profiles) {
        if ($p.NetworkCategory -eq 'Public') {
            if (-not $PSCmdlet.ShouldProcess($p.Name, 'set NetworkCategory Private')) { continue }
            try {
                Set-NetConnectionProfile -InterfaceIndex $p.InterfaceIndex -NetworkCategory Private
                Write-Fix "'$($p.Name)' Public -> Private (Public blocks inbound rules and mDNS)"
            } catch {
                Write-Note "'$($p.Name)' could not be moved off Public: $($_.Exception.Message)"
            }
        }
        else { Write-Ok "'$($p.Name)' is $($p.NetworkCategory)" }
    }
}

function Open-SshFirewall {
    Write-Step "Firewall: inbound TCP $SshPort"

    # Any remote address, Private and Domain profiles only.
    #
    # Not LocalSubnet: access to these boxes is routed - bench and desk sit on different
    # subnets and traffic crosses a gateway - so LocalSubnet would refuse the normal way
    # in. Public stays excluded on purpose: a laptop that joins hotel or conference
    # Wi-Fi then stops listening rather than offering password SSH to it.
    $name = "StandBox-SSH-In-TCP-$SshPort"
    if ($PSCmdlet.ShouldProcess($name, 'create or update firewall rule')) {
        if (Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue) {
            Set-NetFirewallRule -Name $name -Enabled True -Direction Inbound -Action Allow `
                -Profile @('Private','Domain') -RemoteAddress Any
            Write-Fix "updated rule '$name' (Private + Domain, any address)"
        }
        else {
            New-NetFirewallRule -Name $name -DisplayName "SSH (sshd) TCP $SshPort" `
                -Description 'Created by Setup-StandBox.ps1' -Direction Inbound -Action Allow `
                -Protocol TCP -LocalPort $SshPort -Profile @('Private','Domain') -RemoteAddress Any `
                -Enabled True | Out-Null
            Write-Fix "created rule '$name' (Private + Domain, any address)"
        }
    }

    if (Get-NetTCPConnection -State Listen -LocalPort $SshPort -ErrorAction SilentlyContinue) {
        Write-Ok "sshd is listening on TCP $SshPort"
    } else {
        Write-Note "nothing is listening on TCP $SshPort yet - check Get-Service sshd and $env:ProgramData\ssh\logs"
    }
}

function Get-DnsServerAddress {
    @(Get-DnsClientServerAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
      ForEach-Object { $_.ServerAddresses }) |
      Where-Object { $_ -and $_ -notlike '127.*' } | Select-Object -First 1
}

function Resolve-AtServer {
    # Queries the configured DNS server directly.
    #
    # -DnsOnly and -NoHostsFile are the whole point: without them Windows answers from the
    # hosts file, its own cache, LLMNR, or simply from knowing its own computer name - so a
    # machine ALWAYS resolves itself and the check proves nothing. Only what the server
    # returns tells you what a remote client will see.
    param([Parameter(Mandatory)][string]$Name, [string]$Type = 'A')

    $srv = Get-DnsServerAddress
    if (-not $srv) { return $null }
    try {
        $ans = Resolve-DnsName -Name $Name -Type $Type -Server $srv -DnsOnly -NoHostsFile -ErrorAction Stop
        if ($Type -eq 'PTR') { return @($ans | Where-Object NameHost  | Select-Object -First 1).NameHost }
        return @($ans | Where-Object IPAddress | Select-Object -First 1).IPAddress
    } catch { }
    return $null
}

function Get-DnsSuffix {
    (Get-DnsClient -ErrorAction SilentlyContinue |
     Where-Object { $_.ConnectionSpecificSuffix } |
     Select-Object -First 1).ConnectionSpecificSuffix
}

function Register-ComputerDnsName {
    # Asks Windows to register its own name in DNS, then checks whether it took.
    #
    # This is what would make `ssh <computer-name>` work across subnets, where .local
    # cannot reach. It often fails on a workgroup machine: corporate zones usually
    # accept only secure dynamic updates from domain members, and any existing record
    # may have come from a DHCP reservation using a different name entirely. Reported
    # either way, because a silent no-op here looks identical to success.
    Write-Step 'DNS registration'

    if (-not $PSCmdlet.ShouldProcess($env:COMPUTERNAME, 'register in DNS')) { return }

    $r = Invoke-Native ipconfig.exe @('/registerdns')
    if ($r.ExitCode -ne 0) { Write-Note "ipconfig /registerdns exited $($r.ExitCode)"; return }
    Write-Ok 'registration requested'

    # The request is asynchronous; the record does not appear instantly.
    Start-Sleep -Seconds 10

    $suffix = Get-DnsSuffix
    $want   = if ($suffix) { "$env:COMPUTERNAME.$suffix" } else { $env:COMPUTERNAME }
    $got    = Resolve-AtServer -Name $want -Type A

    if ($got) {
        Write-Ok "the DNS server returns $want -> $got"
    } else {
        Write-Note "the DNS server has no A record for $want, so 'ssh $env:COMPUTERNAME' will not work from another subnet. Workgroup machines usually cannot self-register in a zone requiring secure dynamic updates - ask IT for an A record, or use the DNS name in the summary."
    }
}

function Get-MdnsResponderName {
    # Process name(s) holding UDP 5353, or $null when nothing does. Returned as a string
    # rather than a list so an unresolvable owning process still counts as "listening".
    $listening = @(Get-NetUDPEndpoint -LocalPort 5353 -ErrorAction SilentlyContinue)
    if (-not $listening) { return $null }
    $names = @($listening |
               ForEach-Object { (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName } |
               Where-Object { $_ } | Sort-Object -Unique)
    if ($names) { return ($names -join ', ') }
    return 'unknown process'
}

function Open-MdnsFirewall {
    Write-Step 'mDNS: inbound UDP 5353'

    $name = 'StandBox-mDNS-In-UDP-5353'
    if ($PSCmdlet.ShouldProcess($name, 'create or update firewall rule')) {
        if (Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue) {
            Set-NetFirewallRule -Name $name -Enabled True -Profile Private -RemoteAddress LocalSubnet
            Write-Fix "updated rule '$name'"
        }
        else {
            New-NetFirewallRule -Name $name -DisplayName 'mDNS (Bonjour/.local) UDP 5353' `
                -Description 'Created by Setup-StandBox.ps1' -Direction Inbound -Action Allow `
                -Protocol UDP -LocalPort 5353 -Profile Private -RemoteAddress LocalSubnet `
                -Enabled True | Out-Null
            Write-Fix "created rule '$name'"
        }
    }

    $responder = Get-MdnsResponderName
    if ($responder) {
        # Holding the port is not the same as advertising this machine's name. Browsers and
        # media apps bind 5353 for their own discovery and answer for nothing else, so the
        # owning process decides whether this means anything at all.
        if ($responder -match 'svchost|mDNSResponder|Bonjour') {
            Write-Ok "an mDNS responder is listening on UDP 5353 ($responder)"
            $script:MdnsOk = $true
        } else {
            Write-Note "UDP 5353 is held by '$responder', an application doing its own discovery rather than a responder that advertises this machine. Do not assume <name>.local resolves - test it from a client on this subnet."
        }
        return
    }

    # Flagged, not warned about: .local cannot cross a router, so on a routed network
    # DNS is the name path and this responder is simply irrelevant.
    Write-Skip 'nothing is listening on UDP 5353 - .local will not resolve (only matters for clients on this subnet)'
    if (-not $InstallBonjour) {
        Write-Skip 're-run with -InstallBonjour if you need .local for same-subnet clients'
        return
    }

    Install-BonjourResponder

    # The responder service binds a moment after the installer returns, so a check taken
    # immediately would report failure for an install that actually worked.
    Start-Sleep -Seconds 5
    $responder = Get-MdnsResponderName
    if ($responder) {
        Write-Fix "an mDNS responder is now listening on UDP 5353 ($responder)"
        $script:MdnsOk = $true
    } else {
        Write-Note 'still nothing listening on UDP 5353 after installing Bonjour - a reboot is probably needed before .local will resolve'
    }
}

function Install-BonjourResponder {
    Write-Step 'Bonjour for Windows'

    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Note 'winget is not available - install Bonjour by hand from https://support.apple.com/kb/DL999 (Bonjour Print Services for Windows)'
        return
    }
    if (-not $PSCmdlet.ShouldProcess('Bonjour', 'winget install')) { return }

    # Package identifiers move around; search rather than assume one exists.
    $found = (Invoke-Native $winget.Source @('search', '--name', 'Bonjour', '--disable-interactivity')).Output
    if ($found -notmatch 'Bonjour') {
        Write-Note 'no Bonjour package found in the configured winget sources - install it by hand from https://support.apple.com/kb/DL999'
        return
    }
    $inst = Invoke-Native $winget.Source @('install', '--name', 'Bonjour', '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity')
    foreach ($l in ($inst.Output -split '\r?\n')) { if ($l.Trim()) { Write-Host "    $l" -ForegroundColor DarkGray } }
    if ($inst.ExitCode -eq 0) { Write-Fix 'Bonjour installed' }
    else { Write-Note "winget exited $($inst.ExitCode) - install Bonjour by hand from https://support.apple.com/kb/DL999" }
}

#endregion

#region main -----------------------------------------------------------------

if ($InstallWakeLockTaskOnly) {
    Install-WakeLockTask
    return
}

if ($ResultsShareOnly) {
    Install-ResultsMirror
    return
}

if (-not $SshOnly) {
    Set-NeverSleep
    Disable-DevicePowerSaving
    Disable-ScreenSaver
    Set-AutoLockPolicy
    Get-WakeLock
    Install-WakeLockTask
}

if (-not $PowerOnly) {
    $script:SshdOk = Install-Sshd

    $key = Resolve-PublicKey -Value $PublicKey
    if ($key) { $script:KeyOk = Add-AuthorizedKey -Key $key }
    else      { Write-Step 'Authorized key'; Write-Skip 'no key supplied - password auth only' }

    Set-ProfilesPrivate
    Open-SshFirewall
    Open-MdnsFirewall
    Register-ComputerDnsName
}

# Neither half's job, so it is gated on both: -PowerOnly and -SshOnly each say they
# leave the other half alone, and neither of them mentions the results share.
if (-not $PowerOnly -and -not $SshOnly) {
    Install-ResultsMirror
}

#endregion

#region summary --------------------------------------------------------------

Write-Step 'Summary'

$fqdn = "$(($env:COMPUTERNAME).ToLower()).local"

# @() matters throughout: with a single NIC these are bare strings, and $string[0] is a
# character, not an address.
# Ordered by which interface carries the default route. A stand box often has a virtual
# or instrument-side adapter (Hyper-V, ICS, a 192.168.x gateway) whose address sorts first
# but is unreachable from anywhere else - and both the PTR lookup and the suggested ssh
# command below take the first entry, so getting this wrong makes the summary contradict
# itself and hand out an address no client can use.
$primaryIf = (Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
              Sort-Object RouteMetric | Select-Object -First 1).InterfaceIndex
$allIp = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
           Where-Object { $_.IPAddress -notlike '127.*' } |
           Sort-Object @{ Expression = { if ($_.InterfaceIndex -eq $primaryIf) { 0 } else { 1 } } }, IPAddress |
           Select-Object -ExpandProperty IPAddress)
$ips       = @($allIp | Where-Object { $_ -notlike '169.254.*' })
# Reported rather than hidden: on a bench segment with no DHCP server, a link-local
# address may be the only one this box has.
$linkLocal = @($allIp | Where-Object { $_ -like '169.254.*' })

# The name the rest of the network actually knows this box by, which is not necessarily
# its Windows computer name - DHCP registers whatever it registers.
# Asked of the DNS server explicitly. A local PTR lookup answers from the machine's own
# identity and would report a name no other client can actually use.
$dnsName = $null
if ($ips -and -not $PowerOnly) { $dnsName = Resolve-AtServer -Name $ips[0] -Type PTR }
if ($dnsName) { $dnsName = $dnsName.TrimEnd('.') }

Write-Host ("  hostname   {0}  (Windows computer name)" -f $env:COMPUTERNAME)
Write-Host ("  DNS name   {0}" -f ($(if ($dnsName) { $dnsName } else { 'not registered in DNS' })))
Write-Host ("  mDNS name  {0}  (same-subnet clients only)" -f $fqdn)
Write-Host ("  addresses  {0}" -f ($(if ($ips) { $ips -join ', ' } else { 'none routable' })))
if ($linkLocal) {
    Write-Host ("  link-local {0}  (self-assigned - no DHCP server on that segment)" -f ($linkLocal -join ', '))
}
Write-Host ("  account    {0}" -f $TargetUser)
# Queried, not inferred from a flag: with -SshOnly the flag is false even though the task
# exists from an earlier run, and reporting that as "not registered" would be a lie.
$wakeState = 'not registered'
try {
    $wt = Get-ScheduledTask -TaskName $WakeTaskName -ErrorAction Stop
    $wakeState = "registered at startup ($($wt.State))"
} catch { }
Write-Host ("  wake task  {0}" -f $wakeState)

# Queried for the same reason the wake task is: with -PowerOnly the flag is false even
# though an earlier run registered it.
$mirrorState = 'not registered'
try {
    $mt = Get-ScheduledTask -TaskName $MirrorTaskName -ErrorAction Stop
    $mirrorState = "every $MirrorIntervalMinutes min as $TargetUser ($($mt.State))"
} catch { }
Write-Host ("  mirror     {0}" -f $mirrorState)

# Only on a run that did the share work. Test-Path against an unreachable UNC does
# not fail fast - it blocks for the SMB session timeout - so asking on every run
# would hang the summary of a -PowerOnly run that never touched the share.
if (-not $PowerOnly -and -not $SshOnly) {
    $shareState = if (Test-Path $ResultsShareUnc -ErrorAction SilentlyContinue) {
        "$ResultsShareUnc reachable"
    } else {
        "$ResultsShareUnc NOT REACHABLE"
    }
    Write-Host ("  results    {0}" -f $shareState)
}

if (-not $PowerOnly) {
    Write-Host ("  sshd       {0}" -f $(if ($script:SshdOk) { 'installed and running' } else { 'NOT VERIFIED' }))
    Write-Host ("  key auth   {0}" -f $(if ($script:KeyOk) { 'authorized' } else { 'password only' }))
    Write-Host ("  mDNS       {0}" -f $(if ($script:MdnsOk) { 'responder listening on UDP 5353' } else { 'NO RESPONDER - .local will not resolve' }))

    Write-Host "`n  Connect from your workstation:" -ForegroundColor Cyan
    if ($dnsName) {
        Write-Host "    ssh $TargetUser@$dnsName" -ForegroundColor White
    }
    $fallback = @($ips + $linkLocal)
    if ($fallback) { Write-Host ("    ssh {0}@{1}" -f $TargetUser, $fallback[0]) -ForegroundColor White }
    Write-Host "    ssh $TargetUser@$fqdn   # only if that client shares this subnet - .local cannot cross a router" -ForegroundColor DarkGray
}

if ($script:Notes.Count) {
    Write-Host "`n  Needs attention:" -ForegroundColor Magenta
    foreach ($n in $script:Notes) { Write-Host "    - $n" -ForegroundColor Magenta }
}

Write-Host "`n  Undo power scheme changes with: powercfg -restoredefaultschemes" -ForegroundColor DarkGray

if ($KeepAwake) { Invoke-HoldAwake }

#endregion
