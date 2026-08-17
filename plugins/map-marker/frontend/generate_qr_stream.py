#!/usr/bin/env python3
"""QR-transfer 编码器：将轨迹 JSON 数据编码为动态二维码视频流。

用法：python generate_qr_stream.py <input.json> <output.mp4> [--version 20] [--fps 12]

协议与 https://github.com/Zhen-Ni/QR-transfer 的 qrtransfer.py 兼容，
生成的视频可被 qr-video-decode 插件解码。

每帧 QR 内容格式（字符串）：
  [0:4]   base64(3B) = 帧序号
  [4:8]   base64(3B) = 总帧数
  [8:12]  base64(3B) = FEC 参数 (k<<8 | m)
  [12:]   base64 编码的数据分块

视频帧排列（QR-transfer 协议）：
  总帧数 = nblocks * m
  frame[i + j*nblocks] = 第 j 组 FEC 的第 i 个数据块
  其中 nblocks = 数据块总数 / k
"""

import argparse
import base64
import json
import math
import os
import struct
import sys
import tempfile

import cv2
import numpy as np
import qrcode
import zfec


# ---------- 基础编码工具 ----------

def encode_index(n: int) -> str:
    """将整数编码为 4 字符 base64（3 字节大端序）。"""
    return base64.b64encode(n.to_bytes(3, "big")).decode("ascii")


def encode_fecinfo(k: int, m: int) -> str:
    """将 FEC 参数 (k, m) 编码为 4 字符 base64。"""
    return encode_index((k << 8) | m)


def make_frame_payload(frame_idx: int, total_frames: int, k: int, m: int,
                       chunk_b64: str) -> str:
    """拼接一帧 QR 内容字符串。"""
    return f"{encode_index(frame_idx)}{encode_index(total_frames)}{encode_fecinfo(k, m)}{chunk_b64}"


# ---------- QR 容量计算 ----------

def qr_data_capacity(version: int, ec_level: str = "L") -> int:
    """估算指定版本/纠错级别的 QR 码最大数据容量（字节）。"""
    # QR 版本与容量对照表（EC level L，字节模式）
    capacities = {
        1: 17, 2: 32, 3: 53, 4: 78, 5: 106, 6: 134, 7: 154, 8: 192,
        9: 230, 10: 271, 11: 321, 12: 367, 13: 425, 14: 458, 15: 520,
        16: 586, 17: 644, 18: 718, 19: 792, 20: 858, 21: 929, 22: 1003,
        23: 1091, 24: 1171, 25: 1273, 26: 1367, 27: 1465, 28: 1528,
        29: 1628, 30: 1732, 31: 1840, 32: 1952, 33: 2068, 34: 2188,
        35: 2303, 36: 2431, 37: 2563, 38: 2699, 39: 2809, 40: 2953,
    }
    return capacities.get(version, 100)


# ---------- 数据分块与 FEC ----------

def prepare_data(data_bytes: bytes, chunk_size: int) -> bytes:
    """在数据前加 4 字节长度前缀，然后按 chunk_size 对齐填充。"""
    data_size = len(data_bytes)
    prefixed = struct.pack(">I", data_size) + data_bytes
    padded_len = math.ceil(len(prefixed) / chunk_size) * chunk_size
    padded = prefixed.ljust(padded_len, b"\x00")
    return padded


def fec_encode(padded_data: bytes, chunk_size: int, k: int, m: int) -> list:
    """使用 zfec 编码，返回按 QR-transfer 协议排列的帧列表。

    协议排列：frame[i + j*nblocks] = 第 j 组 FEC 的第 i 个块
    """
    total_blocks = len(padded_data) // chunk_size
    # 补齐到 k 的倍数
    if total_blocks % k != 0:
        pad_count = k - (total_blocks % k)
        padded_data = padded_data + b"\x00" * (pad_count * chunk_size)
        total_blocks = len(padded_data) // chunk_size

    nblocks = total_blocks // k
    encoder = zfec.Encoder(k, m)

    # 每组独立编码
    groups_encoded = []
    for g in range(nblocks):
        group = []
        for i in range(k):
            offset = (g * k + i) * chunk_size
            group.append(padded_data[offset:offset + chunk_size])
        encoded = encoder.encode(group)
        groups_encoded.append(encoded)

    # 按协议排列：frame[idx] = groups_encoded[g][j] where idx = i + j*nblocks
    fec_frames = []
    for j in range(m):
        for i in range(nblocks):
            fec_frames.append(groups_encoded[i][j])

    return fec_frames


# ---------- QR 生成 ----------

def generate_qr_image(content: str, version: int, box_size: int = 10,
                       border: int = 2) -> any:
    """生成一个 QR 码图片，返回 PIL Image 对象。"""
    qr = qrcode.QRCode(
        version=version,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=box_size,
        border=border,
    )
    qr.add_data(content)
    qr.make(fit=False)
    img = qr.make_image(fill_color="black", back_color="white")
    return img


def pil_to_cv2(pil_img) -> np.ndarray:
    """将 PIL Image 转换为 OpenCV 格式（BGR）。"""
    arr = np.array(pil_img.convert("RGB"))
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


# ---------- 主流程 ----------

