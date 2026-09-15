@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Work IQ MCP サンプル

if not exist ".venv\Scripts\python.exe" (
    echo 仮想環境を作成します...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 goto fail
    echo.
    echo 依存パッケージを導入します。初回は数分かかります。
    .venv\Scripts\python.exe -m pip install --upgrade pip
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    if errorlevel 1 goto fail
)

if not exist ".env" copy ".env.example" ".env" >nul

rem 値が入っているかまで見る。キーだけで空なら未設定として扱う。
findstr /r /c:"^WORKIQ_CLIENT_ID=." ".env" >nul 2>&1
if errorlevel 1 goto needconfig

echo.
echo http://localhost:8000 を開きます。
echo 終了するときは、このウィンドウで Ctrl+C を押してください。
echo.
start "" /min powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 4; Start-Process 'http://localhost:8000'"
rem --reload: コードを直すと自動で反映される。無いとテンプレートだけ新しくなり食い違う。
.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
goto end

:needconfig
echo.
echo .env の設定がまだです。メモ帳で開くので、値を入れて保存してから
echo このファイルをもう一度ダブルクリックしてください。
echo   %CD%\.env
echo.
echo 設定する値は README.md の「セットアップ」を参照してください。
start "" notepad ".env"
goto end

:fail
echo.
echo セットアップに失敗しました。Python 3.10 以上が入っているか確認してください。

:end
echo.
pause
