# Stand box provisioning

Windows test-stand machines and the script that prepares them.

## The machines

Both run the same test framework from this repo, as the local user `seitteam`, with
results under `C:\Users\seitteam\Desktop\mytestresults\runs\`.

| | zdrive stand | ydrive stand |
|---|---|---|
| Windows computer name | `SEIT-LT-2` | `SEIT-STATION-7` |
| Address | `10.100.9.115` | `10.100.9.107` |
| Connect with | `ssh zdrive` or `ssh seitteam@seit-lt-2` | `ssh seitteam@seit-nuc-7` |
| Tests it runs | `zdrive_brake_endurance_test`, `zdrive_cycle_brake_hold_test` | `brake_endurance_test`, `cycle_brake_endurance_test` |
| Default shell over SSH | Windows PowerShell 5.1 | PowerShell 7 |

Identify a box by the test names in its run directories: the zdrive rulebook prefixes
its tests with `zdrive_`, the ydrive rulebook does not.

### Names are not what you would expect

**SEIT-STATION-7 resolves as `seit-nuc-7.mytra.co`.** `seit-station-7` is not in DNS and
cannot be added by the machine: it is a workgroup box against a zone requiring secure
dynamic updates, and the record for that address is already `seit-nuc-7`. An A record
needs an IT request. SEIT-LT-2 does register under its own name.

**`<name>.local` does not work from a desk machine.** Desks sit on `10.100.4.0/23`, the
stands on `10.100.9.0/24`, routed through `corp-gateway.mytra.co`. mDNS is link-local and
cannot cross a router. It works only between machines on the stand subnet.

**SEIT-LT-2 also has an adapter at `192.168.50.1`** which is not reachable from a desk
machine. Do not use it as an address for that box.

## Setup-StandBox.ps1

Prepares a box for unattended runs: never sleeps, never powers down USB or the network
adapters, and answers SSH. Idempotent - re-running fixes only what is wrong, and `-WhatIf`
is a true dry run that changes nothing.

    # first-time setup, at the console
    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
    .\Setup-StandBox.ps1 -WhatIf        # inspect
    .\Setup-StandBox.ps1

Useful switches: `-PowerOnly` / `-SshOnly` to run one half, `-PublicKey <text|path>` to
authorise a key, `-MaxProcessor` to pin the CPU (runs hot), `-DisableHibernation`.
`Get-Help .\Setup-StandBox.ps1 -Full` documents the rest.

### -ResultsShareOnly

    .\Setup-StandBox.ps1 -ResultsShareOnly

Maps the results share for every session on the box and registers
`StandBox-ResultsMirror`, the scheduled task that copies finished runs to it (see
`tools/README.md`). Prompts for the share credential, which is stored by Windows and
never written into this repo; pass `-ResultsShareCredential` when running over SSH,
where there is nothing to prompt with.

This is the repair a run's operator prompt names when it reports that finished runs are
not reaching the share - most likely because the box was reimaged. Nothing is urgent: the
run is recorded locally either way and the mirror backfills whatever it missed, so this
can be run during a run or after it.

The full script also runs it, so a freshly provisioned box needs no separate step.

### Run it from the console for first-time setup

Over SSH the script deliberately skips `Restart-Service sshd`, because stopping the
service takes its per-connection child processes with it and would kill the session doing
the work. Changes to `DefaultShell` or `Port` therefore do not take effect until sshd is
restarted some other way.

### What it changes

Power: sleep, hibernate, hybrid sleep, **unattended sleep**, display, console-lock
display, disk spindown, adaptive brightness all off or never; USB selective suspend, USB3
link power management, PCIe ASPM, wireless power saving and Energy Saver disabled; lid
close and sleep button do nothing; the per-device "allow the computer to turn off this
device" checkbox cleared for USB and network devices; screen saver off. A scheduled task
`StandBox-WakeLock` holds a wake lock from boot.

Network: `sshd` automatic with restart-on-failure, `DefaultShell` set to PowerShell,
inbound TCP 22 allowed from **any address on the Private and Domain profiles** - not
`LocalSubnet`, which would refuse the routed path desks actually use. Public is excluded
so a laptop on untrusted Wi-Fi stops listening. Inbound UDP 5353 for mDNS, and connection
profiles moved off Public.

Undo the power half with `powercfg -restoredefaultschemes` (that resets every scheme to
Windows defaults, not only what this touched).

### The lock screen is not a power setting

Both boxes shipped with **`InactivityTimeoutSecs = 1200`** under
`HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System` - the *Interactive
logon: Machine inactivity limit* policy. It locks the console after 20 minutes of no input
**regardless of screen saver and power settings**, so a box with every timeout set to
never still ends up behind a lock screen. Tests keep running; what you lose is the
operator dashboard and any on-screen GUI.

`-DisableAutoLock` sets it to 0. It is opt-in because it leaves the box logged in
unattended and `seitteam` is a local administrator. Without the switch the script reports
the current value rather than changing it. Both stands were set to 0 on 2026-08-27.

The change applies from the next sign-in, so a box already locked needs unlocking by hand
once; if it locks again after 20 minutes, sign out and back in.

Also worth knowing: `powercfg /requests` on these boxes shows `DISPLAY: None` even with
the `StandBox-WakeLock` task running. A task running as SYSTEM in session 0 can assert
`ES_SYSTEM_REQUIRED` but not usefully `ES_DISPLAY_REQUIRED` - the display stays on because
the powercfg timeout is `never`, not because of the task.

### Things that mislead

A box is a poor witness to whether the network can find it. `Resolve-DnsName` answers from
the hosts file, the local cache, LLMNR or the machine's own identity unless given
`-Server <addr> -DnsOnly -NoHostsFile`, so a machine always "resolves itself". A process
holding UDP 5353 is not necessarily an mDNS responder - on SEIT-STATION-7 it is Chrome.
The script accounts for both; ad-hoc checks usually do not.

`Add-WindowsCapability` can take 15 minutes or more. The script waits up to 30, reporting
a heartbeat, then abandons **the wait** but never the install - that runs inside
TrustedInstaller, and killing `TiWorker` risks the component store. Re-run once it settles.
