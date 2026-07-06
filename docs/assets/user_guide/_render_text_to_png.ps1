# -*- 截图脚本 -*-
# 把 .txt 文件用黑底白字等宽字体渲染成 PNG
# 用法：powershell -File _render_text_to_png.ps1 -InputFile <input.txt> -OutputFile <output.png>

param(
    [Parameter(Mandatory=$true)] [string]$InputFile,
    [Parameter(Mandatory=$true)] [string]$OutputFile,
    [int]$Width = 1100,
    [int]$FontSize = 13
)

Add-Type -AssemblyName System.Drawing

$lines = Get-Content -Path $InputFile -Encoding UTF8
$lineHeight = [int]($FontSize * 1.45)
$height = ($lines.Count + 4) * $lineHeight

$bmp = New-Object System.Drawing.Bitmap $Width, $height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.Clear([System.Drawing.Color]::FromArgb(12, 12, 12))
$g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::ClearTypeGridFit

$font = New-Object System.Drawing.Font("Consolas", [single]$FontSize)
$brush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(220, 220, 220))
$accentBrush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(80, 220, 120))
$titleFont = New-Object System.Drawing.Font("Microsoft YaHei", [single]($FontSize - 1), [System.Drawing.FontStyle]::Bold)

$g.DrawString("PowerShell -- MTCA CLI 演示", $titleFont, $accentBrush, 12, 6)

$y = 6 + $lineHeight
foreach ($line in $lines) {
    $g.DrawString($line, $font, $brush, 12, $y)
    $y += $lineHeight
}

$borderPen = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(60, 60, 60)), 1
$g.DrawRectangle($borderPen, 0, 0, $Width - 1, $height - 1)

$bmp.Save($OutputFile, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose()
$bmp.Dispose()

Write-Output ("saved: {0}  size={1}x{2}" -f $OutputFile, $Width, $height)
