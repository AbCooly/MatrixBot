"""拟人行为噪声层（Human Behavior Noise Layer）。

背景
  平台脚本风控不仅能看“你是不是自动化”，更看重**行为指纹**：
  - 鼠标：直线 + 匀速 + 固定步数 = 典型自动化（真人手部运动是「弹道段 + 微修正段」，
    有加速度包络、弯曲、颤振、过冲/回拉，事件间隔呈重尾而非均匀）；
  - 键盘：均匀固定延迟 = 机器特征（真人键入是脉冲串 + 思考停顿 + 偶尔纠错）；
  - 导航：每任务“直达发布页” = 可疑（真人会先在页面上浏览/游走/悬停）；
  - 等待：`wait_for_timeout(4000)` 这种固定值本身就是特征。

本模块只做一件事：把「离散的一次性动作」替换成**符合人手生物力学输出分布**的
连续事件序列，间隔高熵、非均匀、可复现地“像人”。

设计要点
  - 轨迹：起点→终点的三次贝塞尔曲线，控制点带随机法向弯度；沿曲线按
    u^gamma（gamma<1，接近目标时步距收敛）取点 = 弹道长步 + 末端微调；
    长距离约 45% 概率过冲若干像素再回拉；到位后 1~3 次微小“震颤”收敛。
  - 事件间隔：每次 mousemove 的 dt 取自混合分布（多数 6~16ms + 偶发长停
    45ms+），熵值高于均匀/正态；事件数由运动时长决定，与距离无关。
  - 游走/悬停/滚动：`idle_wander` 模拟“阅读时的漫无目的”：光标在兴趣点间
    移动、悬停、微颤，穿插小幅双向滚动与思考停顿。
  - 思考停顿：时长走重尾分布（短为主、偶发长），而不是 [a,b] 均匀。

开关
  HUMANIZER_ENABLED=0  关闭（全部退化为最小快捷动作，用于调试/故障排查）。
"""
from __future__ import annotations

import asyncio
import math
import os
import random
from typing import TYPE_CHECKING

from ...logger import log

if TYPE_CHECKING:
    from playwright.async_api import Locator, Page

ENABLED = os.environ.get("HUMANIZER_ENABLED", "1") != "0"

# 模块内记录“我们最后发出鼠标事件的坐标”。只要整个发布链路里所有真实鼠标动作
# 都走本模块（或纯 JS click），该坐标与浏览器实际指针位置保持一致，从而避免
# 从“未知位置”一次性瞬移（单事件超大 movementX 也是风控特征）。
_cursor: dict[int, tuple[float, float]] = {}

# 安全边距：鼠标活动范围不贴视口边缘（真人很少把指针死死顶在边框上）
_EDGE = 4.0


# ---------------------------------------------------------------------------
# 基础概率分布（高熵 / 重尾）
# ---------------------------------------------------------------------------
def pause_ms(lo: float, hi: float) -> float:
    """操作间停顿：偏短值的重尾分布（短停顿为主，偶发“再想想”长停顿）。

    返回毫秒；保证落在 [lo, hi] 内。
    """
    if not ENABLED:
        return lo
    if hi <= lo:
        return lo
    # ~78% 落在低端（重尾），其余在 [lo,hi] 均匀（长尾）
    if random.random() < 0.78:
        return lo + (hi - lo) * (random.random() ** 1.9)
    return random.uniform(lo, hi)


def think_ms(lo_s: float, hi_s: float) -> float:
    """“思考停顿”：对数正态气质分布，用于动作之间的人类决策间隙。"""
    if not ENABLED:
        return lo_s * 1000.0
    mu = (math.log(lo_s * 1000) + math.log(hi_s * 1000)) / 2.0
    sigma = random.uniform(0.25, 0.45)
    v = random.lognormvariate(mu, sigma)
    return max(lo_s * 1000.0, min(hi_s * 1000.0, v))


