#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""onnx2rknn.py —— 把 yolov5 导出的 ONNX 转成板卡（RK3576J）能跑的 .rknn

================================================================================
 ★ 这个脚本只能在 **x86_64 Linux / WSL2** 里跑 ★
   rknn-toolkit2 官方只发 **x86_64 的 manylinux 轮子**：
     · 没有 Windows 版（Windows 侧装不上，也不要在 Windows 的 Python 里装）
     · 没有 aarch64 版（板卡上装的是 rknn_toolkit_lite2，那是**推理**用的，不能转换）
   环境怎么搭（含两个必踩坑：`onnx==1.16.1` / pip 用腾讯云镜像）见
     ai/train/README_wsl_rknn.md
================================================================================
 ★ 版本必须和板卡 runtime 对齐 ★
   本仓库板卡侧是 `rknn_toolkit_lite2` **2.3.0**（见 rk3576/rk_pcb_view.py 的提示文本），
   所以转换工具固定用 `rknn-toolkit2==2.3.0`。
   模型版本 > 板卡 runtime 版本时 `load_rknn` 会直接失败 —— 要升就两边一起升。

--------------------------------------------------------------------------------
流水线位置（"训练 → 板上能跑" 就两条命令）：

    # ① yolov5 导出 ONNX（yolov5 仓库里的 export.py，**不是**本脚本的职责）
    python export.py --weights best.pt --include onnx --opset 12 --imgsz 640 640

    # ② 本脚本：ONNX -> RKNN（INT8 量化）
    python ai/export/onnx2rknn.py --onnx best.onnx --out pcb_v1.rknn \
        --classes ai/dataset/classes.txt \
        --calib ai/dataset/pcb_v1/images/train --calib-num 200 --quant

    # 只想先验证"能不能转"（不量化，不需要校准集）：
    python ai/export/onnx2rknn.py --onnx best.onnx --out best_fp16.rknn --no-quant

    # 本机自检（不需要板卡/不需要数据集，手工造一个最小 ONNX 跑通全链路）：
    python ai/export/onnx2rknn.py --selftest

--------------------------------------------------------------------------------
校准集规则（很重要，别踩）：
    · 只从 `--calib` **这一层目录**取图，**不递归** —— 必须传 .../images/train，
      绝不能传 .../images（否则 val 的图会漏进校准集，量化精度评估就不干净了）
    · 按文件名排序，取前 `--calib-num` 张
    · `--dataset-txt` 给了就用它，不再自动生成

退出码：0 成功；2 参数/环境错误；3 转换失败
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time

# ---------------------------------------------------------------------------
# 常量 / 提示文案
# ---------------------------------------------------------------------------
TOOLKIT_VERSION = "2.3.0"          # 与板卡 rknn_toolkit_lite2 对齐
TOOLKIT_HINT = "ai/train/README_wsl_rknn.md"
ONNX_PIN = "onnx==1.16.1"
ONNX_PIN_HINT = "pip install %s" % ONNX_PIN
IMG_EXTS = (".jpg", ".jpeg", ".png", ".bmp")
MARK_SHAPES = "输出形状"            # 自检要 grep 的标记，别改字面量

# rknn-toolkit2 2.3.0 的合法取值（实测，见 ai/train/README_wsl_rknn.md §6）
LEGAL_TARGETS = ("rk3566", "rk3568", "rk3588", "rv1103", "rv1106",
                 "rk3562", "rk3576", "rv1103b", "rv1106b", "rk2118")


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def log(tag, msg):
    """结构化一行日志。tag 用固定集合，方便 grep: ENV/INPUT/SHAPES/CALIB/TIMING/RESULT/OK/WARN/ERR"""
    print("[%s] %s" % (tag, msg), flush=True)


def die(msg, hint=None, code=2):
    log("ERR", msg)
    if hint:
        for line in hint.strip().splitlines():
            log("ERR", "  -> " + line.strip())
    raise SystemExit(code)


# ---------------------------------------------------------------------------
# 环境检查
# ---------------------------------------------------------------------------
def check_platform():
    """rknn-toolkit2 只有 x86_64 Linux 轮子；Windows / aarch64 直接给可操作提示。"""
    mach = platform.machine().lower()
    sysname = platform.system()
    log("ENV", "python           : %s (%s %s)" % (platform.python_version(), sysname, mach))
    if sysname != "Linux":
        die("onnx2rknn.py 只能在 x86_64 Linux / WSL2 里跑，当前是 %s" % sysname,
            "rknn-toolkit2 官方只发 x86_64 manylinux 轮子，没有 Windows 版。\n"
            "请在 WSL 里跑：wsl -d Ubuntu-22.04\n"
            "环境搭建见 %s" % TOOLKIT_HINT)
    if mach not in ("x86_64", "amd64"):
        die("当前架构是 %s，rknn-toolkit2 没有 aarch64/arm 轮子" % mach,
            "板卡上的 rknn_toolkit_lite2 只能做**推理**，不能转换。\n"
            "转换请在 x86_64 Linux / WSL2 上做，见 %s" % TOOLKIT_HINT)


