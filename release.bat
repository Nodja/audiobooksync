@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem Builds the web client and the Android APK, then publishes them as a GitHub release for the
rem version in audiobooksync\__init__.py.  Needs the GitHub CLI (https://cli.github.com), signed in
rem with `gh auth login`, and the audiobooksync branch pushed: the tag is created on its head.
rem Run again for the same version to replace the files on the existing release.

set "ROOT=%~dp0"
set "OUT=%ROOT%clients"
set "BRANCH=audiobooksync"

where gh >nul 2>&1
if errorlevel 1 (
    echo The GitHub CLI is not installed: https://cli.github.com
    exit /b 1
)
gh auth status >nul 2>&1
if errorlevel 1 (
    echo Not signed in to GitHub. Run: gh auth login
    exit /b 1
)

for /f "usebackq delims=" %%v in (`powershell -NoProfile -Command "(Select-String -Path '%ROOT%audiobooksync\__init__.py' -Pattern '__version__\s*=\s*.([0-9][^.]*[.][0-9]+[.][0-9]+)').Matches[0].Groups[1].Value"`) do set "VERSION=%%v"
if not defined VERSION (
    echo Could not read the version from audiobooksync\__init__.py
    exit /b 1
)
set "TAG=v%VERSION%"

rem Old builds would otherwise be uploaded with the new ones.
if exist "%OUT%\*.apk" del /q "%OUT%\*.apk"
if exist "%OUT%\*.zip" del /q "%OUT%\*.zip"

rem The Android script pauses at the end unless told not to.
set "NO_PAUSE=1"

echo == Building the web client
call "%ROOT%build-web.bat"
if errorlevel 1 goto fail

echo == Building the Android app
call "%ROOT%build-android.bat"
if errorlevel 1 goto fail

set "FILES="
for %%f in ("%OUT%\*.zip" "%OUT%\*.apk") do set FILES=!FILES! "%%f"
if not defined FILES goto fail

echo == Publishing %TAG%
gh release view %TAG% >nul 2>&1
if errorlevel 1 (
    gh release create %TAG% %FILES% --target %BRANCH% --title "audiobooksync %VERSION%" --notes "abs-sync %VERSION%, with the web client and Android app that read its sync files."
) else (
    gh release upload %TAG% %FILES% --clobber
)
if errorlevel 1 goto fail

echo.
echo Released %TAG%
exit /b 0

:fail
echo.
echo Release failed.
exit /b 1