def _motion_dt(per_ms: float) -> float:
    """单次 mousemove 事件的间隔：围绕 per_ms 的高熵分布（毫秒）。

    真实系统鼠标事件大致落在 8~20ms 栅格上，但人“握持-抬手”会产生偶发长停，
    因此混合：多数贴近均值，~12% 概率出现 1.8x~5x 的“犹豫/停滞”。
    """
    r = random.random()
    if r < 0.12:
        return per_ms * random.uniform(1.8, 5.0)
    if r < 0.2:
        return per_ms * random.uniform(0.35, 0.7)
    return per_ms * random.uniform(0.7, 1.5)


# ---------------------------------------------------------------------------
# 鼠标：生物力学轨迹
# ---------------------------------------------------------------------------
def _bezier3(p0, p1, p2, p3, t: float) -> tuple[float, float]:
    u = 1.0 - t
    a, b, c = u * u * u, 3 * u * u * t, 3 * u * t * t
    d = t * t * t
    return (
        a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
        a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1],
    )


def _plan_curve(sx: float, sy: float, tx: float, ty: float,
                allow_overshoot: bool) -> list[tuple[float, float, float]]:
    """生成从 (sx,sy) 到 (tx,ty) 的轨迹点 [(x, y, dt_ms), ...]。

    - 时间 T 由距离决定（短距离快、长距离按比例放慢，类似 Fitts 定律粗粒度）；
    - 事件数 N = T / ~11ms（事件疏密由时间决定，不是“每 N 像素一步”）；
    - 沿贝塞尔按 u^gamma 取参数 → 起点大步弹道、末端密步微调；
    - 长距离一定概率过冲再回拉（人手瞄准常掠过目标再回来）。
    """
    d = math.hypot(tx - sx, ty - sy)
    if d < 2.0:
        return [(tx, ty, 8.0)]

    # 1) 是否需要过冲
    aim_x, aim_y = tx, ty
    overshoot_back = False
    if allow_overshoot and d > 90 and random.random() < 0.45:
        o = random.uniform(6.0, 14.0)
        ux, uy = (tx - sx) / d, (ty - sy) / d
        aim_x, aim_y = tx + ux * o, ty + uy * o
        overshoot_back = True

    # 2) 总时长（毫秒）：基础决策 + 与距离弱相关
    T = (random.uniform(90, 260) + 1.15 * d * random.uniform(0.7, 1.4))
    if not ENABLED:
        T = 60.0
    n = int(max(4.0, min(240.0, T / 11.0)))
    per = T / n

    # 3) 控制点：随机法向弯度（两个控制点方向相反，弯度不同 → 非对称弧线）
    nx, ny = -(ty - sy) / d, (tx - sx) / d
    bend = random.uniform(-1.0, 1.0) * min(80.0, d * 0.16)
    bend2 = random.uniform(-1.0, 1.0) * min(40.0, d * 0.08)
    p0 = (sx, sy)
    p1 = (sx + (aim_x - sx) * 0.30 + nx * bend, sy + (aim_y - sy) * 0.30 + ny * bend)
    p2 = (sx + (aim_x - sx) * 0.72 + nx * bend2, sy + (aim_y - sy) * 0.72 + ny * bend2)
    p3 = (aim_x, aim_y)

    gamma = random.uniform(0.45, 0.92)  # <1 → 接近目标时步距收敛
    sigma = 0.35 + d * 0.0016            # 手部颤振量级

    pts: list[tuple[float, float, float]] = []
    prev: tuple[float, float] | None = None
    for i in range(1, n + 1):
        u = i / n
        sh = u ** gamma
        x, y = _bezier3(p0, p1, p2, p3, sh)
        x += random.gauss(0, sigma)
        y += random.gauss(0, sigma)
        # 保证不回退（人手极少反向走主路径）
        if prev is not None:
            x = max(x, prev[0] - 1.0) if tx >= sx else min(x, prev[0] + 1.0)
            y = max(y, prev[1] - 1.0) if ty >= sy else min(y, prev[1] + 1.0)
        dt = _motion_dt(per)
        pts.append((x, y, dt))
        prev = (x, y)
        # 中段偶发“犹豫”：原地微颤一下再继续
        if 0.35 < sh < 0.85 and random.random() < 0.08:
            pts.append((x + random.uniform(-2, 2), y + random.uniform(-2, 2),
                        random.uniform(60, 160)))

    # 4) 过冲回拉：放慢、小幅、带停顿
    if overshoot_back:
        pts.append((tx, ty, random.uniform(60, 200)))
        for _ in range(random.randint(1, 3)):
            pts.append((tx + random.uniform(-1.5, 1.5),
                        ty + random.uniform(-1.5, 1.5),
                        random.uniform(30, 90)))
        pts.append((tx, ty, random.uniform(20, 60)))
    return pts


