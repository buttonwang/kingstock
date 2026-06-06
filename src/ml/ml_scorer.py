"""ML评分器 (V2) - 加载训练好的XGBoost+LightGBM Stacking融合模型

用法:
    from src.ml.ml_scorer import MLScorer
    scorer = MLScorer()
    score = scorer.predict_score(stock_daily_df)

评分范围: 0 ~ SCORE_ML_MAX (默认15分)

V2 改进:
    1. XGBoost + LightGBM Stacking融合预测
    2. 评分校准（Isotonic Regression）
    3. 阈值优化（precision-recall最优截断）
"""
import os
import json
import warnings
from typing import Optional

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

from src.ml.feature_engine import extract_all_features

# 全局模型缓存
_CLS_MODELS = None       # {"xgb":, "lgb":, "meta":}
_REG_MODELS = None       # {"xgb":, "lgb":, "meta":}
_SCALER = None
_FEATURE_COLS = None
_CALIBRATOR = None
_BEST_THRESHOLD = 0.5
_MODEL_LOADED = False
_MODEL_VERSION = 1       # 1=旧版XGBoost-only, 2=新版Stacking
_SCORE_ML_MAX = 15


def _get_models_dir() -> str:
    """获取模型目录路径"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")


def load_models() -> bool:
    """加载训练好的模型

    自动检测V2（Stacking）或V1（XGBoost-only）模型。

    返回: 是否成功加载
    """
    global _CLS_MODELS, _REG_MODELS, _SCALER, _FEATURE_COLS, _MODEL_LOADED
    global _CALIBRATOR, _BEST_THRESHOLD, _MODEL_VERSION

    model_dir = _get_models_dir()

    # 尝试加载V2模型配置
    config_path = os.path.join(model_dir, "model_config.json")
    v2_available = os.path.exists(config_path)

    try:
        # 先检查是否有V2模型
        if v2_available:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            _MODEL_VERSION = config.get("model_version", 2)
            _BEST_THRESHOLD = config.get("best_threshold", 0.5)
            _SCORE_ML_MAX = config.get("score_ml_max", 15)
        else:
            _MODEL_VERSION = 1
            _BEST_THRESHOLD = 0.5

        import xgboost as xgb
        import joblib

        # 加载scaler
        scaler_path = os.path.join(model_dir, "scaler.pkl")
        if os.path.exists(scaler_path):
            _SCALER = joblib.load(scaler_path)
        else:
            return False

        # 加载特征列
        feat_path = os.path.join(model_dir, "feature_columns.json")
        if os.path.exists(feat_path):
            with open(feat_path, "r", encoding="utf-8") as f:
                _FEATURE_COLS = json.load(f)
        else:
            return False

        if _MODEL_VERSION == 2:
            # V2: Stacking模型

            cls_xgb_path = os.path.join(model_dir, "xgb_classifier.json")
            cls_lgb_path = os.path.join(model_dir, "lgb_classifier.pkl")
            meta_cls_path = os.path.join(model_dir, "meta_classifier.pkl")
            calibrator_path = os.path.join(model_dir, "calibrator.pkl")

            if not all(os.path.exists(p) for p in [cls_xgb_path, cls_lgb_path, meta_cls_path]):
                return False

            _CLS_MODELS = {}
            _CLS_MODELS["xgb"] = xgb.XGBClassifier()
            _CLS_MODELS["xgb"].load_model(cls_xgb_path)
            _CLS_MODELS["lgb"] = joblib.load(cls_lgb_path)
            _CLS_MODELS["meta"] = joblib.load(meta_cls_path)

            if os.path.exists(calibrator_path):
                _CALIBRATOR = joblib.load(calibrator_path)
            else:
                _CALIBRATOR = None

            # 回归模型
            reg_xgb_path = os.path.join(model_dir, "xgb_regressor.json")
            reg_lgb_path = os.path.join(model_dir, "lgb_regressor.pkl")
            meta_reg_path = os.path.join(model_dir, "meta_regressor.pkl")

            if all(os.path.exists(p) for p in [reg_xgb_path, reg_lgb_path, meta_reg_path]):
                _REG_MODELS = {}
                _REG_MODELS["xgb"] = xgb.XGBRegressor()
                _REG_MODELS["xgb"].load_model(reg_xgb_path)
                _REG_MODELS["lgb"] = joblib.load(reg_lgb_path)
                _REG_MODELS["meta"] = joblib.load(meta_reg_path)

        else:
            # V1: XGBoost-only (向后兼容)
            cls_path = os.path.join(model_dir, "xgb_classifier.json")
            reg_path = os.path.join(model_dir, "xgb_regressor.json")

            if not os.path.exists(cls_path):
                return False

            _CLS_MODELS = {}
            _CLS_MODELS["xgb"] = xgb.XGBClassifier()
            _CLS_MODELS["xgb"].load_model(cls_path)

            if os.path.exists(reg_path):
                _REG_MODELS = {}
                _REG_MODELS["xgb"] = xgb.XGBRegressor()
                _REG_MODELS["xgb"].load_model(reg_path)

        _MODEL_LOADED = True
        return True

    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("加载ML模型失败: %s", e)
        return False


def _get_feature_vector(df: pd.DataFrame,
                         sector_returns: list = None,
                         sector_volumes: list = None,
                         rps_rank: int = None,
                         rps_top_n: int = 20) -> Optional[np.ndarray]:
    """从股票日线数据提取特征向量

    返回: 标准化后的特征向量 (1D array)，如果失败返回None
    """
    global _FEATURE_COLS

    try:
        feat_dict = extract_all_features(
            df, sector_returns=sector_returns,
            sector_volumes=sector_volumes,
            rps_rank=rps_rank, rps_top_n=rps_top_n,
        )
    except Exception:
        return None

    if _FEATURE_COLS is None:
        return np.zeros(len(feat_dict))

    # 构建特征向量（按模型训练时的列顺序）
    vec = []
    for col in _FEATURE_COLS:
        if col in feat_dict:
            vec.append(feat_dict[col])
        else:
            vec.append(0.0)
    return np.array(vec)


def predict_score(df: pd.DataFrame,
                   sector_returns: list = None,
                   sector_volumes: list = None,
                   rps_rank: int = None,
                   rps_top_n: int = 20) -> float:
    """对单只股票计算ML评分（V2 Stacking融合版）

    参数:
        df: 股票日线DataFrame [date, open, high, low, close, volume]
        sector_returns: 同板块其他股票的收益率列表
        sector_volumes: 同板块其他股票的成交量列表
        rps_rank: 板块RPS排名
        rps_top_n: RPS TOP N

    返回:
        ML评分 (0 ~ SCORE_ML_MAX)
    """
    global _SCORE_ML_MAX, _MODEL_LOADED, _MODEL_VERSION

    # 如果没有加载模型，尝试加载
    if not _MODEL_LOADED:
        load_models()

    # 提取特征
    vec = _get_feature_vector(df, sector_returns, sector_volumes,
                               rps_rank, rps_top_n)
    if vec is None:
        return _SCORE_ML_MAX / 2  # 默认中位分

    # 如果模型未加载，返回中位分
    if not _MODEL_LOADED:
        return _SCORE_ML_MAX / 2

    try:
        # 标准化
        vec_scaled = _SCALER.transform(vec.reshape(1, -1))

        # ── 分类概率预测 ──
        if _MODEL_VERSION == 2 and _CLS_MODELS is not None and "meta" in _CLS_MODELS:
            # V2: Stacking融合预测
            xgb_prob = _CLS_MODELS["xgb"].predict_proba(vec_scaled)[0, 1]
            lgb_prob = _CLS_MODELS["lgb"].predict_proba(vec_scaled)[0, 1]
            meta_X = np.array([[xgb_prob, lgb_prob]])
            cls_prob = _CLS_MODELS["meta"].predict_proba(meta_X)[0, 1]
        elif _CLS_MODELS is not None and "xgb" in _CLS_MODELS:
            # V1: XGBoost-only
            cls_prob = _CLS_MODELS["xgb"].predict_proba(vec_scaled)[0, 1]
        else:
            cls_prob = 0.5

        # ── 评分校准 ──
        if _CALIBRATOR is not None:
            calibrated_prob = _CALIBRATOR.transform(np.array([cls_prob]))[0]
            calibrated_prob = np.clip(calibrated_prob, 0.0, 1.0)
        else:
            calibrated_prob = cls_prob

        # ── 回归预测（调节因子） ──
        reg_pred = 0.0
        if _REG_MODELS is not None:
            if _MODEL_VERSION == 2 and "meta" in _REG_MODELS:
                xgb_pred = _REG_MODELS["xgb"].predict(vec_scaled)[0]
                lgb_pred = _REG_MODELS["lgb"].predict(vec_scaled)[0]
                meta_X = np.array([[xgb_pred, lgb_pred]])
                reg_pred = _REG_MODELS["meta"].predict(meta_X)[0]
            elif "xgb" in _REG_MODELS:
                reg_pred = _REG_MODELS["xgb"].predict(vec_scaled)[0]

        # ── 融合评分 ──
        reg_norm = np.clip(reg_pred / 5.0, -1.0, 1.0)  # ±5%以外的截断
        combined = calibrated_prob * (1.0 + reg_norm * 0.3)
        combined = np.clip(combined, 0.0, 1.0)

        score = round(float(combined * _SCORE_ML_MAX), 1)
        return score

    except Exception:
        return _SCORE_ML_MAX / 2


def set_score_max(score_max: int):
    """设置ML评分最大值（默认15分）"""
    global _SCORE_ML_MAX
    _SCORE_ML_MAX = score_max


def is_available() -> bool:
    """检查ML模型是否已加载"""
    if not _MODEL_LOADED:
        return load_models()
    return _MODEL_LOADED