def import_rknn():
    """导入 rknn.api.RKNN；失败时给可操作提示。"""
    try:
        from rknn.api import RKNN  # noqa: F401
        return RKNN
    except Exception as exc:  # ImportError / 依赖坏了都走这里
        die("导入 rknn-toolkit2 失败：%s: %s" % (type(exc).__name__, exc),
            "1) 确认在 venv 里并且装的是 2.3.0：\n"
            "     python -m pip show rknn-toolkit2\n"
            "2) 没装就装（pip 必须用腾讯云镜像，阿里云拉大轮子会卡死）：\n"
            "     python -m pip config set global.index-url https://mirrors.cloud.tencent.com/pypi/simple\n"
            "     python -m pip install rknn-toolkit2==%s\n"
            "3) 完整环境与坑见 %s" % (TOOLKIT_VERSION, TOOLKIT_HINT))


def toolkit_version():
    """尽量拿到 rknn-toolkit2 版本号（不依赖 importlib.metadata 之外的私有 API）。"""
    try:
        import importlib.metadata as md
        return md.version("rknn-toolkit2")
    except Exception:
        try:
            from rknn.api import RKNN            # noqa: F401
            import rknn as _rknn
            return getattr(_rknn, "__version__", "unknown")
        except Exception:
            return "unknown"


def check_onnx():
    """onnx 版本是这条链路上最坑的一环：onnx>=1.17 删了 onnx.mapping，rknn 2.3.0 会崩。"""
    try:
        import onnx
    except Exception as exc:
        die("导入 onnx 失败：%s: %s" % (type(exc).__name__, exc),
            "在 venv 里装：%s" % ONNX_PIN_HINT)
    ver = getattr(onnx, "__version__", "unknown")
    has_map = hasattr(onnx, "mapping")
    log("ENV", "onnx             : %s (onnx.mapping=%s)" % (ver, has_map))
    if not has_map:
        die("onnx %s 太新：onnx>=1.17 删掉了 onnx.mapping，"
            "rknn-toolkit2 的 load_onnx 会直接报 AttributeError" % ver,
            "把 onnx 钉回 1.16.1（rknn-toolkit2 的依赖写了 onnx>=1.10 没有上界，"
            "所以 pip 会装最新的）：\n"
            "  %s\n"
            "验证：python -c \"import onnx; print(hasattr(onnx,'mapping'))\"  # 要 True" % ONNX_PIN_HINT)
    return onnx, ver


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def parse_vec(text, name, n=3):
    """'0,0,0' / '0 0 0' / '128' -> [int,...]，长度 1 或 n。"""
    raw = [p for p in re.split(r"[\s,;]+", str(text).strip()) if p]
    if not raw:
        die("%s 解析失败：空字符串" % name, "示例：--%s 0,0,0" % name)
    try:
        vals = [int(float(p)) for p in raw]
    except ValueError:
        die("%s 解析失败：%r" % (name, text), "只接受数字，示例：--%s 0,0,0" % name)
    if len(vals) == 1:
        vals = vals * n
    if len(vals) != n:
        die("%s 需要 1 个或 %d 个数，实际 %d 个：%r" % (name, n, len(vals), text),
            "示例：--%s 0,0,0" % name)
    return vals


def read_classes(path):
    """读 classes.txt：跳过 # 注释与空行，行号=类别 id（去 BOM）。返回 list。"""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            names = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
    except OSError as exc:
        die("读不到类别表 %s：%s" % (path, exc), "示例：--classes ai/dataset/classes.txt")
    if not names:
        die("类别表是空的：%s" % path, "一行一个类名，行号=类别 id")
    return names


