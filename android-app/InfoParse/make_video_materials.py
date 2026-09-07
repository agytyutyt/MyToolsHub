# -*- coding: utf-8 -*-
"""make_video_materials.py —— 生成二维码视频流测试素材（二期 FR-09 / TC-13）

用法: python make_video_materials.py
产物: qr_test_materials/ 下 3 个 MP4（小/中/大），每个都经桌面端解码链路往返校验。
"""
import os
import sys

sys.path.insert(0, r"D:\JZToolsHub")
sys.path.insert(0, r"D:\JZToolsHub\plugins\info-transfer")

from backend import routes as R  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "qr_test_materials")
os.makedirs(OUT, exist_ok=True)

CASES = [
    # (名称, 文本内容, 说明)
    ("video_small", "小视频测试。" * 100,          # ~1KB → ~30 帧
     "快速往返，验证基本链路"),
    ("video_medium", "机密视频数据行-%d：" % 1 + "".join(
        ("第%d行：甲乙丙丁戊己庚辛壬癸\n" % i) for i in range(200)),  # ~4KB
     "中等长度，验证进度显示"),
    ("video_large", "".join(
        ("大文件模拟-%d：" % i) + ("0123456789ABCDEF" * 20) + "\n"
        for i in range(100)),                      # ~35KB
     "长视频，验证帧收集器与耗时"),
]


def make(name, text):
    env = R.build_envelope("text", "视频-" + name, text)
    path = os.path.join(OUT, name + ".mp4")
    R.encode_to_video(env, 15, path)
    # 桌面端链路往返校验（重组结果 = 信封 JSON 字节，应与 env 完全一致）
    codes = R.decode_video_frames(path)
    raw = R.reassemble_qrtransfer(codes)
    ok = raw == env
    n = len(codes)
    size_kb = os.path.getsize(path) / 1024
    print(f"{name}.mp4  {size_kb:.0f}KB  解码 {n} 帧  往返{'一致' if ok else '不一致!!'}")
    assert ok, name + " 往返校验失败"
    return path


if __name__ == "__main__":
    for name, text, desc in CASES:
        make(name, text)
        print("   说明:", desc)
    print("\n全部视频素材已生成并校验:", OUT)
