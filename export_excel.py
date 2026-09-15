#!/usr/bin/env python3
"""
从 CSV 生成格式化的 Excel 文件（含发布日期）。

用法:
    python3 export_excel.py \
      --input esg_jd.json \
      --output ESG岗位收集.xlsx \
      --new-only
"""

import argparse
import csv
import io
import json
import re
import sys
import os

try:
    import openpyxl
    from openpyxl.styles import Font, Alignment, Border, Side, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError:
    print("[ERROR] 需要安装 openpyxl: pip3 install openpyxl", file=sys.stderr)
    sys.exit(1)


# ★★★ 11列表头 ★★★
HEADERS_11 = [
    "序号", "类型", "行业", "公司", "公司评分",
    "职位名称", "薪资范围", "工作描述", "地点", "发布日期", "职位名称点开的链接",
]

COL_WIDTHS = {
    "序号": 6,
    "类型": 10,
    "行业": 12,
    "公司": 28,
    "公司评分": 8,
    "职位名称": 30,
    "薪资范围": 18,
    "工作描述": 60,
    "地点": 18,
    "发布日期": 14,
    "职位名称点开的链接": 40,
}


def read_csv(filepath: str) -> list:
    with open(filepath, "r", encoding="utf-8-sig") as f:
        content = f.read()
    reader = csv.DictReader(io.StringIO(content))
    rows = []
    for row in reader:
        if not any(v.strip() for v in row.values() if v):
            continue
        rows.append(row)
    return rows


