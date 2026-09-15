#!/usr/bin/env python3
"""
从 Jobsdb HK 搜索 URL 抓取所有岗位列表（通过 API，自动分页）。

用法:
    python3 fetch_jobs.py "<搜索URL>" [-o output.json] [--max-pages N] [--page-size N]
    python3 fetch_jobs.py --classification-id 6008 --worktype-id 242 --salaryrange 20000-25000 --salarytype monthly

URL 示例:
    https://hk.jobsdb.com/zh/ESG-jobs/in-Hong-Kong-SAR
    https://hk.jobsdb.com/jobs-in-marketing-communications/full-time/on-site?salaryrange=20000-25000&salarytype=monthly
"""

import argparse
import json
import re
import sys
import time
import subprocess
import urllib.parse

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

# ── Worktype slug → ID mapping ─────────────────────────────────────────────
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

# ── Work arrangement ────────────────────────────────────────────────────────
WORK_ARRANGEMENT_MAP = {
    "on-site": "1",
    "remote": "2",
    "hybrid": "3",
}

API_BASE = "https://hk.jobsdb.com/api/jobsearch/v5/search"
DEFAULT_PAGE_SIZE = 50


def curl_get(url: str, retries: int = 3) -> str:
    """使用 curl 获取 URL 内容，带重试。"""
    for attempt in range(retries):
        cmd = [
            "curl", "-s", "-L",
            "--max-time", "30",
            "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "-H", "Accept: application/json, text/plain, */*",
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
            time.sleep(2 ** attempt)
    
    try:
        return result.stdout.decode('utf-8').strip()
    except UnicodeDecodeError:
        return result.stdout.decode('gbk', errors='ignore').strip()


def extract_keyword_from_url(url: str) -> str:
    """从JobsDB URL中提取搜索关键词"""
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.strip("/")
    
    # 尝试从路径中提取关键词
    patterns = [
        r'/(?:zh/)?([^/]+)-jobs(?:/|$)',  # ESG-jobs 或 /zh/ESG-jobs/
        r'/jobs-([^/]+)(?:/|$)',           # jobs-ESG
    ]
    for pattern in patterns:
        match = re.search(pattern, path)
        if match:
            keyword = match.group(1)
            # 如果是分类名称（在 CLASSIFICATION_MAP 中），跳过
            if keyword.lower() not in CLASSIFICATION_MAP:
                return keyword
    
    # 从 query string 提取 keywords
    qs = urllib.parse.parse_qs(parsed.query)
    if 'keywords' in qs:
        return qs['keywords'][0]
    
    return None


def parse_search_url(url: str) -> dict:
    """从搜索 URL 提取 API 参数。"""
    params = {
        "siteKey": "HK-Main",
        "sourcesystem": "houston",
        "pageSize": DEFAULT_PAGE_SIZE,
    }

    parsed = urllib.parse.urlparse(url)
    path = parsed.path.strip("/")
    segments = path.split("/")

    # 用于存储从路径中提取的可能关键词
    potential_keyword = None

    for seg in segments:
        seg_lower = seg.lower()
        # 处理 "jobs-in-marketing-communications" 格式
        clean_seg = seg_lower
        if seg_lower.startswith("jobs-in-"):
            clean_seg = seg_lower[len("jobs-in-"):]
        elif seg_lower.startswith("jobs-"):
            # 提取 jobs- 后面的部分
            keyword_part = seg_lower[len("jobs-"):]
            # 检查是否是已知的分类
            if keyword_part in CLASSIFICATION_MAP:
                params["classification"] = CLASSIFICATION_MAP[keyword_part]
            else:
                # 如果不是已知分类，保存为潜在关键词
                potential_keyword = keyword_part
            continue
        # 去掉语言前缀
        if clean_seg in ["zh", "en"]:
            continue

        if clean_seg in CLASSIFICATION_MAP:
            params["classification"] = CLASSIFICATION_MAP[clean_seg]
        elif seg_lower in CLASSIFICATION_MAP:
            params["classification"] = CLASSIFICATION_MAP[seg_lower]
        elif seg_lower in WORKTYPE_MAP:
            params["worktype"] = WORKTYPE_MAP[seg_lower]
        elif seg_lower in WORK_ARRANGEMENT_MAP:
            params["workarrangement"] = WORK_ARRANGEMENT_MAP[seg_lower]

    # 从 query string 提取 salaryrange, salarytype, where, keywords 等
    qs = urllib.parse.parse_qs(parsed.query)
    for key in ["salaryrange", "salarytype", "where", "keywords", "pageSize"]:
        if key in qs:
            params[key] = qs[key][0]

    # 如果有潜在关键词且不在分类列表中，添加到 params
    if potential_keyword and potential_keyword not in CLASSIFICATION_MAP:
        params["keyword"] = potential_keyword
        params["keywords"] = potential_keyword

    return params


def fetch_page(params: dict, page: int, keyword: str = None) -> dict:
    """获取单页搜索结果。"""
    url_params = params.copy()
    url_params["page"] = str(page)
    
    # 如果有关键词，添加到参数中
    if keyword:
        url_params["keyword"] = keyword
        url_params["keywords"] = keyword
        url_params["sortMode"] = "Date"

    # ★★★ 新增：按日期排序（最新优先） ★★★
    # 如果 params 中没有 sortMode，则默认使用 Date 排序
    if "sortMode" not in url_params:
        url_params["sortMode"] = "Date"

    query_string = "&".join(f"{k}={urllib.parse.quote_plus(str(v))}" for k, v in url_params.items())
    api_url = f"{API_BASE}?{query_string}"

    print(f"  [API] 第 {page} 页: {api_url}", file=sys.stderr)
    raw = curl_get(api_url)

    try:
        data = json.loads(raw)
        return data
    except json.JSONDecodeError:
        print(f"  [ERROR] JSON 解析失败 (第 {page} 页), 前200字符: {raw[:200]}", file=sys.stderr)
        return {}


def fetch_all(params: dict, max_pages: int = 0, keyword: str = None) -> list:
    """获取所有页的搜索结果。"""
    print("[INFO] 获取第 1 页以确认总数...", file=sys.stderr)
    if keyword:
        print(f"[INFO] 使用关键词: {keyword}", file=sys.stderr)
    
    first = fetch_page(params, 1, keyword=keyword)

    if not first or "data" not in first:
        print("[ERROR] 第 1 页无数据，请检查搜索 URL 是否正确", file=sys.stderr)
        return []

    total_count = first.get("totalCount", 0)
    total_pages = (total_count + params.get("pageSize", DEFAULT_PAGE_SIZE) - 1) // params.get("pageSize", DEFAULT_PAGE_SIZE)

    if max_pages > 0:
        total_pages = min(total_pages, max_pages)

    print(f"[INFO] 总计 {total_count} 个岗位, {total_pages} 页", file=sys.stderr)

    all_jobs = list(first.get("data", []))

    # 获取剩余页面
    for page in range(2, total_pages + 1):
        time.sleep(1.5)
        result = fetch_page(params, page, keyword=keyword)
        jobs = result.get("data", [])
        all_jobs.extend(jobs)
        print(f"  [INFO] 第 {page}/{total_pages} 页完成, 累计 {len(all_jobs)} 个", file=sys.stderr)

    return all_jobs


def flatten_job(job: dict, params: dict = None) -> dict:
    """将 API 返回的 job 对象扁平化为统一格式。"""
    classifications = job.get("classifications", [])
    subclass_name = ""
    class_name = ""
    if classifications:
        subclass_name = classifications[0].get("subclassification", {}).get("description", "")
        class_name = classifications[0].get("classification", {}).get("description", "")

    locations = job.get("locations", [])
    location_str = ""
    if locations:
        location_parts = []
        for loc in locations[:2]:
            label = loc.get("label", "")
            if label:
                location_parts.append(label)
        location_str = ", ".join(location_parts)

    salary = job.get("salaryLabel", "面议") or "面议"

    advertiser = job.get("advertiser", {})
    company = advertiser.get("description", "") or job.get("companyName", "")

    bullets = job.get("bulletPoints", [])

    job_id = str(job.get("id", ""))
    link = f"https://hk.jobsdb.com/zh/job/{job_id}"

    return {
        "id": job_id,
        "company": company,
        "title": job.get("title", ""),
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
    parser.add_argument("--classification-id", help="手动指定 classification ID")
    parser.add_argument("--worktype-id", help="手动指定 worktype ID")
    parser.add_argument("--salaryrange", help="手动指定薪资范围")
    parser.add_argument("--salarytype", help="手动指定薪资类型")
    parser.add_argument("--where", help="手动指定地点")
    parser.add_argument("--keyword", help="手动指定搜索关键词")
    parser.add_argument("--first-page-only", action="store_true", help="仅获取第 1 页（测试用）")

    args = parser.parse_args()

    # 确定 API 参数
    keyword = None
    if args.url:
        # 清理 URL（去除 token 等无关参数）
        url = re.sub(r'[?&]token=[^&]*', '', args.url)
        url = re.sub(r'[?&]ref=[^&]*', '', url)
        params = parse_search_url(url)
        
        # 从 params 中提取关键词（如果 parse_search_url 已经提取了）
        if "keyword" in params:
            keyword = params["keyword"]
            print(f"[INFO] 从 URL 自动提取到关键词: {keyword}", file=sys.stderr)
        
        # 如果 params 中没有关键词，尝试用 extract_keyword_from_url 提取
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
        if args.classification_id:
            params["classification"] = args.classification_id
        if args.worktype_id:
            params["worktype"] = args.worktype_id
        if args.salaryrange:
            params["salaryrange"] = args.salaryrange
        if args.salarytype:
            params["salarytype"] = args.salarytype
        if args.where:
            params["where"] = args.where

    # 手动指定的关键词覆盖URL提取的
    if args.keyword:
        keyword = args.keyword
        print(f"[INFO] 使用手动指定的关键词: {keyword}", file=sys.stderr)

    # 验证必要参数
    if "classification" not in params and not keyword:
        print("[WARN] 未指定 classification 或 keyword，可能返回所有职位", file=sys.stderr)

    params["pageSize"] = args.page_size

    # 抓取
    max_pages = 1 if args.first_page_only else args.max_pages
    all_raw = fetch_all(params, max_pages=max_pages, keyword=keyword)

    if not all_raw:
        print("[ERROR] 未获取到任何岗位", file=sys.stderr)
        sys.exit(1)

    # 扁平化
    flattened = [flatten_job(job, params) for job in all_raw]

    # 输出
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump({
            "total": len(flattened),
            "search_params": params,
            "keyword": keyword,
            "jobs": flattened,
        }, f, ensure_ascii=False, indent=2)

    print(f"\n[DONE] 共 {len(flattened)} 个岗位 → {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()