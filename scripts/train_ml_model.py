"""训练ML模型（V2增强版）：LightGBM + XGBoost Stacking融合

用法:
    python scripts/train_ml_model.py [--quick]

流程:
    1. 加载训练数据 (ml_training_data.csv)
    2. 提取特征列，标准化
    3. 训练XGBoost + LightGBM 分类模型，Stacking融合
    4. 阈值优化（基于验证集precision-recall曲线）
    5. 评分校准（分位数校准）
    6. 训练XGBoost + LightGBM 回归模型
    7. 评估、特征重要性分析
    8. 保存模型到 src/ml/models/
"""
import os
import sys
import warnings
import json
import joblib

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from src.ml.feature_engine import get_feature_names
from src.utils import setup_logging
from config.settings import SCORE_ML_MAX

logger = setup_logging("train_ml_model")

# Stacking融合权重（在验证集上优化）
STACK_WEIGHTS = [0.5, 0.5]  # [XGB, LGB]，如果不用meta-learner则用固定权重


def get_feature_columns(df: pd.DataFrame) -> list:
    """从DataFrame中识别特征列（排除元数据列和标签列）"""
    exclude = {"date", "code", "name", "sector", "split",
               "label_cls", "label_reg", "label_multi", "label_rank",
               "sample_weight", "fwd_return"}
    return [c for c in df.columns if c not in exclude]


def _load_data(csv_path: str, quick: bool = False):
    """加载并分割数据"""
    logger.info("加载训练数据...")
    df = pd.read_csv(csv_path)

    if quick:
        df = df.sample(frac=0.2, random_state=42).reset_index(drop=True)
        logger.info("快速模式: 取20%%样本 (%d 条)", len(df))

    logger.info("数据加载完成: %d 条, %d 列", len(df), len(df.columns))

    train_df = df[df["split"] == "train"].copy()
    val_df = df[df["split"] == "val"].copy()
    test_df = df[df["split"] == "test"].copy()

    logger.info("训练集: %d | 验证集: %d | 测试集: %d",
                 len(train_df), len(val_df), len(test_df))
    return df, train_df, val_df, test_df


def _prepare_features(df, train_df, val_df, test_df):
    """提取特征并标准化"""
    feature_cols = get_feature_columns(df)
    logger.info("特征维度: %d", len(feature_cols))

    # 检查特征列
    missing_features = [c for c in feature_cols if c not in df.columns]
    if missing_features:
        logger.warning("缺失特征列: %s", missing_features[:5])
        feature_cols = [c for c in feature_cols if c in df.columns]

    # 填充NaN
    for col in feature_cols:
        if df[col].dtype in (np.float64, np.float32, np.int64, np.int32):
            train_df[col] = train_df[col].fillna(0)
            val_df[col] = val_df[col].fillna(0)
            test_df[col] = test_df[col].fillna(0)

    X_train = train_df[feature_cols].values
    X_val = val_df[feature_cols].values
    X_test = test_df[feature_cols].values

    # 标准化
    from sklearn.preprocessing import StandardScaler
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_val_scaled = scaler.transform(X_val)
    X_test_scaled = scaler.transform(X_test)

    return feature_cols, scaler, X_train_scaled, X_val_scaled, X_test_scaled


