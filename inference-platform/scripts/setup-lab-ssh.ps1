#requires -Version 5.1
<#
Run this script yourself in an interactive Windows PowerShell terminal.
Order: ConfigureAgent (administrator), then Setup guides the remaining actions.
Never send a password or key passphrase to a chat. No automatic elevation or windows.
TrustHost requires a fingerprint checked through an already trusted server session:
    ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
Only the dedicated key/known_hosts and the server user's authorized_keys are managed.
#>
[CmdletBinding()]
param(
    [ValidateSet('Setup', 'ConfigureAgent', 'CreateKey', 'TrustHost', 'Unlock', 'RegisterKey', 'Status')]
    [string]$Action = 'Status',
    [string]$VerifiedHostFingerprint
)

$ErrorActionPreference = 'Stop'

function Get-LabPaths {
    if ([string]::IsNullOrWhiteSpace($env:USERPROFILE) -or -not [IO.Path]::IsPathRooted($env:USERPROFILE)) {
        throw 'USERPROFILE must be an absolute local user profile path.'
    }
    $directory = Join-Path ([IO.Path]::GetFullPath($env:USERPROFILE)) '.ssh'
    [pscustomobject]@{
        Directory = $directory
        PrivateKey = Join-Path $directory 'id_ed25519_aixm_lab'
        PublicKey = Join-Path $directory 'id_ed25519_aixm_lab.pub'
        KnownHosts = Join-Path $directory 'known_hosts_aixm_lab'
        HostCandidate = Join-Path $env:USERPROFILE 'AppData\Local\Temp\codex-swufefic-v100-known_hosts'
    }
}

function Assert-LabRegularPath([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        $item = Get-Item -LiteralPath $Path -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Dedicated SSH paths must not be links or junctions.'
        }
    }
}

function Initialize-LabDirectory($Paths) {
    Assert-LabRegularPath $Paths.Directory
    if (-not (Test-Path -LiteralPath $Paths.Directory -PathType Container)) {
        New-Item -ItemType Directory -Path $Paths.Directory -ErrorAction Stop | Out-Null
    }
}

function Assert-LabInteractive {
    if (-not [Environment]::UserInteractive -or $Host.Name -ne 'ConsoleHost' -or
        [Console]::IsInputRedirected -or [Console]::IsOutputRedirected) {
        throw 'Run this action yourself in an interactive PowerShell terminal; redirected input/output is forbidden.'
    }
}

