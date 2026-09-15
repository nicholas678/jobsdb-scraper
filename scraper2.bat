@echo off
chcp 65001 >nul
set "BASE_DIR=D:\jobsdb-scraper"
set "MAX_PAGES=0"
set "CLASSIFICATION_ID_ESG=6076"
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

:: ★★★ 修改顺序：ESG 第一，environmental-engineer 第二，quality-engineer 第三 ★★★
for %%i in (esg eng qe qa it se da) do (
    if "%%i"=="esg" set "KEYWORD=ESG"
    if "%%i"=="eng" set "KEYWORD=environmental-engineer"
    if "%%i"=="qe" set "KEYWORD=quality-engineer"
    if "%%i"=="qa" set "KEYWORD=quality-assurance"
    if "%%i"=="it" set "KEYWORD=it-support"
    if "%%i"=="se" set "KEYWORD=support-engineer"
    if "%%i"=="da" set "KEYWORD=data-analyst"
    
    echo.
    echo ============================================================
    echo 【处理】!KEYWORD!
    echo ============================================================
    
    :: ── 第1步：抓取列表 ──────────────────────────────────────
    echo [1/5] 抓取 !KEYWORD! 岗位列表...
    if "%%i"=="esg" (
        python fetch_jobs.py --classification-id %CLASSIFICATION_ID_ESG% --keyword "!KEYWORD!" --where "Hong Kong" --max-pages %MAX_PAGES% -o "%BASE_DIR%\%%i_jobs.json"
    ) else (
        python fetch_jobs.py "https://hk.jobsdb.com/zh/!KEYWORD!-jobs" --max-pages %MAX_PAGES% -o "%BASE_DIR%\%%i_jobs.json"
    )
    if errorlevel 1 (
        echo [ERROR] !KEYWORD! 第1步失败，跳过
        timeout /t 2 >nul
        goto :continue_%%i
    )
    timeout /t 2 >nul
    
    :: ── 第2步：去重 ──────────────────────────────────────────
    echo [2/5] 去重（与基准文件比较）...
    python dedup_append.py --new "%BASE_DIR%\%%i_jobs.json" --existing "%BASELINE_CSV%" --output "%BASE_DIR%\%%i_new_unique.json" --updated-csv "%BASELINE_CSV%"
    if errorlevel 1 (
        echo [ERROR] !KEYWORD! 去重失败，跳过
        timeout /t 2 >nul
        goto :continue_%%i
    )
    timeout /t 2 >nul
    
    :: ── 检查是否有新岗位 ──────────────────────────────────
    if not exist "%BASE_DIR%\%%i_new_unique.json" (
        echo [INFO] !KEYWORD! 没有新岗位，跳过后续步骤
        goto :continue_%%i
    )
    
    findstr /c:"[" "%BASE_DIR%\%%i_new_unique.json" >nul 2>&1
    if errorlevel 1 (
        echo [INFO] !KEYWORD! 没有新岗位，跳过后续步骤
        goto :continue_%%i
    )
    
    :: ── 第3步：获取完整 JD ──────────────────────────────────
    echo [3/5] 获取 !KEYWORD! 完整 JD...
    python fetch_jd.py --input "%BASE_DIR%\%%i_new_unique.json" --output "%BASE_DIR%\%%i_jd.json"
    if errorlevel 1 (
        echo [ERROR] !KEYWORD! 第3步失败，跳过
        timeout /t 2 >nul
        goto :continue_%%i
    )
    timeout /t 2 >nul
    
    :: ── 第4步：Node.js 筛选 ──────────────────────────────────
    echo [4/5] Node.js 筛选 !KEYWORD!...
    if exist "parse-jobs.js" (
        node parse-jobs.js "%BASE_DIR%\%%i_jd.json" "%BASE_DIR%\filtered_jobs_%%i_7days.json"
        if errorlevel 1 (
            echo [ERROR] !KEYWORD! Node.js 筛选失败
        ) else (
            echo [OK] 已生成 filtered_jobs_%%i_7days.json
        )
    ) else (
        echo [WARN] 找不到 parse-jobs.js，跳过筛选
        copy "%BASE_DIR%\%%i_jd.json" "%BASE_DIR%\filtered_jobs_%%i_7days.json" >nul
    )
    timeout /t 2 >nul
    
    :: ── 第5步：生成 Excel（直接使用 jd.json）───────────────
    echo [5/5] 生成 !KEYWORD! Excel...
    if exist "%BASE_DIR%\%%i_jd.json" (
        python export_excel.py --input "%BASE_DIR%\%%i_jd.json" --output "%BASE_DIR%\%%i_岗位收集.xlsx" --new-only
        echo [OK] 已生成 %%i_岗位收集.xlsx
    ) else (
        echo [WARN] 没有数据可导出
    )
    timeout /t 2 >nul
    
    echo [OK] !KEYWORD! 全部完成！
    
    :continue_%%i
    echo.
)

:: ── 完成 ────────────────────────────────────────────────────────
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