def _train_classifier_stacking(X_train, y_train, X_val, y_val,
                                sample_weight_train=None):
    """训练XGBoost + LightGBM Stacking融合分类器

    返回:
        dict: 包含各模型和融合预测结果
    """
    import xgboost as xgb
    import lightgbm as lgb

    logger.info("=" * 60)
    logger.info("训练Stacking融合分类模型 (XGBoost + LightGBM)...")

    # ── 基学习器1: XGBoost ──
    logger.info("  训练XGBoost...")
    xgb_cls = xgb.XGBClassifier(
        max_depth=6,
        learning_rate=0.1,
        n_estimators=500,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_weight=3,
        reg_alpha=0.1,
        reg_lambda=1.0,
        random_state=42,
        n_jobs=-1,
        eval_metric="logloss",
        early_stopping_rounds=20,
        verbosity=0,
    )

    xgb_cls.fit(
        X_train, y_train,
        eval_set=[(X_val, y_val)],
        verbose=False,
        sample_weight=sample_weight_train,
    )

    # ── 基学习器2: LightGBM ──
    logger.info("  训练LightGBM...")
    lgb_cls = lgb.LGBMClassifier(
        max_depth=6,
        learning_rate=0.1,
        n_estimators=500,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_weight=3,
        reg_alpha=0.1,
        reg_lambda=1.0,
        random_state=42,
        n_jobs=-1,
        metric="binary_logloss",
        verbosity=-1,
    )

    lgb_cls.fit(
        X_train, y_train,
        eval_set=[(X_val, y_val)],
        callbacks=[lgb.early_stopping(20), lgb.log_evaluation(0)],
        sample_weight=sample_weight_train,
    )

    # ── 验证集上的预测概率 ──
    xgb_val_prob = xgb_cls.predict_proba(X_val)[:, 1]
    lgb_val_prob = lgb_cls.predict_proba(X_val)[:, 1]

    # ── 训练元学习器（LogisticRegression） ──
    from sklearn.linear_model import LogisticRegression
    meta_X_val = np.column_stack([xgb_val_prob, lgb_val_prob])
    meta_cls = LogisticRegression(random_state=42, C=1.0, penalty="l2")
    meta_cls.fit(meta_X_val, y_val)

    meta_weight = meta_cls.coef_[0]
    logger.info("  Stacking元学习器权重: XGB=%.3f, LGB=%.3f",
                 meta_weight[0], meta_weight[1])

    return {
        "xgb": xgb_cls,
        "lgb": lgb_cls,
        "meta": meta_cls,
    }


def _evaluate_classifier(models, X_train, X_val, X_test, y_train, y_val, y_test):
    """评估分类模型（各基学习器 + Stacking融合）"""
    from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score, roc_auc_score

    splits = [
        ("训练集", X_train, y_train),
        ("验证集", X_val, y_val),
        ("测试集", X_test, y_test),
    ]

    for model_name in ["xgb", "lgb"]:
        model = models[model_name]
        logger.info("  ── %s ──", model_name.upper())
        for split_name, X_s, y_s in splits:
            y_prob = model.predict_proba(X_s)[:, 1]
            y_pred = (y_prob >= 0.5).astype(int)
            acc = accuracy_score(y_s, y_pred)
            prec = precision_score(y_s, y_pred, zero_division=0)
            rec = recall_score(y_s, y_pred, zero_division=0)
            f1 = f1_score(y_s, y_pred, zero_division=0)
            auc = roc_auc_score(y_s, y_prob) if len(np.unique(y_s)) > 1 else 0.0
            logger.info("    %s: Acc=%.4f Prec=%.4f Rec=%.4f F1=%.4f AUC=%.4f",
                        split_name, acc, prec, rec, f1, auc)

    # Stacking融合评估
    logger.info("  ── STACKING ──")
    meta = models["meta"]
    for split_name, X_s, y_s in splits:
        xgb_prob = models["xgb"].predict_proba(X_s)[:, 1]
        lgb_prob = models["lgb"].predict_proba(X_s)[:, 1]
        meta_X = np.column_stack([xgb_prob, lgb_prob])
        y_prob = meta.predict_proba(meta_X)[:, 1]
        y_pred = (y_prob >= 0.5).astype(int)
        acc = accuracy_score(y_s, y_pred)
        prec = precision_score(y_s, y_pred, zero_division=0)
        rec = recall_score(y_s, y_pred, zero_division=0)
        f1 = f1_score(y_s, y_pred, zero_division=0)
        auc = roc_auc_score(y_s, y_prob) if len(np.unique(y_s)) > 1 else 0.0
        logger.info("    %s: Acc=%.4f Prec=%.4f Rec=%.4f F1=%.4f AUC=%.4f",
                    split_name, acc, prec, rec, f1, auc)


