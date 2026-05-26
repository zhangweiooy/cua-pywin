"""
Dubins-path motion engine for the agent cursor overlay.

Pure math — no platform deps. Translated directly from the Swift
AgentCursorRenderer: Dubins path planning, smootherstep velocity
profile, and damped-spring overshoot/settle.
"""

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# ---------------------------------------------------------------------------
# Tuning knobs (matching Swift AgentCursorRenderer defaults)
# ---------------------------------------------------------------------------

TURN_RADIUS = 80.0         # minimum turning radius in points
CLICK_OFFSET = 16.0        # overshoot past click point along end-heading
PEAK_SPEED = 2340.0        # peak speed in pt/s (fast burst)
MIN_START_SPEED = 780.0    # speed floor during acceleration phase
MIN_END_SPEED = 195.0      # speed floor during deceleration phase (gentle approach)
SPRING_K = 400.0           # spring constant
SPRING_C = 17.0            # damping coefficient
SPRING_OVERSHOOT = 0.8     # fraction of arrival speed → spring init velocity
DWELL_SECONDS = 0.4        # seconds to hold at rest before hiding

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class Pose:
    x: float
    y: float
    heading: float


class Phase(str, Enum):
    IDLE = "idle"
    FLYING = "flying"
    SPRING = "spring"
    DWELL = "dwell"
    DONE = "done"


@dataclass
class MotionState:
    x: float = 0.0
    y: float = 0.0
    heading: float = 0.0          # visual tip direction (radians)
    phase: Phase | str = Phase.IDLE
    path_seg1: float = 0.0
    path_seg2: float = 0.0
    path_seg3: float = 0.0
    path_types: list = field(default_factory=list)
    path_length: float = 0.0
    path_r: float = 0.0
    path_x0: float = 0.0
    path_y0: float = 0.0
    path_th0: float = 0.0
    distance: float = 0.0
    spring_ox: float = 0.0
    spring_oy: float = 0.0
    spring_vx: float = 0.0
    spring_vy: float = 0.0
    target_x: float = 0.0
    target_y: float = 0.0
    end_heading: float = 0.0


@dataclass
class _DubinsSolution:
    t: float
    p: float
    q: float
    types: list

    @property
    def length(self) -> float:
        return self.t + self.p + self.q


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mod2pi(x: float) -> float:
    tau = 2 * math.pi
    r = x - tau * math.floor(x / tau)
    return r + tau if r < 0 else r


def _smootherstep(u: float) -> float:
    """Asymmetric bell: peak at u≈0.3, fast rise, slow decay (先快后慢)."""
    if u <= 0.0:
        return 0.0
    if u >= 1.0:
        return 0.0
    # Rise phase (0..0.3): quick acceleration
    if u < 0.3:
        t = u / 0.3
        return t * t * (3 - 2 * t)  # smoothstep: 0→1
    # Decay phase (0.3..1.0): slow deceleration
    t = (u - 0.3) / 0.7
    return 1.0 - t * t * (3 - 2 * t)  # reversed smoothstep: 1→0


def _rotate_toward(current: float, desired: float, max_step: float) -> float:
    diff = desired - current
    while diff > math.pi:
        diff -= 2 * math.pi
    while diff < -math.pi:
        diff += 2 * math.pi
    return current + max(-max_step, min(max_step, diff))


# ---------------------------------------------------------------------------
# Dubins solvers (6 variants, matching Swift lines 290-331)
# ---------------------------------------------------------------------------


def _dubins_lsl(d: float, a: float, b: float) -> Optional[_DubinsSolution]:
    tmp0 = d + math.sin(a) - math.sin(b)
    p2 = 2 + d * d - 2 * math.cos(a - b) + 2 * d * (math.sin(a) - math.sin(b))
    if p2 < 0:
        return None
    tmp1 = math.atan2(math.cos(b) - math.cos(a), tmp0)
    return _DubinsSolution(t=_mod2pi(-a + tmp1), p=math.sqrt(p2), q=_mod2pi(b - tmp1), types=["L", "S", "L"])


def _dubins_rsr(d: float, a: float, b: float) -> Optional[_DubinsSolution]:
    tmp0 = d - math.sin(a) + math.sin(b)
    p2 = 2 + d * d - 2 * math.cos(a - b) + 2 * d * (math.sin(b) - math.sin(a))
    if p2 < 0:
        return None
    tmp1 = math.atan2(math.cos(a) - math.cos(b), tmp0)
    return _DubinsSolution(t=_mod2pi(a - tmp1), p=math.sqrt(p2), q=_mod2pi(-b + tmp1), types=["R", "S", "R"])


def _dubins_lsr(d: float, a: float, b: float) -> Optional[_DubinsSolution]:
    p2 = -2 + d * d + 2 * math.cos(a - b) + 2 * d * (math.sin(a) + math.sin(b))
    if p2 < 0:
        return None
    p = math.sqrt(p2)
    tmp1 = math.atan2(-math.cos(a) - math.cos(b), d + math.sin(a) + math.sin(b)) - math.atan2(-2, p)
    return _DubinsSolution(t=_mod2pi(-a + tmp1), p=p, q=_mod2pi(-_mod2pi(b) + tmp1), types=["L", "S", "R"])


