@echo off
rem Локальная сборка .exe (Python нужен только на машине, где собираете)
pip install -r requirements.txt
pyinstaller --onefile --windowed --name SimpleLauncher --collect-all minecraft_launcher_lib simple_launcher.py
echo Готово: dist\SimpleLauncher.exe
pause
