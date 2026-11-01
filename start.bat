@echo off
chcp 65001 >nul
title PHANTOM CUTS - Barber Booking
cd /d "%~dp0"

rem ---- หา Python (ลอง py ก่อน แล้วค่อย python) ----
set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto :nopython

rem ---- ติดตั้งไลบรารีหลัก (Flask, Pillow) ถ้ายังไม่มี ----
%PY% -c "import flask, PIL" >nul 2>nul
if errorlevel 1 goto :install
goto :optional

:install
echo กำลังติดตั้งไลบรารีหลักครั้งแรก รอสักครู่...
%PY% -m pip install -r requirements.txt
if errorlevel 1 goto :pipfail

:optional
rem ---- ไลบรารีเสริม OpenCV (อ่าน QR สลิป) ลองติดตั้งครั้งเดียว ไม่สำเร็จก็ใช้งานต่อได้ ----
if exist .no_opencv goto :run
%PY% -c "import cv2" >nul 2>nul
if not errorlevel 1 goto :run
echo กำลังติดตั้งตัวอ่าน QR สลิป OpenCV ใช้เวลาสักครู่...
%PY% -m pip install -r requirements-optional.txt
if errorlevel 1 goto :nocv
goto :run

:nocv
echo [แจ้งเตือน] ติดตั้ง OpenCV ไม่สำเร็จ ระบบยังใช้งานได้ แต่จะอ่าน QR บนสลิปอัตโนมัติไม่ได้
echo ต้องตรวจสลิปด้วยตา ลองติดตั้งภายหลังด้วย  pip install opencv-python-headless
echo ไม่สำเร็จ> .no_opencv

:run
echo.
echo ===============================================
echo   PHANTOM CUTS  -  ระบบจองคิวร้านตัดผม v2
echo   เปิดที่  http://127.0.0.1:5000
echo   ปิดเซิร์ฟเวอร์: กด Ctrl+C หรือปิดหน้าต่างนี้
echo ===============================================
echo.
rem เปิดเบราว์เซอร์อัตโนมัติหลังรอ 2 วินาที
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:5000"
%PY% app.py
echo.
echo เซิร์ฟเวอร์หยุดทำงานแล้ว
pause
exit /b 0

:nopython
echo [ผิดพลาด] ไม่พบ Python ในเครื่อง
echo ดาวน์โหลดได้ที่ https://www.python.org/downloads/
echo ตอนติดตั้งให้ติ๊ก "Add python.exe to PATH" ด้วย
pause
exit /b 1

:pipfail
echo [ผิดพลาด] ติดตั้งไลบรารีไม่สำเร็จ ตรวจสอบอินเทอร์เน็ตแล้วลองใหม่
pause
exit /b 1
