#!/usr/bin/env python3
"""
从 Jobsdb HK 搜索 URL 抓取所有岗位列表（网页内嵌 JSON + 浏览器预热绕过 Cloudflare）。

用法:
    python3 fetch_jobs.py "<搜索URL>" [-o output.json] [--max-pages N] [--page-size N]

URL 示例:
    https://hk.jobsdb.com/zh/ESG-jobs
    https://hk.jobsdb.com/zh/quality-engineer-jobs
"""

import argparse
import json
import random
import re
import sys
import time
import urllib.parse

from curl_cffi import requests as cffi_requests

try:
    from playwright.sync_api import sync_playwright
    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False

# ── Classification slug → ID mapping (Jobsdb HK) ──────────────────────────
CLASSIFICATION_MAP = {
    "marketing-communications": "6008",
    "sales": "6004",
    "information-technology": "6014",
    "accounting": "6001",
    "admin-human-resources": "6002",
    "banking-financial-services": "6003",
    "building-construction": "6005",
    "design-architecture": "6006",
    "education-training": "6007",
    "engineering": "6009",
    "fashion": "6019",
    "healthcare": "6010",
    "hospitality-f-b": "6011",
    "insurance": "6012",
    "legal": "6016",
    "management": "6013",
    "manufacturing": "6015",
    "media-advertising": "6017",
    "property-real-estate": "6018",
    "public-sector": "6020",
    "sciences-research": "6021",
    "supply-chain": "6024",
    "transport-logistics": "6022",
    "trades-services": "6023",
}

WORKTYPE_MAP = {
    "full-time": "242",
    "part-time": "243",
    "contract": "244",
    "temporary": "245",
    "casual": "246",
    "freelance": "250",
    "internship": "248",
    "graduate": "247",
}

WORK_ARRANGEMENT_MAP = {
    "on-site": "1",
    "remote": "2",
    "hybrid": "3",
}

DEFAULT_PAGE_SIZE = 50

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

    print("[INFO] 启动浏览器预热 Cloudflare...", file=sys.stderr)
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


def _extract_redux_json(html: str):
    """从 HTML 中截取 window.SEEK_REDUX_DATA 的完整 JSON 对象（括号配平）。"""
    i = html.find("window.SEEK_REDUX_DATA")
    if i < 0:
        return None
    s = html.find("{", i)
    if s < 0:
        return None
    depth = 0
    end = -1
    instr = False
    esc = False
    for j in range(s, len(html)):
        c = html[j]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            instr = not instr
            continue
        if instr:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    if end < 0:
        return None
    try:
        return json.loads(html[s:end])
    except json.JSONDecodeError:
        return None


def fetch_page_web(keyword: str, page: int) -> list:
    """从 /zh/{keyword}-jobs 网页提取岗位数组，带 Session 失效重预热。"""
    global _shared_session

    url = f"https://hk.jobsdb.com/zh/{keyword}-jobs"
    if page > 1:
        url += f"?page={page}"

    print(f"  [WEB] 第 {page} 页: {url}", file=sys.stderr)

    for attempt in range(2):
        session = _warmup_session()
        try:
            resp = session.get(url, timeout=30)
        except Exception as e:
            print(f"  [WARN] 第 {page} 页请求异常: {e}", file=sys.stderr)
            _shared_session = None
            time.sleep(3)
            continue

        html = resp.text
        if "Just a moment" in html or "cf-chl" in html:
            print(f"  [WARN] 第 {page} 页被拦，重置 Session 重新预热", file=sys.stderr)
            _shared_session = None
            time.sleep(5)
            continue

        data = _extract_redux_json(html)
        if not data:
            print(f"  [ERROR] 第 {page} 页找不到 SEEK_REDUX_DATA", file=sys.stderr)
            return []

        return data.get("results", {}).get("results", {}).get("jobs", [])

    print(f"  [ERROR] 第 {page} 页预热后仍被拦，放弃", file=sys.stderr)
    return []


