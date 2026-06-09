# 66大顺 V2.2 — QMT 实盘接入指南

> 本文档指导你从零开始接入 miniQMT 实盘交易，预计耗时 1-2 周。
>
> 前提：已有一个稳定运行的纸上交易系统（当前已完成）。

---

## 一、整体架构

```
你的 Python 环境（本项目）
├── 数据层：AKShare + SQLite（不变）
├── 信号层：V2.2 双轨道信号生成（不变）
├── 执行层：src/qmt_trader.py（新增，替代 paper_trader）
│       ↓ 调用
│   xtquant SDK（Python 库）
│       ↓ 连接
│   miniQMT 客户端（后台运行）
│       ↓ 通信
│   券商交易服务器
```

**核心原则**：数据层和信号层完全不动，只替换执行层。

---

## 二、选择券商 & 开通 QMT

### 2.1 推荐券商（QMT 友好型）

| 券商 | QMT 门槛 | 佣金 | 优势 |
|------|---------|------|------|
| 国金证券 | 无资金门槛 | 万1-万1.5 | QMT生态最成熟，社区活跃 |
| 国盛证券 | 无资金门槛 | 万1 | 低佣金，QMT稳定 |
| 华鑫证券 | 50万 | 万1.5 | 老牌量化券商 |
| 中泰证券 | 20万 | 万1.2 | XTP接口备选 |

> **建议**：国金证券，QMT 社区最活跃，问题容易找到解决方案。

### 2.2 开通步骤

1. **联系客户经理**
   - 微信搜索"QMT开通"或知乎找渠道（比官网开户佣金更低）
   - 告诉对方：需要开通 QMT 实盘权限

2. **签署协议**
   - 《量化交易风险揭示书》
   - 《程序化交易协议》

3. **获取账号**
   - 资金账号（如 88888888）
   - 交易密码
   - QMT 客户端下载链接

4. **下载 QMT 客户端**
   - 安装后首次登录需要激活
   - 安装路径建议：`D:\国金QMT`（避免中文路径）

### 2.3 验证 QMT 可用

1. 启动 QMT 客户端，用资金账号登录
2. 确认能看到行情数据和账户信息
3. 手动下一笔测试单（买 100 股低价股，确认能成交）
4. 立即卖出（确认卖出也正常）

---

## 三、安装 xtquant SDK

### 3.1 从 QMT 安装目录获取

xtquant 库在 QMT 安装目录下：

```
D:\国金QMT\bin.x64\Lib\site-packages\xtquant\
```

### 3.2 安装到 Python 环境

**方法一：直接复制（推荐）**

```powershell
# 把 xtquant 目录复制到你的 Python site-packages
Copy-Item -Recurse "D:\国金QMT\bin.x64\Lib\site-packages\xtquant" "$env:PYTHON_HOME\Lib\site-packages\xtquant"
```

**方法二：添加路径**

```python
# 在代码开头添加
import sys
sys.path.insert(0, r"D:\国金QMT\bin.x64\Lib\site-packages")
```

### 3.3 验证安装

```python
python -c "from xtquant import xttrader, xtdata; print('xtquant 安装成功')"
```

如果报错 `ImportError`，检查路径是否正确。

---

## 四、配置本项目

### 4.1 修改配置文件

打开 `config/qmt_config.py`，修改以下 3 项：

```python
# 1. QMT 安装路径（bin.x64 目录）
QMT_PATH = r"D:\国金QMT\bin.x64"

# 2. 你的资金账号
ACCOUNT_ID = "88888888"  # 替换为你的真实账号

# 3. 先用模拟模式
DRY_RUN = True  # 重要！先不要改 False
```

### 4.2 验证配置

```powershell
python -c "from config.qmt_config import DRY_RUN, ACCOUNT_ID; print(f'DRY_RUN={DRY_RUN}, ACCOUNT={ACCOUNT_ID}')"
```

---

## 五、DRY_RUN 模拟测试（1-2 周）

### 5.1 什么是 DRY_RUN

`DRY_RUN=True` 模式下：
- 信号生成逻辑 100% 正常运行
- 风控检查 100% 正常执行
- **不调用 xtquant，不实际下单**
- 所有买入/卖出只打印日志

目的：验证信号和风控逻辑正确，不花真金白银。

