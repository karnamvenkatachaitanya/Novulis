@echo off
title WaiverPro QA Dashboard Controller
echo =====================================================================
echo Starting WaiverPro QA Compliance Agent Control Dashboard...
echo =====================================================================
cd dashboard
echo Opening browser to dashboard control center...
start http://localhost:3000
echo Launching Next.js development server...
npm run dev
