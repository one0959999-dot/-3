Add-Type -Name P -Namespace W -MemberDefinition '[System.Runtime.InteropServices.DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint e);'
[W.P]::SetThreadExecutionState([uint32]"0x80000041") | Out-Null
while ($true) { Start-Sleep -Seconds 600; [W.P]::SetThreadExecutionState([uint32]"0x80000041") | Out-Null }
