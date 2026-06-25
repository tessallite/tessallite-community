@echo off
REM Fill missing i18n translations from en into every other locale.
REM Runs the standalone translate_i18n.py next to this file. Re-run whenever
REM en gains new keys. Pass-through args, e.g.:
REM   translate-i18n.bat --dry-run
REM   translate-i18n.bat --provider zai --model glm-4.6 --yes
setlocal
cd /d "%~dp0"
where python >nul 2>nul && (python translate_i18n.py %*) || (py translate_i18n.py %*)
endlocal
