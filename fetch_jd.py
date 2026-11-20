#!/usr/bin/env python3
"""
从 Jobsdb 岗位详情页提取完整 JD（Playwright 预热 + curl_cffi Session 复用）。

用法:
    python3 fetch_jd.py --input /tmp/new_jobs.json --output /tmp/new_jobs_with_jd.json
    python3 fetch_jd.py --job-id 92935113  # 测试单个岗位

输入 JSON 格式:
    { "jobs": [ { "id": "92995260", "link": "...", ... }, ... ] }
    # 或直接是 jobs 数组: [ { "id": "...", ... }, ... ]
"""

import argparse
import json
import re
import sys
import time

from curl_cffi import requests as cffi_requests

try:
    from playwright.sync_api import sync_playwright
    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False

JOB_URL_TEMPLATE = "https://hk.jobsdb.com/zh/job/{job_id}"

# ── 全局共享 Session（预热一次，后续复用 cf_clearance cookie）─────────────
_shared_session = None
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _warmup_session():
    """用真实浏览器过一次 Cloudflare，拿到 cookie 后注入 curl_cffi Session。"""
    global _shared_session
    if _shared_session is not None:
        return _shared_session

    if not _HAS_PLAYWRIGHT:
        print("[WARN] 未安装 playwright，退化为无预热 Session", file=sys.stderr)
        _shared_session = cffi_requests.Session(impersonate="chrome124")
        _shared_session.headers["User-Agent"] = _UA
        return _shared_session

    print("[INFO] 启动浏览器预热 Cloudflare（JD 抓取）...", file=sys.stderr)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=_UA)
        page = context.new_page()
        page.goto(
            "https://hk.jobsdb.com/zh/ESG-jobs",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        try:
            page.wait_for_function(
                "() => !document.title.includes('Just a moment')",
                timeout=30000,
            )
        except Exception:
            print("[WARN] 预热等待挑战通过超时，继续尝试", file=sys.stderr)

        cookies = context.cookies()
        try:
            ua = page.evaluate("navigator.userAgent")
        except Exception:
            ua = _UA
        browser.close()

    session = cffi_requests.Session(impersonate="chrome124")
    for c in cookies:
        try:
            session.cookies.set(
                c["name"], c["value"], domain=c.get("domain", ".jobsdb.com")
            )
        except Exception:
            pass
    session.headers["User-Agent"] = ua
    _shared_session = session
    print(f"[INFO] 预热完成，注入 {len(cookies)} 个 cookie", file=sys.stderr)
    return session


def curl_get(url: str, retries: int = 2) -> str:
    """用预热后的 Session 请求 URL，被拦时重置 Session 重试一次。"""
    global _shared_session

    for attempt in range(retries):
        session = _warmup_session()
        try:
            resp = session.get(url, timeout=30)
        except Exception as e:
            print(f"  [WARN] 请求异常: {e}", file=sys.stderr)
            _shared_session = None
            time.sleep(3)
            continue

        html = resp.text
        if html and "Just a moment" not in html and "cf-chl" not in html:
            return html

        print(f"  [WARN] 被拦，重置 Session 重新预热（第 {attempt + 1} 次）", file=sys.stderr)
        _shared_session = None
        time.sleep(5)

    return ""


def clean_html_content(text: str) -> str:
    if not text:
        return ""

    # 1. 移除 <style> 和 <script> 标签及其内容
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)

    # 2. 移除 CSS 注释和 HTML 注释
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)

    # 3. 将块级 HTML 标签替换为换行
    block_tags = ['<br', '</p>', '</div>', '</li>', '</h1>', '</h2>', '</h3>',
                  '</h4>', '</h5>', '</h6>', '</tr>', '</section>', '</article>']
    for tag in block_tags:
        text = text.replace(tag, '\n')

    # 4. 将行内标签替换为空格
    inline_tags = ['</td>', '</span>', '</a>', '</strong>', '</b>', '</i>', '</em>']
    for tag in inline_tags:
        text = text.replace(tag, ' ')

    # 5. 移除所有剩余的 HTML 标签
    text = re.sub(r'<[^>]+>', '', text)

    # 6. 清理多余的空白字符
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'^\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\s+$', '', text, flags=re.MULTILINE)

    # 7. 移除空行过多的情况
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