### 5.2 运行方式

```powershell
# 和纸上交易一样，收盘后运行
python scripts/run_live_trade.py --date 20260609
```

### 5.3 对比检查清单

每天运行后，逐项检查：

- [ ] 信号与纸上交易一致（同样的股票、同样的轨道）
- [ ] 挂单数量正确（不超过每日上限）
- [ ] 风控未误触发（日亏损/仓位限制正常）
- [ ] 涨停股正确跳过
- [ ] 退出信号正确（Track A 到期、Track B 止损/止盈）
- [ ] 状态文件 `data/output/live_trade_state.json` 正常写入
- [ ] 日报正常打印

### 5.4 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| `xtquant 未安装` | SDK 未安装 | DRY_RUN 模式下可忽略，不影响 |
| 信号为空 | 数据未更新 | 先运行 `scripts/update_daily.py` |
| 不是交易日 | 周末/节假日 | 用 `--date` 指定交易日 |

### 5.5 对比纸上交易结果

运行纸上交易和实盘模拟，对比信号：

```powershell
# 同一天分别运行
python scripts/run_paper_trade.py --date 20260609
python scripts/run_live_trade.py --date 20260609
```

两边应该产生**完全相同的信号**（股票代码、轨道、ML评分一致）。

---

## 六、启动 miniQMT 客户端

### 6.1 什么是 miniQMT

miniQMT 是 QMT 的轻量版客户端：
- 无 GUI 界面（只有一个系统托盘图标）
- 只保留交易通道功能
- 占用资源极少
- 你的 Python 代码通过 xtquant 连接它

### 6.2 启动方法

1. 打开 QMT 完整版客户端
2. 在菜单中找到 **"miniQMT"** 或 **"极简模式"**
3. 启动后 QMT 主窗口可以关闭，miniQMT 在后台保持运行

> 具体入口因券商版本不同而异，找不到就咨询客户经理。

### 6.3 设为开机自启

```powershell
# 创建快捷方式到启动目录
# miniQMT 的 exe 通常在: D:\国金QMT\bin.x64\XtItClient.exe
# 快捷方式放到: C:\Users\你的用户名\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\
```

### 6.4 验证连接

```powershell
# 确保 miniQMT 已启动后运行
python -c "
from xtquant import xttrader
t = xttrader.XtQuantTrader(r'D:\国金QMT\bin.x64', 'test_conn')
t.start()
result = t.connect()
print('连接结果:', '成功' if result == 0 else f'失败({result})')
t.stop()
"
```

---

## 七、切换实盘模式

### 7.1 前提条件

在切换前确认：

- [ ] DRY_RUN 模式已运行 **至少 5 个交易日**
- [ ] 信号与纸上交易 **完全一致**
- [ ] miniQMT 客户端 **稳定运行**
- [ ] 已用手动测试单 **验证买卖正常**
- [ ] 账户中有 **足够资金**（建议至少 30 万）

### 7.2 切换步骤

**第一步：修改配置**

```python
# config/qmt_config.py
DRY_RUN = False  # 切换为实盘
```

**第二步：小额测试**

先用小资金（5-10 万）跑 1 周：

```powershell
python scripts/run_live_trade.py
```

关注：
- 委托是否正确提交
- 成交价格与预期的偏差（滑点）
- 佣金费率是否与约定一致

**第三步：检查成交**

```powershell
# 查看交易日志
type data\output\live_trade_log.csv

# 查看当日状态
python -c "
import json
s = json.load(open('data/output/live_trade_state.json', encoding='utf-8'))
print(f'持仓: {len(s.get(\"positions\", {}))}只')
print(f'挂单: {len(s.get(\"pending_orders\", []))}笔')
print(f'历史交易: {len(s.get(\"trade_history\", []))}笔')
"
```

**第四步：逐步加仓**

小额测试 1 周无异常后，逐步增加资金到目标规模。

---

## 八、每日执行流程

### 8.1 手动执行（推荐初期使用）

每天收盘后运行一次：

```powershell
# 1. 更新数据（15:30 之后）
python scripts/update_daily.py

# 2. 运行实盘交易（含信号生成 + 次日挂单）
python scripts/run_live_trade.py
```

