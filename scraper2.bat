@echo off
chcp 65001 >nul
set "BASE_DIR=D:\jobsdb-scraper"
set "MAX_PAGES=20"
set "BASELINE_CSV=%BASE_DIR%\岗位收集_主文件.csv"

echo ============================================================
echo Jobsdb 批量抓取 + 去重 + JD + Excel（7类岗位）
echo ============================================================
echo.

cd /d "%BASE_DIR%"

:: ── 检查基准文件 ──────────────────────────────────────────────
if not exist "%BASELINE_CSV%" (
    echo [WARN] 基准文件不存在，将创建新文件
    echo. > "%BASELINE_CSV%"
)

setlocal enabledelayedexpansion

:: ★★★ 顺序：ESG 第一，environmental-engineer 第二，quality-engineer 第三 ★★★
for %%i in (esg eng qe qa it se da) do (
    call :process_one %%i
)
goto :all_done

:: ============================================================
:: 子程序：处理单个关键词
:: 参数 %1 = 任务代码（esg / eng / qe / qa / it / se / da）
:: ============================================================
:process_one
set "CODE=%~1"

if "%CODE%"=="esg" set "KEYWORD=ESG"
if "%CODE%"=="eng" set "KEYWORD=environmental-engineer"
if "%CODE%"=="qe"  set "KEYWORD=quality-engineer"
if "%CODE%"=="qa"  set "KEYWORD=quality-assurance"
if "%CODE%"=="it"  set "KEYWORD=it-support"
if "%CODE%"=="se"  set "KEYWORD=support-engineer"
if "%CODE%"=="da"  set "KEYWORD=data-analyst"

echo.
echo ============================================================
echo 【处理】!KEYWORD!
echo ============================================================

:: ── 第1步：抓取列表（全部走网页路径，ESG 也不例外）──────────
echo [1/5] 抓取 !KEYWORD! 岗位列表...
python fetch_jobs.py "https://hk.jobsdb.com/zh/!KEYWORD!-jobs" --max-pages %MAX_PAGES% -o "%BASE_DIR%\%CODE%_jobs.json"
if errorlevel 1 (
    echo [ERROR] !KEYWORD! 第1步失败，跳过
    timeout /t 2 >nul
    exit /b 1
)
timeout /t 2 >nul

:: ── 第2步：去重 ──────────────────────────────────────────
echo [2/5] 去重（与基准文件比较）...
python dedup_append.py --new "%BASE_DIR%\%CODE%_jobs.json" --existing "%BASELINE_CSV%" --output "%BASE_DIR%\%CODE%_new_unique.json" --updated-csv "%BASELINE_CSV%"
if errorlevel 1 (
    echo [ERROR] !KEYWORD! 去重失败，跳过
    timeout /t 2 >nul
    exit /b 1
)
timeout /t 2 >nul

:: ── 检查是否有新岗位（用 Python 判断 JSON 是否为空数组）──
if not exist "%BASE_DIR%\%CODE%_new_unique.json" (
    echo [INFO] !KEYWORD! 没有新岗位，跳过后续步骤
    exit /b 0
)

python -c "import json,sys; d=json.load(open(r'%BASE_DIR%\%CODE%_new_unique.json',encoding='utf-8')); sys.exit(0 if d else 1)"
if errorlevel 1 (
    echo [INFO] !KEYWORD! 没有新岗位，跳过后续步骤
    exit /b 0
)

:: ── 第3步：获取完整 JD ──────────────────────────────────
echo [3/5] 获取 !KEYWORD! 完整 JD...
python fetch_jd.py --input "%BASE_DIR%\%CODE%_new_unique.json" --output "%BASE_DIR%\%CODE%_jd.json"
if errorlevel 1 (
    echo [ERROR] !KEYWORD! 第3步失败，跳过
    timeout /t 2 >nul
    exit /b 1
)
timeout /t 2 >nul

:: ── 第4步：Node.js 筛选 ──────────────────────────────────
echo [4/5] Node.js 筛选 !KEYWORD!...
if exist "parse-jobs.js" (
    node parse-jobs.js "%BASE_DIR%\%CODE%_jd.json" "%BASE_DIR%\filtered_jobs_%CODE%_7days.json"
    if errorlevel 1 (
        echo [ERROR] !KEYWORD! Node.js 筛选失败
    ) else (
        echo [OK] 已生成 filtered_jobs_%CODE%_7days.json
    )
) else (
    echo [WARN] 找不到 parse-jobs.js，跳过筛选
    copy "%BASE_DIR%\%CODE%_jd.json" "%BASE_DIR%\filtered_jobs_%CODE%_7days.json" >nul
)
timeout /t 2 >nul

:: ── 第5步：生成 Excel（直接使用 jd.json）───────────────
echo [5/5] 生成 !KEYWORD! Excel...
if exist "%BASE_DIR%\%CODE%_jd.json" (
    python export_excel.py --input "%BASE_DIR%\%CODE%_jd.json" --output "%BASE_DIR%\%CODE%_岗位收集.xlsx" --new-only
    echo [OK] 已生成 %CODE%_岗位收集.xlsx
) else (
    echo [WARN] 没有数据可导出
)
timeout /t 2 >nul

echo [OK] !KEYWORD! 全部完成！
exit /b 0

:: ============================================================
:all_done
echo ============================================================
echo ✅ 全部任务完成！
echo.
echo 基准文件: %BASELINE_CSV%
echo.
echo 输出文件汇总:
echo   - *_jobs.json           (原始岗位列表)
echo   - *_new_unique.json     (去重后的新岗位)
echo   - *_jd.json             (含完整 JD)
echo   - filtered_jobs_*_7days.json (筛选后)
echo   - *_岗位收集.xlsx       (Excel 报表)
echo ============================================================
pause