async def _anchor(page: Page) -> tuple[float, float]:
    """返回“已知/假设”的当前指针位置（首次按视口中部估算）。"""
    key = id(page)
    cur = _cursor.get(key)
    if cur is not None:
        return cur
    try:
        dim = await page.evaluate(
            "() => ({w: window.innerWidth || 1280, h: window.innerHeight || 900})"
        )
    except Exception:  # noqa: BLE001
        dim = {"w": 1280.0, "h": 900.0}
    cur = (dim["w"] * random.uniform(0.35, 0.65), dim["h"] * random.uniform(0.4, 0.7))
    _cursor[key] = cur
    return cur


async def _viewport_of(viewport: tuple[float, float, float, float] | None,
                       page: Page) -> tuple[float, float, float, float]:
    """活动区域：默认避开顶部导航(~12%)/右侧滚动条，留边距。"""
    if viewport:
        x0, y0, x1, y1 = viewport
    else:
        try:
            dim = await page.evaluate(
                "() => ({w: window.innerWidth || 1280, h: window.innerHeight || 900})"
            )
            x0, y0, x1, y1 = 0.0, 0.0, dim["w"], dim["h"]
        except Exception:  # noqa: BLE001
            x0, y0, x1, y1 = 0.0, 0.0, 1280.0, 900.0
    return (
        max(_EDGE, x0 + 12),
        max(_EDGE, y0 + max(70.0, y1 * 0.08)),
        min(x1 - _EDGE, max(x0 + 12, x1 - 24)),
        min(y1 - _EDGE, y0 + (y1 - y0) * 0.92),
    )


async def _send_move(page: Page, x: float, y: float, dt_ms: float) -> None:
    """发送一次 mousemove 并记录坐标（坐标转 int，避免 CDP 无谓浮点）。"""
    ix, iy = int(round(x)), int(round(y))
    try:
        await page.mouse.move(ix, iy)
    except Exception:  # noqa: BLE001
        return
    _cursor[id(page)] = (float(ix), float(iy))
    if dt_ms > 0.5:
        await asyncio.sleep(dt_ms / 1000.0)


async def move_human(page: Page, x: float, y: float,
                     viewport: tuple[float, float, float, float] | None = None,
                     overshoot: bool = True) -> None:
    """拟人移动鼠标到 (x, y)。viewport 为 (x0,y0,x1,y1) 可选活动区域。"""
    if not ENABLED:
        try:
            await page.mouse.move(int(x), int(y))
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        sx, sy = await _anchor(page)
    except Exception:  # noqa: BLE001
        sx, sy = 0.0, 0.0
    x = min(max(x, _EDGE), (viewport[2] if viewport else 5000) - _EDGE)
    y = min(max(y, _EDGE), (viewport[3] if viewport else 5000) - _EDGE)
    for px, py, dt in _plan_curve(sx, sy, x, y, overshoot):
        await _send_move(page, px, py, dt)
    _cursor[id(page)] = (x, y)


async def settle(page: Page, radius: float = 2.5) -> None:
    """到位后的手部震颤收敛：1~3 次微小抖动（模拟悬停时手臂不稳）。"""
    if not ENABLED:
        return
    try:
        x, y = _cursor.get(id(page), (0, 0))
        for _ in range(random.randint(0, 2)):
            if random.random() < 0.7:
                await _send_move(page, x + random.uniform(-radius, radius),
                                 y + random.uniform(-radius, radius),
                                 random.uniform(25, 90))
        _cursor[id(page)] = (x, y)
    except Exception:  # noqa: BLE001
        pass


