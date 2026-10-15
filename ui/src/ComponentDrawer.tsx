import { useEffect, useState } from "react";
import { api } from "./api";

export default function ComponentDrawer({ tenant, name, onClose }: { tenant: string; name: string; onClose: () => void }) {
  const [data, setData] = useState<Record<string, any> | null>(null);
  useEffect(() => {
    setData(null);
    api.component(tenant, name).then(setData).catch(() => setData(null));
  }, [tenant, name]);

  const o = data?.overview;
  return (
    <div className="drawer">
      <div className="drawer-head">
        <h3>{name}</h3>
        <button className="link" onClick={onClose}>close ✕</button>
      </div>
      {!data ? <span className="spinner" /> : (
        <>
          <div className="kv">
            <span>Kind</span><b>{o.kind}</b>
            <span>Owner</span><b>{o.owner_team}</b>
            <span>Tier</span><b>{o.tier}</b>
            <span>PageRank</span><b>{o.criticality}</b>
            <span>On call</span><b>{data.on_call[0]?.on_call ? `@${data.on_call[0].on_call} (until ${data.on_call[0].on_call_until})` : "—"}</b>
            <span>Depends on</span><b>{o.depends_on.join(", ") || "—"}</b>
            <span>Used by</span><b>{o.used_by.join(", ") || "—"}</b>
            <span>User flows</span><b>{o.flows.join(", ") || "—"}</b>
          </div>
          {data.experts.length > 0 && <>
            <h4>Who knows it</h4>
            <div className="chips">{data.experts.map((e: any) => <span key={e.person} className="chip chip-Person">@{e.person} · {e.changes} PRs</span>)}</div>
          </>}
          {data.recent_changes.length > 0 && <>
            <h4>Recent changes (72h, incl. dependencies)</h4>
            <ul>{data.recent_changes.map((c: any) => <li key={c.pr}>PR {c.pr} on <b>{c.component}</b>: {c.title} — @{c.author}, {c.merged_at}</li>)}</ul>
          </>}
          {data.decisions.length > 0 && <>
            <h4>Design decisions</h4>
            <ul>{data.decisions.map((d: any) => <li key={d.decision}><b>{d.decision}</b> {d.title} — @{d.made_by}</li>)}</ul>
          </>}
          {(data.fixes.past_fixes.length > 0 || data.fixes.lessons.length > 0) && <>
            <h4>Memory: past fixes & lessons</h4>
            <ul>
              {data.fixes.past_fixes.map((f: any) => <li key={f.incident}><b>{f.incident}</b> {f.kind}: {f.fix}</li>)}
              {data.fixes.lessons.map((l: any) => <li key={l.id}>💡 {l.text}</li>)}
            </ul>
          </>}
        </>
      )}
    </div>
  );
}
