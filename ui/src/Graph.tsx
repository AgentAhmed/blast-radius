import { useEffect, useRef } from "react";
import cytoscape, { Core, ElementDefinition } from "cytoscape";
import type { Evidence, Topology } from "./api";

interface Props {
  topology: Topology | null;
  evidence: Evidence | null;
  selected: string | null;
  onSelect: (component: string | null) => void;
}

// Typed loosely: cytoscape's typings miss some valid style keys (e.g. padding).
const STYLE: any[] = [
  {
    selector: "node",
    style: {
      label: "data(label)", color: "#c9d4e5", "font-size": 11, "font-family": "Inter, sans-serif",
      "text-valign": "bottom", "text-margin-y": 6, "background-color": "#3b82f6",
      width: "mapData(crit, 0, 0.011, 22, 46)", height: "mapData(crit, 0, 0.011, 22, 46)",
      "border-width": 2, "border-color": "#0b0f17", "transition-property": "opacity, background-color, border-color",
      "transition-duration": 250,
    },
  },
  { selector: "node[kind = 'Datastore']", style: { shape: "barrel", "background-color": "#a855f7" } },
  { selector: "node[kind = 'Topic']", style: { shape: "round-tag", "background-color": "#f59e0b" } },
  {
    selector: "edge",
    style: {
      width: 1.4, "line-color": "#2a3547", "target-arrow-color": "#2a3547", "target-arrow-shape": "triangle",
      "curve-style": "bezier", "arrow-scale": 0.8, "transition-property": "opacity, line-color, width",
      "transition-duration": 250,
    },
  },
  { selector: "node.firing", style: { "border-color": "#ff5c5c", "border-width": 3 } },
  { selector: "node.alerting", style: { "background-color": "#ef4444", "border-color": "#fecaca", "border-width": 2 } },
  {
    selector: "node.root",
    style: {
      "background-color": "#ff2d55", "border-color": "#ffd1dc", "border-width": 5, width: 54, height: 54,
      "font-weight": "bold", color: "#ffffff", "font-size": 13,
    },
  },
  {
    selector: "edge.path",
    style: { "line-color": "#ff5c5c", "target-arrow-color": "#ff5c5c", width: 3.2, "z-index": 10 },
  },
  { selector: ".dim", style: { opacity: 0.18 } },
  {
    selector: "node.ctx",
    style: {
      shape: "round-rectangle", width: "label", height: 22, padding: "6px", "font-size": 10,
      "text-valign": "center", "text-margin-y": 0, color: "#0b0f17", "background-color": "#e2e8f0",
      "text-wrap": "ellipsis", "text-max-width": "150px",
    },
  },
  { selector: "node.ctx-PullRequest", style: { "background-color": "#fde68a" } },
  { selector: "node.ctx-Person", style: { "background-color": "#86efac" } },
  { selector: "node.ctx-Incident", style: { "background-color": "#c4b5fd" } },
  { selector: "node.ctx-Runbook, node.ctx-Lesson, node.ctx-Decision", style: { "background-color": "#7dd3fc" } },
  {
    selector: "edge.ctx",
    style: {
      "line-style": "dashed", "line-color": "#64748b", "target-arrow-color": "#64748b", width: 1.2,
      label: "data(label)", "font-size": 8, color: "#94a3b8", "text-rotation": "autorotate",
      "text-background-color": "#0b0f17", "text-background-opacity": 1, "text-background-padding": "2px",
    },
  },
  { selector: "node:selected", style: { "overlay-color": "#60a5fa", "overlay-opacity": 0.25, "overlay-padding": 6 } },
];

