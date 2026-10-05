# -*- coding: utf-8 -*-
"""supervisor 进程入口：python -m supervisor --data-root <数据根> [--port 5100]。

Windows 服务化（T-O2-11d）：NSSM 包装本入口（Q9 拍板：独立安装包，主应用升级
不动 supervisor）。见 supervisor/install/install-supervisor.ps1。
"""
import argparse
import logging
import os
import sys


def main():
    ap = argparse.ArgumentParser(description="JZToolsHub 插件运行架构 · 管理进程")
    ap.add_argument("--data-root", dest="data_root", default="",
                    help="数据根目录（缺省读 JZTOOLS_DATA_ROOT 环境变量）")
    ap.add_argument("--port", dest="port", type=int, default=0,
                    help="控制接口端口（缺省读注册表 supervisor.port，再缺省 5100）")
    ap.add_argument("--log-file", dest="log_file", default="",
                    help="supervisor 自身日志文件（缺省 <数据根>\\logs\\supervisor.log）")
    args = ap.parse_args()

    data_root = args.data_root or os.environ.get("JZTOOLS_DATA_ROOT") or ""
    if not data_root:
        print("缺少数据根：--data-root 或环境变量 JZTOOLS_DATA_ROOT", file=sys.stderr)
        return 2
    data_root = os.path.abspath(data_root)

    log_file = args.log_file or os.path.join(data_root, "logs", "supervisor.log")
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    logger = logging.getLogger("jztools.supervisor")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        from logging.handlers import RotatingFileHandler
        _h = RotatingFileHandler(log_file, maxBytes=2 * 1024 * 1024,
                                 backupCount=3, encoding="utf-8")
        _h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(_h)

    def log(msg):
        logger.info(msg)
        try:
            sys.stderr.write("[supervisor] %s\n" % msg)
        except Exception:
            pass

    from supervisor import control_api
    return control_api.run(data_root, port=args.port or None, log=log)


if __name__ == "__main__":
    sys.exit(main())