def generate_excel(rows: list, output_path: str, from_seq: int = 0,
                   title: str = "", sheet_name: str = "岗位收集"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name

    header_font = Font(name="Microsoft YaHei", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
    header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    cell_font = Font(name="Microsoft YaHei", size=10)
    cell_alignment = Alignment(vertical="top", wrap_text=True)
    center_alignment = Alignment(horizontal="center", vertical="top", wrap_text=True)
    thin_border = Border(
        left=Side(style="thin", color="D9D9D9"),
        right=Side(style="thin", color="D9D9D9"),
        top=Side(style="thin", color="D9D9D9"),
        bottom=Side(style="thin", color="D9D9D9"),
    )
    alt_fill = PatternFill(start_color="F2F7FC", end_color="F2F7FC", fill_type="solid")

    current_row = 1
    if title:
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=11)
        title_cell = ws.cell(row=1, column=1, value=title)
        title_cell.font = Font(name="Microsoft YaHei", size=14, bold=True, color="2F5496")
        title_cell.alignment = Alignment(horizontal="center", vertical="center")
        current_row = 2

    for col_idx, header in enumerate(HEADERS_11, 1):
        cell = ws.cell(row=current_row, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_alignment
        cell.border = thin_border
    current_row += 1

    row_count = 0
    for data_row in rows:
        try:
            seq = int(data_row.get("序号", "0").strip())
        except (ValueError, AttributeError):
            seq = 0
        if from_seq > 0 and seq < from_seq:
            continue

        for col_idx, header in enumerate(HEADERS_11, 1):
            field_map = {
                "序号": "序号",
                "类型": "类型",
                "行业": "行业",
                "公司": "公司",
                "公司评分": "公司评分",
                "职位名称": "职位名称",
                "薪资范围": "薪资范围",
                "工作描述": "工作描述",
                "地点": "地点",
                "发布日期": "发布日期",
                "职位名称点开的链接": "职位名称点开的链接",
            }
            field = field_map.get(header, header)
            value = data_row.get(field, "")
            cell = ws.cell(row=current_row, column=col_idx, value=value)
            cell.font = cell_font
            cell.border = thin_border
            if header in ("序号", "类型", "公司评分", "薪资范围"):
                cell.alignment = center_alignment
            else:
                cell.alignment = cell_alignment
            if row_count % 2 == 1:
                cell.fill = alt_fill
        current_row += 1
        row_count += 1

    for col_idx, header in enumerate(HEADERS_11, 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = COL_WIDTHS.get(header, 15)

    freeze_row = 2 if title else 1
    ws.freeze_panes = f"A{freeze_row + 1}"
    ws.row_dimensions[freeze_row].height = 22
    wb.save(output_path)
    return row_count


def format_jd_text(job: dict) -> str:
    parts = []
    jd = job.get("jd", "")
    if isinstance(jd, list):
        jd = "\n".join(jd)
    if jd and jd.strip():
        parts.append(jd.strip())

    bullets = job.get("bullet_points", [])
    if isinstance(bullets, str):
        bullets = [b.strip() for b in bullets.split("\n") if b.strip()]
    if bullets and len(bullets) > 0:
        if parts:
            parts.append("\n" + "=" * 40 + "\n")
        parts.append("【岗位亮点】")
        for bullet in bullets:
            if isinstance(bullet, str) and bullet.strip():
                parts.append(f"• {bullet.strip()}")

    if not parts:
        teaser = job.get("teaser", "")
        if teaser and teaser.strip():
            parts.append(teaser.strip())
    if not parts:
        return ""
    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description="生成格式化 Excel")
    parser.add_argument("--input", "-i", required=True, help="输入 CSV 或 JSON 文件")
    parser.add_argument("--output", "-o", required=True, help="输出 Excel 文件 (.xlsx)")
    parser.add_argument("--from-seq", type=int, default=0, help="仅导出此序号及之后的行（仅 CSV 模式）")
    parser.add_argument("--title", help="Excel 标题")
    parser.add_argument("--sheet-name", default="岗位收集", help="工作表名称")
    parser.add_argument("--new-only", action="store_true", help="从 JSON 直接生成 Excel（新岗位模式）")

    args = parser.parse_args()

    if args.new_only:
        with open(args.input, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            jobs = data
        elif isinstance(data, dict) and "jobs" in data:
            jobs = data["jobs"]
        elif isinstance(data, dict):
            jobs = [data]
        else:
            jobs = []
            print("[WARN] 无法识别的 JSON 格式", file=sys.stderr)

        if not jobs:
            print("[ERROR] 没有可导出的数据", file=sys.stderr)
            sys.exit(1)

        print(f"[INFO] 读取 {len(jobs)} 个岗位", file=sys.stderr)

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = args.sheet_name

        header_font = Font(name="Microsoft YaHei", size=11, bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
        header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell_font = Font(name="Microsoft YaHei", size=10)
        cell_alignment = Alignment(vertical="top", wrap_text=True)
        thin_border = Border(
            left=Side(style="thin", color="D9D9D9"),
            right=Side(style="thin", color="D9D9D9"),
            top=Side(style="thin", color="D9D9D9"),
            bottom=Side(style="thin", color="D9D9D9"),
        )

        for col_idx, h in enumerate(HEADERS_11, 1):
            cell = ws.cell(row=1, column=col_idx, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_alignment
            cell.border = thin_border

        for i, job in enumerate(jobs):
            print(f"[DEBUG] 第 {i+1} 条: title={job.get('title', '空')}, jd长度={len(job.get('jd', ''))}", file=sys.stderr)
            row_num = i + 2

            industry = job.get("industry_cn", "")
            if not industry:
                industry = job.get("industry", "")
            if not industry:
                industry = job.get("subclass", "")

            jd_text = format_jd_text(job)

            # ★★★ 提取发布日期 ★★★
            listing_date = job.get("listing_date", "")
            if listing_date:
                if "T" in listing_date:
                    listing_date = listing_date.split("T")[0]
                elif " " in listing_date:
                    listing_date = listing_date.split(" ")[0]

            values = [
                str(i + 1),
                job.get("company_type", "") or "待确认",
                industry or "其他",
                job.get("company", ""),
                job.get("rating", "") or "无评分",
                job.get("title", ""),
                job.get("salary", "面议"),
                jd_text,
                job.get("location", ""),
                listing_date,  # ★★★ 发布日期列 ★★★
                job.get("link", ""),
            ]

            for col_idx, val in enumerate(values, 1):
                cell = ws.cell(row=row_num, column=col_idx, value=val)
                cell.font = cell_font
                cell.alignment = cell_alignment
                cell.border = thin_border

        for col_idx, header in enumerate(HEADERS_11, 1):
            ws.column_dimensions[get_column_letter(col_idx)].width = COL_WIDTHS.get(header, 15)

        ws.freeze_panes = "A2"
        wb.save(args.output)
        print(f"[DONE] {len(jobs)} 个岗位 → {args.output}", file=sys.stderr)
        return

    # ── CSV 模式 ──────────────────────────────────────────────
    rows = read_csv(args.input)
    print(f"[INFO] 从 CSV 读取 {len(rows)} 行", file=sys.stderr)

    output_path = args.output
    if not output_path.endswith(".xlsx"):
        output_path += ".xlsx"

    count = generate_excel(
        rows, output_path,
        from_seq=args.from_seq,
        title=args.title or "",
        sheet_name=args.sheet_name,
    )

    print(f"[DONE] {count} 行 → {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()