function Get-LabExecutable([ValidateSet('ssh', 'ssh-add', 'ssh-keygen')][string]$Name) {
    $binary = Join-Path $env:WINDIR ('System32\OpenSSH\' + $Name + '.exe')
    if (-not (Test-Path -LiteralPath $binary -PathType Leaf)) {
        throw 'The built-in Windows OpenSSH client is missing. No tools were installed.'
    }
    return $binary
}

function Invoke-LabNative([string]$Executable, [string[]]$Arguments, [switch]$Capture) {
    # Use an argument array, never Invoke-Expression. Native failures are not PowerShell exceptions.
    # A pre-existing SSH_ASKPASS_REQUIRE=force must not open a password window.
    $originalAskpass = $env:SSH_ASKPASS_REQUIRE
    $env:SSH_ASKPASS_REQUIRE = 'never'
    try {
        if ($Capture) {
            $lines = @(& $Executable @Arguments)
            $nativeCode = $LASTEXITCODE
            if ($nativeCode -ne 0) { throw ('OpenSSH action failed (exit ' + $nativeCode + ').') }
            return $lines
        }
        & $Executable @Arguments
        $nativeCode = $LASTEXITCODE
        if ($nativeCode -ne 0) { throw ('OpenSSH action failed (exit ' + $nativeCode + ').') }
    }
    finally { $env:SSH_ASKPASS_REQUIRE = $originalAskpass }
}

function Read-LabUInt32([byte[]]$Bytes, [ref]$Offset) {
    if ($Offset.Value -lt 0 -or $Bytes.Length - $Offset.Value -lt 4) { throw 'Invalid SSH key envelope.' }
    $i = [int]$Offset.Value
    $value = [uint64]$Bytes[$i] * 16777216 + [uint64]$Bytes[$i + 1] * 65536 +
        [uint64]$Bytes[$i + 2] * 256 + [uint64]$Bytes[$i + 3]
    $Offset.Value = $i + 4
    return $value
}

function Read-LabSshString([byte[]]$Bytes, [ref]$Offset) {
    $size = Read-LabUInt32 $Bytes $Offset
    if ($size -gt ($Bytes.Length - $Offset.Value)) { throw 'Invalid SSH key envelope.' }
    $value = New-Object byte[] ([int]$size)
    [Array]::Copy($Bytes, [int]$Offset.Value, $value, 0, [int]$size)
    $Offset.Value += [int]$size
    return ,$value
}

function ConvertTo-LabPublicKey([string]$Text) {
    if ($Text.Length -gt 4096 -or $Text.Trim() -match '[\r\n]') { throw 'Expected one ED25519 public-key line.' }
    $parts = $Text.Trim() -split '\s+', 3
    if ($parts.Count -lt 2 -or $parts[0] -cne 'ssh-ed25519' -or $parts[1] -cnotmatch '^[A-Za-z0-9+/]+={0,2}$') {
        throw 'Only a plain ED25519 public key is accepted.'
    }
    try { $blob = [Convert]::FromBase64String($parts[1]) } catch { throw 'Invalid public-key encoding.' }
    if ([Convert]::ToBase64String($blob) -cne $parts[1]) { throw 'Non-canonical public-key encoding.' }
    $offset = 0
    $kind = [Text.Encoding]::ASCII.GetString((Read-LabSshString $blob ([ref]$offset)))
    $key = Read-LabSshString $blob ([ref]$offset)
    if ($kind -cne 'ssh-ed25519' -or $key.Length -ne 32 -or $offset -ne $blob.Length) {
        throw 'Invalid ED25519 public-key structure.'
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $fingerprint = 'SHA256:' + [Convert]::ToBase64String($sha.ComputeHash($blob)).TrimEnd('=') }
    finally { $sha.Dispose() }
    [pscustomobject]@{ Blob = $parts[1]; Line = 'ssh-ed25519 ' + $parts[1] + ' aixm-lab'; Fingerprint = $fingerprint }
}

function Read-LabPublicKey($Paths) {
    Assert-LabRegularPath $Paths.Directory
    Assert-LabRegularPath $Paths.PublicKey
    if (-not (Test-Path -LiteralPath $Paths.PublicKey -PathType Leaf) -or
        (Get-Item -LiteralPath $Paths.PublicKey).Length -gt 4096) { throw 'The dedicated public key is missing or invalid.' }
    return ConvertTo-LabPublicKey ([IO.File]::ReadAllText($Paths.PublicKey))
}

function Assert-LabEncryptedKey($Paths) {
    # Read only the OpenSSH container metadata in local memory; never decrypt or print private material.
    # Only CreateKey/Unlock and guided setup inspect metadata. RegisterKey never opens the private file.
    Assert-LabRegularPath $Paths.PrivateKey
    $public = Read-LabPublicKey $Paths
    if (-not (Test-Path -LiteralPath $Paths.PrivateKey -PathType Leaf) -or
        (Get-Item -LiteralPath $Paths.PrivateKey).Length -gt 16384) { throw 'Dedicated private-key envelope is missing or invalid.' }
    $text = [IO.File]::ReadAllText($Paths.PrivateKey)
    if ($text -notmatch '\A\s*-----BEGIN OPENSSH PRIVATE KEY-----\s+([A-Za-z0-9+/=\r\n]+)\s+-----END OPENSSH PRIVATE KEY-----\s*\z') {
        throw 'Expected an OpenSSH private-key envelope.'
    }
    try { $bytes = [Convert]::FromBase64String($Matches[1]) } catch { throw 'Invalid private-key envelope.' }
    $magic = [Text.Encoding]::ASCII.GetBytes("openssh-key-v1`0")
    if ($bytes.Length -le $magic.Length) { throw 'Invalid private-key envelope.' }
    for ($index = 0; $index -lt $magic.Length; $index++) {
        if ($bytes[$index] -ne $magic[$index]) { throw 'Invalid private-key envelope.' }
    }
    $offset = $magic.Length
    $cipher = [Text.Encoding]::ASCII.GetString((Read-LabSshString $bytes ([ref]$offset)))
    $kdf = [Text.Encoding]::ASCII.GetString((Read-LabSshString $bytes ([ref]$offset)))
    $kdfOptions = Read-LabSshString $bytes ([ref]$offset)
    if ($cipher -eq 'none' -or $kdf -cne 'bcrypt' -or $kdfOptions.Length -eq 0) {
        throw 'The dedicated key is not passphrase-encrypted. Set a non-empty passphrase in your own terminal before continuing; no key was overwritten.'
    }
    if ($cipher -cnotmatch '^(aes(128|192|256)-(ctr|cbc)|aes(128|256)-gcm@openssh.com|chacha20-poly1305@openssh.com)$') {
        throw 'Unsupported encrypted OpenSSH envelope.'
    }
    $kdfOffset = 0
    $salt = Read-LabSshString $kdfOptions ([ref]$kdfOffset)
    $rounds = Read-LabUInt32 $kdfOptions ([ref]$kdfOffset)
    if ($salt.Length -lt 16 -or $rounds -lt 1 -or $kdfOffset -ne $kdfOptions.Length) { throw 'Invalid bcrypt metadata.' }
    if ((Read-LabUInt32 $bytes ([ref]$offset)) -ne 1) { throw 'Expected one dedicated key.' }
    $publicBlob = Read-LabSshString $bytes ([ref]$offset)
    $encrypted = Read-LabSshString $bytes ([ref]$offset)
    if ([Convert]::ToBase64String($publicBlob) -cne $public.Blob -or $encrypted.Length -lt 16 -or $offset -ne $bytes.Length) {
        throw 'Private-key envelope and dedicated public key do not match.'
    }
}

function Read-LabHostEntry([string]$Path) {
    Assert-LabRegularPath $Path
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf) -or (Get-Item -LiteralPath $Path).Length -gt 65536) {
        throw 'The existing host-key candidate file is missing or invalid.'
    }
    $entries = @()
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        $parts = $line.Trim() -split '\s+', 4
        if ($parts.Count -ge 3 -and $parts[0] -ceq '[10.8.21.33]:52222' -and $parts[1] -ceq 'ssh-ed25519') {
            $entry = ConvertTo-LabPublicKey ($parts[1] + ' ' + $parts[2])
            if (-not ($entries | Where-Object { $_.Blob -ceq $entry.Blob })) { $entries += $entry }
        }
    }
    if ($entries.Count -ne 1) { throw 'Expected exactly one unambiguous ED25519 key for [10.8.21.33]:52222.' }
    return $entries[0]
}

