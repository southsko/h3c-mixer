@echo off
rem Drag one or more .3mf files onto this file.
python "%~dp0h3c_mix.py" %*
if errorlevel 9009 (
  echo Python was not found. Install it from python.org and tick "Add to PATH".
  pause
)
