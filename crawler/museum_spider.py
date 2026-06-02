#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
海外藏中国文物爬虫 — 入口脚本（薄封装）。

实现代码位于 ``museum_crawler/`` 包内，按博物馆与职责拆分模块。
运行方式不变：在本目录执行 ``python museum_spider.py --help``。

作者信息增量（Wikidata + 可选维基摘要）::

    python enrich_wikidata.py --help
    # 或：python museum_spider.py enrich-wikidata --help

CSV 与 MySQL 同步::

    python csv_mysql_sync.py import --csv output/harvard_art_museums.csv
    python museum_spider.py csv-sync export --csv output/from_db.csv --museum-id 2
"""

import sys

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("enrich-wikidata", "enrich_wikidata"):
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        from museum_crawler.wikidata_enrich import main as enrich_main

        enrich_main()
    elif len(sys.argv) > 1 and sys.argv[1] in ("csv-sync", "csv_sync"):
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        from museum_crawler.csv_db_sync import main as sync_main

        sync_main()
    else:
        from museum_crawler.cli import main

        main()
