@echo off
chcp 65001 > nul
echo 바탕화면에 라씨봇 아이콘을 만드는 중...

set SCRIPT_DIR=%~dp0
set PYTHON=%SCRIPT_DIR%venv\Scripts\pythonw.exe
set TARGET=%SCRIPT_DIR%lassi_desktop.py
set DESKTOP=%USERPROFILE%\Desktop
set SHORTCUT=%DESKTOP%\라씨 매매비서.lnk

powershell -NoProfile -Command ^
  "$ws = New-Object -ComObject WScript.Shell;" ^
  "$s = $ws.CreateShortcut('%SHORTCUT%');" ^
  "$s.TargetPath = '%PYTHON%';" ^
  "$s.Arguments = '%TARGET%';" ^
  "$s.WorkingDirectory = '%SCRIPT_DIR%';" ^
  "$s.Description = '라씨 매매비서';" ^
  "$s.Save()"

echo.
echo 완료! 바탕화면에 [라씨 매매비서] 아이콘이 생겼습니다.
echo 더블클릭하면 봇이 시작되고 브라우저가 자동으로 열립니다.
echo.
pause