def list_calib_images(calib_dir, num):
    """**只取这一层目录**的图片（不递归），按文件名排序取前 num 张。

    返回 (全部图, 选中图)。
    """
    if not os.path.isdir(calib_dir):
        die("校准集目录不存在：%s" % calib_dir,
            "应该指向 **训练集图片目录**，例如 ai/dataset/pcb_v1/images/train")
    paths = sorted(
        os.path.join(calib_dir, fn) for fn in os.listdir(calib_dir)
        if fn.lower().endswith(IMG_EXTS) and os.path.isfile(os.path.join(calib_dir, fn))
    )
    if not paths:
        die("校准集目录里没有图片（%s）：%s" % (",".join(IMG_EXTS), calib_dir),
            "先跑 tools/make_yolo_dataset.py 生成数据集，或用 --dataset-txt 直接指定清单")
    return paths, paths[:max(1, int(num))]


def write_dataset_txt(images, txt_path):
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(images) + "\n")
    return txt_path


def onnx_io_shapes(onnx_mod, onnx_path):
    """ONNX 侧的形状（后备手段：万一 toolkit 的 inputs_meta 拿不到）。"""
    try:
        model = onnx_mod.load(onnx_path)
        model = onnx_mod.shape_inference.infer_shapes(model)
        def _one(v):
            tt = v.type.tensor_type
            dims = [(d.dim_value if d.HasField("dim_value") else (d.dim_param or "?"))
                    for d in tt.shape.dim]
            return (v.name, dims, onnx_mod.TensorProto.DataType.Name(tt.elem_type))
        return ([_one(v) for v in model.graph.input],
                [_one(v) for v in model.graph.output])
    except Exception as exc:
        log("WARN", "ONNX 侧解析形状失败（不影响转换）：%s" % exc)
        return ([], [])


def rknn_io_shapes(rknn_obj):
    """从 rknn-toolkit2 内部 inputs_meta 取形状（load_onnx 之后才有）。

    实测（2.3.0）真实结构是 **两层** 的：`inputs_meta = {'attrs': {名字: {...}}}`，
    里层每个 dict 才有 shape / is_output / dtype。别直接迭代外层，否则会把 'attrs'
    当成一个"输入"、输出数变 0（这个坑踩过，自检里加了断言防回归）。
    """
    meta = getattr(getattr(rknn_obj, "rknn_base", None), "inputs_meta", None)
    if not isinstance(meta, dict):
        return None
    inner = meta.get("attrs", meta)          # 兼容一层/两层两种结构
    if not isinstance(inner, dict):
        return None
    ins, outs = [], []
    for name, info in inner.items():
        if not isinstance(info, dict) or "shape" not in info:
            continue
        item = (name, list(info.get("shape") or []), str(info.get("dtype", "?")))
        (outs if info.get("is_output") else ins).append(item)
    ins.sort(key=lambda t: t[0])
    outs.sort(key=lambda t: t[0])
    return (ins, outs)


def judge_output_channels(outs, nc):
    """判定输出通道数对不对。

    yolov5 的检测头在不同导出参数下，通道维落在不同位置：
        [1, 25200, 3*(5+nc)]   扁平化（--include onnx 默认，最常见）
        [1, 3*(5+nc), 80, 80]  NCHW 形态
        [1, 3, 80, 80, 5+nc]   多头 anchor 形态（**只有这一种看最后一维**）
    所以：
        · 3*(5+nc) 出现在**任意轴**  -> OK（打印轴号）
        · 否则最后一维 == 5+nc      -> OK（多头 anchor 形态）
        · 其余                      -> CHECK
    注意别把 "5+nc 出现在别的轴" 也算 OK —— nc=3 时 5+nc=8 是很常见的特征图尺寸，
    那样会把任意 [1,C,8,8] 都误判成合格（踩过）。
    返回 (lines, ok_flag)
    """
    want_all = 3 * (5 + nc)
    want_one = 5 + nc
    lines, ok = [], True
    for name, shape, dtype in outs:
        dims = [d for d in shape if isinstance(d, int)]
        hit_all = [i for i, d in enumerate(shape) if d == want_all]
        if hit_all:
            verdict = "OK   通道维 %d == 3*(5+nc) 在 axis %d" % (want_all, hit_all[0])
        elif shape and shape[-1] == want_one:
            verdict = ("OK   最后一维 %d == 5+nc（多头 anchor 形态，3 个 anchor 在别的轴）"
                       % want_one)
        elif not dims or max(dims) < want_one:
            verdict = ("CHECK 所有维都 < 5+nc=%d，不像 yolov5 检测输出"
                       "（是不是导出时把 head 裁掉/加了后处理？）" % want_one)
            ok = False
        else:
            verdict = ("CHECK 找不到 %d (=3*(5+nc))，最后一维也 != %d (=5+nc)"
                       " —— nc/类别表或导出参数可能不对" % (want_all, want_one))
            ok = False
        lines.append("out %-24s shape=%-22s %-8s %s" % (name, shape, dtype, verdict))
    return lines, ok


