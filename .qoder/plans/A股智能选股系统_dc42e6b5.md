# A股智能选股交易系统建设方案

## 系统概览

本地Python脚本系统，每日收盘后运行，从AKShare获取A股行情数据，按照5大选股规则逐步筛选，最终输出符合条件的股票列表。

---

## 技术栈

- **语言**: Python 3.10+
- **数据源**: AKShare（免费、稳定、覆盖A股行情/板块/财务数据）
- **计算库**: pandas, numpy（指标计算）
- **存储**: 本地SQLite（缓存历史数据，避免重复拉取）
- **输出**: 终端打印 + CSV/Excel文件导出
- **调度**: 手动运行或Windows任务计划程序定时触发

---

## 项目目录结构

```
d:\wwcode\stock\
├── config/
│   └── settings.py          # 配置文件（参数、路径、阈值）
├── data/
│   ├── db/                  # SQLite数据库文件
│   └── output/              # 选股结果输出
├── src/
│   ├── __init__.py
│   ├── data_fetcher.py      # 数据获取模块（AKShare接口封装）
│   ├── indicators/
│   │   ├── __init__.py
│   │   ├── macd.py          # MACD指标计算
│   │   ├── kdj.py           # KDJ指标计算
│   │   ├── zjtj.py          # 庄家控盘ZJTJ指标计算
│   │   └── rps.py           # 板块RPS强度排名计算
│   ├── filters/
│   │   ├── __init__.py
│   │   ├── sector_filter.py # 规则1：热门板块RPS筛选
│   │   ├── macd_filter.py   # 规则2：MACD买入信号筛选
│   │   ├── zjtj_filter.py   # 规则3：庄家控盘筛选
│   │   ├── kdj_filter.py    # 规则4：KDJ买入信号筛选
│   │   └── finance_filter.py# 规则5：财务基本面筛选
│   ├── stock_selector.py    # 主选股引擎（串联所有filter）
│   └── utils.py             # 工具函数
├── 手工选股/                  # 手工选股截图和记录（已有）
├── main.py                  # 入口脚本
├── requirements.txt         # 依赖列表
└── 需求.md                   # 需求文档（已有）
```

---

## 核心模块设计

### Task 1: 数据获取模块 (`data_fetcher.py`)

负责从AKShare获取所有必要数据并缓存到SQLite：

- **个股日线行情**: 全A股每日OHLCV数据（计算技术指标所需）
- **板块数据**: 概念板块/行业板块列表及成分股
- **板块行情**: 板块每日涨跌幅（计算板块RPS）
- **财务数据**: 近三年净利润数据
- **筹码分布数据**: WINNER/COST函数所需的筹码分布（注：AKShare可能无法直接获取，需备选方案）

关键设计：
- 增量更新：只拉取最新交易日数据，避免每次全量拉取
- 数据缓存：SQLite存储历史数据，加速后续计算
- 异常处理：网络超时重试、数据缺失标记

### Task 2: 板块RPS强度排名 (`rps.py` + `sector_filter.py`)

**RPS计算逻辑**：
- 获取所有概念/行业板块近20日涨幅
- 对板块按20日涨幅排名，计算相对强度百分比
- 筛选出排名前20的强势板块
- 输出这些板块中包含的个股集合

### Task 3: MACD指标模块 (`macd.py` + `macd_filter.py`)

严格按照通达信公式实现：
```
DIF = EMA(CLOSE, 12) - EMA(CLOSE, 26)
DEA = EMA(DIF, 9)
MACD = (DIF - DEA) * 2
```

**买入信号判定**：
- DIFF > 0 且 DEA > 0（均为正值）
- DIFF 向上突破 DEA（今日DIFF > DEA，昨日DIFF <= DEA）
- 或：MACD柱由绿变红（前一日MACD < 0，今日MACD >= 0）

### Task 4: 庄家控盘ZJTJ指标模块 (`zjtj.py` + `zjtj_filter.py`)

严格按照通达信公式实现：
```
VAR1 = EMA(EMA(CLOSE, 9), 9)
控盘 = (VAR1 - REF(VAR1, 1)) / REF(VAR1, 1) * 1000
```