async def click_point(page: Page, x: float, y: float, label: str = "") -> None:
    """拟人点击：弯轨移动 → 悬停迟疑 → 按下(持键) → 松开 → 松手后小幅回弹。

    真实点击包含“按下-保持-松开”的持键过程（50~180ms），且按下前后指针会漂移；
    直接 mouse.click() 的一瞬间触发是机器特征。
    """
    if not ENABLED:
        try:
            await page.mouse.click(int(x), int(y))
        except Exception:  # noqa: BLE001
            pass
        return
    # 瞄准散射：真人点按钮不是“像素级正中”，会有 ±(1~5)px 落点散布
    ax, ay = x + random.gauss(0, 1.6), y + random.gauss(0, 1.6)
    await move_human(page, ax, ay)
    await asyncio.sleep(think_ms(0.10, 0.55) / 1000.0)
    # 按下前最后一次精瞄（如果之前落点偏出按钮 3px+）
    if random.random() < 0.35 and math.hypot(ax - x, ay - y) > 3.0:
        await move_human(page, x + random.gauss(0, 0.8), y + random.gauss(0, 0.8),
                         overshoot=False)
        await asyncio.sleep(random.uniform(40, 180) / 1000.0)
    await settle(page, radius=1.2)
    try:
        await page.mouse.down()
        await asyncio.sleep(random.uniform(55, 190) / 1000.0)  # 持键
        await page.mouse.up()
    except Exception as exc:  # noqa: BLE001
        log.info("拟人点击失败[%s]: %s", label or "?", exc)
        return
    # 松手后手臂自然回弹/漂移
    if random.random() < 0.4:
        try:
            await _send_move(page,
                             x + random.gauss(0, 1.2) + random.uniform(-6, 6),
                             y + random.gauss(0, 1.2) + random.uniform(-3, 3),
                             random.uniform(30, 130))
        except Exception:  # noqa: BLE001
            pass
    if label:
        log.info("拟人点击完成: %s", label)


async def click_locator_human(page: Page, loc: "Locator", label: str = "") -> bool:
    """把 Playwright Locator 滚入视野后按中心拟人点击。失败返回 False。"""
    try:
        await loc.scroll_into_view_if_needed(timeout=8000)
        await asyncio.sleep(random.uniform(120, 420) / 1000.0)
        box = await loc.bounding_box()
        if not box or box["width"] < 1 or box["height"] < 1:
            return False
        await click_point(page, box["x"] + box["width"] / 2,
                          box["y"] + box["height"] / 2, label or "locator")
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 游走 / 悬停 / 滚动（阅读式漫无目的）
# ---------------------------------------------------------------------------
async def wander(page: Page, seconds: float,
                 viewport: tuple[float, float, float, float] | None = None,
                 click_targets: list["Locator"] | None = None) -> None:
    """“阅读时”的漫无目的鼠标：在兴趣点间移动、悬停、微颤、思考。

    click_targets：可悬停但不点击的候选元素（兴趣卡片/链接）。不会真的点下去。
    """
    if not ENABLED or seconds <= 0:
        return
    deadline = asyncio.get_event_loop().time() + seconds
    x0, y0, x1, y1 = await _viewport_of(viewport, page)
    targets: list[tuple[float, float]] = []
    if click_targets:
        for loc in click_targets[:8]:
            try:
                if await loc.is_visible():
                    box = await loc.bounding_box()
                    if box and box["width"] > 2:
                        targets.append((box["x"] + box["width"] / 2,
                                        box["y"] + box["height"] / 2))
            except Exception:  # noqa: BLE001
                continue
    tries = 0
    while asyncio.get_event_loop().time() < deadline and tries < 40:
        tries += 1
        if targets and random.random() < 0.5:
            tx, ty = random.choice(targets)
            # 悬停点带散布，避免每次都戳中心
            tx += random.gauss(0, 4); ty += random.gauss(0, 4)
        else:
            span_x = (x1 - x0) * random.uniform(0.02, 0.3)
            span_y = (y1 - y0) * random.uniform(0.02, 0.22)
            tx = min(max(x0, random.gauss((x0 + x1) / 2, span_x)), x1)
            ty = min(max(y0, random.gauss((y0 + y1) / 2, span_y)), y1)
        try:
            await move_human(page, tx, ty)
            await asyncio.sleep(think_ms(0.6, 3.4) / 1000.0)
            await settle(page)
        except Exception:  # noqa: BLE001
            pass
    # 结束时把鼠标放回视口中部偏下（不留在按钮/边缘上）
    try:
        await move_human(page, (x0 + x1) / 2 + random.uniform(-80, 80),
                         (y0 + y1) * random.uniform(0.55, 0.75), overshoot=False)
    except Exception:  # noqa: BLE001
        pass


