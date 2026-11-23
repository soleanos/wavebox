@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
echo Creation du raccourci WaveBox Studio sur ton Bureau...
powershell.exe -NoProfile -NonInteractive -Command "$dir = (Get-Location).Path; $desktop = [Environment]::GetFolderPath('Desktop'); $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop 'WaveBox Studio.lnk')); $lnk.TargetPath = (Join-Path $dir 'LANCER_WINDOWS.bat'); $lnk.WorkingDirectory = $dir; $lnk.IconLocation = (Join-Path $dir 'assets\wavebox.ico'); $lnk.Description = 'WaveBox Studio 4 - Ma bibliotheque audio'; $lnk.Save()"
if errorlevel 1 (
  echo La creation du raccourci a echoue. Aucun autre fichier n'a ete modifie.
) else (
  echo Raccourci ajoute au Bureau. Ne deplace pas le dossier WaveBox apres cette operation.
)
pause
endlocal