def _dubins_rsl(d: float, a: float, b: float) -> Optional[_DubinsSolution]:
    p2 = d * d - 2 + 2 * math.cos(a - b) - 2 * d * (math.sin(a) + math.sin(b))
    if p2 < 0:
        return None
    p = math.sqrt(p2)
    tmp1 = math.atan2(math.cos(a) + math.cos(b), d - math.sin(a) - math.sin(b)) - math.atan2(2, p)
    return _DubinsSolution(t=_mod2pi(a - tmp1), p=p, q=_mod2pi(b - tmp1), types=["R", "S", "L"])


def _dubins_rlr(d: float, a: float, b: float) -> Optional[_DubinsSolution]:
    tmp = (6 - d * d + 2 * math.cos(a - b) + 2 * d * (math.sin(a) - math.sin(b))) / 8
    if abs(tmp) > 1:
        return None
    p = _mod2pi(2 * math.pi - math.acos(tmp))
    t = _mod2pi(a - math.atan2(math.cos(a) - math.cos(b), d - math.sin(a) + math.sin(b)) + p / 2)
    return _DubinsSolution(t=t, p=p, q=_mod2pi(a - b - t + p), types=["R", "L", "R"])


def _dubins_lrl(d: float, a: float, b: float) -> Optional[_DubinsSolution]:
    tmp = (6 - d * d + 2 * math.cos(a - b) + 2 * d * (math.sin(b) - math.sin(a))) / 8
    if abs(tmp) > 1:
        return None
    p = _mod2pi(2 * math.pi - math.acos(tmp))
    t = _mod2pi(-a + math.atan2(-math.cos(a) + math.cos(b), d + math.sin(a) - math.sin(b)) + p / 2)
    return _DubinsSolution(t=t, p=p, q=_mod2pi(_mod2pi(b) - a - t + p), types=["L", "R", "L"])


_SOLVERS = [_dubins_lsl, _dubins_rsr, _dubins_lsr, _dubins_rsl, _dubins_rlr, _dubins_lrl]


# ---------------------------------------------------------------------------
# Path planning
# ---------------------------------------------------------------------------


def plan_path(x0: float, y0: float, th0: float, x1: float, y1: float, th1: float,
              end_visual_heading: float, target_x: float, target_y: float,
              r: float = TURN_RADIUS) -> MotionState:
    """Build a MotionState with Dubins path segments loaded (or linear fallback).

    For targets very close to the start position, a perpendicular detour
    is added so the arrow always flies a visible arc.
    """
    return plan_path_from_poses(
        start=Pose(x0, y0, th0),
        end=Pose(x1, y1, th1),
        end_visual_heading=end_visual_heading,
        target_x=target_x,
        target_y=target_y,
        r=r,
    )


def plan_path_from_poses(
    start: Pose,
    end: Pose,
    end_visual_heading: float,
    target_x: float,
    target_y: float,
    r: float = TURN_RADIUS,
) -> MotionState:
    """Build a MotionState from explicit start/end poses."""
    x0, y0, th0 = start.x, start.y, start.heading
    x1, y1, th1 = end.x, end.y, end.heading
    dx = x1 - x0
    dy = y1 - y0
    D = math.hypot(dx, dy)

    # For nearby targets, rotate the arrival heading and scale up the
    # turn radius so Dubins draws a large visible loop.  The path still
    # ends at (x1, y1) — only the approach angle and arc size change.
    MIN_ARC = r * 2.0  # 160 px
    detour_r = r
    if D < MIN_ARC:
        t = D / MIN_ARC  # 0 → 1 as distance approaches MIN_ARC
        sweep = math.radians(160) * (1.0 - t) + math.radians(45) * t
        th1 = _mod2pi(th1 + sweep)
        # Scale turn radius up to 2× for nearby targets → bigger loops
        detour_r = r * (2.0 - t)  # 2r at D=0, 1r at D=MIN_ARC

    state = MotionState(
        x=x0, y=y0, heading=th0 + math.pi,  # visual heading = motion + pi (tip trails)
        phase=Phase.FLYING,
        path_x0=x0, path_y0=y0, path_th0=th0,
        path_r=detour_r,
        target_x=target_x, target_y=target_y,
        end_heading=end_visual_heading,
        distance=0.0,
    )

    # Try Dubins; fall back to linear
    solution = _plan_dubins(x0, y0, th0, x1, y1, th1, detour_r)
    if solution is not None:
        state.path_seg1 = solution.t
        state.path_seg2 = solution.p
        state.path_seg3 = solution.q
        state.path_types = solution.types
        state.path_length = (solution.t + solution.p + solution.q) * detour_r
    else:
        D2 = math.hypot(x1 - x0, y1 - y0)
        state.path_seg1 = D2
        state.path_seg2 = 0.0
        state.path_seg3 = 0.0
        state.path_types = ["S"]
        state.path_length = max(1.0, D2)

    return state


