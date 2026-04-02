"""cav_topo.py - map CAV topology TOML to a diagram_svg Diagram.

Usage:
    python cav_topo.py topology.toml output.svg
"""

import sys
import tomllib
from diagram_svg import Diagram, Grid, Node, Edge, render

# Domain rules: how CAV types map to diagram visuals.
SHAPE_MAP = {
    "worktree": "rect",
    "remote_branch": "ellipse",
}

COLOR_MAP = {
    "worktree": "teal",
    "remote_branch": "purple",
}

# Override color for specific node ids.
NODE_COLOR = {
    "host_main_wt": "coral",
    "github": "gray",
}

EDGE_COLOR = {
    "push": "#0F6E56",
    "pull": "#0F6E56",
    "approve": "#BA7517",
    "rebase": "#185FA5",
}


def load(path: str) -> Diagram:
    with open(path, "rb") as f:
        topo = tomllib.load(f)

    grid = Grid(
        rows=topo["axes"]["branch"],
        cols=topo["axes"]["location"],
    )

    nodes = []
    for n in topo["nodes"]:
        nodes.append(Node(
            id=n["id"],
            row=n["branch"],
            col=n["location"],
            shape=SHAPE_MAP[n["type"]],
            label=n["label"],
            sublabel=n.get("sublabel", ""),
            color=NODE_COLOR.get(n["id"], COLOR_MAP[n["type"]]),
        ))

    edges = []
    for e in topo["edges"]:
        edges.append(Edge(
            from_id=e["from"],
            to_id=e["to"],
            label=e["operation"],
            direction="forward",
            style=e.get("style", "solid"),
            color=EDGE_COLOR.get(e["operation"], "#555"),
        ))

    return Diagram(grid=grid, nodes=nodes, edges=edges)


def main():
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <topology.toml> <output.svg>")
        sys.exit(1)

    diagram = load(sys.argv[1])
    svg = render(diagram)
    with open(sys.argv[2], "w") as f:
        f.write(svg)


if __name__ == "__main__":
    main()
