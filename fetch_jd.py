#!/usr/bin/env python3
"""
从 Jobsdb 岗位详情页提取完整 JD。

用法:
    python3 fetch_jd.py --input /tmp/new_jobs.json --output /tmp/new_jobs_with_jd.json
    python3 fetch_jd.py --job-id 92935113  # 测试单个岗位

输入 JSON 格式:
    { "jobs": [ { "id": "92995260", "link": "...", ... }, ... ] }
    # 或直接是 jobs 数组: [ { "id": "...", ... }, ... ]

通过 /zh/job/{id} 路径绕过 Cloudflare，从 <script> 标签 JSON 中提取
"content" 字段（Unicode 转义编码的 HTML），解码后去除 HTML 标签。
"""

import argparse
import json
import re
import sys
import time
import subprocess

JOB_URL_TEMPLATE = "https://hk.jobsdb.com/zh/job/{job_id}"


def curl_get(url: str, retries: int = 3) -> str:
    """使用 curl 获取 URL 内容，带重试和 Cloudflare 检测。"""
    for attempt in range(retries):
        cmd = [
            "curl", "-s", "-L",
            "--max-time", "30",
            "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
            url,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=35)
        
        try:
            stdout = result.stdout.decode('utf-8').strip()
        except UnicodeDecodeError:
            stdout = result.stdout.decode('gbk', errors='ignore').strip()
        
        if stdout and "Just a moment" not in stdout and "cf-chl" not in stdout:
            return stdout
        if attempt < retries - 1:
            time.sleep(3 ** attempt)
    
    try:
        return result.stdout.decode('utf-8').strip()
    except UnicodeDecodeError:
        return result.stdout.decode('gbk', errors='ignore').strip()


def clean_html_content(text: str) -> str:
    if not text:
        return ""
    
    # 1. 移除 <style> 和 <script> 标签及其内容（这些是真正的样式和脚本）
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    
    # 2. ★★★ 删除：不再全局移除 CSS 规则块 ★★★
    # text = re.sub(r'[.#][a-zA-Z0-9_-]+\s*\{[^{}]*\}', '', text)  # ← 删除这行
    
    # 3. ★★★ 删除：不再移除 @media 查询 ★★★
    # text = re.sub(r'@media[^{]*\{[^{}]*\{[^}]*\}[^{}]*\}', '', text, flags=re.DOTALL)  # ← 删除这行
    
    # 4. 移除 CSS 注释和 HTML 注释（这些是安全的）
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
    
    # 5. 将块级 HTML 标签替换为换行
    block_tags = ['<br', '</p>', '</div>', '</li>', '</h1>', '</h2>', '</h3>', 
                  '</h4>', '</h5>', '</h6>', '</tr>', '</section>', '</article>']
    for tag in block_tags:
        text = text.replace(tag, '\n')
    
    # 6. 将行内标签替换为空格
    inline_tags = ['</td>', '</span>', '</a>', '</strong>', '</b>', '</i>', '</em>']
    for tag in inline_tags:
        text = text.replace(tag, ' ')
    
    # 7. 移除所有剩余的 HTML 标签
    text = re.sub(r'<[^>]+>', '', text)
    
    # 8. 清理多余的空白字符
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'^\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\s+$', '', text, flags=re.MULTILINE)
    
    # 9. 移除空行过多的情况
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    return text.strip()


def extract_jd_from_html(html: str) -> str:
    """从详情页 HTML 的 <script> 标签 JSON 中提取 JD。"""
    # 提取所有 script 标签内容
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

    # ★★★ 过滤掉包含 "Career Advice" 的候选 ★★★
    filtered_candidates = [c for c in candidates if "Career Advice" not in c]
    if filtered_candidates:
        candidates = filtered_candidates

    # 选最长的候选
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

    # 解码常见 HTML 实体
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

    # ★★★ 步骤 2: 使用增强的清理函数 ★★★
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
        # 单条记录
        return [data]
    return []


def main():
    parser = argparse.ArgumentParser(description="从 Jobsdb 详情页提取 JD")
    parser.add_argument("--input", "-i", help="输入 JSON 文件路径")
    parser.add_argument("--output", "-o", default="new_jobs_with_jd.json", help="输出 JSON 文件路径")
    parser.add_argument("--job-id", help="单个岗位 ID（测试用）")
    parser.add_argument("--delay", type=float, default=1.5, help="请求间隔秒数")
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