function Set-LabHostTrust($Paths, [string]$Fingerprint) {
    if ($Fingerprint -cnotmatch '^SHA256:[A-Za-z0-9+/]{43}$') { throw 'Supply -VerifiedHostFingerprint from an independently trusted server session.' }
    $entry = Read-LabHostEntry $Paths.HostCandidate
    if ($entry.Fingerprint -cne $Fingerprint) { throw 'Host fingerprint mismatch. No trust file was written.' }
    Initialize-LabDirectory $Paths
    Assert-LabRegularPath $Paths.KnownHosts
    $content = '[10.8.21.33]:52222 ssh-ed25519 ' + $entry.Blob + "`n"
    if (Test-Path -LiteralPath $Paths.KnownHosts) {
        if ([IO.File]::ReadAllText($Paths.KnownHosts).Replace("`r`n", "`n") -cne $content) {
            throw 'The dedicated known_hosts already differs. It will not be overwritten.'
        }
        Write-Host 'Verified host key is already recorded.'
        return
    }
    $stream = [IO.File]::Open($Paths.KnownHosts, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $data = [Text.Encoding]::ASCII.GetBytes($content); $stream.Write($data, 0, $data.Length) }
    finally { $stream.Dispose() }
    Write-Host 'Verified ED25519 host key recorded in the dedicated known_hosts.'
}

function Assert-LabAgentRunning {
    $agent = Get-Service -Name 'ssh-agent' -ErrorAction Stop
    if ($agent.Status -ne 'Running') { throw 'ssh-agent is not running. Run ConfigureAgent in an administrator terminal first.' }
}

function Assert-LabAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'ConfigureAgent requires an administrator PowerShell terminal; no automatic elevation is performed.'
    }
}

