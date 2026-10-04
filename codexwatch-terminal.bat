@echo off
rem Both launchers share terminal detection, fallback, and Python selection.
call "%~dp0codexwatch.bat" %*
exit /b %ERRORLEVEL%