export default function Graph({ topology, evidence, selected, onSelect }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const cy = useRef<Core | null>(null);
  const positions = useRef<Record<string, cytoscape.Position>>({});

  // Build the topology once per tenant.
  useEffect(() => {
    if (!host.current || !topology) return;
    const els: ElementDefinition[] = [
      ...topology.nodes.map((n) => ({
        data: { id: n.id, label: n.id, kind: n.kind, crit: n.criticality ?? 0, firing: n.firing },
      })),
      ...topology.edges.map((e) => ({ data: { id: `${e.source}->${e.target}`, source: e.source, target: e.target } })),
    ];
    cy.current?.destroy();
    const c = cytoscape({
      container: host.current, elements: els, style: STYLE, wheelSensitivity: 0.25, minZoom: 0.3, maxZoom: 2.5,
      layout: { name: "breadthfirst", directed: true, roots: ["load-generator"], spacingFactor: 1.05, padding: 30 },
    });
    c.nodes().forEach((n) => { positions.current[n.id()] = { ...n.position() }; });
    c.on("tap", "node", (ev) => {
      const id: string = ev.target.id();
      if (!id.startsWith("ctx:")) onSelect(id);
    });
    c.on("tap", (ev) => { if (ev.target === c) onSelect(null); });
    cy.current = c;
    return () => { c.destroy(); cy.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [topology?.nodes.length, topology?.edges.length, topology?.nodes[0]?.id]);

  // Live alert rings when no incident is selected.
  useEffect(() => {
    const c = cy.current;
    if (!c || !topology) return;
    topology.nodes.forEach((n) => c.getElementById(n.id).toggleClass("firing", n.firing > 0 && !evidence));
  }, [topology, evidence]);

  // Evidence overlay for the selected incident.
  useEffect(() => {
    const c = cy.current;
    if (!c) return;
    c.batch(() => {
      c.remove(".ctx");
      c.elements().removeClass("alerting root path dim");
      if (!evidence) return;
      c.elements().addClass("dim");
      evidence.alerting.forEach((a) => c.getElementById(a).removeClass("dim").addClass("alerting"));
      for (const path of evidence.paths) {
        path.forEach((name, i) => {
          c.getElementById(name).removeClass("dim");
          if (i > 0) c.getElementById(`${path[i - 1]}->${name}`).removeClass("dim").addClass("path");
        });
      }
      if (evidence.root_cause) c.getElementById(evidence.root_cause).removeClass("dim alerting").addClass("root");

      // Context: changes, people and past incidents the finding cites, placed around their component.
      const items = [...evidence.context];
      evidence.paged.forEach((h) => {
        if (!items.some((i) => i.label === "Person" && i.key === h))
          items.push({ label: "Person", key: h, title: h, rel: "PAGED", component: evidence.root_cause, author: null });
      });
      const perAnchor: Record<string, number> = {};
      items.forEach((it) => {
        const anchor = it.component || evidence.root_cause;
        if (!anchor || !c.getElementById(anchor).nonempty()) return;
        const id = `ctx:${it.label}:${it.key}`;
        if (c.getElementById(id).nonempty()) return;
        const k = (perAnchor[anchor] = (perAnchor[anchor] ?? 0) + 1);
        const base = positions.current[anchor] ?? c.getElementById(anchor).position();
        const angle = -Math.PI / 2 + (k - 1) * 0.9;
        const text = it.label === "PullRequest" ? `PR ${it.key}: ${it.title}` :
          it.label === "Person" ? `@${it.key}` : `${it.key}: ${it.title ?? ""}`;
        c.add({ group: "nodes", data: { id, label: text }, classes: `ctx ctx-${it.label}`,
                position: { x: base.x + Math.cos(angle) * 150 + 90, y: base.y + Math.sin(angle) * 70 } });
        const rel = it.label === "Person" && evidence.paged.includes(it.key) ? "PAGED" : it.rel ?? "";
        const [src, dst] = rel === "PAGED" ? [anchor, id] : [id, anchor];
        c.add({ group: "edges", data: { id: `${id}->${anchor}`, source: src, target: dst, label: rel }, classes: "ctx" });
        if (it.author) {
          const pid = `ctx:Person:${it.author}`;
          if (c.getElementById(pid).empty()) {
            const p = c.getElementById(id).position();
            c.add({ group: "nodes", data: { id: pid, label: `@${it.author}` }, classes: "ctx ctx-Person",
                    position: { x: p.x + 40, y: p.y - 46 } });
          }
          c.add({ group: "edges", data: { id: `${pid}->${id}`, source: pid, target: id, label: "AUTHORED" }, classes: "ctx" });
        }
      });
    });
  }, [evidence]);

  useEffect(() => {
    const c = cy.current;
    if (!c) return;
    c.nodes().unselect();
    if (selected) c.getElementById(selected).select();
  }, [selected]);

  return (
    <div className="graph-wrap">
      <div ref={host} className="graph" />
      <div className="legend">
        <span><i className="dot svc" />Service</span>
        <span><i className="dot ds" />Datastore</span>
        <span><i className="dot topic" />Kafka topic</span>
        <span><i className="dot alert" />Alerting</span>
        <span><i className="dot root" />Root cause</span>
        <span><i className="line" />Failure path</span>
        <span className="muted">Node size = PageRank criticality</span>
      </div>
    </div>
  );
}