def encode_to_video(input_path: str, output_path: str, version: int = 20,
                    fps: int = 12, box_size: int = 10, border: int = 2):
    """读取 JSON 文件，生成 QR 视频流。"""
    # 读取输入数据
    with open(input_path, "r", encoding="utf-8") as f:
        raw = f.read()
    data_bytes = raw.encode("utf-8")
    print(f"输入数据: {len(data_bytes)} 字节 ({len(raw)} 字符)")

    # 计算 QR 码容量
    capacity = qr_data_capacity(version, "L")
    # 帧头固定开销: 4+4+4 = 12 字符 base64 = 9 字节
    header_bytes = 9
    max_payload_b64 = capacity - 12  # 帧头占 12 字符
    # base64 编码: chunk_size 字节 → chunk_size * 4/3 字符
    # 所以 chunk_size = max_payload_b64 * 3/4
    chunk_size = int(max_payload_b64 * 3 / 4) - 4  # 留一点余量
    print(f"QR 版本 {version} 容量: {capacity} 字节, chunk_size: {chunk_size} 字节")

    # 准备数据
    padded = prepare_data(data_bytes, chunk_size)
    total_data_blocks = len(padded) // chunk_size
    print(f"填充后: {len(padded)} 字节, {total_data_blocks} 个数据块")

    # FEC 参数
    # 选择 k 和 m 使得: nblocks = total_data_blocks / k 是整数
    # 且总帧数 nblocks * m 合理
    def find_k(total):
        """找到合适的 k，使得 total % k == 0 且 nblocks 不太大。"""
        best_k = 1
        for candidate in range(1, total + 1):
            if total % candidate == 0:
                nblocks = total // candidate
                # 目标: nblocks 在 100-500 之间
                if 50 <= nblocks <= 500:
                    return candidate
                if best_k == 1 or abs(nblocks - 200) < abs(total // best_k - 200):
                    best_k = candidate
        return best_k

    k = find_k(total_data_blocks)
    nblocks = total_data_blocks // k
    # m = k + FEC 冗余块数
    # 目标: m 约为 k 的 1.5-2 倍（50%-100% 冗余）
    m = k + max(1, k // 2)  # 50% 冗余
    # 确保 m >= k + 1
    if m <= k:
        m = k + 1

    total_frames = nblocks * m
    print(f"FEC: k={k}, m={m}, nblocks={nblocks}, 总帧数={total_frames}")

    # 生成 FEC 块
    fec_blocks = fec_encode(padded, chunk_size, k, m)
    assert len(fec_blocks) == total_frames, \
        f"帧数不匹配: 期望 {total_frames}, 实际 {len(fec_blocks)}"

    # QR 码尺寸
    qr_img = generate_qr_image(
        make_frame_payload(0, total_frames, k, m,
                           base64.b64encode(fec_blocks[0]).decode("ascii")),
        version, box_size, border)
    qr_w, qr_h = qr_img.size
    margin = 20
    frame_w = qr_w + margin * 2
    frame_h = qr_h + margin * 2
    print(f"QR 尺寸: {qr_w}x{qr_h}, 帧尺寸: {frame_w}x{frame_h}")

    # 生成视频
    with tempfile.TemporaryDirectory() as tmpdir:
        print(f"正在生成 {total_frames} 帧...")
        for idx, block in enumerate(fec_blocks):
            chunk_b64 = base64.b64encode(block).decode("ascii")
            content = make_frame_payload(idx, total_frames, k, m, chunk_b64)
            img = generate_qr_image(content, version, box_size, border)
            cv2_img = pil_to_cv2(img)

            canvas = np.ones((frame_h, frame_w, 3), dtype=np.uint8) * 255
            y_off = (frame_h - qr_h) // 2
            x_off = (frame_w - qr_w) // 2
            canvas[y_off:y_off + qr_h, x_off:x_off + qr_w] = cv2_img

            frame_path = os.path.join(tmpdir, f"frame_{idx:06d}.png")
            cv2.imwrite(frame_path, canvas)

            if (idx + 1) % 100 == 0 or idx == total_frames - 1:
                print(f"  {idx + 1}/{total_frames}")

        print(f"正在合成视频...")
        # 使用 imageio-ffmpeg 的 ffmpeg 二进制合成 H.264 视频
        import subprocess
        import imageio_ffmpeg
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        input_pattern = os.path.join(tmpdir, "frame_%06d.png")
        cmd = [
            ffmpeg_exe, "-y",
            "-framerate", str(fps),
            "-i", input_pattern,
            "-c:v", "libx264",
            "-preset", "slow",
            "-crf", "28",
            "-pix_fmt", "yuv420p",
            output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"ffmpeg 错误: {result.stderr}", file=sys.stderr)
            # 回退到 OpenCV
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer = cv2.VideoWriter(output_path, fourcc, fps, (frame_w, frame_h))
            for idx in range(total_frames):
                frame_path = os.path.join(tmpdir, f"frame_{idx:06d}.png")
                frame = cv2.imread(frame_path)
                if frame is not None:
                    writer.write(frame)
            writer.release()
        size_kb = os.path.getsize(output_path) / 1024
        duration = total_frames / fps
        print(f"完成: {output_path}")
        print(f"  大小: {size_kb:.0f} KB, 时长: {duration:.1f}s, {total_frames} 帧 @ {fps}fps")


def main():
    parser = argparse.ArgumentParser(
        description="将轨迹 JSON 编码为 QR-transfer 格式的动态二维码视频流")
    parser.add_argument("input", help="输入 JSON 文件路径")
    parser.add_argument("output", help="输出 MP4 文件路径")
    parser.add_argument("--version", type=int, default=20, help="QR 码版本号 (默认: 20)")
    parser.add_argument("--fps", type=int, default=12, help="视频帧率 (默认: 12)")
    parser.add_argument("--box-size", type=int, default=10, help="QR 模块像素大小 (默认: 10)")
    parser.add_argument("--border", type=int, default=2, help="QR 边距模块数 (默认: 2)")
    args = parser.parse_args()

    if not os.path.isfile(args.input):
        print(f"错误: 输入文件不存在: {args.input}", file=sys.stderr)
        sys.exit(1)

    encode_to_video(args.input, args.output, version=args.version, fps=args.fps,
                    box_size=args.box_size, border=args.border)


if __name__ == "__main__":
    main()