def _optimize_threshold(models, X_val, y_val):
    """基于验证集precision-recall曲线选择最优截断值

    返回:
        float: 最优概率截断值
    """
    from sklearn.metrics import precision_recall_curve

    # 获取Stacking融合概率
    xgb_prob = models["xgb"].predict_proba(X_val)[:, 1]
    lgb_prob = models["lgb"].predict_proba(X_val)[:, 1]
    meta_X = np.column_stack([xgb_prob, lgb_prob])
    y_prob = models["meta"].predict_proba(meta_X)[:, 1]

    precisions, recalls, thresholds = precision_recall_curve(y_val, y_prob)

    # 找到F1最优的阈值
    f1_scores = 2 * precisions[:-1] * recalls[:-1] / (precisions[:-1] + recalls[:-1] + 1e-10)
    best_idx = np.argmax(f1_scores)
    best_threshold = thresholds[best_idx]

    logger.info("=" * 60)
    logger.info("阈值优化:")
    logger.info("  最优截断值: %.4f (F1=%.4f, Prec=%.4f, Rec=%.4f)",
                best_threshold, f1_scores[best_idx],
                precisions[best_idx], recalls[best_idx])

    # 显示不同阈值的效果
    for thresh in [0.3, 0.4, 0.5, 0.6, 0.7]:
        y_pred = (y_prob >= thresh).astype(int)
        from sklearn.metrics import precision_score, recall_score, f1_score
        p = precision_score(y_val, y_pred, zero_division=0)
        r = recall_score(y_val, y_pred, zero_division=0)
        f = f1_score(y_val, y_pred, zero_division=0)
        logger.info("  阈值=%.1f: Prec=%.4f Rec=%.4f F1=%.4f", thresh, p, r, f)

    return best_threshold


def _calibration_curve(y_true, y_prob, n_bins=15):
    """计算分位数校准映射

    将[0,1]概率分成n_bins个分位区间，统计每个区间的实际正样本率

    返回:
        edges: 分位边界
        bin_corrected: 校准后的概率值
    """
    from sklearn.isotonic import IsotonicRegression

    # 使用等渗回归进行校准
    iso_reg = IsotonicRegression(out_of_bounds="clip", increasing=True)
    y_calibrated = iso_reg.fit_transform(y_prob, y_true)

    logger.info("  评分校准: 使用等渗回归 (Isotonic Regression)")
    return iso_reg


def _score_calibration(y_val, stacking_prob, X_val, models):
    """评分校准：将概率映射到0~SCORE_ML_MAX的分位数校准"""
    from sklearn.isotonic import IsotonicRegression

    # 等渗回归校准
    iso_reg = IsotonicRegression(out_of_bounds="clip", increasing=True)
    iso_reg.fit(stacking_prob, y_val)

    # 测试集校准效果
    calibrated = iso_reg.transform(stacking_prob)

    # 统计分桶效果
    logger.info("=" * 60)
    logger.info("评分校准效果 (验证集):")

    buckets = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0)]
    for lo, hi in buckets:
        mask = (stacking_prob >= lo) & (stacking_prob < hi)
        if mask.sum() > 0:
            actual_pos = y_val[mask].mean() * 100
            avg_prob = stacking_prob[mask].mean() * 100
            logger.info("  原始概率 [%.0f%%~%.0f%%): 实际正样本率=%.1f%%, 平均概率=%.1f%%, n=%d",
                        lo*100, hi*100, actual_pos, avg_prob, mask.sum())

    # 校准后分桶看效果
    for lo, hi in buckets:
        mask = (calibrated >= lo) & (calibrated < hi)
        if mask.sum() > 0:
            actual_pos = y_val[mask].mean() * 100
            logger.info("  校准后 [%.0f%%~%.0f%%): 实际正样本率=%.1f%%, n=%d",
                        lo*100, hi*100, actual_pos, mask.sum())

    return iso_reg


def _train_regressor(X_train, y_train, X_val, y_val, sample_weight_train=None):
    """训练XGBoost + LightGBM Stacking回归模型"""
    import xgboost as xgb
    import lightgbm as lgb
    from sklearn.linear_model import LinearRegression

    logger.info("=" * 60)
    logger.info("训练Stacking融合回归模型 (预测涨跌幅)...")

    # XGBoost回归
    logger.info("  训练XGBoost回归...")
    xgb_reg = xgb.XGBRegressor(
        max_depth=6,
        learning_rate=0.1,
        n_estimators=500,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_weight=3,
        reg_alpha=0.1,
        reg_lambda=1.0,
        random_state=42,
        n_jobs=-1,
        eval_metric="rmse",
        early_stopping_rounds=20,
        verbosity=0,
    )

    xgb_reg.fit(
        X_train, y_train,
        eval_set=[(X_val, y_val)],
        verbose=False,
        sample_weight=sample_weight_train,
    )

    # LightGBM回归
    logger.info("  训练LightGBM回归...")
    lgb_reg = lgb.LGBMRegressor(
        max_depth=6,
        learning_rate=0.1,
        n_estimators=500,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_weight=3,
        reg_alpha=0.1,
        reg_lambda=1.0,
        random_state=42,
        n_jobs=-1,
        metric="rmse",
        verbosity=-1,
    )

    lgb_reg.fit(
        X_train, y_train,
        eval_set=[(X_val, y_val)],
        callbacks=[lgb.early_stopping(20), lgb.log_evaluation(0)],
        sample_weight=sample_weight_train,
    )

    # 元学习器（简单线性回归）
    xgb_val_pred = xgb_reg.predict(X_val)
    lgb_val_pred = lgb_reg.predict(X_val)
    meta_X_val = np.column_stack([xgb_val_pred, lgb_val_pred])
    meta_reg = LinearRegression()
    meta_reg.fit(meta_X_val, y_val)

    logger.info("  Stacking回归权重: XGB=%.3f, LGB=%.3f",
                 meta_reg.coef_[0], meta_reg.coef_[1])

    return {
        "xgb": xgb_reg,
        "lgb": lgb_reg,
        "meta": meta_reg,
    }