**筛选条件**（有庄控盘或高度控盘）：
- 有庄控盘：控盘 > REF(控盘, 1) 且 控盘 > 0
- 高度控盘：VAR2 > 50 且 COST(85) < CLOSE 且 控盘 > 0
  - 其中 VAR2 = 100 * WINNER(CLOSE * 0.95)

**注意**：WINNER和COST函数依赖筹码分布数据，AKShare可能无直接接口。备选方案：
- 方案A：用换手率近似估算筹码分布
- 方案B：仅使用"有庄控盘"条件（不依赖筹码数据），跳过"高度控盘"
- 方案C：从通达信导出筹码数据

### Task 5: KDJ指标模块 (`kdj.py` + `kdj_filter.py`)

严格按照通达信公式实现（N=9, M1=3, M2=3）：
```
RSV = (CLOSE - LLV(LOW, 9)) / (HHV(HIGH, 9) - LLV(LOW, 9)) * 100
K = SMA(RSV, 3, 1)
D = SMA(K, 3, 1)
J = 3*K - 2*D
```

**买入信号判定**：
- K在20左右向上交叉D（K < 30 且 今日K > D 且 昨日K <= D）
- 或 J < 0 后反转上涨（J从负值转正）

### Task 6: 财务基本面筛选 (`finance_filter.py`)

- 获取近三年（2023/2024/2025）年度净利润数据
- 筛选条件：
  - 每年净利润均为正
  - 每年净利润同比增长
  - 近三年复合增长率超过20%

### Task 7: 主选股引擎 (`stock_selector.py`)

串联所有筛选器的漏斗式流程：

```
全A股股票池
  → 规则1筛选：板块RPS前20的股票
  → 规则2筛选：MACD买入信号
  → 规则3筛选：ZJTJ有庄控盘/高度控盘
  → 规则4筛选：KDJ买入信号
  → 规则5筛选：近三年净利润增长>20%
  → 输出最终选股结果
```

每一步筛选后记录剩余数量，方便调试和回溯。

### Task 8: 入口脚本与结果输出 (`main.py`)

- 解析命令行参数（指定日期、是否强制更新数据等）
- 调用选股引擎执行完整流程
- 输出结果：
  - 终端彩色打印选股结果（股票代码、名称、所属板块、各指标值）
  - 导出CSV/Excel到 `data/output/` 目录
  - 日志记录每次运行的筛选过程

---

## 筹码数据问题的处理策略

ZJTJ指标中的WINNER和COST函数依赖精确的筹码分布数据，这在免费数据源中较难获取。建议分阶段处理：

1. **第一阶段（先实现）**: ZJTJ只用"有庄控盘"条件（控盘 > REF(控盘,1) 且 控盘 > 0），不依赖筹码数据
2. **第二阶段（后续增强）**: 研究用换手率数据近似计算筹码分布，实现WINNER/COST函数，启用"高度控盘"条件

---

## 配置参数 (`settings.py`)

```python
# 板块RPS参数
RPS_PERIOD = 20        # RPS计算周期（天）
RPS_TOP_N = 20         # 取前N个板块

# MACD参数
MACD_SHORT = 12
MACD_LONG = 26
MACD_MID = 9

# KDJ参数
KDJ_N = 9
KDJ_M1 = 3
KDJ_M2 = 3

# 财务参数
PROFIT_GROWTH_YEARS = 3      # 考察年数
PROFIT_GROWTH_MIN = 0.20     # 最低增长率

# 数据路径
DB_PATH = "data/db/stock.db"
OUTPUT_PATH = "data/output/"
```

---

## 建设步骤（执行顺序）

1. 初始化项目结构和依赖
2. 实现数据获取模块（AKShare封装 + SQLite缓存）
3. 实现各技术指标计算模块（MACD、KDJ、ZJTJ、RPS）
4. 实现各筛选器模块
5. 实现主选股引擎（串联筛选器）
6. 实现入口脚本和结果输出
7. 端到端测试与调参

---

## 后续扩展方向（本期不实现）

- 手工选股学习：将 `手工选股/` 中的截图和记录作为训练数据，辅助优化选股参数
- 回测系统：历史数据回测选股策略的收益率
- 消息推送：微信/钉钉通知当日选股结果
- Web界面：Streamlit可视化展示
