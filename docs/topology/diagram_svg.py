"""diagram_svg.py - lightweight grid-based diagram renderer to SVG.

Two-layer architecture:
  1. This module: takes a conceptual diagram description, outputs SVG.
  2. Domain-specific mappers (e.g. cav_topo.py) produce the description.

No external dependencies beyond stdlib.
All edges are straight lines. Edges render behind nodes.
Arrows stop at the node shape boundary.
Parallel edges between the same node pair are auto-offset.
"""

from dataclasses import dataclass, field
from typing import Literal
import math


@dataclass
class Node:
    id: str
    row: str
    col: str
    shape: Literal["rect", "ellipse"]
    label: str
    sublabel: str = ""
    color: str = "teal"


@dataclass
class Edge:
    from_id: str
    to_id: str
    label: str = ""
    direction: Literal["forward", "bidirectional", "none"] = "forward"
    style: Literal["solid", "dashed"] = "solid"
    color: str = "#555"


@dataclass
class Grid:
    rows: list[str]
    cols: list[str]


@dataclass
class Diagram:
    grid: Grid
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)


# --- Layout constants ---

MARGIN_LEFT = 100
MARGIN_TOP = 60
COL_WIDTH = 190
ROW_HEIGHT = 140
RECT_W = 120
RECT_H = 56
ELLIPSE_RX = 56
ELLIPSE_RY = 28
ARROW_MARKER_SIZE = 12
FONT_SIZE = 14
FONT_SIZE_SMALL = 12
PARALLEL_OFFSET = 8
ARROW_GAP = 4
LABEL_OFFSET = 12


# --- Color palette ---

COLORS = {
    "teal": {"fill": "#E1F5EE", "stroke": "#0F6E56", "text": "#085041"},
    "purple": {"fill": "#EEEDFE", "stroke": "#534AB7", "text": "#3C3489"},
    "coral": {"fill": "#FAECE7", "stroke": "#993C1D", "text": "#712B13"},
    "gray": {"fill": "#F1EFE8", "stroke": "#5F5E5A", "text": "#444441"},
    "blue": {"fill": "#E6F1FB", "stroke": "#185FA5", "text": "#0C447C"},
    "amber": {"fill": "#FAEEDA", "stroke": "#854F0B", "text": "#633806"},
}


def _col_x(grid: Grid, col: str) -> float:
    i = grid.cols.index(col)
    return MARGIN_LEFT + i * COL_WIDTH + COL_WIDTH / 2


def _row_y(grid: Grid, row: str) -> float:
    i = grid.rows.index(row)
    return MARGIN_TOP + i * ROW_HEIGHT + ROW_HEIGHT / 2


def _node_by_id(diagram: Diagram, node_id: str) -> Node:
    return next(n for n in diagram.nodes if n.id == node_id)


def _node_center(diagram: Diagram, node_id: str) -> tuple[float, float]:
    node = _node_by_id(diagram, node_id)
    return _col_x(diagram.grid, node.col), _row_y(diagram.grid, node.row)


def _boundary_point(node: Node, grid: Grid, dx: float, dy: float,
                    gap: float = 0.0) -> tuple[float, float]:
    """Point on shape boundary in direction (dx, dy) from center, plus optional gap."""
    cx = _col_x(grid, node.col)
    cy = _row_y(grid, node.row)
    if dx == 0 and dy == 0:
        return cx, cy

    length = math.hypot(dx, dy)
    ndx, ndy = dx / length, dy / length

    if node.shape == "rect":
        hw, hh = RECT_W / 2, RECT_H / 2
        if ndx == 0:
            dist = hh
        elif ndy == 0:
            dist = hw
        else:
            dist = min(hw / abs(ndx), hh / abs(ndy))
        dist += gap
        return cx + ndx * dist, cy + ndy * dist
    else:
        angle = math.atan2(dy, dx)
        bx = ELLIPSE_RX * math.cos(angle)
        by = ELLIPSE_RY * math.sin(angle)
        border_dist = math.hypot(bx, by)
        total = border_dist + gap
        scale = total / border_dist
        return cx + bx * scale, cy + by * scale


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _parallel_key(edge: Edge) -> tuple[str, str]:
    """Canonical key for edges between the same node pair (order-independent)."""
    return tuple(sorted([edge.from_id, edge.to_id]))


