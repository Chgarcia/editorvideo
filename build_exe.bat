@echo off
setlocal EnableExtensions

cd /d "%~dp0"

set "PYTHON_EXE=C:\Users\gariz\AppData\Local\Programs\Python\Python313\python.exe"
if exist "%~dp0.venv-1\Scripts\python.exe" set "PYTHON_EXE=%~dp0.venv-1\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

set "FFMPEG_EXE="
set "FFPROBE_EXE="

for /f "delims=" %%I in ('where ffmpeg 2^>nul') do (
    set "FFMPEG_EXE=%%~fI"
    goto :ffmpeg_found
)

set "WINGET_DIR=%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
for /r "%WINGET_DIR%" %%I in (ffmpeg.exe) do (
    set "FFMPEG_EXE=%%~fI"
    goto :ffmpeg_found
)

:ffmpeg_found
for /f "delims=" %%I in ('where ffprobe 2^>nul') do (
    set "FFPROBE_EXE=%%~fI"
    goto :ffprobe_found
)

for /r "%WINGET_DIR%" %%I in (ffprobe.exe) do (
    set "FFPROBE_EXE=%%~fI"
    goto :ffprobe_found
)

:ffprobe_found
if not exist "%FFMPEG_EXE%" (
    echo No se encontro ffmpeg.exe.
    echo Instala FFmpeg en esta maquina antes de compilar.
    exit /b 1
)

if not exist "%FFPROBE_EXE%" (
    echo No se encontro ffprobe.exe.
    echo Instala FFmpeg en esta maquina antes de compilar.
    exit /b 1
)

echo Usando Python: %PYTHON_EXE%
echo Usando ffmpeg: %FFMPEG_EXE%
echo Usando ffprobe: %FFPROBE_EXE%

"%PYTHON_EXE%" -m pip install --upgrade pyinstaller
if errorlevel 1 exit /b 1

"%PYTHON_EXE%" -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name editvideo ^
  --distpath dist ^
  --workpath build\windows\work ^
  --specpath build\windows\spec ^
  --add-binary "%FFMPEG_EXE%;." ^
  --add-binary "%FFPROBE_EXE%;." ^
  --add-data "%~dp0Manual_de_usuario.html;." ^
  muteador.py

if errorlevel 1 exit /b 1

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path 'dist\editvideo.exe' -DestinationPath 'dist\editvideo-windows.zip' -Force"
if errorlevel 1 exit /b 1

"%PYTHON_EXE%" updater.py manifest windows dist\editvideo.exe
if errorlevel 1 exit /b 1

echo.
echo Ejecutable generado en: %CD%\dist\editvideo.exe
echo Paquete ZIP generado en: %CD%\dist\editvideo-windows.zip
echo Manifiesto de version en: %CD%\dist\version.json
echo Configura EDITVIDEO_UPDATE_BASE_URL con la URL HTTPS donde se publicaran las actualizaciones.
echo El ejecutable incluye ffmpeg y ffprobe.
endlocal