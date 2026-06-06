"""选股系统配置参数"""
import os

# 策略版本
STRATEGY_VERSION = "1.0"  # V1.0「66大顺」: 纯信号动态持有期模式 (2026-06-06定版)

# 板块RPS参数
RPS_PERIOD = 20        # RPS计算周期（天）
RPS_TOP_N = 20         # 取前N个板块

# 板块筛选类型: "concept"(概念板块) 或 "industry"(行业板块)
SECTOR_TYPE = "concept"

# 双阶段RPS筛选（V2 优化）
DUAL_RPS_ENABLED = False         # 是否启用双阶段RPS（需同时加载行业和概念板块）
RPS_PRIMARY_TOP_N = 15           # 第一阶RPS取前N个板块（行业板块）
RPS_SECONDARY_TOP_N = 10         # 第二阶段RPS取前N个板块（概念板块）

# RPS持续性检查（V2 优化）
RPS_CONSISTENCY_ENABLED = False  # 是否启用RPS持续性检查
RPS_CONSISTENCY_DAYS = 3         # 检查过去N日的一致性
RPS_CONSISTENCY_RANK = 30        # 板块RPS需连续N日在排名前30

# 动态TOP_N（V2 优化）
RPS_DYNAMIC_ENABLED = False      # 是否启用动态TOP_N
RPS_DYNAMIC_BASE_N = 20          # 基础TOP_N
RPS_DYNAMIC_MIN_N = 10           # 最小TOP_N（熊市收紧）
RPS_DYNAMIC_MAX_N = 30           # 最大TOP_N（牛市放宽）
RPS_DYNAMIC_THRESHOLD = 0.0      # 市场平均涨幅阈值（>0为牛市，<0为熊市）

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
PROFIT_GROWTH_MIN = 0.20     # 最低增长率20%

# 数据路径
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "db", "stock.db")
OUTPUT_PATH = os.path.join(BASE_DIR, "data", "output")
LOG_PATH = os.path.join(BASE_DIR, "data", "logs")

# 数据获取配置
REQUEST_TIMEOUT = 30        # 请求超时（秒）
REQUEST_RETRY = 3           # 重试次数
REQUEST_DELAY = 0.5         # 请求间隔（秒），避免被限频

# ============================================================
# 邮件通知配置
# 每次选股完成后，将分析报告发送到以下邮箱
# 留空或注释掉则不发送邮件
# ============================================================

# SMTP 服务器配置
SMTP_HOST = "smtp.163.com"         # SMTP服务器地址（163邮箱）
SMTP_PORT = 465                     # SSL端口
SMTP_USER = "ddxxzx2021@163.com"    # 发件邮箱账号
SMTP_PASSWORD = "VHQLPEXXMPRKKEQA"  # 发件邮箱授权码（非登录密码）

# 收件人列表（支持多个）
EMAIL_RECIPIENTS = ["buttonww@163.com", "18235156686@163.com"]
EMAIL_FROM_NAME = "智能选股系统"

# ============================================================
# PushPlus 微信推送配置
# 关注 PushPlus 公众号获取 token
# ============================================================
PUSHPLUS_TOKEN = "7e328f5d9fa349b58f757ba381494387"  # 填入你的 PushPlus token

# HTML报告输出路径
HTML_OUTPUT_PATH = os.path.join(BASE_DIR, "data", "output", "html")
HTML_REPORT_FILENAME = "report_{date}.html"

# 手工选股目录
MANUAL_STOCK_DIR = os.path.join(BASE_DIR, "手工选股")

# 龙头公司EBK文件路径
EBK_FILE = os.path.join(BASE_DIR, "龙头公司.EBK")

# ============================================================
# 增强辅助规则配置（Phase 1 规则优化）
# ============================================================

# 成交量确认
VOLUME_CONFIRM_ENABLED = True       # 是否启用成交量确认
VOLUME_MA_PERIOD = 5                 # 均量计算周期
VOLUME_THRESHOLD = 1.5               # 选股日成交量 > MA均量的倍数阈值

# 均线多头排列
MA_ALIGN_ENABLED = True              # 是否启用均线多头排列检查
MA_SHORT = 5                         # 短期均线
MA_MID = 10                          # 中期均线
MA_LONG = 20                         # 长期均线
MA_EXTRA = 60                        # 超长期均线

# 价格位置过滤
PRICE_POSITION_ENABLED = False       # 是否启用价格位置过滤 (Phase 6: 数据证明该过滤在帮倒忙，禁用以提升信号量)
PRICE_LOOKBACK = 60                  # 回顾周期
PRICE_LOWER_PCT = 20                 # 最低分位数(%)
PRICE_UPPER_PCT = 80                 # 最高分位数(%)

