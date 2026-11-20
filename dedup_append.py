#!/usr/bin/env python3
"""
去重 + 追加：将新岗位与已有基准文件比较，输出去重后的新岗位和更新后的 CSV。
去重规则（2026-11-20 起）：只按 job ID，不再按公司名（旧规则会误杀同公司新岗位）。

用法:
    python3 dedup_append.py \
      --new /tmp/all_jobs.json \
      --existing "../工作 - Sheet1.csv" \
      --output /tmp/new_unique_jobs.json \
      --updated-csv "../工作 - Sheet1.csv"

支持基准文件格式: .csv (逗号分隔) 或 .xlsx (Excel)
"""

import argparse
import csv
import io
import json
import re
import sys
import os


def extract_job_id(url_or_id: str) -> str:
    """从 URL 或 ID 字符串中提取 job ID。"""
    if not url_or_id:
        return ""
    match = re.search(r'/job/(\d+)', str(url_or_id))
    if match:
        return match.group(1)
    if re.match(r'^\d{6,}$', str(url_or_id)):
        return str(url_or_id)
    return ""


def normalize_company(name: str) -> str:
    """标准化公司名用于比较。"""
    if not name:
        return ""
    name = name.lower().strip()
    name = re.sub(r'\s*\(h\.?k\.?\)\s*', ' ', name)
    name = re.sub(r'\s*\(hong kong\)\s*', ' ', name)
    name = re.sub(r'\s*limited\s*', ' ', name)
    name = re.sub(r'\s*ltd\.?\s*', ' ', name)
    name = re.sub(r'\s*co\.?,?\s*', ' ', name)
    name = re.sub(r'\s*inc\.?\s*', ' ', name)
    name = re.sub(r'\s*corp\.?\s*', ' ', name)
    name = re.sub(r'\s+', ' ', name)
    return name.strip()


def read_existing_from_csv(filepath: str) -> tuple:
    """从 CSV 读取已有岗位，返回 (existing_ids, existing_companies, header, rows)。"""
    existing_ids = set()
    existing_companies = set()
    rows = []
    header = []

    with open(filepath, "r", encoding="utf-8-sig") as f:
        content = f.read()

    reader = csv.reader(io.StringIO(content))
    try:
        header = next(reader)
    except StopIteration:
        print("[WARN] CSV 文件为空", file=sys.stderr)
        return existing_ids, existing_companies, [], []

    for row in reader:
        if not row or all(cell.strip() == "" for cell in row):
            continue
        rows.append(row)

        # 从第11列（职位链接）提取 job ID（索引10）
        if len(row) >= 11:
            jid = extract_job_id(row[10])
            if jid:
                existing_ids.add(jid)

        # 从第4列（公司）提取公司名
        if len(row) >= 4:
            norm = normalize_company(row[3])
            if norm:
                existing_companies.add(norm)

    print(f"[INFO] CSV: {len(rows)} 行, {len(existing_ids)} 个已有 job ID, "
          f"{len(existing_companies)} 个已有公司", file=sys.stderr)
    return existing_ids, existing_companies, header, rows


def read_existing_from_xlsx(filepath: str) -> tuple:
    """从 Excel 读取已有岗位，返回 (existing_ids, existing_companies, header, rows)。"""
    try:
        import openpyxl
    except ImportError:
        print("[ERROR] 需要安装 openpyxl: pip3 install openpyxl", file=sys.stderr)
        sys.exit(1)

    wb = openpyxl.load_workbook(filepath, read_only=True)
    ws = wb.active

    existing_ids = set()
    existing_companies = set()
    rows = []
    header = []

    for i, row in enumerate(ws.iter_rows(values_only=True)):
        row_vals = [str(cell) if cell is not None else "" for cell in row]
        if i == 0:
            header = row_vals
            continue
        rows.append(row_vals)

        if len(row_vals) >= 11:
            jid = extract_job_id(row_vals[10])
            if jid:
                existing_ids.add(jid)

        if len(row_vals) >= 4:
            norm = normalize_company(row_vals[3])
            if norm:
                existing_companies.add(norm)

    wb.close()
    print(f"[INFO] Excel: {len(rows)} 行, {len(existing_ids)} 个已有 job ID, "
          f"{len(existing_companies)} 个已有公司", file=sys.stderr)
    return existing_ids, existing_companies, header, rows


def load_new_jobs(filepath: str) -> list:
    """加载新抓取的岗位。"""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "jobs" in data:
        return data["jobs"]
    return []


