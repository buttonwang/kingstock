这个问题我比较了解，整理一下给你：

---

## 🏆 首推：**AKShare**

**网址：** [akshare.akfamily.xyz](https://akshare.akfamily.xyz)  
**协议：** 完全开源免费（MIT）

**优点：**
- 覆盖最全 — A 股实时行情、历史数据、财务数据、龙虎榜、北向资金等
- 底层对接东方财富、新浪财经、上交所/深交所官网等多个源
- 纯 Python，pip 一键装，更新活跃
- **支持通达信数据格式** — 可以读写 `.day`、`.min`、`.lc1` 等通达信数据文件

```python
import akshare as ak

# 获取A股历史行情（类似通达信日线）
df = ak.stock_zh_a_hist(symbol="000001", period="daily", 
                         start_date="20250101", end_date="20250601")
print(df)
```

---

## 📊 其他选项对比

| 库 | 免费程度 | 通达信兼容 | 特点 |
|---|---|---|---|
| **AKShare** ⭐ | 完全免费 ✅ | ✅ 直接支持 | 数据源最多，更新最快 |
| **BaoStock** | 完全免费 ✅ | ❌ 不直接支持 | 数据质量高，适合量化回测 |
| **TuShare** | 部分免费 ⚠️ | ❌ 不直接支持 | 需注册，老牌但限制越来越多 |
| **efinance** | 完全免费 ✅ | ❌ 不直接支持 | 轻量，基于东方财富 |
| **Qlib (微软)** | 开源免费 ✅ | ❌ 不直接支持 | 量化框架，偏机器学习 |

---

## 🔧 通达信数据兼容的具体用法

AKShare 里有专门的通达信数据处理模块：

```python
# 读取通达信日线文件
df = ak.tdx_daily_file_to_df(r"C:\new_tdx\vipdoc\sh\lday\sh000001.day")

# 读取通达信分钟线文件
df = ak.tdx_minute_file_to_df(r"C:\new_tdx\vipdoc\sh\minline\sh000001.zcf")

# 获取通达信行情（远程）
df = ak.stock_zh_a_spot_em()  # 东方财富源，兼容通达信代码
```

---

**总结：** 如果你的需求是 **免费 + 通达信兼容 + A 股全数据**，**AKShare** 是目前最合适的选择，没有之一。