function Assert-LabAgentHasKey($Public) {
    Assert-LabAgentRunning
    $lines = @(Invoke-LabNative (Get-LabExecutable 'ssh-add') @('-L') -Capture)
    foreach ($line in $lines) {
        try {
            $entry = ConvertTo-LabPublicKey $line
            if ($entry.Blob -ceq $Public.Blob) { return }
        } catch { continue }
    }
    throw 'The dedicated key is not unlocked in ssh-agent. Run Unlock yourself before RegisterKey.'
}

function Get-LabRegistrationCommand($Public) {
    # Only validated public material is interpolated. A remote lock serializes append-only registration.
    $validated = ConvertTo-LabPublicKey $Public.Line
    $remote = @'
set -eu
umask 077
d="$HOME/.ssh"
test ! -L "$d" || { echo 'Refusing linked .ssh directory' >&2; exit 1; }
mkdir -p "$d"
chmod 700 "$d"
a="$d/authorized_keys"
test ! -L "$a" || { echo 'Refusing linked authorized_keys' >&2; exit 1; }
test ! -e "$a" || test -f "$a"
lock="$d/.aixm-authorized-keys.lock"
mkdir "$lock" || { echo 'Registration lock exists; inspect before retrying' >&2; exit 1; }
trap 'rmdir "$lock"' EXIT HUP INT TERM
touch "$a"
chmod 600 "$a"
if ssh-keygen -l -E sha256 -f "$a" 2>/dev/null | awk -v fingerprint='__FINGERPRINT__' '$2 == fingerprint { found=1 } END { exit !found }'; then
    printf '%s\n' 'Dedicated public key already registered.'
else
    printf '\n%s\n' '__PUBLIC_KEY__' >> "$a"
    printf '%s\n' 'Dedicated public key appended; existing entries retained.'
fi
'@
    return $remote.Replace('__FINGERPRINT__', $validated.Fingerprint).Replace('__PUBLIC_KEY__', $validated.Line).Replace("`r`n", "`n")
}

function Get-LabRegistrationTransport($Public) {
    # PS5.1 native argument marshalling can strip nested quotes. Transfer the fixed ASCII script
    # as base64, so the one remote argument contains no quotes/newlines or user-supplied commands.
    $remote = Get-LabRegistrationCommand $Public
    $encoded = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($remote))
    return 'printf %s ' + $encoded + ' | base64 -d | sh'
}

function Invoke-LabGuidedSetup([string]$Fingerprint) {
    Assert-LabInteractive
    $paths = Get-LabPaths
    $agent = Get-Service -Name 'ssh-agent' -ErrorAction Stop
    if ($agent.Status -ne 'Running' -or $agent.StartType -ne 'Manual') {
        # ConfigureAgent checks the existing token; it never opens an elevated window.
        Invoke-LabSshSetup 'ConfigureAgent' ''
    }
    if ([string]::IsNullOrWhiteSpace($Fingerprint)) {
        $Fingerprint = Read-Host 'Paste the PUBLIC SHA256 host fingerprint checked in your trusted server terminal (not a password)'
    }
    if ($Fingerprint -cnotmatch '^SHA256:[A-Za-z0-9+/]{43}$') { throw 'A verified public SHA256 host fingerprint is required.' }
    if ((Test-Path -LiteralPath $paths.PrivateKey) -or (Test-Path -LiteralPath $paths.PublicKey)) {
        Assert-LabEncryptedKey $paths
    } else {
        Invoke-LabSshSetup 'CreateKey' ''
    }
    if (Test-Path -LiteralPath $paths.KnownHosts) {
        $existing = Read-LabHostEntry $paths.KnownHosts
        if ($existing.Fingerprint -cne $Fingerprint) { throw 'Existing dedicated host fingerprint differs; no overwrite or connection.' }
    } else {
        Invoke-LabSshSetup 'TrustHost' $Fingerprint
    }
    Invoke-LabSshSetup 'Unlock' ''
    Invoke-LabSshSetup 'RegisterKey' ''
    Invoke-LabSshSetup 'Status' ''
}

