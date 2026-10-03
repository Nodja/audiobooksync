@echo off
setlocal
cd /d "%~dp0"

set "ROOT=%~dp0"
set "APP=%ROOT%audiobookshelf-app"
set "OUT=%ROOT%clients"

rem Nuxt 2 bundles with webpack 4, which needs the legacy OpenSSL provider.
set "NODE_OPTIONS=--openssl-legacy-provider --max-old-space-size=8192"
set "GRADLE_USER_HOME=%ROOT%build-tools\gradle-home"

for /f "usebackq delims=" %%v in (`powershell -NoProfile -Command "(Get-Content '%APP%\package.json' -Raw | ConvertFrom-Json).version"`) do set "VERSION=%%v"
if not defined VERSION set "VERSION=unknown"

echo == Generating web assets
cd /d "%APP%"
call npm run generate
if errorlevel 1 goto fail

echo == Syncing Capacitor
call npx cap sync android
if errorlevel 1 goto fail

echo == Assembling debug APK
cd /d "%APP%\android"
call gradlew.bat assembleDebug
if errorlevel 1 goto fail

cd /d "%ROOT%"
set "APK=%APP%\android\app\build\outputs\apk\debug\app-debug.apk"
if not exist "%APK%" goto nofile

if not exist "%OUT%" mkdir "%OUT%"
copy /y "%APK%" "%OUT%\audiobookshelf-app-%VERSION%-sync-debug.apk" >nul
if errorlevel 1 goto fail

echo.
echo APK: %OUT%\audiobookshelf-app-%VERSION%-sync-debug.apk
pause
exit /b 0

:fail
echo.
echo Android build failed.
pause
exit /b 1

:nofile
echo.
echo Gradle reported success but the APK is missing:
echo   %APK%
pause
exit /b 1
