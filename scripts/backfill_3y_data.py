"""补拉3年历史数据（板块+股票）

将数据向前扩展到3年，以便进行3年期回测。
回测期: start_date=20230601, lookback ≈ 20220901
所以需要补拉:
  - sector_daily: 2022-09-01 ~ 2025-06-01 (现有从2025-06-03开始)
  - stock_daily:  2022-09-01 ~ 2024-08-25 (现有从2024-08-26开始)
"""
import sys
import os
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from src.data_fetcher import DataFetcher
from src.utils import setup_logging

logger = setup_logging("backfill_3y")

def backfill_sectors(fetcher, fill_start, fill_end):
    """补拉板块日线数据"""
    sectors = fetcher._sql_to_df(
        "SELECT DISTINCT sector_name, sector_type FROM sector_daily"
    )
    total = len(sectors)
    logger.info("【板块】共 %d 个，补拉 %s ~ %s ...", total, fill_start, fill_end)

    success = 0
    for i, (_, row) in enumerate(sectors.iterrows()):
        name = row["sector_name"]
        stype = row["sector_type"]

        if i % 10 == 0:
            logger.info("【板块】进度 %d/%d (%.0f%%)", i, total, 100 * i / total)

        try:
            df = fetcher.get_sector_daily(name, stype, fill_start, fill_end)
            if not df.empty:
                success += 1
            time.sleep(0.3)
        except Exception as e:
            logger.warning("【板块】'%s' 失败: %s", name, e)
            time.sleep(1)

    logger.info("【板块】完成: %d/%d 成功", success, total)


def backfill_stocks(fetcher, fill_start, fill_end):
    """补拉股票日线数据"""
    cons_df = fetcher._sql_to_df("SELECT DISTINCT code FROM sector_constituents")
    all_codes = sorted(cons_df["code"].tolist())
    total = len(all_codes)
    logger.info("【股票】共 %d 只，补拉 %s ~ %s ...", total, fill_start, fill_end)

    success = 0
    for i, code in enumerate(all_codes):
        if i % 20 == 0:
            logger.info("【股票】进度 %d/%d (%.0f%%)", i, total, 100 * i / total)

        try:
            df = fetcher.get_stock_daily(code, fill_start, fill_end)
            if not df.empty:
                success += 1
            time.sleep(0.2)  # 股票API较快，用短间隔
        except Exception as e:
            logger.warning("【股票】'%s' 失败: %s", code, e)
            time.sleep(1)

    logger.info("【股票】完成: %d/%d 成功", success, total)


def main():
    fetcher = DataFetcher()

    # === 阶段1: 补拉板块数据 ===
    # 现有 sector_daily 从 2025-06-03 开始
    # 补拉 2022-09-01 ~ 2025-06-01（lookback需要250天回看）
    logger.info("=" * 50)
    logger.info("阶段1/2: 补拉板块日线数据")
    logger.info("=" * 50)
    backfill_sectors(fetcher, "20220901", "20250601")
    fetcher.close()

    # === 阶段2: 补拉股票数据 ===
    logger.info("=" * 50)
    logger.info("阶段2/2: 补拉股票日线数据")
    logger.info("=" * 50)
    fetcher2 = DataFetcher()
    # 现有 stock_daily 从 2024-08-26 开始
    # 补拉 2022-09-01 ~ 2024-08-25
    backfill_stocks(fetcher2, "20220901", "20240825")
    fetcher2.close()

    # === 验证 ===
    logger.info("=" * 50)
    logger.info("验证数据范围")
    logger.info("=" * 50)
    fetcher3 = DataFetcher()
    sd = fetcher3._sql_to_df(
        "SELECT MIN(date) as min_d, MAX(date) as max_d, COUNT(*) as n FROM sector_daily"
    )
    st = fetcher3._sql_to_df(
        "SELECT MIN(date) as min_d, MAX(date) as max_d, COUNT(*) as n FROM stock_daily"
    )
    print("\n【sector_daily】", sd.to_string())
    print("【stock_daily】", st.to_string())
    fetcher3.close()


if __name__ == "__main__":
    main()