# ---------------------------------------------------------------------------
# 转换主流程
# ---------------------------------------------------------------------------
def convert(args):
    RKNN = import_rknn()
    onnx_mod, onnx_ver = check_onnx()

    # ---- 参数归一化 ----
    onnx_path = os.path.abspath(args.onnx)
    if not os.path.isfile(onnx_path):
        die("ONNX 文件不存在：%s" % onnx_path,
            "先用 yolov5 的 export.py 导出：\n"
            "  python export.py --weights best.pt --include onnx --opset 12 --imgsz 640 640")
    onnx_size = os.path.getsize(onnx_path)
    if onnx_size <= 0:
        die("ONNX 文件是空的：%s" % onnx_path)

    out_path = os.path.abspath(args.out or (os.path.splitext(onnx_path)[0] + ".rknn"))
    if not out_path.lower().endswith(".rknn"):
        out_path += ".rknn"

    mean = parse_vec(args.mean, "mean")
    std = parse_vec(args.std, "std")
    do_quant = bool(args.quant) and not args.no_quant

    classes = read_classes(args.classes) if args.classes else None
    nc = len(classes) if classes else None

    # ---- 校准集 ----
    dataset_txt = None
    tmpdir = None
    if do_quant:
        if args.dataset_txt:
            dataset_txt = os.path.abspath(args.dataset_txt)
            if not os.path.isfile(dataset_txt):
                die("--dataset-txt 不存在：%s" % dataset_txt,
                    "不给 --dataset-txt 时脚本会按 --calib 目录自动生成一个")
            log("CALIB", "使用给定的 dataset txt：%s" % dataset_txt)
            if args.calib:
                log("WARN", "同时给了 --calib 和 --dataset-txt，按规则用后者，--calib 被忽略")
        elif args.calib:
            calib_dir = os.path.abspath(args.calib)
            low = calib_dir.replace("\\", "/").lower()
            if low.endswith("/val") or "/val/" in low or low.endswith("/val/"):
                die("--calib 指向了 **验证集**：%s" % calib_dir,
                    "校准集只能用训练图（否则量化精度评估不干净）：\n"
                    "  --calib ai/dataset/pcb_v1/images/train")
            images, selected = list_calib_images(calib_dir, args.calib_num)
            tmpdir = tempfile.mkdtemp(prefix="onnx2rknn-calib-")
            dataset_txt = write_dataset_txt(selected, os.path.join(tmpdir, "dataset.txt"))
            log("CALIB", "校准集目录：%s（只取这一层，不递归）" % calib_dir)
            log("CALIB", "发现 %d 张图，按文件名排序取前 %d 张" % (len(images), len(selected)))
            log("CALIB", "  first: %s" % os.path.basename(selected[0]))
            log("CALIB", "  last : %s" % os.path.basename(selected[-1]))
            log("CALIB", "dataset txt：%s" % dataset_txt)
        else:
            die("--quant 需要校准集：请给 --calib <训练图目录> 或 --dataset-txt <清单>",
                "只想先验证能不能转换（不量化）就加 --no-quant")

    # ---- 打印环境 ----
    log("ENV", "rknn-toolkit2    : %s（板卡 runtime 应为 %s）" % (toolkit_version(), TOOLKIT_VERSION))
    log("ENV", "target_platform  : %s" % args.target)
    log("ENV", "quantization     : %s" % ("INT8 (w8a8) + 校准集" if do_quant else "FP16（不量化）"))
    log("ENV", "mean / std       : %s / %s" % (mean, std))
    log("ENV", "optimize level   : %s" % args.optimize)
    log("ENV", "onnx             : %s (%d B)" % (onnx_path, onnx_size))
    log("ENV", "输出             : %s" % out_path)
    if classes:
        log("ENV", "类别表           : %d 类 %s" % (nc, classes))

    t_all = time.time()
    rknn = RKNN(verbose=False)

    # ---- config ----
    t = time.time()
    try:
        ret = rknn.config(target_platform=args.target,
                          mean_values=[mean], std_values=[std],
                          optimization_level=int(args.optimize),
                          quantized_dtype="w8a8")
    except SystemExit:
        raise
    except Exception as exc:
        msg = str(exc)
        if "target_platform" in msg and args.target not in LEGAL_TARGETS:
            die("target_platform '%s' 不合法" % args.target,
                "合法值（rknn-toolkit2 2.3.0 实测）：%s\n详见 %s §6"
                % (", ".join(LEGAL_TARGETS), TOOLKIT_HINT))
        die("rknn.config() 异常：%s" % msg,
            "合法 target：%s\n详见 %s §6" % (", ".join(LEGAL_TARGETS), TOOLKIT_HINT))
    log("TIMING", "config      ret=%s  %.2fs" % (ret, time.time() - t))
    if ret != 0:
        die("rknn.config() 返回 %s" % ret, "合法 target：%s" % ", ".join(LEGAL_TARGETS), code=3)

    # ---- load_onnx ----
    t = time.time()
    try:
        ret = rknn.load_onnx(model=onnx_path)
    except Exception as exc:
        msg = str(exc)
        if "mapping" in msg:
            die("load_onnx 报 onnx.mapping 缺失：%s" % msg,
                "onnx 版本太新，钉回 1.16.1：\n  %s" % ONNX_PIN_HINT)
        die("rknn.load_onnx() 异常：%s" % msg, "见 %s §5" % TOOLKIT_HINT)
    log("TIMING", "load_onnx   ret=%s  %.2fs" % (ret, time.time() - t))
    if ret != 0:
        die("rknn.load_onnx() 返回 %s" % ret,
            "常见原因：opset 太新/含不支持算子。用 --opset 12 重新导出，"
            "或 `python -m onnxsim` 简化后再试", code=3)

    # ---- 形状打印（关键：一眼确认输出通道数）----
    io = rknn_io_shapes(rknn)
    if io:
        ins, outs = io
        log("SHAPES", "%s（来自 rknn-toolkit2 inputs_meta）: %d 输入 / %d 输出"
            % (MARK_SHAPES, len(ins), len(outs)))
    else:                                     # 后备：从 onnx 文件解析
        ins, outs = onnx_io_shapes(onnx_mod, onnx_path)
        log("SHAPES", "%s（来自 ONNX 图解析，toolkit 内部表未取到）: %d 输入 / %d 输出"
            % (MARK_SHAPES, len(ins), len(outs)))
    for name, shape, dtype in ins:
        log("SHAPES", "in  %-24s shape=%-22s %s" % (name, shape, dtype))
    if nc:
        log("SHAPES", "校验：输出必须有一维 = 3*(5+nc) = %d 或 5+nc = %d  （nc=%d，3 类 -> 24）"
            % (3 * (5 + nc), 5 + nc, nc))
        lines, ok = judge_output_channels(outs, nc)
        for ln in lines:
            log("SHAPES", ln)
        if not ok:
            log("WARN", "输出形状和 yolov5 检测头对不上 —— 板卡侧后处理会解析失败，先查 nc/导出参数")
        elif not outs:
            log("WARN", "没解析到任何输出张量（toolkit 内部表结构可能变了），请看上面的 shape 行")
    else:
        for name, shape, dtype in outs:
            log("SHAPES", "out %-24s shape=%-22s %s" % (name, shape, dtype))
        log("WARN", "没给 --classes，跳过 3*(5+nc) 通道校验（建议给上，这是最容易错的地方）")

    # ---- build ----
    t = time.time()
    try:
        ret = rknn.build(do_quantization=do_quant, dataset=dataset_txt)
    except Exception as exc:
        die("rknn.build() 异常：%s" % exc,
            "先跑 --selftest 确认环境本身没问题；再看 %s §5" % TOOLKIT_HINT, code=3)
    dt_build = time.time() - t
    log("TIMING", "build       ret=%s  %.2fs" % (ret, dt_build))
    if ret != 0:
        die("rknn.build() 返回 %s" % ret,
            "量化失败常见原因：校准图片数与输入尺寸不符 / 校准集有坏图。"
            "可先 --no-quant 验证链路", code=3)

    # ---- export ----
    t = time.time()
    try:
        ret = rknn.export_rknn(out_path)
    except Exception as exc:
        die("rknn.export_rknn() 异常：%s" % exc, "检查输出目录是否有写权限", code=3)
    log("TIMING", "export_rknn ret=%s  %.2fs" % (ret, time.time() - t))
    if ret != 0:
        die("rknn.export_rknn() 返回 %s" % ret, "检查输出目录：%s" % os.path.dirname(out_path),
            code=3)

    try:
        rknn.release()
    except Exception:
        pass

    # ---- 结果 ----
    if not os.path.isfile(out_path):
        die("build/export 都返回 0，但产物不存在：%s" % out_path, code=3)
    size = os.path.getsize(out_path)
    with open(out_path, "rb") as f:
        magic = f.read(4)
    log("RESULT", "产物：%s" % out_path)
    log("RESULT", "大小：%d B (%.2f MB)   magic=%s (%s)"
        % (size, size / (1024.0 * 1024.0), magic.decode("latin-1"),
           "RKNN OK" if magic == b"RKNN" else "异常！不是 RKNN 文件"))
    log("RESULT", "build %.2fs / 全程 %.2fs" % (dt_build, time.time() - t_all))
    if magic != b"RKNN":
        die("产物 magic 不是 RKNN，文件不对", code=3)

    if tmpdir:
        shutil.rmtree(tmpdir, ignore_errors=True)

    log("OK", "转换完成（%s）" % ("INT8 量化" if do_quant else "FP16"))
    log("OK", "拷到板卡后：python3 rk_pcb_view.py --model %s --classes classes.txt"
        % os.path.basename(out_path))
    return 0