def _compute_parallel_offsets(edges: list[Edge]) -> dict[int, float]:
    """For each edge index, compute perpendicular offset if parallel edges exist."""
    groups: dict[tuple[str, str], list[int]] = {}
    for i, edge in enumerate(edges):
        key = _parallel_key(edge)
        groups.setdefault(key, []).append(i)

    offsets: dict[int, float] = {}
    for indices in groups.values():
        n = len(indices)
        if n == 1:
            offsets[indices[0]] = 0.0
        else:
            for rank, idx in enumerate(indices):
                offsets[idx] = (rank - (n - 1) / 2) * PARALLEL_OFFSET * 2
    return offsets


def render(diagram: Diagram) -> str:
    total_w = MARGIN_LEFT + len(diagram.grid.cols) * COL_WIDTH + 20
    total_h = MARGIN_TOP + len(diagram.grid.rows) * ROW_HEIGHT + 20

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="100%"'
        f' viewBox="0 0 {total_w} {total_h}"'
        f' style="font-family: sans-serif;">',
        '<defs>',
        f'<marker id="arr-end" viewBox="0 0 10 10" refX="8" refY="5"'
        f' markerWidth="{ARROW_MARKER_SIZE}" markerHeight="{ARROW_MARKER_SIZE}"'
        ' orient="auto-start-reverse">',
        '<path d="M3 0.5L8 5L3 9.5" fill="none" stroke="context-stroke"'
        ' stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>',
        '</marker>',
        f'<marker id="arr-start" viewBox="0 0 10 10" refX="2" refY="5"'
        f' markerWidth="{ARROW_MARKER_SIZE}" markerHeight="{ARROW_MARKER_SIZE}"'
        ' orient="auto-start-reverse">',
        '<path d="M7 0.5L2 5L7 9.5" fill="none" stroke="context-stroke"'
        ' stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>',
        '</marker>',
        '</defs>',
    ]

    # Column headers
    for col in diagram.grid.cols:
        x = _col_x(diagram.grid, col)
        parts.append(
            f'<text x="{x}" y="30" text-anchor="middle"'
            f' font-size="{FONT_SIZE_SMALL}" fill="#888">'
            f'{_escape(col)}</text>'
        )

    # Row headers
    for row in diagram.grid.rows:
        y = _row_y(diagram.grid, row)
        parts.append(
            f'<text x="{MARGIN_LEFT - 20}" y="{y}" text-anchor="end"'
            f' dominant-baseline="central" font-size="{FONT_SIZE}"'
            f' font-weight="500" fill="#666">{_escape(row)}</text>'
        )

    # Lane dividers
    if len(diagram.grid.rows) > 1:
        line_w = MARGIN_LEFT + len(diagram.grid.cols) * COL_WIDTH
        for i in range(1, len(diagram.grid.rows)):
            y = MARGIN_TOP + i * ROW_HEIGHT
            parts.append(
                f'<line x1="{MARGIN_LEFT - 40}" y1="{y}"'
                f' x2="{line_w}" y2="{y}"'
                f' stroke="#ccc" stroke-width="0.5" stroke-dasharray="4 4"/>'
            )

    # Compute parallel offsets
    offsets = _compute_parallel_offsets(diagram.edges)

    # --- Edges (render BEFORE nodes) ---
    for i, edge in enumerate(diagram.edges):
        src = _node_by_id(diagram, edge.from_id)
        dst = _node_by_id(diagram, edge.to_id)
        src_cx, src_cy = _node_center(diagram, edge.from_id)
        dst_cx, dst_cy = _node_center(diagram, edge.to_id)

        dx = dst_cx - src_cx
        dy = dst_cy - src_cy
        length = math.hypot(dx, dy)
        if length == 0:
            continue
        ndx, ndy = dx / length, dy / length

        # Perpendicular based on canonical (sorted) direction so parallel
        # edges offset consistently regardless of which way they point.
        canon = _parallel_key(edge)
        if canon[0] == edge.from_id:
            perp_x, perp_y = -ndy, ndx
        else:
            perp_x, perp_y = ndy, -ndx

        # Boundary points: source flush, destination with gap for arrowhead
        x1, y1 = _boundary_point(src, diagram.grid, dx, dy, gap=0)
        x2, y2 = _boundary_point(dst, diagram.grid, -dx, -dy, gap=ARROW_GAP)

        # Apply parallel offset
        offset = offsets.get(i, 0.0)
        x1 += perp_x * offset
        y1 += perp_y * offset
        x2 += perp_x * offset
        y2 += perp_y * offset

        # Line style
        dash = ' stroke-dasharray="4 3"' if edge.style == "dashed" else ""

        # Arrowhead markers
        if edge.direction == "forward":
            markers = ' marker-end="url(#arr-end)"'
        elif edge.direction == "bidirectional":
            markers = ' marker-start="url(#arr-start)" marker-end="url(#arr-end)"'
        else:
            markers = ""

        parts.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}"'
            f' x2="{x2:.1f}" y2="{y2:.1f}"'
            f' stroke="{edge.color}" stroke-width="1"{dash}{markers}/>'
        )

        # Label next to its arrow.
        # Parallel edges: label on the outward side of its arrow.
        # Solo edges: label above the line (when read left-to-right).
        # Diagonal edges: rotate label to match edge angle.
        if edge.label:
            mx = (x1 + x2) / 2
            my = (y1 + y2) / 2

            # Edge's own perpendicular (right-hand normal: above for left-to-right)
            edge_perp_x, edge_perp_y = ndy, -ndx

            # Compute readable angle from the actual rendered line, not centers.
            line_dx = x2 - x1
            line_dy = y2 - y1
            angle_deg = math.degrees(math.atan2(line_dy, line_dx))
            flipped = False
            if angle_deg > 90:
                angle_deg -= 180
                flipped = True
            elif angle_deg < -90:
                angle_deg += 180
                flipped = True

            # "Above" when reading left-to-right.
            # edge_perp is the left-hand normal of the from->to direction.
            # If we flipped for readability, "above" is the opposite of edge_perp.
            if flipped:
                above_x, above_y = -edge_perp_x, -edge_perp_y
            else:
                above_x, above_y = edge_perp_x, edge_perp_y

            edge_offset = offsets.get(i, 0.0)
            nudge = 12
            if edge_offset == 0.0:
                # Solo edge: nudge above the line
                lx = mx + above_x * nudge
                ly = my + above_y * nudge
            else:
                # Parallel edge: nudge in same direction as offset
                sign = 1 if edge_offset > 0 else -1
                lx = mx + perp_x * nudge * sign
                ly = my + perp_y * nudge * sign

            # Only apply rotation for non-horizontal edges
            if abs(angle_deg) > 5:
                transform = f' transform="rotate({angle_deg:.1f} {lx:.1f} {ly:.1f})"'
            else:
                transform = ""

            parts.append(
                f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle"'
                f' dominant-baseline="central" font-size="{FONT_SIZE_SMALL}"'
                f' fill="{edge.color}"{transform}>{_escape(edge.label)}</text>'
            )

    # --- Nodes (render AFTER edges) ---
    for node in diagram.nodes:
        cx = _col_x(diagram.grid, node.col)
        cy = _row_y(diagram.grid, node.row)
        c = COLORS.get(node.color, COLORS["gray"])

        if node.shape == "rect":
            x = cx - RECT_W / 2
            y = cy - RECT_H / 2
            parts.append(
                f'<rect x="{x}" y="{y}" width="{RECT_W}" height="{RECT_H}"'
                f' rx="8" fill="{c["fill"]}" stroke="{c["stroke"]}"'
                f' stroke-width="0.5"/>'
            )
            if node.sublabel:
                parts.append(
                    f'<text x="{cx}" y="{cy - 8}" text-anchor="middle"'
                    f' dominant-baseline="central" font-size="{FONT_SIZE}"'
                    f' font-weight="500" fill="{c["text"]}">'
                    f'{_escape(node.label)}</text>'
                )
                parts.append(
                    f'<text x="{cx}" y="{cy + 10}" text-anchor="middle"'
                    f' dominant-baseline="central" font-size="{FONT_SIZE_SMALL}"'
                    f' fill="{c["text"]}" opacity="0.7">'
                    f'{_escape(node.sublabel)}</text>'
                )
            else:
                parts.append(
                    f'<text x="{cx}" y="{cy}" text-anchor="middle"'
                    f' dominant-baseline="central" font-size="{FONT_SIZE}"'
                    f' font-weight="500" fill="{c["text"]}">'
                    f'{_escape(node.label)}</text>'
                )
        else:
            parts.append(
                f'<ellipse cx="{cx}" cy="{cy}"'
                f' rx="{ELLIPSE_RX}" ry="{ELLIPSE_RY}"'
                f' fill="{c["fill"]}" stroke="{c["stroke"]}"'
                f' stroke-width="0.5"/>'
            )
            parts.append(
                f'<text x="{cx}" y="{cy}" text-anchor="middle"'
                f' dominant-baseline="central" font-size="{FONT_SIZE}"'
                f' font-weight="500" fill="{c["text"]}">'
                f'{_escape(node.label)}</text>'
            )

    parts.append('</svg>')
    return "\n".join(parts)
