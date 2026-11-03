"""Locate the dominant dark PCB on the station's light work surface.

Coordinates refer to the displayed camera image, never to defect boxes. This
heuristic checks framing; it does not identify a PCB model or certify a pass.
"""
try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = np = None


def locate_pcb(image, edge_margin=.02, center_tolerance=.10):
    """Return normalized bounds and movement in image coordinates (BGR input)."""
    if cv2 is None:
        return dict(kind='unavailable', directions=[], edges=[], text='位置提醒需要 OpenCV / NumPy')
    missing = dict(kind='unknown', directions=[], edges=[], text='未识别到PCB，请完整入镜')
    if image is None or image.size == 0 or image.ndim != 3 or image.shape[2] != 3:
        return missing
    height, width = image.shape[:2]
    if min(height, width) < 32:
        return missing
    scale = min(1., 480. / width)
    small = cv2.resize(image, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA) if scale < 1 else image
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    threshold, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    # Keep an empty/dark/uniform frame from becoming a full-screen "board".
    threshold = max(35., min(190., threshold))
    mask = (gray < threshold).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if count < 2:
        return missing
    index = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    x, y, w, h, area = map(int, stats[index])
    sh, sw = gray.shape
    if area < sw * sh * .035 or area > sw * sh * .92 or w < sw * .12 or h < sh * .12:
        return missing
    # Require a visible, brighter work surface around the candidate. Uniform
    # dark or lens-covered frames have no reliable movement direction.
    foreground = gray[labels == index]
    background = gray[mask == 0]
    if len(background) < sw * sh * .06 or np.median(background) - np.median(foreground) < 25:
        return missing
    bounds = [x / sw, y / sh, (x + w) / sw, (y + h) / sh]
    x0, y0, x1, y1 = bounds
    edges = [name for name, hit in (
        ('left', x0 <= edge_margin), ('right', x1 >= 1 - edge_margin),
        ('top', y0 <= edge_margin), ('bottom', y1 >= 1 - edge_margin)) if hit]
    # Opposite edges cannot be fixed by opposing movement instructions.
    if {'left', 'right'} <= set(edges) or {'top', 'bottom'} <= set(edges):
        return dict(kind='too_large', bounds=bounds, edges=edges, directions=[], text='两侧均贴边，请拉远PCB或抬高相机')
    directions = []
    if 'left' in edges: directions.append('right')
    elif 'right' in edges: directions.append('left')
    if 'top' in edges: directions.append('down')
    elif 'bottom' in edges: directions.append('up')
    # Correct cropping first. Once all edges are visible, allow a central dead
    # band so small hand motion never produces repeated left/right changes.
    if not directions:
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if cx < .5 - center_tolerance: directions.append('right')
        elif cx > .5 + center_tolerance: directions.append('left')
        if cy < .5 - center_tolerance: directions.append('down')
        elif cy > .5 + center_tolerance: directions.append('up')
    words = dict(left='左', right='右', up='上', down='下')
    arrows = dict(left='←', right='→', up='↑', down='↓')
    kind = 'move' if directions else 'centered'
    text = ''.join(arrows[d] for d in directions) + ' 请向' + '、'.join(words[d] for d in directions) + '移动PCB' if directions else 'PCB 位置正常'
    return dict(kind=kind, bounds=bounds, edges=edges, directions=directions, text=text)


class PositionConfirmation:
    """Require two successive sampled images; loss of a board clears at once."""
    def __init__(self):
        self.key = None
        self.count = 0

    def update(self, result):
        key = (result['kind'], tuple(result['directions']), tuple(result['edges']))
        self.count = self.count + 1 if key == self.key else 1
        self.key = key
        if result['kind'] in ('unknown', 'unavailable') or self.count >= 2:
            return result
        return dict(kind='pending', directions=[], edges=[], text='正在确认PCB位置…')