# ---------------------------------------------------------------------------
# 自检：手工造最小 ONNX（不用 torch / 不用数据集），把整条链路走一遍
# ---------------------------------------------------------------------------
def build_selftest_onnx(path, nc=3):
    """造一个"结构像 yolov5 检测头"的最小 ONNX：
       输入 [1,3,64,64]，两个输出最后一维都等于 3*(5+nc)，用来验证通道校验那行也走到。
    """
    import numpy as np
    import onnx
    from onnx import helper, TensorProto, numpy_helper

    rng = np.random.default_rng(20261023)
    want = 3 * (5 + nc)                      # nc=3 -> 24

    def w(shape, name):
        return numpy_helper.from_array((rng.standard_normal(shape) * 0.1).astype(np.float32), name)

    inits = [w([want, 3, 3, 3], "wa"), w([want, 3, 3, 3], "wb")]
    nodes = [
        # 两支卷积 + Reshape：输出 [1,64,24] / [1,256,24]，最后一维都是 3*(5+nc)
        helper.make_node("Conv", ["images", "wa"], ["ca"], kernel_shape=[3, 3],
                         pads=[1, 1, 1, 1], strides=[8, 8], name="headA_conv"),
        helper.make_node("Reshape", ["ca", "sa"], ["outA"], name="headA_flat"),
        helper.make_node("Conv", ["images", "wb"], ["cb"], kernel_shape=[3, 3],
                         pads=[1, 1, 1, 1], strides=[4, 4], name="headB_conv"),
        helper.make_node("Reshape", ["cb", "sb"], ["outB"], name="headB_flat"),
    ]
    inits += [
        numpy_helper.from_array(np.array([1, 64, want], np.int64), "sa"),
        numpy_helper.from_array(np.array([1, 256, want], np.int64), "sb"),
    ]
    graph = helper.make_graph(
        nodes, "onnx2rknn_selftest",
        [helper.make_tensor_value_info("images", TensorProto.FLOAT, [1, 3, 64, 64])],
        [helper.make_tensor_value_info("outA", TensorProto.FLOAT, [1, 64, want]),
         helper.make_tensor_value_info("outB", TensorProto.FLOAT, [1, 256, want])],
        initializer=inits)
    model = helper.make_model(graph, producer_name="onnx2rknn-selftest",
                             opset_imports=[helper.make_opsetid("", 12)])
    model.ir_version = 8
    onnx.checker.check_model(model)
    onnx.save(model, path)
    return path