function Invoke-LabSshSetup([string]$SelectedAction, [string]$Fingerprint) {
    $paths = Get-LabPaths
    switch ($SelectedAction) {
        'Setup' { Invoke-LabGuidedSetup $Fingerprint }
        'ConfigureAgent' {
            Assert-LabAdministrator
            Set-Service -Name 'ssh-agent' -StartupType Manual -ErrorAction Stop
            Start-Service -Name 'ssh-agent' -ErrorAction Stop
            $agent = Get-Service -Name 'ssh-agent' -ErrorAction Stop
            $agent.WaitForStatus('Running', [TimeSpan]::FromSeconds(10))
            Write-Host 'ssh-agent is running; startup type is Manual.'
        }
        'CreateKey' {
            Assert-LabInteractive
            Initialize-LabDirectory $paths
            if ((Test-Path -LiteralPath $paths.PrivateKey) -or (Test-Path -LiteralPath $paths.PublicKey)) {
                throw 'A dedicated key or public key already exists; neither will be overwritten.'
            }
            Write-Host 'Enter a NON-EMPTY passphrase twice in ssh-keygen. Do not paste it into chat.'
            Invoke-LabNative (Get-LabExecutable 'ssh-keygen') @('-t', 'ed25519', '-a', '64', '-C', 'aixm-lab-swufe', '-f', $paths.PrivateKey)
            Assert-LabEncryptedKey $paths
            Write-Host 'Dedicated ED25519 key created with an encrypted OpenSSH envelope.'
        }
        'TrustHost' { Set-LabHostTrust $paths $Fingerprint }
        'Unlock' {
            Assert-LabInteractive
            Assert-LabAgentRunning
            Assert-LabEncryptedKey $paths
            Invoke-LabNative (Get-LabExecutable 'ssh-add') @('-t', '3600', $paths.PrivateKey)
            Assert-LabAgentHasKey (Read-LabPublicKey $paths)
            Write-Host 'Dedicated identity is loaded in ssh-agent (requested lifetime: 3600 seconds).'
        }
        'RegisterKey' {
            Assert-LabInteractive
            $public = Read-LabPublicKey $paths
            Assert-LabAgentHasKey $public
            $null = Read-LabHostEntry $paths.KnownHosts
            $sshArguments = @('-F', 'none', '-p', '52222', '-o', 'StrictHostKeyChecking=yes',
                '-o', ('UserKnownHostsFile=' + $paths.KnownHosts), '-o', 'GlobalKnownHostsFile=NUL',
                '-o', 'HostKeyAlgorithms=ssh-ed25519', '-o', 'UpdateHostKeys=no',
                '-o', 'IdentitiesOnly=yes', '-i', $paths.PublicKey, '-o', 'BatchMode=no',
                '-o', 'ClearAllForwardings=yes', '-o', 'PermitLocalCommand=no',
                '-o', 'ConnectTimeout=10', '-o', 'ConnectionAttempts=1',
                '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
                'swufe@10.8.21.33', (Get-LabRegistrationTransport $public))
            Write-Host 'Authenticate directly in this terminal if prompted. Only your public key is transferred.'
            Invoke-LabNative (Get-LabExecutable 'ssh') $sshArguments
        }
        'Status' {
            $fingerprintValue = $null
            if (Test-Path -LiteralPath $paths.PublicKey -PathType Leaf) { $fingerprintValue = (Read-LabPublicKey $paths).Fingerprint }
            $agent = Get-Service -Name 'ssh-agent' -ErrorAction SilentlyContinue
            [pscustomobject]@{
                PrivateKeyExists = Test-Path -LiteralPath $paths.PrivateKey -PathType Leaf
                PublicKeyExists = Test-Path -LiteralPath $paths.PublicKey -PathType Leaf
                KnownHostsExists = Test-Path -LiteralPath $paths.KnownHosts -PathType Leaf
                AgentStatus = $(if ($null -eq $agent) { 'NotInstalled' } else { [string]$agent.Status })
                AgentStartType = $(if ($null -eq $agent) { $null } else { [string]$agent.StartType })
                PublicKeyFingerprint = $fingerprintValue
            } | ConvertTo-Json
        }
        default { throw 'Unsupported setup action.' }
    }
}

# Dot-sourcing loads functions for isolated tests only; it performs no action.
if ($MyInvocation.InvocationName -ne '.') {
    try { Invoke-LabSshSetup $Action $VerifiedHostFingerprint }
    catch { Write-Error -Message $_.Exception.Message -ErrorAction Continue; exit 1 }
}
