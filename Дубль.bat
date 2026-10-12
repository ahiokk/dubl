@echo off
rem Zapusk okna. Esli okno ne poyavilos, smotri oshibka.log
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" "run.py"
