$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$buildRoot = Join-Path $projectRoot ".build"
$distRoot = Join-Path $projectRoot ".dist"
$releaseRoot = Join-Path $projectRoot "Releases"

foreach ($path in @($buildRoot, $distRoot)) {
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Recurse -Force
    }
    New-Item -ItemType Directory -Path $path | Out-Null
}
New-Item -ItemType Directory -Force -Path $releaseRoot | Out-Null

python -m PyInstaller --noconfirm --clean --windowed `
    --name "Aran-Keybinds" `
    --distpath $distRoot `
    --workpath (Join-Path $buildRoot "aran") `
    --specpath $buildRoot `
    --add-data "$(Join-Path $projectRoot 'Aran keybinds\assets');assets" `
    --add-data "$(Join-Path $projectRoot 'Aran keybinds\config.json');." `
    (Join-Path $projectRoot "Aran keybinds\dreamms_bot.py")

python -m PyInstaller --noconfirm --clean --windowed `
    --name "VoS-Bot" `
    --distpath $distRoot `
    --workpath (Join-Path $buildRoot "vos") `
    --specpath $buildRoot `
    --add-data "$(Join-Path $projectRoot 'Vos Bot\assets');assets" `
    --add-data "$(Join-Path $projectRoot 'Vos Bot\config.json');." `
    (Join-Path $projectRoot "Vos Bot\vos_bot.py")

$aranPackage = Join-Path $distRoot "Aran-Keybinds"
$vosPackage = Join-Path $distRoot "VoS-Bot"

Copy-Item -LiteralPath (Join-Path $projectRoot "Aran keybinds\README.md") -Destination $aranPackage
Copy-Item -LiteralPath (Join-Path $projectRoot "Aran keybinds\arduino_teensy_alt") -Destination $aranPackage -Recurse
Copy-Item -LiteralPath (Join-Path $projectRoot "Vos Bot\README.md") -Destination $vosPackage
Copy-Item -LiteralPath (Join-Path $projectRoot "Vos Bot\arduino_teensy_vos") -Destination $vosPackage -Recurse

$aranZip = Join-Path $releaseRoot "Aran-Keybinds-Windows-x64.zip"
$vosZip = Join-Path $releaseRoot "VoS-Bot-Windows-x64.zip"
foreach ($zip in @($aranZip, $vosZip)) {
    if (Test-Path -LiteralPath $zip) {
        Remove-Item -LiteralPath $zip -Force
    }
}
Compress-Archive -Path (Join-Path $aranPackage "*") -DestinationPath $aranZip -CompressionLevel Optimal
Compress-Archive -Path (Join-Path $vosPackage "*") -DestinationPath $vosZip -CompressionLevel Optimal

Get-Item -LiteralPath $aranZip, $vosZip | Select-Object FullName, Length
