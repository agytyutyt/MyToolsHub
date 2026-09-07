# gen_test_materials.py —— 测试素材批量生成（文档 7.1）
# 用法：把本文件放到 JZToolsHub 项目根目录运行：python gen_test_materials.py
# 依赖：JZToolsHub 插件 info-transfer 的后端（qrcode / opencv / zfec）
import os
import sys

sys.path.insert(0, r"D:\JZToolsHub")
sys.path.insert(0, r"D:\JZToolsHub\plugins\info-transfer")

from backend import routes as R  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qr_test_materials")
os.makedirs(OUT, exist_ok=True)

# 1) 单张中文文本
env = R.build_envelope("text", "会议通知", "本周五 14:00 在三楼会议室召开季度总结会，请准时参加。")
R.encode_static_output("text", "会议通知", env, 15, OUT, "case1_single")

# 2) 多页拆分（5 页）
big = "机密数据行-%d：" + "甲乙丙丁戊己庚辛壬癸" * 50 + "\n"
env2 = R.build_envelope("text", "长文示例", "".join(big % i for i in range(8)))
paths, n = R.encode_static_output("text", "长文示例", env2, 15, OUT, "case2_multi")
print("case2 页数:", n)

# 3) excel 表格（含中文/数字/空值）
env3 = R.build_envelope(
    "excel", "花名册",
    [["姓名", "部门", "备注"], ["张三", "刑侦", 1], ["李四", "网安", None]],
)
R.encode_static_output("excel", "花名册", env3, 15, OUT, "case3_excel")

# 4) markdown
env4 = R.build_envelope("markdown", "值班安排", "# 值班安排\n\n- 周一：张三\n- 周二：李四")
R.encode_static_output("markdown", "值班安排", env4, 15, OUT, "case4_md")

# 5) 二维码视频（二期用）
env5 = R.build_envelope("text", "视频示例", "视频往返验证。" * 300)
mp4 = os.path.join(OUT, "case5_video.mp4")
R.encode_to_video(env5, 15, mp4)
print("case5 视频:", mp4)

print("全部素材已生成到:", OUT)