# ============================================================
# 评分权重配置（Phase 1 评分排序系统）
# ============================================================

# 各维度最高分
SCORE_MACD_MAX = 30                  # MACD信号强度
SCORE_ZJTJ_MAX = 20                  # ZJTJ控盘强度
SCORE_KDJ_MAX = 15                   # KDJ信号质量
SCORE_RPS_MAX = 15                   # RPS板块排名
SCORE_VOLUME_MAX = 10                # 成交量确认
SCORE_FINANCE_MAX = 10               # 财务增长

# 满分配置信号质量阈值
SCORE_DIF_DEA_GAP_THRESHOLD = 0.1    # DIF与DEA差值≥此值得满分MACD分
SCORE_KONGPAN_THRESHOLD = 0.5        # 控盘度≥此值得满分ZJTJ分

# ============================================================
# ML评分权重配置（Phase 2 机器学习集成）
# ============================================================

SCORE_ML_MAX = 15                    # XGBoost ML评分最高分
ML_SCORE_ENABLED = True              # 是否启用ML评分

# ============================================================
# 选股信号降噪参数（Phase 3 优化 V2）
# ============================================================

MIN_TOTAL_SCORE = 60           # 最低综合评分，低于此分不输出
MAX_DAILY_OUTPUT = 5           # 每日最多输出股票数
ENHANCED_RULES_MIN = 1         # 增强规则最少通过数（Phase 6: 从2降为1，配合PRICE_POSITION_ENABLED=False）

# ============================================================
# ZJTJ控盘分级参数（Phase 3 优化 V2）
# ============================================================

# 控盘强度分级阈值
KONGPAN_STRONG = 1.0        # 强控盘阈值
KONGPAN_MEDIUM = 0.5        # 中控盘阈值
KONGPAN_WEAK = 0.0          # 弱控盘阈值（>0即可）

# 控盘趋势检查
KONGPAN_TREND_DAYS = 5      # 检查过去N日控盘趋势
KONGPAN_TREND_MIN_PCT = 60  # N日中至少百分之多少的天数呈上升趋势

# 仅弱控盘是否作为买入依据（True=弱控盘单独可买入，False=仅作为辅助）
KONGPAN_WEAK_BUY = False

# ============================================================
# 止损止盈参数（Phase 3 优化 V2）
# ============================================================

# 止盈档次: (阈值, 卖出比例(相对原始仓位), 标签)
TP_LEVELS = [
    (0.30, 0.50, "TAKE_PROFIT_30"),   # +30% 卖出50%
    (0.20, 0.30, "TAKE_PROFIT_20"),   # +20% 卖出30%
    (0.10, 0.20, "TAKE_PROFIT_10"),   # +10% 卖出20%
]

STOP_LOSS_DRAWDOWN = 0.12     # 跟踪止损回撤阈值（从-8%放宽到-12%）
HARD_STOP_LOSS = 0.08         # 固定硬止损（-8%，不跟踪，到达立即平仓）
MAX_HOLD_DAYS = 80            # 最大持仓天数（从60放宽到80）

# ============================================================
# 仓位管理参数（Level 1 改进）
# ============================================================
POSITION_SIZING_ENABLED = True      # 是否启用仓位管理
POSITION_BASE_PCT = 0.02            # 基础仓位（总资金2%）
POSITION_MAX_PCT = 0.05             # 最大单仓（总资金5%）
POSITION_SCORE_WEIGHT = True        # 是否按评分加权分配
POSITION_KELLY_FRACTION = 0.5       # 凯利分数（半凯利）

# ============================================================
# 动态止损止盈参数（Level 1 改进）
# ============================================================
TRAILING_STOP_LOSS = 0.12           # 跟踪止损回撤阈值（从8%放宽到12%以适应波动）
HARD_STOP_LOSS = 0.15               # 硬止损（-15%，原8%太紧导致大量假止损）
TIME_STOP_LOSS_DAYS = 25            # 时间止损天数（从20天放宽到25天）
TIME_STOP_LOSS_MIN_RETURN = -0.03   # 时间止损最低收益要求（容忍-3%亏损）
TAKE_PROFIT_TARGET = 0.20           # 目标止盈阈值（+20%减仓50%）
SCORE_STOP_THRESHOLD = 7            # ML评分低于此值平仓（0-15分）
EXIT_CHECK_FREQ = 1                 # 退出检查频率（每N日检查一次）

# ============================================================
# Walk-Forward 参数寻优网格（Phase 3 优化 V2）
# ============================================================

# MACD参数寻优范围
MACD_GRID = {
    "MACD_SHORT": [5, 8, 10, 12, 15, 20],
    "MACD_LONG": [20, 22, 24, 26, 30, 35],
    "MACD_MID": [5, 7, 9, 12, 14],
}