def make_random_jpegs(out_dir, n=8, size=64):
    """8 张随机 JPEG 当校准集（不碰用户的真实数据）。优先 cv2（rknn 的硬依赖），退 PIL。"""
    os.makedirs(out_dir, exist_ok=True)
    try:
        import numpy as np
        import cv2
        rng = np.random.default_rng(7)
        paths = []
        for i in range(n):
            img = rng.integers(0, 256, (size, size, 3), dtype=np.uint8)
            p = os.path.join(out_dir, "cal_%02d.jpg" % i)
            cv2.imwrite(p, img)
            paths.append(p)
        return paths
    except Exception:
        from PIL import Image
        import random
        random.seed(7)
        paths = []
        for i in range(n):
            img = Image.new("RGB", (size, size))
            img.putdata([(random.randrange(256), random.randrange(256), random.randrange(256))
                         for _ in range(size * size)])
            p = os.path.join(out_dir, "cal_%02d.jpg" % i)
            img.save(p, quality=90)
            paths.append(p)
        return paths


def selftest():
    """在本机把 CLI 完整跑两遍（FP16 / INT8），逐项断言。"""
    items = []          # (ok: bool, 描述, 细节)

    def chk(ok, desc, detail=""):
        detail = "" if detail is None else str(detail)
        items.append((bool(ok), desc, detail))
        log("OK" if ok else "WARN", "%s %s%s" % ("PASS" if ok else "FAIL", desc,
                                                ("  | " + detail) if detail else ""))
    log("ENV", "=== onnx2rknn self-test（不需要板卡 / 不需要数据集）===")
    check_platform()
    _, onnx_ver = check_onnx()
    chk(True, "onnx 可用且带 onnx.mapping", "onnx %s" % onnx_ver)
    import_rknn()
    tv = toolkit_version()
    chk(True, "rknn-toolkit2 可导入", "version=%s" % tv)

    work = tempfile.mkdtemp(prefix="onnx2rknn-selftest-")
    try:
        onnx_path = os.path.join(work, "dummy.onnx")
        build_selftest_onnx(onnx_path, nc=3)
        chk(os.path.getsize(onnx_path) > 0, "手工构造的 ONNX 已生成",
            "%s (%d B)" % (os.path.basename(onnx_path), os.path.getsize(onnx_path)))

        cls_txt = os.path.join(work, "classes.txt")
        with open(cls_txt, "w", encoding="utf-8") as f:
            f.write("# selftest\nscratch\nmissing\nstain\n")
        jpgs = make_random_jpegs(os.path.join(work, "calib"), 8)
        chk(len(jpgs) == 8, "生成 8 张随机 JPEG 当校准集")

        def run(extra, tag):
            cmd = [sys.executable, os.path.abspath(__file__), "--onnx", onnx_path] + extra
            t = time.time()
            p = subprocess.run(cmd, capture_output=True, text=True)
            out = (p.stdout or "") + (p.stderr or "")
            log("TIMING", "%s rc=%d  %.1fs" % (tag, p.returncode, time.time() - t))
            return p.returncode, out

        # ---- A) FP16：不量化 ----
        out_a = os.path.join(work, "fp16.rknn")
        rc, txt = run(["--out", out_a, "--classes", cls_txt, "--no-quant"], "selftest-A FP16")
        chk(rc == 0, "A) --no-quant 退出码 0", "rc=%d" % rc)
        chk(os.path.isfile(out_a) and os.path.getsize(out_a) > 0,
            "A) 产物存在且非空",
            "%d B" % (os.path.getsize(out_a) if os.path.isfile(out_a) else 0))
        magic = b""
        if os.path.isfile(out_a):
            with open(out_a, "rb") as f:
                magic = f.read(4)
        chk(magic == b"RKNN", "A) 产物 magic == 'RKNN'", repr(magic))
        chk(MARK_SHAPES in txt, "A) 日志里有'%s'那一行" % MARK_SHAPES)
        chk("3*(5+nc)" in txt, "A) 日志里做了 3*(5+nc) 通道校验",
            "期望 24 (nc=3)")
        # ↓ 这几条是防回归：toolkit 的 inputs_meta 是两层结构，解错了会"0 输出 / attrs"，
        #   但只 grep '输出形状' 是查不出来的（踩过这个坑）
        chk("2 输出" in txt, "A) 真的解析出 2 个输出张量（不是 0 个）",
            [ln for ln in txt.splitlines() if MARK_SHAPES in ln][:1])
        chk("0 输出" not in txt, "A) 没有出现'0 输出'（inputs_meta 解包正确）")
        chk("shape=[1, 64, 24]" in txt, "A) 打印出 outA 的真实形状 [1, 64, 24]")
        chk("axis 2" in txt, "A) 通道维判定指出了轴号（axis 2）")

        # ---- B) INT8：量化 + 校准集 ----
        out_b = os.path.join(work, "int8.rknn")
        rc, txt = run(["--out", out_b, "--classes", cls_txt,
                       "--calib", os.path.join(work, "calib"), "--calib-num", "8", "--quant"],
                      "selftest-B INT8")
        chk(rc == 0, "B) --quant 退出码 0", "rc=%d" % rc)
        chk(os.path.isfile(out_b) and os.path.getsize(out_b) > 0,
            "B) INT8 产物存在且非空",
            "%d B" % (os.path.getsize(out_b) if os.path.isfile(out_b) else 0))
        magic = b""
        if os.path.isfile(out_b):
            with open(out_b, "rb") as f:
                magic = f.read(4)
        chk(magic == b"RKNN", "B) INT8 产物 magic == 'RKNN'", repr(magic))
        chk(MARK_SHAPES in txt, "B) INT8 日志里有'%s'那一行" % MARK_SHAPES)
        chk("2 输出" in txt and "shape=[1, 256, 24]" in txt,
            "B) INT8 也正确解析出 2 个输出及其形状")

        # ---- C) 负向：--quant 却没有校准集，必须给可操作报错 ----
        rc, txt = run(["--out", os.path.join(work, "x.rknn"), "--quant"], "selftest-C 负向")
        chk(rc != 0, "C) --quant 缺校准集时非 0 退出", "rc=%d" % rc)
        chk("--calib" in txt and "--dataset-txt" in txt,
            "C) 报错提示了 --calib/--dataset-txt 两个可选修法")

        # ---- D) 负向：校准集指向 val，必须拒绝 ----
        val_dir = os.path.join(work, "images", "val")
        os.makedirs(val_dir, exist_ok=True)
        make_random_jpegs(val_dir, 2)
        rc, txt = run(["--out", os.path.join(work, "y.rknn"), "--classes", cls_txt,
                       "--calib", val_dir, "--quant"], "selftest-D 负向")
        chk(rc != 0 and "验证集" in txt, "D) --calib 指向 val 时被拒绝并说明原因", "rc=%d" % rc)

        # ---- E) 负向：非法 target ----
        rc, txt = run(["--out", os.path.join(work, "z.rknn"), "--no-quant",
                       "--target", "rk9999"], "selftest-E 负向")
        chk(rc != 0, "E) 非法 --target 非 0 退出", "rc=%d" % rc)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    bad = [i for i in items if not i[0]]
    print("", flush=True)
    print("onnx2rknn-selftest: %d 项, 失败 %d 项 -> %s"
          % (len(items), len(bad), "PASS" if not bad else "FAIL"), flush=True)
    if bad:
        for _ok, desc, detail in bad:
            print("    FAILED: %s %s" % (desc, detail), flush=True)
        return 1
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser():
    ap = argparse.ArgumentParser(
        prog="onnx2rknn.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="把 yolov5 的 ONNX 转成 RK3576 能跑的 .rknn（只能在 x86_64 Linux/WSL2 里跑）",
        epilog="示例：\n"
               "  python ai/export/onnx2rknn.py --onnx best.onnx --out pcb_v1.rknn \\\n"
               "      --classes ai/dataset/classes.txt \\\n"
               "      --calib ai/dataset/pcb_v1/images/train --calib-num 200 --quant\n"
               "  python ai/export/onnx2rknn.py --onnx best.onnx --out fp16.rknn --no-quant\n"
               "  python ai/export/onnx2rknn.py --selftest\n"
               "\n环境见 ai/train/README_wsl_rknn.md（rknn-toolkit2 %s + onnx==1.16.1）" % TOOLKIT_VERSION)
    ap.add_argument("--onnx", help="输入 ONNX 路径（yolov5 export.py --include onnx --opset 12 产出）")
    ap.add_argument("--out", help="输出 .rknn 路径（默认与 onnx 同名 .rknn）")
    ap.add_argument("--classes", help="classes.txt（行号=类别 id）；给了才做 3*(5+nc) 通道校验")
    ap.add_argument("--calib", help="校准集图片目录（**只取这一层**，请指向 images/train）")
    ap.add_argument("--calib-num", type=int, default=200, help="校准图片数（默认 200）")
    ap.add_argument("--quant", action="store_true", help="INT8 量化（需要 --calib 或 --dataset-txt）")
    ap.add_argument("--no-quant", action="store_true", help="不量化（FP16），用于先验证链路")
    ap.add_argument("--target", default="rk3576", help="target_platform（默认 rk3576）")
    ap.add_argument("--mean", default="0,0,0", help="mean_values，逗号分隔（默认 0,0,0）")
    ap.add_argument("--std", default="255,255,255", help="std_values，逗号分隔（默认 255,255,255）")
    ap.add_argument("--optimize", type=int, default=3, help="optimization_level 0~3（默认 3）")
    ap.add_argument("--dataset-txt", dest="dataset_txt",
                    help="现成的校准清单；不给就按 --calib 目录自动生成临时 txt")
    ap.add_argument("--selftest", action="store_true",
                    help="本机自检：手工造最小 ONNX 跑通 FP16+INT8 全链路并断言")
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    if not args.onnx:
        ap.print_help()
        print("\n[ERR] 缺 --onnx（或用 --selftest 自检）", flush=True)
        return 2
    if args.quant and args.no_quant:
        die("--quant 和 --no-quant 只能选一个")
    if args.optimize < 0 or args.optimize > 3:
        log("WARN", "--optimize %d 超出 0~3，rknn-toolkit2 会自动夹到 3" % args.optimize)
    check_platform()
    return convert(args)


if __name__ == "__main__":
    sys.exit(main())
