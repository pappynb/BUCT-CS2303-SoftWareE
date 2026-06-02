# -*- coding: utf-8 -*-
"""
命令行入口：解析参数、初始化日志与可选 MySQL、调度各馆爬虫。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date, datetime
from pathlib import Path

from museum_crawler.config import BASE_DIR, LOG_PATH, setup_logging
from museum_crawler.db import MySQLWriter, mysql_configured
from museum_crawler.harvard import crawl_harvard, repair_harvard_multi_images
from museum_crawler.kg_export import export_knowledge_graph
from museum_crawler.mfa_boston import crawl_mfa, repair_mfa_images, repair_mfa_metadata
from museum_crawler.quality import quality_check
from museum_crawler.smithsonian import crawl_smithsonian

log = logging.getLogger("spider")


def _print_summary(stats: dict[str, tuple[int, int]]) -> None:
    print("\n" + "=" * 50)
    print("  爬取汇总（课程设计数量参考：≥5000 条）")
    print("=" * 50)
    total_r = total_i = 0
    for name, (nr, ni) in stats.items():
        print(f"  {name:30s} {nr:6d} 条  {ni:6d} 张图")
        total_r += nr
        total_i += ni
    print("-" * 50)
    print(f"  {'合计':30s} {total_r:6d} 条  {total_i:6d} 张图")
    target = 5000
    if total_r < target:
        shortage = target - total_r
        print(f"\n  [!] 距 {target} 条还差 {shortage} 条，可去掉 --limit 全量爬取。")
    print("=" * 50 + "\n")


def _append_run_ledger(
    out_dir: Path,
    *,
    args: argparse.Namespace,
    stats: dict[str, tuple[int, int]],
    kg_stats: dict[str, int] | None,
) -> None:
    """
    追加运行台账，满足“增量更新可追溯”要求。

    说明：
    - `crawl_runs.jsonl` 按行追加，每行一条运行记录，便于后续统计与审计。
    - 记录参数、各馆结果、KG 导出统计，避免仅保留最后一次摘要。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "params": {
            "museums": args.museums,
            "limit": args.limit,
            "delay": args.delay,
            "img_delay": args.img_delay,
            "page_size": args.page_size,
            "si_rows": args.si_rows,
            "ham_allow_no_image": args.ham_allow_no_image,
            "mysql_enabled": not args.no_mysql,
            "kg_export": not args.no_kg_export,
        },
        "museums": {
            k: {"records": v[0], "images_downloaded": v[1]}
            for k, v in stats.items()
        },
        "kg": kg_stats or {},
    }
    ledger = out_dir / "crawl_runs.jsonl"
    with open(ledger, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def main() -> None:
    setup_logging()  # 须最先执行，后续各模块 logger 才能输出到文件与控制台
    ap = argparse.ArgumentParser(
        description="海外博物馆中国文物爬虫（模块化 v5）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python museum_spider.py --museums all --limit 200
  python museum_spider.py --museums harvard --limit 0
  # 配置 .env 中 MYSQL_* 后自动写库；仅 CSV 不写库：
  python museum_spider.py --no-mysql --museums harvard --limit 50
  # 作者 Wikidata / 维基增量（默认处理 output 下三馆 CSV）：
  python enrich_wikidata.py --csv output/harvard_art_museums.csv
  python museum_spider.py enrich-wikidata --delay 1.5
        """,
    )
    ap.add_argument(
        "--output", type=Path, default=BASE_DIR / "output",
        help="输出根目录（CSV 与 images/）",
    )
    ap.add_argument(
        "--museums", type=str, default="all",
        help="smithsonian, harvard, mfa 或 all（逗号分隔）",
    )
    ap.add_argument(
        "--limit", type=int, default=100,
        help="每馆最多条数（0=不限）",
    )
    ap.add_argument(
        "--delay", type=float, default=1.5,
        help="API / 页面请求基础间隔（秒）",
    )
    ap.add_argument(
        "--img-delay", type=float, default=3.5,
        help="史密森尼图片下载间隔（秒）",
    )
    ap.add_argument("--page-size", type=int, default=100, help="哈佛每页条数 ≤100")
    ap.add_argument("--si-rows", type=int, default=100, help="史密森尼每页 rows")
    ap.add_argument(
        "--si-s3-only",
        action="store_true",
        help="史密森尼：只扫 S3 开放元数据（推荐；API 对 chinese 几乎全无图）",
    )
    ap.add_argument(
        "--si-units",
        type=str,
        default="",
        help="史密森尼 S3 馆别，逗号分隔，如 fsg,chndm,nmah（默认全部艺术相关馆）",
    )
    ap.add_argument(
        "--si-api-max-pages",
        type=int,
        default=40,
        help="史密森尼 API 每档搜索最多翻页数（仅非 --si-s3-only 时）",
    )
    ap.add_argument(
        "--ham-allow-no-image",
        action="store_true",
        help="哈佛：图片全失败也写入（默认：至少成功 1 张图才入库，否则跳过）",
    )
    ap.add_argument(
        "--ham-relaxed-multi",
        action="store_true",
        help="哈佛：允许多图藏品只下到部分图也入库（默认严格：API 有几个图位就要下齐）",
    )
    ap.add_argument(
        "--ham-repair-multi-images",
        action="store_true",
        help="哈佛：按现有 CSV 只重爬/补全多图链接与图片文件（其余列不动）",
    )
    ap.add_argument(
        "--ham-repair-multi-force",
        action="store_true",
        help="与 --ham-repair-multi-images 合用：强制每一行都重拉多图（默认只补缺失）",
    )
    ap.add_argument(
        "--mfa-max-pages",
        type=int,
        default=0,
        help="MFA 每个列表源最多翻页数，0=不设上限直至无新链接（CHINESE 约 762 页；旧默认 40 页≈480 条）",
    )
    ap.add_argument(
        "--mfa-repair-images",
        action="store_true",
        help="MFA：仅根据已有 CSV 补下载缺失的本地图片（不重新收集链接）",
    )
    ap.add_argument(
        "--mfa-repair-metadata",
        action="store_true",
        help="MFA：按 detail_url 重新解析 title/material/accession 等（不重下图）",
    )
    ap.add_argument(
        "--mfa-no-browser",
        action="store_true",
        help="MFA：不用 Playwright，仅用 requests（易被 AWS WAF 拦截，不推荐）",
    )
    ap.add_argument(
        "--mfa-headless",
        action="store_true",
        help="MFA：无头浏览器（易被 AWS WAF 拦截；默认有界面）",
    )
    ap.add_argument(
        "--mfa-show-browser",
        action="store_true",
        help="（已废弃，默认即有界面）保留兼容",
    )
    ap.add_argument(
        "--no-mysql",
        action="store_true",
        help="即使已配置 MYSQL_* 也不写入数据库",
    )
    ap.add_argument(
        "--ensure-mysql-table",
        action="store_true",
        help="启动时若可连接 MySQL 则执行 CREATE TABLE IF NOT EXISTS",
    )
    ap.add_argument(
        "--no-kg-export",
        action="store_true",
        help="只爬取与入库，不导出三元组与实体 CSV",
    )
    args = ap.parse_args()

    out_dir: Path = args.output
    img_root = out_dir / "images"
    img_root.mkdir(parents=True, exist_ok=True)

    selected = [m.strip().lower() for m in args.museums.split(",")]
    if "all" in selected:
        selected = ["smithsonian", "harvard", "mfa"]

    db_writer: MySQLWriter | None = None
    # 同时配置 HOST+DATABASE 即视为启用；失败则降级为仅 CSV
    if not args.no_mysql and mysql_configured():
        try:
            db_writer = MySQLWriter.from_env()
            log.info(
                "MySQL 已启用: %s / %s",
                os.environ.get("MYSQL_HOST"),
                os.environ.get("MYSQL_DATABASE"),
            )
            try:
                # 旧表无 museum_id 时补列并升级主键，避免 1054 Unknown column
                db_writer.ensure_museum_id_schema()
                db_writer.ensure_legacy_author_province_renamed()
                db_writer.ensure_missing_csv_columns()
                db_writer.ensure_loosen_overflow_prone_columns()
            except Exception as exc:
                log.warning("MySQL 表结构升级失败（可改表后重试）: %s", exc)
            if args.ensure_mysql_table:
                db_writer.ensure_table()  # 首次部署可打开；已有表则跳过
        except Exception as exc:
            log.warning("MySQL 不可用，仅写 CSV: %s", exc)
            db_writer = None
    elif not args.no_mysql:
        log.info("未配置 MYSQL_HOST+MYSQL_DATABASE，跳过数据库写入")

    si_key = os.environ.get("SI_DATA_GOV_API_KEY", "").strip()
    hv_key = os.environ.get("HARVARD_ART_MUSEUMS_API_KEY", "").strip()
    stats: dict[str, tuple[int, int]] = {}

    if "smithsonian" in selected:
        print("\n" + "─" * 50)
        print("  ① Smithsonian Institution")
        print("─" * 50)
        if not si_key:
            log.warning("跳过：未设置 SI_DATA_GOV_API_KEY")
        else:
            si_units = None
            if args.si_units.strip():
                si_units = tuple(
                    u.strip().lower()
                    for u in args.si_units.split(",")
                    if u.strip()
                )
            stats["Smithsonian"] = crawl_smithsonian(
                si_key,
                out_dir / "smithsonian_institution.csv",
                img_root,
                args.limit,
                args.si_rows,
                api_delay=args.delay,
                img_delay=args.img_delay,
                db_writer=db_writer,
                s3_only=args.si_s3_only,
                s3_units=si_units,
                api_max_pages=args.si_api_max_pages,
            )
            quality_check(out_dir / "smithsonian_institution.csv")

    if "harvard" in selected:
        print("\n" + "─" * 50)
        print("  ② Harvard Art Museums")
        print("─" * 50)
        if not hv_key:
            log.warning("跳过：未设置 HARVARD_ART_MUSEUMS_API_KEY")
        elif args.ham_repair_multi_images:
            stats["Harvard"] = repair_harvard_multi_images(
                hv_key,
                out_dir / "harvard_art_museums.csv",
                img_root,
                args.delay,
                limit=args.limit,
                strict_multi=not args.ham_relaxed_multi,
                force_all=args.ham_repair_multi_force,
            )
            quality_check(out_dir / "harvard_art_museums.csv")
        else:
            stats["Harvard"] = crawl_harvard(
                hv_key,
                out_dir / "harvard_art_museums.csv",
                img_root,
                args.limit,
                min(args.page_size, 100),
                args.delay,
                db_writer=db_writer,
                allow_no_image=args.ham_allow_no_image,
                strict_multi=not args.ham_relaxed_multi,
            )
            quality_check(out_dir / "harvard_art_museums.csv")

    if "mfa" in selected:
        print("\n" + "─" * 50)
        print("  ③ Museum of Fine Arts, Boston")
        print("─" * 50)
        if args.mfa_repair_images:
            stats["MFA_Boston"] = repair_mfa_images(
                out_dir / "museum_of_fine_arts_boston.csv",
                img_root,
                max(args.delay, 0.3),
                browser_headless=args.mfa_headless and not args.mfa_show_browser,
                limit=args.limit,
            )
            quality_check(out_dir / "museum_of_fine_arts_boston.csv")
        elif args.mfa_repair_metadata:
            stats["MFA_Boston"] = repair_mfa_metadata(
                out_dir / "museum_of_fine_arts_boston.csv",
                max(args.delay, 0.3),
                browser_headless=args.mfa_headless and not args.mfa_show_browser,
                limit=args.limit,
                db_writer=db_writer,
            )
            quality_check(out_dir / "museum_of_fine_arts_boston.csv")
        else:
            stats["MFA_Boston"] = crawl_mfa(
                out_dir / "museum_of_fine_arts_boston.csv",
                img_root,
                args.limit,
                max(args.delay, 0.3),
                db_writer=db_writer,
                use_browser=not args.mfa_no_browser,
                browser_headless=args.mfa_headless and not args.mfa_show_browser,
                max_pages_per_list=args.mfa_max_pages,
            )
            quality_check(out_dir / "museum_of_fine_arts_boston.csv")

    kg_stats: dict[str, int] | None = None
    if not args.no_kg_export:
        csv_candidates = [
            out_dir / "smithsonian_institution.csv",
            out_dir / "harvard_art_museums.fixed.csv",
            out_dir / "harvard_art_museums.csv",
            out_dir / "museum_of_fine_arts_boston.csv",
        ]
        # 哈佛 fixed 与原版只保留其一
        seen_csv: set[str] = set()
        kg_inputs: list[Path] = []
        for p in csv_candidates:
            key = p.name.replace(".fixed", "")
            if key in seen_csv:
                continue
            if p.exists() and p.stat().st_size > 0:
                seen_csv.add(key)
                kg_inputs.append(p)
        try:
            kg_stats = export_knowledge_graph(kg_inputs, out_dir)
        except Exception as exc:
            log.error("[KG] 导出失败（不影响爬取主流程）: %s", exc)
    else:
        log.info("[KG] 已按参数关闭知识图谱导出")

    _print_summary(stats)

    summary = {
        "crawl_date": date.today().isoformat(),
        "museums": {
            k: {"records": v[0], "images_downloaded": v[1]}
            for k, v in stats.items()
        },
    }
    with open(out_dir / "crawl_summary.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)

    _append_run_ledger(out_dir, args=args, stats=stats, kg_stats=kg_stats)

    log.info("全部完成。输出目录: %s", out_dir)
    log.info("日志文件: %s", LOG_PATH)


if __name__ == "__main__":
    main()
