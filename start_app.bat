@echo off
REM オートレース予想 Streamlit アプリ 起動スクリプト
REM デスクトップにコピーしてダブルクリックで起動可能。
REM 終了時はこのウィンドウで Ctrl+C を 2 回押す。

cd /d "%~dp0"

echo ===========================================
echo  オートレース予想 エンタメ版 起動
echo  プロジェクト: %CD%
echo  URL: http://localhost:8501
echo  終了: Ctrl+C を 2 回
echo ===========================================
echo.

streamlit run app/streamlit_app.py

REM streamlit が異常終了したらメッセージ出して止まる
echo.
echo Streamlit が停止しました。何かキーを押すとウィンドウが閉じます。
pause >nul
