"""补拉历史板块日线数据（2025-06-01 ~ 2025-11-13）

将板块日线数据向前扩展到至少1年，以便进行1年期回测。
"""
import sys
import os
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from src.data_fetcher import DataFetcher
from src.utils import setup_logging

logger = setup_logging("backfill_sector")

def main():
    fetcher = DataFetcher()

    # 获取所有板块
    sectors = fetcher._sql_to_df(
        "SELECT DISTINCT sector_name, sector_type FROM sector_daily"
    )
    total = len(sectors)
    logger.info("共 %d 个板块，开始补拉历史数据...", total)

    # 补拉时间段：2025-06-01 ~ 2025-11-13
    fill_start = "20250601"
    fill_end = "20251113"

    success = 0
    for i, (_, row) in enumerate(sectors.iterrows()):
        name = row["sector_name"]
        stype = row["sector_type"]

        if i % 10 == 0:
            logger.info("进度 %d/%d (%.0f%%)", i, total, 100 * i / total)

        try:
            df = fetcher.get_sector_daily(name, stype, fill_start, fill_end)
            if not df.empty:
                success += 1
            time.sleep(0.3)  # 避免被限频
        except Exception as e:
            logger.warning("板块 '%s' 补拉失败: %s", name, e)
            time.sleep(1)

    fetcher.close()
    logger.info("补拉完成: %d/%d 板块成功获取历史数据", success, total)

    # 验证
    fetcher2 = DataFetcher()
    stats = fetcher2._sql_to_df(
        "SELECT MIN(date) as min_d, MAX(date) as max_d, COUNT(*) as n FROM sector_daily"
    )
    print("sector_daily 范围:", stats.to_string())
    fetcher2.close()


if __name__ == "__main__":
    main()
