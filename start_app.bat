@echo off
cd /d "%~dp0"
echo.
echo ===========================================
echo  Autorace Live App
echo  URL: http://localhost:8501
echo ===========================================
echo.
python -m streamlit run app/streamlit_app.py
echo.
echo [Streamlit ended with exit code: %errorlevel%]
pause
