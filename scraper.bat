@echo off
chcp 65001 >nul
set "BASE_DIR=D:\jobsdb-scraper"
set "MAX_PAGES=20"
set "CLASSIFICATION_ID_ESG=6076"

echo ============================================================
echo Jobsdb 批量抓取 + 补全 + Excel 导出（7类岗位）
echo ============================================================
echo.

cd /d "%BASE_DIR%"

:: ── 循环处理每个任务 ──────────────────────────────────────────
setlocal enabledelayedexpansion

for %%i in (qe qa eng da se it esg) do (
    if "%%i"=="qe" set "KEYWORD=quality-engineer"
    if "%%i"=="qa" set "KEYWORD=quality-assurance"
    if "%%i"=="eng" set "KEYWORD=environmental-engineer"
    if "%%i"=="da" set "KEYWORD=data-analyst"
    if "%%i"=="se" set "KEYWORD=support-engineer"
    if "%%i"=="it" set "KEYWORD=it-support"
    if "%%i"=="esg" set "KEYWORD=ESG"
    
    echo.
    echo ============================================================
    echo 【处理】!KEYWORD!
    echo ============================================================
    
    :: ── 第1步：抓取 ──────────────────────────────────────────
    echo [1/4] 抓取 !KEYWORD! 岗位...
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
    
    :: ── 第2步：获取完整 JD ──────────────────────────────────
    echo [2/4] 获取 !KEYWORD! 完整 JD...
    python fetch_jd.py --input "%BASE_DIR%\%%i_jobs.json" --output "%BASE_DIR%\%%i_jd.json"
    if errorlevel 1 (
        echo [ERROR] !KEYWORD! 第2步失败，跳过
        timeout /t 2 >nul
        goto :continue_%%i
    )
    timeout /t 2 >nul
    
:: ── 第3步：补全公司信息（已跳过） ──────────────────────────
:: echo [3/4] 补全 !KEYWORD! 公司信息...
:: python enrich_company.py --input "%BASE_DIR%\%%i_jd.json" --output "%BASE_DIR%\%%i_enriched.json" --skip-rating
:: if errorlevel 1 (
::     echo [ERROR] !KEYWORD! 第3步失败，跳过
::     timeout /t 2 >nul
::     goto :continue_%%i
:: )
:: timeout /t 2 >nul
    
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
    )
    timeout /t 2 >nul
    
    :: ── 第5步：生成 Excel ──────────────────────────────────
    echo [5/5] 生成 !KEYWORD! Excel...
    if exist "filtered_jobs_%%i_7days.json" (
        python export_excel.py --input "%BASE_DIR%\filtered_jobs_%%i_7days.json" --output "%BASE_DIR%\%%i_岗位收集.xlsx" --new-only
    ) else (
        python export_excel.py --input "%BASE_DIR%\%%i_jd.json" --output "%BASE_DIR%\%%i_岗位收集.xlsx" --new-only
    )
    if errorlevel 1 (
        echo [ERROR] !KEYWORD! 第5步失败
        timeout /t 2 >nul
        goto :continue_%%i
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
echo 输出文件汇总:
echo   工作类型       筛选后文件
echo   ────────────────────────────────────────────────
echo   quality-engineer    filtered_jobs_qe_7days.json
echo   quality-assurance   filtered_jobs_qa_7days.json
echo   environmental-eng   filtered_jobs_eng_7days.json
echo   data-analyst        filtered_jobs_da_7days.json
echo   support-engineer    filtered_jobs_se_7days.json
echo   IT-support          filtered_jobs_it_7days.json
echo   ESG                 filtered_jobs_esg_7days.json
echo.
echo   完整文件列表:
echo   - *_jobs.json       (原始岗位列表)
echo   - *_jd.json         (含完整 JD)
echo   - *_enriched.json   (含公司信息)
echo   - filtered_jobs_*_7days.json (Node.js 筛选后)
echo   - *_岗位收集.xlsx   (Excel 报表)
echo ============================================================
pause