def _evaluate_regressor(models, X_train, X_val, X_test, y_train, y_val, y_test):
    """评估回归模型"""
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

    splits = [
        ("训练集", X_train, y_train),
        ("验证集", X_val, y_val),
        ("测试集", X_test, y_test),
    ]

    for model_name in ["xgb", "lgb"]:
        model = models[model_name]
        logger.info("  ── %s ──", model_name.upper())
        for split_name, X_s, y_s in splits:
            y_pred = model.predict(X_s)
            mae = mean_absolute_error(y_s, y_pred)
            rmse = mean_squared_error(y_s, y_pred) ** 0.5
            r2 = r2_score(y_s, y_pred)
            logger.info("    %s: MAE=%.4f RMSE=%.4f R2=%.4f",
                        split_name, mae, rmse, r2)

    # Stacking融合
    meta = models["meta"]
    logger.info("  ── STACKING ──")
    for split_name, X_s, y_s in splits:
        xgb_pred = models["xgb"].predict(X_s)
        lgb_pred = models["lgb"].predict(X_s)
        meta_X = np.column_stack([xgb_pred, lgb_pred])
        y_pred = meta.predict(meta_X)
        mae = mean_absolute_error(y_s, y_pred)
        rmse = mean_squared_error(y_s, y_pred) ** 0.5
        r2 = r2_score(y_s, y_pred)
        logger.info("    %s: MAE=%.4f RMSE=%.4f R2=%.4f",
                    split_name, mae, rmse, r2)