次日开盘后：
- 9:25 集合竞价结束
- 9:26 系统自动提交挂单（如果有的话）
- 运行 `python scripts/run_live_trade.py --phase execute` 执行买入

### 8.2 分阶段执行（适合自动化）

```powershell
# 盘后 15:45 — 更新数据 + 生成信号
python scripts/update_daily.py
python scripts/run_live_trade.py --phase signal

# 次日盘前 09:26 — 执行挂单 + 检查退出
python scripts/run_live_trade.py --phase execute
```

### 8.3 定时任务（进阶）

用 Windows 任务计划程序自动执行（参考项目中的 `setup_task.bat`）。

---

## 九、监控与告警

### 9.1 微信推送（已集成）

每日收盘后自动推送日报到微信（通过 PushPlus）：
- 当日买卖明细
- 持仓盈亏
- 待执行挂单

### 9.2 邮件日报（已集成）

同纸上交易，发送到配置的邮箱。

### 9.3 盘中检查建议

| 时间 | 检查项 |
|------|--------|
| 09:30 | 确认开盘委托已成交/已撤单 |
| 11:30 | 检查是否有异常委托 |
| 15:00 | 确认收盘，准备运行脚本 |

---

## 十、风控说明

### 10.1 内置风控

| 风控项 | 阈值 | 动作 |
|--------|------|------|
| 日亏损熔断 | 当日亏 -3% | 暂停所有买入 |
| 单股止损 | 单股亏 -10% | 强制平仓 |
| 仓位上限 | 总仓位 80% | 拒绝新买入 |
| 现金保留 | < ¥10,000 | 拒绝新买入 |
| 涨停检测 | 开盘涨 > 9.5% | 跳过不买 |

### 10.2 调整风控参数

编辑 `config/qmt_config.py`：

```python
DAILY_LOSS_LIMIT = -0.03       # 日亏损阈值（-3%）
SINGLE_STOCK_MAX_LOSS = -0.10  # 单股止损（-10%）
MAX_TOTAL_POSITION_PCT = 0.80  # 最大仓位（80%）
MIN_CASH_RESERVE = 10000       # 最低现金保留
```

### 10.3 紧急情况

如果需要紧急停止所有交易：
1. 关闭 miniQMT 客户端（断开交易通道）
2. 在券商 APP 手动撤单
3. 将 `DRY_RUN` 改回 `True`

---

## 十一、文件清单

| 文件 | 用途 | 是否需改 |
|------|------|---------|
| `config/qmt_config.py` | QMT 连接 + 风控配置 | **必须改** |
| `src/qmt_trader.py` | 实盘交易引擎 | 不改 |
| `scripts/run_live_trade.py` | 每日执行入口 | 不改 |
| `data/output/live_trade_state.json` | 实盘状态（自动生成） | 不改 |
| `data/output/live_trade_log.csv` | 交易日志（自动生成） | 不改 |

---

## 十二、接入时间线

```
第1天: 选券商 → 联系客户经理 → 开户
第2-3天: 下载QMT → 登录测试 → 手动测试单
第4天: 安装xtquant → 配置qmt_config.py
第5天: 验证连接 → 开始DRY_RUN模拟
第5-14天: DRY_RUN每日运行 → 对比纸上交易
第15天: 确认一致 → 小额实盘（5-10万）
第15-21天: 小额实盘验证 → 检查滑点和成交
第22天: 逐步加仓到目标规模
```

---

## 十三、FAQ

**Q: miniQMT 掉线了怎么办？**
A: 重新运行 `run_live_trade.py` 会自动重连。建议设 miniQMT 为开机自启。

**Q: 实盘和纸上交易信号不一致？**
A: 不应该发生。两边复用同一个 `generate_v22_signals` 函数。如果不一致，检查是否同一天运行、数据是否相同。

**Q: 可以用多个账户同时运行吗？**
A: 可以。每个账户用不同的 `SESSION_ID` 和 `LIVE_STATE_FILE`。

**Q: 支持港股/美股吗？**
A: 当前只支持 A 股（沪深两市）。代码格式自动转换：6开头→.SH，其他→.SZ。

**Q: 策略容量有多大？**
A: 板块轮动 + 小盘股策略，建议 100-500 万。超过 500 万冲击成本会显著增加。