def _plan_dubins(x0: float, y0: float, th0: float, x1: float, y1: float, th1: float,
                 r: float) -> Optional[_DubinsSolution]:
    dx = x1 - x0
    dy = y1 - y0
    D = math.hypot(dx, dy)
    # Allow very small D — the rotated heading creates a visible loop
    d = max(D, r * 0.05) / max(r, 1.0)
    theta = _mod2pi(math.atan2(dy, dx))
    a = _mod2pi(th0 - theta)
    b = _mod2pi(th1 - theta)

    best: Optional[_DubinsSolution] = None
    best_len = float("inf")
    for solver in _SOLVERS:
        sol = solver(d, a, b)
        if sol is not None and sol.length >= 0 and math.isfinite(sol.length) and sol.length < best_len:
            best_len = sol.length
            best = sol
    return best


# ---------------------------------------------------------------------------
# Path sampling
# ---------------------------------------------------------------------------


def _sample_path(s: float, state: MotionState) -> tuple[float, float, float]:
    """Sample (x, y, heading) at distance s along the planned Dubins path."""
    if s <= 0:
        return state.path_x0, state.path_y0, state.path_th0
    r = state.path_r
    L1 = state.path_seg1 * r
    L2 = state.path_seg2 * r
    L3 = state.path_seg3 * r
    x, y, th = state.path_x0, state.path_y0, state.path_th0
    total = L1 + L2 + L3
    s = min(s, total)

    def advance(length: float, typ: str):
        nonlocal x, y, th
        if typ == "S":
            x += math.cos(th) * length
            y += math.sin(th) * length
        else:
            dth = length / r * (1.0 if typ == "L" else -1.0)
            perp = math.pi / 2 if typ == "L" else -math.pi / 2
            cx = x + math.cos(th + perp) * r
            cy = y + math.sin(th + perp) * r
            ang = math.atan2(y - cy, x - cx)
            x = cx + math.cos(ang + dth) * r
            y = cy + math.sin(ang + dth) * r
            th += dth

    types = state.path_types
    if s <= L1:
        advance(s, types[0])
        return x, y, th
    advance(L1, types[0])
    if s <= L1 + L2:
        advance(s - L1, types[1])
        return x, y, th
    advance(L2, types[1])
    advance(s - L1 - L2, types[2])
    return x, y, th


# ---------------------------------------------------------------------------
# Per-frame tick
# ---------------------------------------------------------------------------


def tick(state: MotionState, dt: float) -> None:
    """Advance the motion state by dt seconds. Idempotent when idle/done."""
    if state.phase in (Phase.IDLE, "idle", Phase.DWELL, "dwell", Phase.DONE, "done"):
        return

    if state.phase in (Phase.FLYING, "flying"):
        _tick_flying(state, dt)
    elif state.phase in (Phase.SPRING, "spring"):
        _tick_spring(state, dt)


def _tick_flying(state: MotionState, dt: float):
    u = min(1.0, state.distance / max(state.path_length, 1.0))
    profile = _smootherstep(u)
    # Blend start/end floor speeds through the asymmetric profile
    floor_speed = MIN_START_SPEED + (MIN_END_SPEED - MIN_START_SPEED) * u
    speed = floor_speed + (PEAK_SPEED - floor_speed) * profile
    state.distance += speed * dt

    if state.distance >= state.path_length:
        end_x, end_y, end_th = _sample_path(state.path_length, state)
        vx = math.cos(end_th) * speed * SPRING_OVERSHOOT
        vy = math.sin(end_th) * speed * SPRING_OVERSHOOT
        # Start spring at the Dubins endpoint so the detour arc is visible.
        # The spring then settles toward (target_x, target_y).
        state.spring_ox = end_x - state.target_x
        state.spring_oy = end_y - state.target_y
        state.spring_vx = vx
        state.spring_vy = vy
        state.x = end_x
        state.y = end_y
        state.heading = state.end_heading
        state.phase = Phase.SPRING
    else:
        sx, sy, sth = _sample_path(state.distance, state)
        state.x = sx
        state.y = sy
        state.heading = _rotate_toward(state.heading, sth + math.pi, 14.0 * dt)


def _tick_spring(state: MotionState, dt: float):
    k = SPRING_K
    c = SPRING_C
    substeps = 4
    sdt = dt / substeps
    ox, oy = state.spring_ox, state.spring_oy
    vx, vy = state.spring_vx, state.spring_vy
    for _ in range(substeps):
        vx += (-k * ox - c * vx) * sdt
        vy += (-k * oy - c * vy) * sdt
        ox += vx * sdt
        oy += vy * sdt
    state.spring_ox = ox
    state.spring_oy = oy
    state.spring_vx = vx
    state.spring_vy = vy
    state.x = state.target_x + ox
    state.y = state.target_y + oy
    if math.hypot(ox, oy) < 0.3 and math.hypot(vx, vy) < 2:
        state.x = state.target_x
        state.y = state.target_y
        state.phase = Phase.DWELL