def dedup(new_jobs: list, existing_ids: set, existing_companies: set = None) -> list:
    """去重：只按 job ID 去重（2026-11-20 按用户要求修改）。

    旧版还按公司名去重——主文件里有该公司的任何旧岗位，就会把该公司
    全部新岗位误杀（实测 eng 285 个岗位被公司规则大量误杀）。改为只认
    职位 ID：ID 没出现过就是新岗位，不理公司；同一批次内重复的 ID
    也只保留第一条。existing_companies 参数保留仅为兼容旧调用，不再使用。
    """
    unique = []
    dupes = 0
    seen_batch = set()
    for job in new_jobs:
        jid = str(job.get("id", ""))

        if jid and (jid in existing_ids or jid in seen_batch):
            dupes += 1
            continue
        if jid:
            seen_batch.add(jid)

        unique.append(job)

    print(f"[INFO] 去重（只按 job ID）: {len(new_jobs)} → {len(unique)} 新岗位 ({dupes} 重复)", file=sys.stderr)
    return unique


def job_to_csv_row(job: dict, seq: int) -> list:
    """将 job dict 转换为 CSV 行（14列，含发布日期）。"""
    # ★★★ 提取发布日期 ★★★
    listing_date = job.get("listing_date", "")
    if listing_date:
        if "T" in listing_date:
            listing_date = listing_date.split("T")[0]
        elif " " in listing_date:
            listing_date = listing_date.split(" ")[0]
    
    return [
        str(seq),                                  # 1. 序号
        job.get("company_type", "") or "待确认",    # 2. 类型
        job.get("industry_cn", "") or job.get("industry", ""),  # 3. 行业
        job.get("company", ""),                    # 4. 公司
        job.get("rating", "") or "无评分",          # 5. 公司评分
        job.get("title", ""),                      # 6. 职位名称
        job.get("salary", "面议"),                 # 7. 薪资范围
        job.get("jd", ""),                         # 8. 工作描述
        job.get("location", ""),                   # 9. 地点
        listing_date,                              # 10. ★★★ 发布日期 ★★★
        job.get("link", ""),                       # 11. 职位链接
        "",                                        # 12. 进程
        "",                                        # 13. 投递时间
        "",                                        # 14. 备注
    ]


def get_max_seq(rows: list, header: list) -> int:
    """从已有行中获取最大序号。"""
    max_seq = 0
    if not header:
        return 0
    try:
        seq_col = header.index("序号")
    except ValueError:
        return len(rows)

    for row in rows:
        if len(row) > seq_col:
            try:
                seq = int(row[seq_col].strip())
                if seq > max_seq:
                    max_seq = seq
            except (ValueError, AttributeError):
                continue
    return max_seq


def main():
    parser = argparse.ArgumentParser(description="去重并追加到 CSV")
    parser.add_argument("--new", required=True, help="新岗位 JSON 文件")
    parser.add_argument("--existing", required=True, help="已有基准文件 (.csv 或 .xlsx)")
    parser.add_argument("--output", default="/tmp/new_unique_jobs.json", help="去重后新岗位 JSON")
    parser.add_argument("--updated-csv", help="更新后的 CSV 输出路径")
    parser.add_argument("--enriched", action="store_true", help="输入已包含补全字段")
    parser.add_argument("--dry-run", action="store_true", help="仅汇报，不写文件")

    args = parser.parse_args()

    ext = os.path.splitext(args.existing)[1].lower()
    if ext == ".xlsx":
        existing_ids, existing_companies, header, rows = read_existing_from_xlsx(args.existing)
    else:
        existing_ids, existing_companies, header, rows = read_existing_from_csv(args.existing)

    new_jobs = load_new_jobs(args.new)
    print(f"[INFO] 新岗位输入: {len(new_jobs)} 个", file=sys.stderr)

    unique = dedup(new_jobs, existing_ids, existing_companies)

    if not unique:
        print("[INFO] 没有新岗位，无需追加", file=sys.stderr)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump([], f)
        return

    if not args.dry_run:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(unique, f, ensure_ascii=False, indent=2)
        print(f"[OUTPUT] {len(unique)} 个新岗位 → {args.output}", file=sys.stderr)

    if args.updated_csv and not args.dry_run:
        max_seq = get_max_seq(rows, header)
        new_rows = []
        for i, job in enumerate(unique):
            seq = max_seq + i + 1
            new_rows.append(job_to_csv_row(job, seq))

        # ★★★ 强制使用14列标准表头 ★★★
        header = [
            "序号", "类型", "行业", "公司", "公司评分",
            "职位名称", "薪资范围", "工作描述", "地点",
            "发布日期", "职位名称点开的链接", "进程", "投递时间", "备注",
        ]

        all_rows = rows + new_rows

        with open(args.updated_csv, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(all_rows)

        print(f"[OUTPUT] CSV 更新: {len(rows)} → {len(all_rows)} 行 → {args.updated_csv}", file=sys.stderr)
        print(f"  (新增 {len(new_rows)} 行, 序号 {max_seq + 1} - {max_seq + len(new_rows)})", file=sys.stderr)


if __name__ == "__main__":
    main()