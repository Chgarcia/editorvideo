@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

rem ============================================================
rem  Publica una nueva version de editvideo para Windows.
rem  Uso:  publicar.bat [version] ["notas de la version"]
rem  Sin argumentos pregunta la version y las notas.
rem
rem  Si tienes la carpeta del servidor accesible (unidad de red
rem  o ruta UNC), escribela aqui para copiar automaticamente:
rem    set "PUBLISH_DIR=\\servidor\wwwroot\editvideo"
rem ============================================================
set "PUBLISH_DIR=%EDITVIDEO_PUBLISH_DIR%"
set "SERVER_URL=%EDITVIDEO_SERVER_URL%"

if not defined PYTHON_EXE if exist "%~dp0.venv-1\Scripts\python.exe" set "PYTHON_EXE=%~dp0.venv-1\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYTHON_EXE set "PYTHON_EXE=python"

set "INTERACTIVE="
if "%~1"=="" set "INTERACTIVE=1"

for /f "delims=" %%V in ('call "%PYTHON_EXE%" updater.py version') do set "CURRENT_VERSION=%%V"
for /f "delims=" %%V in ('call "%PYTHON_EXE%" updater.py next-version') do set "NEXT_VERSION=%%V"
if not defined CURRENT_VERSION (
    echo ERROR: no se pudo leer la version actual con %PYTHON_EXE%.
    goto :fail
)

echo.
echo Version actual: %CURRENT_VERSION%

set "NEW_VERSION=%~1"
if not defined NEW_VERSION (
    set "NEW_VERSION=%NEXT_VERSION%"
    set /p "NEW_VERSION=Nueva version [Enter = %NEXT_VERSION%]: "
)

set "EDITVIDEO_NOTES=%~2"
if defined INTERACTIVE set /p "EDITVIDEO_NOTES=Notas de la version (opcional): "

copy /y updater.py "%TEMP%\editvideo_updater_backup.py" >nul

"%PYTHON_EXE%" updater.py set-version %NEW_VERSION%
if errorlevel 1 goto :fail

echo.
echo Compilando editvideo %NEW_VERSION% ...
if exist dist\version.json del /f /q dist\version.json
call "%~dp0build_exe.bat"
if errorlevel 1 (
    echo.
    echo ERROR: fallo la compilacion. Se restaura la version %CURRENT_VERSION%.
    copy /y "%TEMP%\editvideo_updater_backup.py" updater.py >nul
    goto :fail
)

set "OUT_DIR=%~dp0publicar"
if exist "%OUT_DIR%" rmdir /s /q "%OUT_DIR%"
mkdir "%OUT_DIR%"
copy /y dist\editvideo.exe "%OUT_DIR%\" >nul || goto :fail
copy /y dist\version.json "%OUT_DIR%\" >nul || goto :fail

if defined PUBLISH_DIR (
    echo.
    echo Copiando al servidor: %PUBLISH_DIR%
    rem Primero el exe y al final version.json, para que nadie descargue un exe incompleto.
    copy /y "%OUT_DIR%\editvideo.exe" "%PUBLISH_DIR%\editvideo.exe" >nul || goto :fail
    copy /y "%OUT_DIR%\version.json" "%PUBLISH_DIR%\version.json" >nul || goto :fail
    echo Publicado correctamente.
)

echo.
echo ============================================================
echo  Version %NEW_VERSION% lista.
echo  Archivos en: %OUT_DIR%
echo    1. editvideo.exe
echo    2. version.json
if not defined PUBLISH_DIR (
    echo.
    echo  Configura EDITVIDEO_PUBLISH_DIR para copiar automaticamente al servidor.
    if defined SERVER_URL echo  Sube ambos a: %SERVER_URL%
    echo  Primero editvideo.exe y al final version.json.
    echo  No recompiles antes de subir: la huella SHA-256 cambiaria.
)
echo ============================================================
if defined INTERACTIVE (
    start "" explorer "%OUT_DIR%"
    pause
)
endlocal
exit /b 0

:fail
echo.
echo La publicacion no se completo.
if defined INTERACTIVE pause
endlocal
exit /b 1
