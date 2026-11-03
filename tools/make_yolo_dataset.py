#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_yolo_dataset.py —— 把"原图 + YOLO 标注"切成训练集并生成 data.yaml

做什么：
  1) 切分：按 --val-ratio 随机（--seed 可复现）把图片分到 train/val，
     输出标准 YOLOv5 目录：images/train、images/val、labels/train、labels/val
     （用**复制**而不是硬链接 —— Windows 上硬链接经常出怪问题）
  2) 校验：每张图要有同名 .txt 标签；每行 5 列；cls 是整数且在 [0, nc)；
     cx,cy,w,h 在 [0,1]；w,h > 0；出错会列出 文件:行号
  3) 生成 data.yaml：path/train/val/nc/names，names 顺序**严格按 classes.txt 行序**
     （行号就是类别 id），所以改 classes.txt 必须重新跑本工具
  4) 统计：总图数、train/val 张数、每类框数、无目标图数、跳过数；
     某类在 train 里 < 10 个框会打 [WARN]（训练容易只学到背景）

classes.txt 格式：一行一个类名，`#` 开头是注释、空行忽略；类名只允许 [A-Za-z0-9_-]
  （YOLO → ONNX → RKNN 这条链路上非 ASCII 类名容易出问题，中文/空格会直接报错）

示例：
  python tools\\make_yolo_dataset.py --images ai\\dataset\\raw\\s01 --labels ai\\dataset\\raw\\s01\\labels ^
      --classes ai\\dataset\\classes.txt --out ai\\dataset\\pcb_v1 --val-ratio 0.15 --seed 0
  python tools\\make_yolo_dataset.py --check --dataset ai\\dataset\\pcb_v1
  python tools\\make_yolo_dataset.py --selftest