def extract_jd_from_html(html: str) -> str:
    """从详情页 HTML 的 <script> 标签 JSON 中提取 JD。"""
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)

    candidates = []
    for s in scripts:
        if len(s) < 3000:
            continue

        contents = re.findall(r'"content"\s*:\s*"((?:[^"\\]|\\.)*)"', s)
        for c in contents:
            if len(c) > 200:
                candidates.append(c)

        content_match = re.search(r'"content"\s*:\s*"(.+?)"(?:\s*[,}])', s)
        if content_match:
            c = content_match.group(1)
            if len(c) > 200 and c not in candidates:
                candidates.append(c)

    if not candidates:
        return ""

    # 过滤掉包含 "Career Advice" 的候选
    filtered_candidates = [c for c in candidates if "Career Advice" not in c]
    if filtered_candidates:
        candidates = filtered_candidates

    best = max(candidates, key=len)

    # 步骤 1: 解码 Unicode 转义
    decoded = best
    decoded = decoded.replace('\\u003C', '<')
    decoded = decoded.replace('\\u003E', '>')
    decoded = decoded.replace('\\u002F', '/')
    decoded = decoded.replace('\\u0026', '&')
    decoded = decoded.replace('\\u003c', '<')
    decoded = decoded.replace('\\u003e', '>')
    decoded = decoded.replace('\\u002f', '/')
    decoded = decoded.replace('\\u0026', '&')

    # 解码常见转义
    decoded = decoded.replace('\\"', '"')
    decoded = decoded.replace('\\n', '\n')
    decoded = decoded.replace('\\t', '\t')
    decoded = decoded.replace('\\\\', '\\')

    # 也处理非转义的 HTML 实体
    decoded = decoded.replace('&nbsp;', ' ')
    decoded = decoded.replace('&amp;', '&')
    decoded = decoded.replace('&lt;', '<')
    decoded = decoded.replace('&gt;', '>')
    decoded = decoded.replace('&quot;', '"')
    decoded = decoded.replace('&#39;', "'")
    decoded = decoded.replace('&apos;', "'")

    cleaned = clean_html_content(decoded)

    return cleaned


def fetch_jd(job_id: str) -> str:
    """获取单个岗位的 JD。"""
    url = JOB_URL_TEMPLATE.format(job_id=job_id)
    html = curl_get(url)

    if not html or "Just a moment" in html:
        print(f"  [WARN] 岗位 {job_id}: 页面被拦截 (Cloudflare)", file=sys.stderr)
        return ""

    jd = extract_jd_from_html(html)

    if not jd:
        # 备用方案：尝试用英文路径
        url_en = f"https://hk.jobsdb.com/job/{job_id}"
        html_en = curl_get(url_en)
        if html_en and "Just a moment" not in html_en:
            jd = extract_jd_from_html(html_en)

    return jd


def load_input(input_path: str) -> list:
    """加载输入文件，支持多种 JSON 格式。"""
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if "jobs" in data:
            return data["jobs"]
        if "data" in data:
            return data["data"]
        # 兜底：找第一个 value 是 list 的 key
        for v in data.values():
            if isinstance(v, list):
                return v
        return [data]
    return []


def main():
    parser = argparse.ArgumentParser(description="从 Jobsdb 详情页提取 JD")
    parser.add_argument("--input", "-i", help="输入 JSON 文件路径")
    parser.add_argument("--output", "-o", default="new_jobs_with_jd.json", help="输出 JSON 文件路径")
    parser.add_argument("--job-id", help="单个岗位 ID（测试用）")
    parser.add_argument("--delay", type=float, default=2.0, help="请求间隔秒数")
    parser.add_argument("--skip-existing", action="store_true", help="跳过已有 JD 的岗位")

    args = parser.parse_args()

    if args.job_id:
        print(f"[TEST] 获取岗位 {args.job_id} 的 JD...", file=sys.stderr)
        jd = fetch_jd(args.job_id)
        print(f"[JD 长度] {len(jd)} 字符", file=sys.stderr)
        print(jd[:2000])
        return

    if not args.input:
        print("[ERROR] 需要 --input 或 --job-id", file=sys.stderr)
        sys.exit(1)

    jobs = load_input(args.input)
    print(f"[INFO] 共 {len(jobs)} 个岗位待获取 JD", file=sys.stderr)

    success = 0
    empty = 0
    skipped = 0

    for i, job in enumerate(jobs):
        job_id = str(job.get("id", ""))

        if args.skip_existing and job.get("jd") and len(job.get("jd", "")) > 50:
            skipped += 1
            continue

        print(f"  [{i+1}/{len(jobs)}] 岗位 {job_id}: {job.get('title', 'N/A')[:60]}", file=sys.stderr)

        jd = fetch_jd(job_id)
        job["jd"] = jd

        if jd:
            success += 1
            print(f"    ✓ JD {len(jd)} 字符", file=sys.stderr)
        else:
            empty += 1
            print(f"    ✗ 未找到 JD", file=sys.stderr)

        if i < len(jobs) - 1:
            time.sleep(args.delay)

    print(f"\n[DONE] 成功: {success}, 失败: {empty}, 跳过: {skipped}", file=sys.stderr)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)

    print(f"[OUTPUT] → {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()