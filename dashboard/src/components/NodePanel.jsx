import { Card, CardHeader, StatusDot, Badge } from './UI.jsx'

export function NodePanel({ nodes }) {
  return (
    <Card>
      <CardHeader label="Worker nodes" sub={`${nodes.length} connected`} />
      {nodes.length === 0 ? (
        <div style={{ padding: '24px 14px', textAlign: 'center', color: 'var(--text3)' }}>
          <div style={{ fontFamily: 'var(--mono)', fontSize: 11, marginBottom: 4 }}>NO NODES CONNECTED</div>
          <div style={{ fontSize: 11 }}>Start the agent on each worker VM and point it at this controller.</div>
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column' }}>
          {nodes.map((node, i) => (
            <NodeRow key={node.node_id} node={node} last={i === nodes.length - 1} />
          ))}
        </div>
      )}
    </Card>
  )
}

const CLOCK_WARN_MS = 50  // warn if clock delta exceeds this threshold

function NodeRow({ node, last }) {
  const caps = node.capabilities || []
  const clockDelta = node.clock_delta_ms
  const clockWarn  = clockDelta != null && clockDelta > CLOCK_WARN_MS

  return (
    <div style={{
      display: 'grid', gridTemplateColumns: '10px 1fr auto',
      gap: 10, alignItems: 'center',
      padding: '10px 14px',
      borderBottom: last ? 'none' : '1px solid var(--border)',
    }}>
      <StatusDot status={node.status} />
      <div style={{ minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 2 }}>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 12, fontWeight: 500 }}>
            {node.node_id}
          </span>
          <span style={{ color: 'var(--text3)', fontSize: 11 }}>{node.hostname}</span>
        </div>
        <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', alignItems: 'center' }}>
          <span style={{ fontSize: 10, color: 'var(--text3)', fontFamily: 'var(--mono)' }}>
            {node.ip_range}
          </span>
          {clockDelta != null && (
            <span
              title={clockWarn
                ? `Clock delta ${clockDelta.toFixed(1)} ms exceeds 50 ms — one-way latency readings may be inaccurate. Ensure NTP is running.`
                : `Clock delta ${clockDelta.toFixed(1)} ms — clocks are synchronized`}
              style={{
                fontFamily: 'var(--mono)',
                fontSize: 10,
                color: clockWarn ? 'var(--yellow)' : 'var(--text3)',
                cursor: 'default',
              }}
            >
              {clockWarn ? '⚠ ' : ''}Δt {clockDelta.toFixed(1)} ms
            </span>
          )}
          {clockDelta == null && (
            <span
              title="Clock sync check failed — NTP status unknown"
              style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text3)', cursor: 'default' }}
            >
              Δt —
            </span>
          )}
        </div>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <StatusBadge status={node.status} />
      </div>
    </div>
  )
}

function StatusBadge({ status }) {
  const cfg = {
    connected: { label: 'IDLE',     color: 'var(--text2)' },
    idle:      { label: 'IDLE',     color: 'var(--text2)' },
    running:   { label: 'RUNNING',  color: 'var(--accent)' },
    error:     { label: 'ERROR',    color: 'var(--red)' },
    offline:   { label: 'OFFLINE',  color: 'var(--text3)' },
    pending:   { label: 'PENDING',  color: 'var(--yellow)' },
  }
  const { label, color } = cfg[status] || cfg.idle
  return <Badge label={label} color={color} />
}