"""

from __future__ import annotations

import argparse
import io
import os
import random
import re
import shutil
import sys
import tempfile

CLASS_RE = re.compile(r'^[A-Za-z0-9_-]+$')
IMG_EXTS = ('.jpg', '.jpeg', '.png', '.bmp')
SPLITS = ('train', 'val')
MIN_BOXES_PER_CLASS = 10


# --------------------------------------------------------------------------
# classes.txt
# --------------------------------------------------------------------------
def read_classes(path):
    """读类别表：跳过空行与 # 注释，**保持行序**（行号 = 类别 id）

    用 utf-8-sig：Windows 记事本另存的 classes.txt 常带 BOM，
    否则第一个类名会变成 '\ufeffscratch' —— 标签 id 没错但 data.yaml 的
    names[0] 会带着不可见字符（训练/转换链路里很难查），这里直接剥掉。"""
    names = []
    with io.open(path, encoding='utf-8-sig') as f:
        for line in f:
            s = line.strip().lstrip('\ufeff')
            if not s or s.startswith('#'):
                continue
            names.append(s)
    return names


def validate_classes(names, src='classes.txt'):
    """类名合法性：只允许 [A-Za-z0-9_-]；返回错误列表（空=没问题）"""
    errs = []
    if not names:
        errs.append('%s: 一个类名都没有（每行一个类名，行号=类别 id）' % src)
        return errs
    for i, n in enumerate(names):
        if not CLASS_RE.match(n):
            errs.append('%s: 第 %d 个类名 %r 含非法字符 —— 只允许 [A-Za-z0-9_-]，'
                        '请改成英文短名（如 scratch / missing / offset / stain）'
                        % (src, i, n))
    dup = sorted(set(n for n in names if names.count(n) > 1))
    if dup:
        errs.append('%s: 类名重复 %s —— id 会歧义，请改名' % (src, dup))
    return errs


# --------------------------------------------------------------------------
# 标签解析 / 校验
# --------------------------------------------------------------------------
def parse_label_file(path, nc):
    """解析一个 YOLO 标签文件 -> (boxes, errors)

    boxes = [(cls, cx, cy, w, h)]；errors = ['第 N 行: 原因']（N 从 1 开始）"""
    boxes, errs = [], []
    try:
        with io.open(path, encoding='utf-8') as f:
            lines = f.read().splitlines()
    except OSError as exc:
        return [], ['读不了: %s' % exc]
    for ln, line in enumerate(lines, 1):
        s = line.strip()
        if not s:
            continue
        parts = s.split()
        if len(parts) != 5:
            errs.append('第 %d 行: 应该是 5 列(cls cx cy w h)，实际 %d 列 %r'
                        % (ln, len(parts), s))
            continue
        try:
            cls = int(parts[0])
        except ValueError:
            try:
                cls = int(float(parts[0]))
            except ValueError:
                errs.append('第 %d 行: 类别 %r 不是整数' % (ln, parts[0]))
                continue
        if cls < 0 or cls >= nc:
            errs.append('第 %d 行: 类别 id %d 越界（应该在 0..%d）'
                        % (ln, cls, nc - 1))
            continue
        try:
            cx, cy, w, h = (float(parts[1]), float(parts[2]),
                            float(parts[3]), float(parts[4]))
        except ValueError:
            errs.append('第 %d 行: cx/cy/w/h 有非数字 %r' % (ln, parts[1:]))
            continue
        bad = False
        for nm, v in (('cx', cx), ('cy', cy), ('w', w), ('h', h)):
            if not (0.0 <= v <= 1.0):
                errs.append('第 %d 行: %s=%g 越界（应该在 0~1）' % (ln, nm, v))
                bad = True
        if w <= 0 or h <= 0:
            errs.append('第 %d 行: w=%g h=%g 退化框（w,h 必须 > 0）' % (ln, w, h))
            bad = True
        if not bad:
            boxes.append((cls, cx, cy, w, h))
    return boxes, errs


def label_path_of(img_path, labels_dir):
    stem = os.path.splitext(os.path.basename(img_path))[0]
    return os.path.join(labels_dir, stem + '.txt')


def list_images(images_dir):
    """列出图片目录**顶层**的图片（不递归：labels 常是它的子目录）"""
    out = []
    try:
        names = sorted(os.listdir(images_dir))
    except OSError:
        return out
    for n in names:
        if n.lower().endswith(IMG_EXTS):
            p = os.path.join(images_dir, n)
            if os.path.isfile(p):
                out.append(p)
    return out


def scan_raw(images_dir, labels_dir, nc, allow_empty=False):
    """扫描"原图 + 标签"，做全套校验。

    返回 dict(items=[{img, label, boxes, empty, errors}], errors=[(file, line, msg)],
             skipped=[(img, reason)], n_images, n_negatives)
    allow_empty=True 时：没有 .txt 的图当成**负样本**（空标签），否则算错误/跳过"""
    items, errors, skipped = [], [], []
    n_neg = 0
    imgs = list_images(images_dir)
    for img in imgs:
        lp = label_path_of(img, labels_dir)
        rec = dict(img=img, label=lp, boxes=[], empty=False, errors=[])
        if not os.path.isfile(lp):
            if allow_empty:
                rec['empty'] = True
                n_neg += 1
                items.append(rec)
            else:
                skipped.append((img, '缺标签文件 %s' % lp))
            continue
        boxes, errs = parse_label_file(lp, nc)
        for e in errs:
            errors.append((lp, e))
        rec['boxes'] = boxes
        # 只有"标签本身没毛病、但一个框都没有"才算负样本；
        # 标注写坏了（比如越界被丢掉）不算负样本，否则会把错误说成"正常负样本"
        rec['empty'] = (len(boxes) == 0 and not errs)
        if rec['empty']:
            n_neg += 1
        items.append(rec)
    return dict(items=items, errors=errors, skipped=skipped,
                n_images=len(imgs), n_negatives=n_neg)


def fmt_errors(errors, limit=40):
    lines = []
    for path, msg in errors[:limit]:
        if msg.startswith('第 ') and '行' in msg:
            ln = msg.split('行')[0].replace('第 ', '').strip()
            rest = msg.split(':', 1)[1].strip() if ':' in msg else msg
            lines.append('  %s:%s: %s' % (path, ln, rest))
        else:
            lines.append('  %s: %s' % (path, msg))
    if len(errors) > limit:
        lines.append('  …（还有 %d 条，未全部显示）' % (len(errors) - limit))
    return '\n'.join(lines)


# --------------------------------------------------------------------------
# data.yaml（自己写/自己读：不依赖 PyYAML）
# --------------------------------------------------------------------------
def _yaml_quote(s):
    return "'%s'" % str(s).replace("'", "''")


def write_data_yaml(path, dataset_path, names, train='images/train',
                    val='images/val'):
    """data.yaml：path + train/val + nc + names（顺序 = classes.txt 行序）"""
    body = [
        '# 由 tools/make_yolo_dataset.py 生成 —— 不要手改；改类别请改 classes.txt 后重跑',
        'path: %s' % _yaml_quote(os.path.abspath(dataset_path)),
        'train: %s' % train,
        'val: %s' % val,
        'nc: %d' % len(names),
        'names: [%s]' % ', '.join(_yaml_quote(n) for n in names),
        '',
    ]
    with io.open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(body))
    return body


def read_data_yaml(path):
    """只认本工具生成的格式（也兼容 names 的块状写法），不依赖 PyYAML"""
    info = dict(path=None, train=None, val=None, nc=None, names=[])
    with io.open(path, encoding='utf-8') as f:
        lines = f.read().splitlines()
    for line in lines:
        s = line.strip()
        if not s or s.startswith('#'):
            continue
        if s.startswith('- '):
            info['names'].append(s[2:].strip().strip("'\""))
            continue
        if ':' not in s:
            continue
        k, v = s.split(':', 1)
        k, v = k.strip(), v.strip()
        if k == 'nc':
            try:
                info['nc'] = int(v)
            except ValueError:
                info['nc'] = None
        elif k in ('path', 'train', 'val'):
            info[k] = v.strip("'\"")
        elif k == 'names':
            body = v.strip()
            if body.startswith('[') and body.endswith(']'):
                body = body[1:-1]
                info['names'] = [x.strip().strip("'\"")
                                 for x in body.split(',') if x.strip()]
    return info


# --------------------------------------------------------------------------
# 统计
# --------------------------------------------------------------------------
def class_counts(items, nc):
    cnt = [0] * nc
    for it in items:
        for b in it['boxes']:
            cnt[b[0]] += 1
    return cnt


def print_report(train_items, val_items, nc, names, skipped, tag='',
                 per_class_train=None):
    n_train, n_val = len(train_items), len(val_items)
    all_items = list(train_items) + list(val_items)
    cnt_all = class_counts(all_items, nc)
    cnt_train = (per_class_train if per_class_train is not None
                 else class_counts(train_items, nc))
    n_neg = sum(1 for it in all_items if not it['boxes'])
    print('[dataset]%s 总图数 %d = train %d + val %d'
          % (tag, n_train + n_val, n_train, n_val))
    print('[dataset]%s 每类框数（train/全部）: %s'
          % (tag, ', '.join('%s %d/%d' % (names[i], cnt_train[i], cnt_all[i])
                            for i in range(nc))))
    print('[dataset]%s 无目标图(负样本) %d 张；跳过 %d 张'
          % (tag, n_neg, len(skipped)))
    for i, n in enumerate(names):
        if cnt_train[i] < MIN_BOXES_PER_CLASS:
            print('[WARN] 类别 %s 样本过少(<10): train 里只有 %d 个框, '
                  '训练容易只学到背景' % (n, cnt_train[i]))
    return dict(n_train=n_train, n_val=n_val, cnt_all=cnt_all,
                cnt_train=cnt_train, n_neg=n_neg)


# --------------------------------------------------------------------------
# build：切分 + 复制 + data.yaml + 统计
# --------------------------------------------------------------------------
def group_of(path, sep='_'):
    """文件名 -> 轮次前缀（如 p03_0007.jpg -> 'p03'；没有分隔符就用整个 stem）

    用途：按轮次整组切分验证集，避免"同一块板的另一张"泄漏进 val 导致指标虚高。
    """
    stem = os.path.splitext(os.path.basename(str(path)))[0]
    if sep and sep in stem:
        return stem.split(sep, 1)[0]
    return stem


def split_pairs(images, labels):
    """把 `--images a,b` / `--labels a/labels,b/labels` 拆成 [(图目录, 标签目录), ...]

    为什么要支持多目录：真实数据是**按轮次**放在 norm/photo01..05 里的，
    每轮各自一个 labels/。以前只能一轮一轮跑，或者手工拷成一个平目录。
    图片前缀（p01_/p02_…）全局唯一，所以合并后不会撞名，`--val-groups` 也照样能用。
    """
    ims = [x.strip().strip('"') for x in str(images).split(',') if x.strip()]
    lbs = [x.strip().strip('"') for x in str(labels).split(',') if x.strip()]
    if len(ims) != len(lbs):
        raise SystemExit('[ERR] --images 给了 %d 个目录、--labels 给了 %d 个，'
                         '个数必须一一对应（用英文逗号分隔）' % (len(ims), len(lbs)))
    return list(zip(ims, lbs))


def build(images, labels, classes, out, val_ratio=0.15, seed=0,
          allow_empty=False, force=False, skip_bad=False, quiet=False,
          val_groups=None, group_sep='_'):
    errs = validate_classes(read_classes(classes), classes)
    if errs:
        print('[ERR] classes.txt 有问题:')
        for e in errs:
            print('  ' + e)
        return 1
    names = read_classes(classes)
    nc = len(names)
    pairs = split_pairs(images, labels)
    for im, lb in pairs:
        if not os.path.isdir(im):
            print('[ERR] 图片目录不存在: %s' % im)
            return 1
        if not os.path.isdir(lb):
            print('[ERR] 标签目录不存在: %s' % lb)
            return 1
    out_abs = os.path.abspath(out)
    if not force:
        probe = os.path.join(out_abs, 'images', 'train')
        if os.path.isdir(probe) and list_images(probe):
            print('[ERR] 输出目录已经有数据: %s' % out_abs)
            print('      换一个 --out，或加 --force 覆盖（会清空 images/labels 下的 train/val）')
            return 1

    if len(pairs) == 1:
        rep = scan_raw(pairs[0][0], pairs[0][1], nc, allow_empty=allow_empty)
        print('[dataset] 扫描 %s 得到 %d 张图（负样本 %d 张）'
              % (pairs[0][0], rep['n_images'], rep['n_negatives']))
    else:
        rep = dict(items=[], errors=[], skipped=[], n_images=0, n_negatives=0)
        for im, lb in pairs:
            one = scan_raw(im, lb, nc, allow_empty=allow_empty)
            rep['items'] += one['items']
            rep['errors'] += one['errors']
            rep['skipped'] += one['skipped']
            rep['n_images'] += one['n_images']
            rep['n_negatives'] += one['n_negatives']
            print('[dataset]   %-28s %3d 张（负样本 %d）'
                  % (im, one['n_images'], one['n_negatives']))
        print('[dataset] 合并 %d 个目录共 %d 张图（负样本 %d 张）'
              % (len(pairs), rep['n_images'], rep['n_negatives']))
    if rep['errors']:
        print('[ERR] 标注校验失败 %d 处:' % len(rep['errors']))
        print(fmt_errors(rep['errors']))
        if not skip_bad:
            print('      修好后重跑；或用 --skip-bad 跳过坏图（不推荐，会丢数据）')
            return 1
        bad_imgs = set(p for p, _m in rep['errors'])
        rep['items'] = [it for it in rep['items'] if it['label'] not in bad_imgs]
        print('[WARN] --skip-bad：已跳过 %d 张坏标注的图' % len(bad_imgs))
    if rep['skipped']:
        for img, why in rep['skipped'][:10]:
            print('[WARN] 跳过 %s: %s' % (img, why))
        if len(rep['skipped']) > 10:
            print('[WARN] …（还有 %d 张被跳过）' % (len(rep['skipped']) - 10))

    items = rep['items']
    if not items:
        print('[ERR] 没有可用的图（检查 --images/--labels 和 --allow-empty）')
        return 1

    # ---- 切分（seed 固定可复现；可选按"轮次前缀"整组留出验证集）----
    rnd = random.Random(seed)
    idx = list(range(len(items)))
    rnd.shuffle(idx)
    val_groups = [g.strip() for g in (val_groups or []) if str(g).strip()]
    if val_groups:
        # 为什么要有这个模式：同一块板/同一轮连续拍摄的图高度相似，随机切分会把
        # "同一块板的另一张"放进验证集 -> 验证指标虚高、上板掉点找不到原因。
        # 这里按文件名前缀（默认取第一个 '_' 之前，如 p03_0007 -> p03）**整组**留出。
        seen_groups = {}
        for it in items:
            g = group_of(it['img'], sep=group_sep)
            seen_groups[g] = seen_groups.get(g, 0) + 1
        missing = [g for g in val_groups if g not in seen_groups]
        if missing:
            print('[ERR] --val-groups 里的这些前缀在数据里不存在: %s' % missing)
            print('      现有前缀: %s' % sorted(seen_groups))
            return 1
        val_set = set(i for i, it in enumerate(items)
                      if group_of(it['img'], sep=group_sep) in val_groups)
        if not val_set:
            print('[ERR] 按 --val-groups 分不出验证集')
            return 1
        if len(val_set) >= len(items):
            print('[ERR] --val-groups 把所有图都分到 val 了，train 为空')
            return 1
        print('[dataset] 按轮次切分：val 组=%s（%d 张）；其余 %d 张进 train'
              % (val_groups, len(val_set), len(items) - len(val_set)))
        for g in sorted(seen_groups):
            mark = 'val' if g in val_groups else 'train'
            print('           %-8s %3d 张 -> %s' % (g, seen_groups[g], mark))
    else:
        n_val = int(round(len(items) * float(val_ratio)))
        if len(items) >= 2 and float(val_ratio) > 0:
            n_val = max(1, min(n_val, len(items) - 1))
        else:
            n_val = 0
        val_set = set(idx[:n_val])
    train_items = [items[i] for i in idx if i not in val_set]
    val_items = [items[i] for i in idx if i in val_set]

    # ---- 清空旧目录（--force）----
    for sub in ('images/train', 'images/val', 'labels/train', 'labels/val'):
        d = os.path.join(out_abs, sub.replace('/', os.sep))
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)

    # ---- 复制（不是硬链接）----
    def _copy_split(split, subset):
        n_img = 0
        for it in subset:
            img = it['img']
            stem = os.path.splitext(os.path.basename(img))[0]
            ext = os.path.splitext(img)[1]
            dst_img = os.path.join(out_abs, 'images', split, stem + ext)
            shutil.copy2(img, dst_img)
            dst_lbl = os.path.join(out_abs, 'labels', split, stem + '.txt')
            if os.path.isfile(it['label']):
                shutil.copy2(it['label'], dst_lbl)
            else:                       # --allow-empty 的负样本：写空标签文件
                with io.open(dst_lbl, 'w', encoding='utf-8', newline='\n') as f:
                    f.write('')
            n_img += 1
        return n_img

    _copy_split('train', train_items)
    _copy_split('val', val_items)

    yml_lines = write_data_yaml(os.path.join(out_abs, 'data.yaml'), out_abs,
                                names)
    stats = print_report(train_items, val_items, nc, names, rep['skipped'],
                         tag='')
    if not quiet:
        print('[dataset] data.yaml:')
        for ln in yml_lines:
            print('  ' + ln)
    print('[dataset] 完成：%s' % out_abs)
    print('[dataset]   images/train %d 张、images/val %d 张；labels 同步'
          % (stats['n_train'], stats['n_val']))
    return 0


# --------------------------------------------------------------------------
# check：只校验（不复制、不改文件）
# --------------------------------------------------------------------------
def check(dataset, allow_empty=False, classes=None):
    ds = os.path.abspath(dataset)
    names, nc = [], 0
    yml = os.path.join(ds, 'data.yaml')
    if os.path.isfile(yml):
        info = read_data_yaml(yml)
        names = info['names']
        nc = info['nc'] or len(names)
        print('[check] data.yaml: nc=%s names=%s' % (nc, names))
        if info['nc'] is not None and info['names'] and info['nc'] != len(info['names']):
            print('[ERR] data.yaml 自相矛盾: nc=%d 但 names 有 %d 个'
                  % (info['nc'], len(info['names'])))
            return 1
    elif classes and os.path.isfile(classes):
        errs = validate_classes(read_classes(classes), classes)
        if errs:
            for e in errs:
                print('[ERR] ' + e)
            return 1
        names = read_classes(classes)
        nc = len(names)
        print('[check] 没有 data.yaml，用 %s 的 %d 个类' % (classes, nc))
    else:
        print('[ERR] %s 下没有 data.yaml，也没给 --classes' % ds)
        return 1

    total_errors, all_items, skipped = [], [], []
    per_split = {}
    for sp in SPLITS:
        img_dir = os.path.join(ds, 'images', sp)
        lbl_dir = os.path.join(ds, 'labels', sp)
        if not os.path.isdir(img_dir):
            print('[WARN] 缺少目录 %s（跳过）' % img_dir)
            per_split[sp] = ([], [])
            continue
        rep = scan_raw(img_dir, lbl_dir, nc, allow_empty=allow_empty)
        per_split[sp] = (rep['items'], rep['skipped'])
        all_items.extend(rep['items'])
        skipped.extend(rep['skipped'])
        total_errors.extend(rep['errors'])
        for img, why in rep['skipped'][:5]:
            print('[WARN] %s: 跳过 %s（%s）' % (sp, os.path.basename(img), why))

    if total_errors:
        print('[ERR] 校验失败 %d 处:' % len(total_errors))
        print(fmt_errors(total_errors))
    stats = print_report(per_split['train'][0], per_split['val'][0], nc, names,
                         skipped, tag='[check]')
    ok = (not total_errors) and (stats['n_train'] + stats['n_val'] > 0)
    print('[check] %s' % ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def _mk_image(path, color, box=None):
    from PIL import Image
    im = Image.new('RGB', (320, 240), color)
    if box:
        for y in range(box[1], min(240, box[3])):
            for x in range(box[0], min(320, box[2])):
                im.putpixel((x, y), (255, 255, 255))
    im.save(path, format='JPEG', quality=85)


def run_selftest():
    checks = []

    def chk(name, cond, extra=''):
        checks.append((name, bool(cond)))
        print('  [%s] %-54s %s' % ('PASS' if cond else 'FAIL', name, extra))
        return bool(cond)

    print('=' * 80)
    print('make_yolo_dataset 自检（临时目录，离线）')
    print('=' * 80)
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        print('  [FAIL] 本机没有 PIL，无法生成测试图（pip install pillow）')
        print('make_yolo_dataset-selftest: 1 项, 失败 1 项 -> FAIL')
        return 1

    tmp = tempfile.mkdtemp(prefix='yolo_ds_selftest_')
    rc = 0
    try:
        # ---- classes.txt：含注释行与空行（读的时候必须跳过）----
        classes = os.path.join(tmp, 'classes.txt')
        with io.open(classes, 'w', encoding='utf-8', newline='\n') as f:
            f.write('# PCB 缺陷类别；行号 = 类别 id（改顺序 = 改数据集的标签，'
                    '必须与 data.yaml 一致）\n\n'
                    'scratch\nmissing\nstain\n')
        names = read_classes(classes)
        chk('classes.txt 跳过 # 注释与空行、保持 3 个类',
            names == ['scratch', 'missing', 'stain'], str(names))
        chk('类名合法性校验通过（全 ASCII）',
            validate_classes(names, classes) == [])
        bad_classes = os.path.join(tmp, 'bad_classes.txt')
        with io.open(bad_classes, 'w', encoding='utf-8') as f:
            f.write('scratch\n刮痕\n')
        chk('中文类名会被拦下并给建议',
            len(validate_classes(read_classes(bad_classes), bad_classes)) == 1,
            str(validate_classes(read_classes(bad_classes), bad_classes)[:1]))

        # ---- BOM 坑：记事本另存的 classes.txt 第一个类名会变 '\ufeffscratch' ----
        bom_classes = os.path.join(tmp, 'bom_classes.txt')
        with io.open(bom_classes, 'w', encoding='utf-8-sig', newline='\n') as f:
            f.write('scratch\nmissing\nstain\n')
        bn = read_classes(bom_classes)
        chk('带 BOM 的 classes.txt: 第一个类名不残留 \\ufeff',
            bn == ['scratch', 'missing', 'stain'], repr(bn[:1]))

        # ---- good 原始集：10 张图 + 9 个有框 + 1 个空标签(负样本) ----
        good = os.path.join(tmp, 'good')
        glabels = os.path.join(good, 'labels')
        os.makedirs(glabels, exist_ok=True)
        plan = {   # 文件名 -> (颜色, 框, 标签行列表)；3 类: 0=scratch 1=missing 2=stain
            'a01': ((30, 60, 90), (10, 10, 80, 80), ['0 0.5 0.5 0.2 0.2']),
            'a02': ((30, 60, 90), (40, 20, 120, 90), ['0 0.4 0.3 0.2 0.2']),
            'a03': ((200, 120, 30), (20, 20, 90, 90), ['0 0.5 0.5 0.2 0.2',
                                                      '1 0.3 0.3 0.1 0.1']),
            'a04': ((200, 120, 30), None, ['1 0.6 0.6 0.2 0.2']),
            'a05': ((10, 180, 90), (60, 60, 160, 160), ['2 0.5 0.5 0.3 0.3']),
            'a06': ((10, 180, 90), None, ['2 0.2 0.2 0.1 0.1']),
            'a07': ((120, 120, 200), (30, 30, 100, 100), ['2 0.5 0.5 0.2 0.2']),
            'a08': ((120, 120, 200), None, ['1 0.7 0.7 0.2 0.2']),
            'a09': ((90, 90, 90), (10, 40, 70, 100), ['0 0.4 0.4 0.2 0.2',
                                                      '2 0.6 0.6 0.2 0.2']),
            'a10': ((90, 90, 90), None, []),        # 空标签 = 负样本
        }
        for stem, (color, box, lines) in plan.items():
            _mk_image(os.path.join(good, stem + '.jpg'), color, box)
            if lines:
                with io.open(os.path.join(glabels, stem + '.txt'), 'w',
                             encoding='utf-8', newline='\n') as f:
                    f.write('\n'.join(lines) + '\n')
            else:
                with io.open(os.path.join(glabels, stem + '.txt'), 'w',
                             encoding='utf-8', newline='\n') as f:
                    f.write('')
        chk('造好 10 张图 + 10 个标签（含 1 个空标签负样本）',
            len(list_images(good)) == 10
            and len([n for n in os.listdir(glabels) if n.endswith('.txt')]) == 10)

        # ---- build：切分 + data.yaml ----
        out = os.path.join(tmp, 'pcb_v1')
        rc_build = build(good, glabels, classes, out, val_ratio=0.2, seed=0)
        chk('build 成功返回 0', rc_build == 0, 'rc=%r' % rc_build)
        n_tr = len([n for n in os.listdir(os.path.join(out, 'images', 'train'))
                    if n.lower().endswith(IMG_EXTS)])
        n_va = len([n for n in os.listdir(os.path.join(out, 'images', 'val'))
                    if n.lower().endswith(IMG_EXTS)])
        chk('切分比例对：10 张 -> train 8 / val 2（val-ratio 0.2）',
            (n_tr, n_va) == (8, 2), 'train=%d val=%d' % (n_tr, n_va))
        chk('labels 与 images 数量一致（train/val 各自）',
            len(os.listdir(os.path.join(out, 'labels', 'train'))) == n_tr
            and len(os.listdir(os.path.join(out, 'labels', 'val'))) == n_va)
        yml = read_data_yaml(os.path.join(out, 'data.yaml'))
        chk('data.yaml 的 names 顺序与 classes.txt 一致',
            yml['names'] == names, str(yml['names']))
        chk('data.yaml 的 nc 正确', yml['nc'] == 3, 'nc=%r' % yml['nc'])
        chk('data.yaml 的 path 是绝对路径且指向 out',
            yml['path'] is not None
            and os.path.abspath(yml['path']) == os.path.abspath(out),
            str(yml['path']))
        chk('data.yaml 的 train/val 指向 images/train 与 images/val',
            yml['train'] == 'images/train' and yml['val'] == 'images/val',
            '%r / %r' % (yml['train'], yml['val']))
        # 统计数字：good 里有 13 个框（0:x5 1:x3 2:x2 3:x3 —— 见 plan）
        rep = scan_raw(os.path.join(out, 'images', 'train'),
                       os.path.join(out, 'labels', 'train'), 3)
        # 只校验 check 能过
        rc_check = check(out)
        chk('--check 通过（0 错误，返回 0）', rc_check == 0, 'rc=%r' % rc_check)

        # ---- 统计数字对得上（直接算整个 good 的框数）----
        rep_all = scan_raw(good, glabels, 3)
        cnt = class_counts(rep_all['items'], 3)
        chk('每类框数统计正确（0:4 1:3 2:4 共 11）',
            cnt == [4, 3, 4] and sum(cnt) == 11, str(cnt))
        chk('无目标图(负样本)数 = 1', rep_all['n_negatives'] == 1,
            str(rep_all['n_negatives']))

        # ---- bad 原始集：越界框 + 类别越界 + 缺标签，三条都要被抓出来 ----
        bad = os.path.join(tmp, 'bad')
        blabels = os.path.join(bad, 'labels')
        os.makedirs(blabels, exist_ok=True)
        _mk_image(os.path.join(bad, 'b01.jpg'), (50, 50, 50))
        with io.open(os.path.join(blabels, 'b01.txt'), 'w', encoding='utf-8') as f:
            f.write('0 1.5 0.5 0.2 0.2\n')          # cx 越界
        _mk_image(os.path.join(bad, 'b02.jpg'), (60, 60, 60))
        with io.open(os.path.join(blabels, 'b02.txt'), 'w', encoding='utf-8') as f:
            f.write('99 0.5 0.5 0.2 0.2\n')         # 类别越界
        _mk_image(os.path.join(bad, 'b03.jpg'), (70, 70, 70))   # 缺标签
        rep_bad = scan_raw(bad, blabels, 3)
        msgs = ' | '.join('%s:%s' % (os.path.basename(p), m)
                          for p, m in rep_bad['errors'])
        chk('抓出"cx 越界"', 'cx=1.5' in msgs, msgs[:100])
        chk('抓出"类别越界"', '类别 id 99' in msgs, msgs[:100])
        chk('缺标签的图被记为跳过（默认不静默吞掉）',
            len(rep_bad['skipped']) == 1
            and 'b03' in os.path.basename(rep_bad['skipped'][0][0]),
            str([os.path.basename(i) for i, _w in rep_bad['skipped']]))
        chk('总计 2 处标注错误', len(rep_bad['errors']) == 2,
            str(len(rep_bad['errors'])))

        # ---- --allow-empty 时缺标签的图当负样本 ----
        rep_bad2 = scan_raw(bad, blabels, 3, allow_empty=True)
        chk('--allow-empty 时缺标签的图算负样本',
            len(rep_bad2['skipped']) == 0 and rep_bad2['n_negatives'] == 1,
            'neg=%d skip=%d' % (rep_bad2['n_negatives'],
                                len(rep_bad2['skipped'])))

        # ---- 坏标注时 build 默认中止（不把脏数据混进训练集）----
        rc_bad = build(bad, blabels, classes, os.path.join(tmp, 'bad_out'),
                       force=True)
        chk('坏标注时 build 默认中止（返回 1）', rc_bad == 1, 'rc=%r' % rc_bad)

        # ---- 类别样本过少要打 WARN（train 里 stain 只有 3 个框）----
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc2 = build(good, glabels, classes, os.path.join(tmp, 'pcb_v2'),
                        val_ratio=0.2, seed=0)
        out_txt = buf.getvalue()
        chk('类别样本过少会打 [WARN]',
            '[WARN] 类别 scratch 样本过少(<10)' in out_txt
            and '训练容易只学到背景' in out_txt, 'rc=%r' % rc2)
    finally:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass

    nfail = sum(1 for _n, v in checks if not v)
    print('-' * 80)
    print('make_yolo_dataset-selftest: %d 项, 失败 %d 项 -> %s'
          % (len(checks), nfail, 'PASS' if nfail == 0 else 'FAIL'))
    return 0 if nfail == 0 else 1


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------
def build_argparser():
    epilog = ('示例:\n'
              '  python tools\\make_yolo_dataset.py --images ai\\dataset\\raw\\s01'
              ' --labels ai\\dataset\\raw\\s01\\labels --classes ai\\dataset\\classes.txt'
              ' --out ai\\dataset\\pcb_v1 --val-ratio 0.15 --seed 0\n'
              '  python tools\\make_yolo_dataset.py --check --dataset ai\\dataset\\pcb_v1\n'
              '  python tools\\make_yolo_dataset.py --selftest\n')
    ap = argparse.ArgumentParser(
        prog='make_yolo_dataset.py',
        description=('把"原图 + YOLO 标注"切成 train/val 并生成 data.yaml；'
                     '或 --check 只校验已有数据集（标签格式/类别 id/框坐标/每类框数）。'),
        epilog=epilog, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--selftest', action='store_true',
                    help='离线自检：临时目录造 10 图 + 假标签，验切分/data.yaml/校验/统计')
    ap.add_argument('--check', action='store_true',
                    help='只校验不复制：检查 --dataset（或 --images/--labels/--classes）')
    ap.add_argument('--dataset', help='[--check] 已生成的数据集目录（含 data.yaml）')
    ap.add_argument('--images', help='原图目录（只扫顶层，不递归）')
    ap.add_argument('--labels', help='标签目录（<stem>.txt，与图片同名）')
    ap.add_argument('--classes', help='classes.txt（一行一个类名，行号=类别 id）')
    ap.add_argument('--out', help='输出数据集目录（如 ai\\dataset\\pcb_v1）')
    ap.add_argument('--val-ratio', type=float, default=0.15,
                    help='验证集比例（默认 0.15；会保证 train 至少 1 张）')
    ap.add_argument('--val-groups', default='',
                    help='按轮次前缀整组留出验证集，如 p03,p04（逗号分隔）。'
                         '给了就不看 --val-ratio；用于避免同板/同轮泄漏')
    ap.add_argument('--group-sep', default='_',
                    help='轮次前缀的分隔符（默认 _，即 p03_0007 -> p03）')
    ap.add_argument('--seed', type=int, default=0,
                    help='切分随机种子（默认 0，固定可复现）')
    ap.add_argument('--allow-empty', action='store_true',
                    help='没有 .txt 的图当"负样本"（空标签），而不是跳过')
    ap.add_argument('--skip-bad', action='store_true',
                    help='标注有错时跳过坏图继续（默认直接中止，不把脏数据混进训练集）')
    ap.add_argument('--force', action='store_true',
                    help='输出目录已有数据时覆盖（清空 images|labels 下的 train/val）')
    return ap


def main(argv=None):
    ap = build_argparser()
    args = ap.parse_args(argv)
    if args.selftest:
        return run_selftest()
    if args.check:
        if args.dataset:
            return check(args.dataset, allow_empty=args.allow_empty,
                         classes=args.classes)
        if args.images and args.labels and args.classes:
            return check_dir_pair(args.images, args.labels, args.classes,
                                  args.allow_empty)
        ap.print_help()
        return 2
    if not (args.images and args.labels and args.classes and args.out):
        print('[ERR] build 模式需要 --images --labels --classes --out 四个参数')
        ap.print_help()
        return 2
    return build(args.images, args.labels, args.classes, args.out,
                 val_ratio=args.val_ratio, seed=args.seed,
             val_groups=[g for g in str(args.val_groups or '').split(',') if g.strip()],
             group_sep=args.group_sep,
                 allow_empty=args.allow_empty, force=args.force,
                 skip_bad=args.skip_bad)


def check_dir_pair(images, labels, classes, allow_empty=False):
    """--check 的另一种用法：直接给 images/labels/classes（还没切分时先体检）"""
    errs = validate_classes(read_classes(classes), classes)
    if errs:
        for e in errs:
            print('[ERR] ' + e)
        return 1
    names = read_classes(classes)
    rc_holder = dict(items=[])
    rep = scan_raw(images, labels, len(names), allow_empty=allow_empty)
    for img, why in rep['skipped'][:5]:
        print('[WARN] 跳过 %s（%s）' % (os.path.basename(img), why))
    if rep['errors']:
        print('[ERR] 校验失败 %d 处:' % len(rep['errors']))
        print(fmt_errors(rep['errors']))
    rc_holder['items'] = rep['items']
    stats = print_report(rep['items'], [], len(names), names, rep['skipped'],
                         tag='[raw]')
    ok = (not rep['errors']) and stats['n_train'] > 0
    print('[check] %s' % ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())