async def scroll_human(page: Page, delta_px: int,
                       viewport: tuple[float, float, float, float] | None = None) -> None:
    """拟人滚动：分块、变速、偶发反向微调（滚多了回一点）。delta_px>0 向下。"""
    if not ENABLED or delta_px == 0:
        return
    sign = 1 if delta_px > 0 else -1
    remaining = abs(delta_px)
    try:
        while remaining > 0:
            chunk = random.randint(60, 260)
            chunk = min(chunk, remaining)
            # 约 12% 概率夹一次小反向（“滚过头了滚回来”）——不改变主方向进度
            reverse = 0
            if random.random() < 0.12 and remaining > 120:
                reverse = random.randint(10, 45)
            await page.mouse.wheel(0, int(chunk * sign))
            await asyncio.sleep(random.uniform(60, 240) / 1000.0)
            if reverse:
                await page.mouse.wheel(0, int(-reverse * sign))
                await asyncio.sleep(random.uniform(50, 180) / 1000.0)
            remaining -= chunk
    except Exception:  # noqa: BLE001
        pass


async def idle_wander(page: Page, seconds: float) -> None:
    """在“阅读/思考”间隙做几秒不打断流程的轻微小动作（滚动 + 光标漂移）。"""
    if not ENABLED or seconds <= 0:
        return
    deadline = asyncio.get_event_loop().time() + seconds
    x0, y0, x1, y1 = await _viewport_of(None, page)
    while asyncio.get_event_loop().time() < deadline:
        choice = random.random()
        try:
            if choice < 0.35:
                await scroll_human(page, random.choice([-1, 1]) * random.randint(80, 260))
            elif choice < 0.8:
                await move_human(
                    page,
                    random.uniform(x0, x1), random.uniform(y0 + 60, y1),
                    overshoot=False,
                )
            else:
                await settle(page, radius=1.5)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(think_ms(0.4, 2.2) / 1000.0)


# ---------------------------------------------------------------------------
# 键盘：键入节奏（供 helpers.type_human 使用）
# ---------------------------------------------------------------------------
def keystroke_delay_ms() -> float:
    """单字符键入间隔：脉冲串内偏快、重尾、高熵（55~190ms 主体 + 长尾）。

    主体 ~85ms/字符 ≈ 11 字符/秒（快速录入的峰值段），配合调用方的
    “段间停顿”后整体平均回到真人水平。
    """
    r = random.random()
    if r < 0.1:
        return random.uniform(180, 380)          # 卡顿/换键找键
    if r < 0.3:
        return random.uniform(30, 60)            # 连击（同一词组快速出字）
    return random.uniform(55, 150)


def burst_segment_len() -> int:
    """一次连续输入的“脉冲”长度：输入法联想/词组节奏（1~7 字符，主 2~4）。"""
    r = random.random()
    if r < 0.25:
        return random.randint(1, 2)
    if r < 0.85:
        return random.randint(3, 5)
    return random.randint(6, 9)


def commit_pause_ms() -> float:
    """输入法“选词提交/想想下一句”的段间停顿（120~950ms 重尾）。"""
    return pause_ms(120, 950)


# ---------------------------------------------------------------------------
# 会话预热（发布前的人类“到场感”）
# ---------------------------------------------------------------------------
async def human_goto(page: Page, url: str, wait_until: str = "domcontentloaded",
                     timeout_ms: int = 30_000) -> bool:
    """带网络空闲等待的跳转（保留 helpers.safe_goto 语义，供本层复用）。"""
    try:
        await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        await page.wait_for_load_state("networkidle", timeout=8_000)
        await asyncio.sleep(think_ms(0.8, 2.6) / 1000.0)
        return True
    except Exception:  # noqa: BLE001
        return False