def _save_models(models_cls, models_reg, scaler, feature_cols,
                  best_threshold, calibrator, model_dir):
    """保存所有模型和元数据"""
    os.makedirs(model_dir, exist_ok=True)

    # 保存各基学习器
    models_cls["xgb"].save_model(os.path.join(model_dir, "xgb_classifier.json"))
    joblib.dump(models_cls["lgb"], os.path.join(model_dir, "lgb_classifier.pkl"))
    models_reg["xgb"].save_model(os.path.join(model_dir, "xgb_regressor.json"))
    joblib.dump(models_reg["lgb"], os.path.join(model_dir, "lgb_regressor.pkl"))

    # 保存元学习器
    joblib.dump(models_cls["meta"], os.path.join(model_dir, "meta_classifier.pkl"))
    joblib.dump(models_reg["meta"], os.path.join(model_dir, "meta_regressor.pkl"))

    # 保存scaler
    joblib.dump(scaler, os.path.join(model_dir, "scaler.pkl"))

    # 保存校准器
    joblib.dump(calibrator, os.path.join(model_dir, "calibrator.pkl"))

    # 保存特征列名
    with open(os.path.join(model_dir, "feature_columns.json"), "w", encoding="utf-8") as f:
        json.dump(feature_cols, f, ensure_ascii=False)

    # 保存阈值和配置
    config = {
        "best_threshold": best_threshold,
        "score_ml_max": SCORE_ML_MAX,
        "model_version": 2,
        "models": ["xgb_classifier", "lgb_classifier", "xgb_regressor", "lgb_regressor",
                    "meta_classifier", "meta_regressor"],
    }
    with open(os.path.join(model_dir, "model_config.json"), "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    logger.info("=" * 60)
    logger.info("模型已保存到: %s", model_dir)
    logger.info("  - xgb_classifier.json / lgb_classifier.txt")
    logger.info("  - xgb_regressor.json / lgb_regressor.txt")
    logger.info("  - meta_classifier.pkl / meta_regressor.pkl")
    logger.info("  - calibrator.pkl (评分校准器)")
    logger.info("  - scaler.pkl (标准化器)")
    logger.info("  - feature_columns.json (特征列名)")
    logger.info("  - model_config.json (模型配置)")
    logger.info("  - 最优截断值: %.4f", best_threshold)
    logger.info("=" * 60)


def train_models(quick: bool = False):
    """训练XGBoost+LightGBM Stacking融合模型（V2增强版）"""
    # 1. 加载数据
    data_dir = os.path.join(BASE_DIR, "data", "output")
    csv_path = os.path.join(data_dir, "ml_training_data.csv")

    if not os.path.exists(csv_path):
        logger.error("训练数据不存在: %s", csv_path)
        logger.error("请先运行: python scripts/prepare_training_data.py")
        return

    df, train_df, val_df, test_df = _load_data(csv_path, quick)

    # 2. 提取标签
    y_train_cls = train_df["label_cls"].values
    y_val_cls = val_df["label_cls"].values
    y_test_cls = test_df["label_cls"].values

    y_train_reg = train_df["label_reg"].values
    y_val_reg = val_df["label_reg"].values
    y_test_reg = test_df["label_reg"].values

    # 3. 提取样本权重（时间衰减）
    has_weight = "sample_weight" in train_df.columns
    sample_weight_train = train_df["sample_weight"].values if has_weight else None
    if has_weight:
        logger.info("使用时间衰减样本权重: min=%.4f, max=%.4f",
                     sample_weight_train.min(), sample_weight_train.max())

    # 4. 提取特征并标准化
    feature_cols, scaler, X_train, X_val, X_test = \
        _prepare_features(df, train_df, val_df, test_df)

    # 5. 训练Stacking分类模型
    cls_models = _train_classifier_stacking(
        X_train, y_train_cls, X_val, y_val_cls,
        sample_weight_train=sample_weight_train,
    )

    # 6. 评估分类模型
    _evaluate_classifier(cls_models, X_train, X_val, X_test,
                          y_train_cls, y_val_cls, y_test_cls)

    # 7. 阈值优化
    best_threshold = _optimize_threshold(cls_models, X_val, y_val_cls)

    # 8. 评分校准
    xgb_prob_val = cls_models["xgb"].predict_proba(X_val)[:, 1]
    lgb_prob_val = cls_models["lgb"].predict_proba(X_val)[:, 1]
    meta_X_val = np.column_stack([xgb_prob_val, lgb_prob_val])
    stacking_prob_val = cls_models["meta"].predict_proba(meta_X_val)[:, 1]
    calibrator = _score_calibration(y_val_cls, stacking_prob_val, X_val, cls_models)

    # 9. 训练回归模型
    reg_models = _train_regressor(
        X_train, y_train_reg, X_val, y_val_reg,
        sample_weight_train=sample_weight_train,
    )

    # 10. 评估回归模型
    _evaluate_regressor(reg_models, X_train, X_val, X_test,
                         y_train_reg, y_val_reg, y_test_reg)

    # 11. 特征重要性
    logger.info("=" * 60)
    logger.info("特征重要性 (Top 20, XGBoost):")
    importance = cls_models["xgb"].feature_importances_
    feat_imp = sorted(zip(feature_cols, importance), key=lambda x: -x[1])
    for i, (name, imp) in enumerate(feat_imp[:20]):
        logger.info("  %2d. %s: %.4f", i + 1, name, imp)

    logger.info("特征重要性 (Top 20, LightGBM):")
    lgb_importance = cls_models["lgb"].feature_importances_
    lgb_feat_imp = sorted(zip(feature_cols, lgb_importance), key=lambda x: -x[1])
    for i, (name, imp) in enumerate(lgb_feat_imp[:20]):
        logger.info("  %2d. %s: %.4f", i + 1, name, imp)

    # 12. 保存模型
    model_dir = os.path.join(BASE_DIR, "src", "ml", "models")
    _save_models(cls_models, reg_models, scaler, feature_cols,
                  best_threshold, calibrator, model_dir)

    return cls_models, reg_models, scaler, feature_cols


if __name__ == "__main__":
    quick = "--quick" in sys.argv
    train_models(quick=quick)
