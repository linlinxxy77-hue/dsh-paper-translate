param(
  [string]$InitialPathBase64 = "EMPTY"
)

# 输出统一 UTF-8，中文路径不变成乱码。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ErrorActionPreference = "Stop"

try {
  Add-Type -AssemblyName System.Windows.Forms | Out-Null
  $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
  $dialog.Description = "选择译文与总结的输出文件夹"
  $dialog.ShowNewFolderButton = $true
  $dialog.RootFolder = [System.Environment+SpecialFolder]::MyComputer

  if ($InitialPathBase64 -and $InitialPathBase64 -ne "EMPTY") {
    try {
      $initial = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($InitialPathBase64))
      if ($initial -and (Test-Path -LiteralPath $initial -PathType Container)) {
        $dialog.SelectedPath = $initial
      }
    } catch {
      # 初始路径不可用就退回默认位置，不打断选择。
    }
  }

  $result = $dialog.ShowDialog()
  if ($result -eq [System.Windows.Forms.DialogResult]::OK -and $dialog.SelectedPath) {
    @{ cancelled = $false; path = $dialog.SelectedPath } | ConvertTo-Json -Compress
  } else {
    @{ cancelled = $true } | ConvertTo-Json -Compress
  }
} catch {
  @{ error = $_.Exception.Message } | ConvertTo-Json -Compress
  exit 1
}