# ZJTJ参数寻优范围
ZJTJ_GRID = {
    "KONGPAN_STRONG": [0.8, 1.0, 1.5],
    "KONGPAN_MEDIUM": [0.3, 0.5, 0.8],
    "KONGPAN_TREND_DAYS": [3, 5, 7],
    "KONGPAN_TREND_MIN_PCT": [50, 60, 70],
}

# 参数自动更新设置
PARAM_HISTORY_FILE = "param_history.csv"  # 历史参数记录文件名

# ============================================================
# 六项优化新增参数（Phase 4 性能优化）
# ============================================================

# ML评分入场阈值
ML_SCORE_MIN_THRESHOLD = 10       # 60日模型: 恢复原始阈值

# 动态持仓期（按ML评分分档）
DYNAMIC_HOLD_ML_HIGH = 7          # ML>=13分持有天数
DYNAMIC_HOLD_ML_MID = 8           # ML>=10分持有天数

# ATR波动率止损参数
ATR_STOP_ENABLED = True           # 是否启用ATR止损
ATR_PERIOD = 14                   # ATR计算周期（天）
ATR_STOP_MULTIPLIER = 2.5         # 硬止损：入场价 - 2.5 * ATR
ATR_TRAILING_MULTIPLIER = 2.0     # 跟踪止损：从最高点回撤 > 2.0 * ATR

# Kelly仓位精细管理
KELLY_ML_13 = 1.0                 # ML 13-15分：全仓Kelly
KELLY_ML_11 = 0.75                # ML 11-12分：75% Kelly
KELLY_ML_10 = 0.50                # ML 10分：50% Kelly

# 严格RPS板块轮动
RPS_TOP_N_STRICT = 10             # 加强版RPS仅取前10板块（原为20）

# ============================================================
# 纯信号交易系统参数（Phase 5 方向A优化）
# ============================================================

# 市场状态过滤阈值
MARKET_STRONG_THRESHOLD = 2.0     # bench_10d > 2% = 强势市场
MARKET_WEAK_THRESHOLD = -3.0      # bench_10d < -3% = 弱势市场

# 精细动态持有期（按ML评分分档）
HOLD_ML_13_15 = 10                # ML 13-15分: 持有10天（让利润奔跑）
HOLD_ML_11_12 = 8                 # ML 11-12分: 持有8天
HOLD_ML_10 = 6                    # ML 10分: 持有6天

# 简化价格止损（替代ATR止损）
PURE_STOP_LOSS_PCT = -8.0         # 纯价格止损：入场价下跌8%卖出

# 条件化仓位乘数
POSITION_STRONG_MULT = 1.0        # 强势市场：正常仓位
POSITION_CHOPPY_MULT = 0.5        # 震荡市场：半仓

# 动态评分门槛
SCORE_THRESHOLD_STRONG = 60       # 强势市场最低总分
SCORE_THRESHOLD_CHOPPY = 65       # 震荡市场最低总分
SCORE_THRESHOLD_WEAK = 999        # 弱势市场不交易

# 部分止盈参数
TAKE_PROFIT_TRIGGER = 8.0         # 持仓涨幅达此值时触发部分止盈
TAKE_PROFIT_SELL_RATIO = 0.5      # 部分止盈时卖出比例

# 每日最大信号数（按市场状态）
MAX_DAILY_STRONG = 3              # 强势市场每日最多3只
MAX_DAILY_CHOPPY = 2              # 震荡市场每日最多2只
MAX_DAILY_WEAK = 0                # 弱势市场不交易

# ============================================================
# 纯信号交易系统参数（Phase 6 收益率与可交易性优化）
# ============================================================

# 缩短持有期（基于2日Sharpe 5.50 >> 10日Sharpe 4.70的数据发现）
HOLD_ML_13_15 = 7                # ML 13-15分: 持有7天（原10天）
HOLD_ML_11_12 = 5                # ML 11-12分: 持有5天（原8天）
HOLD_ML_10 = 3                   # ML 10分: 持有3天（原6天）

# 移动止盈（从最高点回落平仓，代替固定持有到期）
TRAILING_STOP_ENABLED = True       # 是否启用移动止盈
TRAILING_STOP_FROM_PEAK_PCT = -2.0 # 从最高点回落2%即平仓
TRAILING_STOP_ACTIVATE_AT = 3.0    # 持仓盈利达3%后才激活移动止盈

# 弱势市场处理（不空仓，仅降低参与度）
WEAK_MARKET_REDUCE_RATIO = 0.3     # 弱势市场仓位比例（30%），取代原空仓逻辑
WEAK_MARKET_MAX_SIGNALS = 1        # 弱势市场每日最多信号数