def extract_keyword_from_url(url: str) -> str:
    """从 JobsDB URL 中提取搜索关键词。"""
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.strip("/")

    patterns = [
        r"/(?:zh/)?([^/]+)-jobs(?:/|$)",
        r"/jobs-([^/]+)(?:/|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, path)
        if match:
            keyword = match.group(1)
            if keyword.lower() not in CLASSIFICATION_MAP:
                return keyword

    qs = urllib.parse.parse_qs(parsed.query)
    if "keywords" in qs:
        return qs["keywords"][0]

    return None


def parse_search_url(url: str) -> dict:
    """从搜索 URL 提取参数。"""
    params = {
        "siteKey": "HK-Main",
        "sourcesystem": "houston",
        "pageSize": DEFAULT_PAGE_SIZE,
    }

    parsed = urllib.parse.urlparse(url)
    path = parsed.path.strip("/")
    segments = path.split("/")
    potential_keyword = None

    for seg in segments:
        seg_lower = seg.lower()
        clean_seg = seg_lower
        if seg_lower.startswith("jobs-in-"):
            clean_seg = seg_lower[len("jobs-in-"):]
        elif seg_lower.startswith("jobs-"):
            keyword_part = seg_lower[len("jobs-"):]
            if keyword_part in CLASSIFICATION_MAP:
                params["classification"] = CLASSIFICATION_MAP[keyword_part]
            else:
                potential_keyword = keyword_part
            continue
        if clean_seg in ["zh", "en"]:
            continue

        if clean_seg in CLASSIFICATION_MAP:
            params["classification"] = CLASSIFICATION_MAP[clean_seg]
        elif seg_lower in WORKTYPE_MAP:
            params["worktype"] = WORKTYPE_MAP[seg_lower]
        elif seg_lower in WORK_ARRANGEMENT_MAP:
            params["workarrangement"] = WORK_ARRANGEMENT_MAP[seg_lower]

    qs = urllib.parse.parse_qs(parsed.query)
    for key in ["salaryrange", "salarytype", "where", "keywords", "pageSize"]:
        if key in qs:
            params[key] = qs[key][0]

    if potential_keyword and potential_keyword not in CLASSIFICATION_MAP:
        params["keyword"] = potential_keyword
        params["keywords"] = potential_keyword

    return params


def fetch_all(params: dict, max_pages: int = 0, keyword: str = None) -> list:
    """获取所有页的搜索结果（网页路径 + 预热 Session）。"""
    if not keyword:
        print("[ERROR] 网页路径必须有关键词", file=sys.stderr)
        return []

    print(f"[INFO] 使用关键词: {keyword}（网页路径 + 预热 Session）", file=sys.stderr)

    all_jobs = []
    seen = set()
    page = 1

    while True:
        if max_pages > 0 and page > max_pages:
            break

        jobs = fetch_page_web(keyword, page)
        if not jobs:
            break

        new = 0
        for job in jobs:
            jid = str(job.get("id", ""))
            if jid and jid not in seen:
                seen.add(jid)
                all_jobs.append(job)
                new += 1

        print(
            f"  [WEB] 第 {page} 页提取 {len(jobs)} 个，新增 {new} 个，累计 {len(all_jobs)} 个",
            file=sys.stderr,
        )

        if new == 0:
            break

        page += 1
        # 随机延迟 3–6 秒，降低连续请求触发风控的概率
        time.sleep(random.uniform(3, 6))

    return all_jobs


def flatten_job(job: dict, params: dict = None) -> dict:
    """将网页 JSON 的 job 对象扁平化为统一格式。"""
    classifications = job.get("classifications", [])
    subclass_name = ""
    class_name = ""
    if classifications:
        subclass_name = (
            classifications[0].get("subclassification", {}).get("description", "")
        )
        class_name = classifications[0].get("classification", {}).get("description", "")

    locations = job.get("locations", [])
    location_str = ""
    if locations:
        parts = []
        for loc in locations[:2]:
            label = loc.get("label", "")
            if label:
                parts.append(label)
        location_str = ", ".join(parts)

    salary = job.get("salaryLabel", "面议") or "面议"

    advertiser = job.get("advertiser", {})
    title = advertiser.get("description", "") or job.get("title", "")
    company = job.get("companyName", "") or advertiser.get("description", "")

    bullets = job.get("bulletPoints", [])
    job_id = str(job.get("id", ""))
    link = f"https://hk.jobsdb.com/zh/job/{job_id}"

    return {
        "id": job_id,
        "company": company,
        "title": title,
        "location": location_str,
        "salary": salary,
        "industry": class_name,
        "subclass": subclass_name,
        "link": link,
        "listing_date": job.get("listingDate", ""),
        "teaser": job.get("teaser", ""),
        "bullet_points": bullets,
        "work_types": [
            w.get("label", "") if isinstance(w, dict) else str(w)
            for w in job.get("workTypes", [])
        ],
        "is_featured": job.get("isFeatured", False),
    }


def main():
    parser = argparse.ArgumentParser(description="从 Jobsdb HK 抓取岗位列表")
    parser.add_argument("url", nargs="?", help="搜索页面 URL")
    parser.add_argument("-o", "--output", default="all_jobs.json", help="输出 JSON 文件路径")
    parser.add_argument("--max-pages", type=int, default=0, help="最大页数限制 (0=全部)")
    parser.add_argument("--page-size", type=int, default=50, help="每页数量")
    parser.add_argument("--keyword", help="手动指定搜索关键词")
    parser.add_argument("--first-page-only", action="store_true", help="仅获取第 1 页（测试用）")

    args = parser.parse_args()

    keyword = None
    if args.url:
        url = re.sub(r"[?&]token=[^&]*", "", args.url)
        url = re.sub(r"[?&]ref=[^&]*", "", url)
        params = parse_search_url(url)

        if "keyword" in params:
            keyword = params["keyword"]
            print(f"[INFO] 从 URL 自动提取到关键词: {keyword}", file=sys.stderr)

        if not keyword:
            keyword = extract_keyword_from_url(url)
            if keyword:
                print(f"[INFO] 从 URL 提取到关键词: {keyword}", file=sys.stderr)
                params["keyword"] = keyword
                params["keywords"] = keyword

        print(f"[INFO] 从 URL 解析参数: {json.dumps(params, ensure_ascii=False)}", file=sys.stderr)
    else:
        params = {
            "siteKey": "HK-Main",
            "sourcesystem": "houston",
            "pageSize": args.page_size,
        }

    if args.keyword:
        keyword = args.keyword
        print(f"[INFO] 使用手动指定的关键词: {keyword}", file=sys.stderr)

    if not keyword:
        print("[ERROR] 网页路径必须有关键词（URL 或 --keyword）", file=sys.stderr)
        sys.exit(1)

    params["pageSize"] = args.page_size

    max_pages = 1 if args.first_page_only else args.max_pages
    all_raw = fetch_all(params, max_pages=max_pages, keyword=keyword)

    if not all_raw:
        print("[ERROR] 未获取到任何岗位", file=sys.stderr)
        sys.exit(1)

    flattened = [flatten_job(job, params) for job in all_raw]

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(
            {
                "total": len(flattened),
                "search_params": params,
                "keyword": keyword,
                "jobs": flattened,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(f"\n[DONE] 共 {len(flattened)} 个岗位 → {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()