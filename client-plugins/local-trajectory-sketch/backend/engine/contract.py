# -*- coding: utf-8 -*-
"""轨迹分析引擎 —— 数据契约层。

本模块定义引擎的**输入 / 输出 / 异常**三类契约，是算法与外界之间唯一的形状约定：

- 输入：``Dataset``（由 ``adapter.build_dataset`` 从过滤后的二维表产出）
- 输出：``dict``（JSON 可序列化；形状见 ``engine/__init__.py`` 文档）
- 异常：``AnalysisError``（message 为**面向用户的中文描述**，不含堆栈与路径，符合 SEC-5）

解耦约束（见设计文档 §5.1）
--------------------------
- D-1 本包（engine/）禁止 import flask / requests / jztools_data / 插件其他模块；
- D-2 本包只依赖标准库；
- D-3 边界只有 ``Dataset`` 进、``dict`` 出，中间一切实现细节不外泄。

因此本文件**不依赖任何第三方库**，可连同 engine/ 整体复制到任何 Python 项目。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class AnalysisError(Exception):
    """分析引擎异常。message 直接展示给用户，禁止携带堆栈 / 绝对路径 / 英文类型名。"""


@dataclass
class Point:
    """一个标准化轨迹点（引擎内部基本单位）。

    字段全部为规整后的值：时间转纪元秒、经纬度转 float、其余全部转字符串。
    """

    ts: int                     # 上报时间（本地时区纪元秒）
    lon: float                  # 经度
    lat: float                  # 纬度
    usernum: str = ""           # 号码（缺失时为空串，由 adapter 兜底为占位名）
    address: str = ""           # 基站 / 逆地理地址
    lai: str = ""               # 位置区码
    ci: str = ""                # 小区标识
    spcode: str = ""            # 运营商 / 局码（本引擎不参与判定，仅随点透传）
    cell: str = ""              # 小区键（LAI-CI，缺失时退化为地址或坐标指纹）
    clu: str = ""               # 地点簇 ID（cluster 阶段填充）
    clu_lon: float = 0.0        # 所属簇质心经度
    clu_lat: float = 0.0        # 所属簇质心纬度

    def path_lon(self) -> float:
        """参与几何判定的经度：有簇质心用簇质心，否则用原始坐标。"""
        return self.clu_lon if self.clu else self.lon

    def path_lat(self) -> float:
        return self.clu_lat if self.clu else self.lat


@dataclass
class Dataset:
    """引擎输入：一次分析所需的全部规范化数据。"""

    users: Dict[str, List[Point]] = field(default_factory=dict)   # 号码 → 点序列（时间升序、已去重）
    raw_points: List[Point] = field(default_factory=list)         # 清洗后**未去重**的点（按号码、时间排序）
    all_usernums: List[str] = field(default_factory=list)         # 原始文件出现的号码全集（含被清洗空的）
    raw_rows: int = 0                                             # 读入数据行数（不含表头）
    valid_rows: int = 0                                           # 通过清洗的行数
    drop_stats: Dict[str, int] = field(default_factory=dict)      # 丢弃原因 → 行数
    n_cells: int = 0                                              # 小区数（基于清洗后未去重的行）
    n_addresses: int = 0                                          # 地址数
    median_interval_s: float = 0.0                                # 采样间隔中位数（秒，含 0 间隔）
    schema: Dict[str, Any] = field(default_factory=dict)          # 列映射结果（自检信息）
    warnings: List[str] = field(default_factory=list)


@dataclass
class ClusterInfo:
    """一个地点簇的属性（cluster 阶段产出）。"""

    clu: str
    lon: float
    lat: float
    address: str = ""
    n: int = 0                                                  # 上报次数
    n_cell: int = 0                                             # 涉及小区数
    jitter_m: float = 0.0                                       # 簇内点到质心的最大距离


@dataclass
class Stay:
    """一个停留点。"""

    usernum: str
    i0: int
    i1: int
    start_ts: int
    end_ts: int
    minutes: float
    lon: float
    lat: float
    address: str = ""
    clu: str = ""
    n_points: int = 0
    jitter_m: float = 0.0


@dataclass
class Trip:
    """一段出行（或位置变动）。"""

    usernum: str
    start_ts: int
    end_ts: int
    minutes: float
    net_m: float
    cum_m: float
    straightness: float
    revisit: int
    speed_kmh: float
    kind: str                                                   # 有效出行 / 位置变动
    confidence: str                                             # 高 / 中
    from_addr: str = ""
    to_addr: str = ""
    from_lon: float = 0.0
    from_lat: float = 0.0
    to_lon: float = 0.0
    to_lat: float = 0.0
    n_points: int = 0


@dataclass
class Entry:
    """报告条目（停留 / 出行 / 无数据 统一形态，按时间排序后即报告正文）。"""

    usernum: str
    start_ts: int
    end_ts: int
    minutes: float
    kind: str
    confidence: str = ""
    from_addr: str = ""
    to_addr: str = ""
    net_m: float = 0.0
    speed_kmh: float = 0.0
    straightness: Optional[float] = None
    from_lon: Optional[float] = None
    from_lat: Optional[float] = None
    to_lon: Optional[float] = None
    to_lat: Optional[float] = None


# 报告条目类型常量（文案与判定共用，避免各处硬编码字符串）
KIND_STAY = "停留"
KIND_TRIP = "有效出行"
KIND_SHIFT = "位置变动"
KIND_GAP = "无数据"
