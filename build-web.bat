@echo off
setlocal
cd /d "%~dp0"

set "ROOT=%~dp0"
set "CLIENT=%ROOT%audiobookshelf\client"
set "OUT=%ROOT%clients\audiobookshelf-client-sync-web.zip"

rem Nuxt 2 bundles with webpack 4, which needs the legacy OpenSSL provider.
set "NODE_OPTIONS=--openssl-legacy-provider --max-old-space-size=8192"

rem The URL prefix baked into the pages; the client defaults to /audiobookshelf.  Set
rem ROUTER_BASE_PATH before running to build for another prefix (an empty value is not accepted).

cd /d "%CLIENT%"

if not exist node_modules (
    echo == Installing client dependencies
    call npm ci
    if errorlevel 1 goto fail
)

echo == Generating the web client
call npm run generate
if errorlevel 1 goto fail

echo == Zipping to %OUT%
if not exist "%ROOT%clients" mkdir "%ROOT%clients"
if exist "%OUT%" del "%OUT%"
rem Windows' own tar (not a Git or MSYS one on PATH) writes a zip with forward-slash entries, which
rem Compress-Archive on Windows PowerShell 5.1 does not.
"%SystemRoot%\System32\tar.exe" -a -c -f "%OUT%" -C "%CLIENT%\dist" .
if errorlevel 1 goto fail

echo.
echo Done: %OUT%
exit /b 0

:fail
echo.
echo Build failed.
exit /b 1
