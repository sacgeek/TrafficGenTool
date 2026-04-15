import { useMemo } from 'react'
import { Card, CardHeader, MOSGauge } from './UI.jsx'
import { mosColor, lossColor, latencyColor, jitterColor,
         fmtMs, fmtPct, streamTypeIcon, streamTypeColor } from '../lib/mos.js'

export function LiveStreamTable({ snapshots, activeSession }) {
  // Group latest snapshot per (node_id, session_id, stream_type)
  const rows = useMemo(() => {
    const map = {}
    for (const s of snapshots) {
      const key = `${s.node_id}|${s.session_id}|${s.stream_type}`
      map[key] = s
    }
    return Object.values(map).sort((a, b) =>
      `${a.stream_type}${a.node_id}`.localeCompare(`${b.stream_type}${b.node_id}`)
    )
  }, [snapshots])

  return (
    <Card>
      <CardHeader
        label="Live streams"
        sub={activeSession ? `session ${activeSession.session_id}` : 'no active session'}
        right={
          <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text3)' }}>
            {rows.length} stream{rows.length !== 1 ? 's' : ''}
          </span>
        }
      />

      {rows.length === 0 ? (
        <Empty />
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ borderBottom: '1px solid var(--border)' }}>
                {['Type', 'Node', 'Session', 'MOS', 'Loss', 'Latency', 'Jitter', 'Pkts recv'].map(h => (
                  <Th key={h}>{h}</Th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <StreamRow key={`${row.node_id}|${row.session_id}|${row.stream_type}`} row={row} odd={i % 2 === 1} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}

function Th({ children }) {
  return (
    <th style={{
      padding: '6px 12px', textAlign: 'left',
      fontFamily: 'var(--mono)', fontSize: 10, fontWeight: 600,
      letterSpacing: '0.06em', textTransform: 'uppercase',
      color: 'var(--text3)', whiteSpace: 'nowrap',
    }}>
      {children}
    </th>
  )
}

function StreamRow({ row, odd }) {
  const typeColor = streamTypeColor(row.stream_type)
  return (
    <tr style={{
      background: odd ? 'var(--bg2)' : 'transparent',
      borderBottom: '1px solid var(--border)',
      transition: 'background 0.1s',
    }}>
      <td style={{ padding: '8px 12px', whiteSpace: 'nowrap' }}>
        <span style={{ color: typeColor, marginRight: 5, fontSize: 10 }}>
          {streamTypeIcon(row.stream_type)}
        </span>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: typeColor }}>
          {row.stream_type}
        </span>
      </td>
      <td style={{ padding: '8px 12px' }}>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text2)' }}>
          {row.node_id}
        </span>
      </td>
      <td style={{ padding: '8px 12px' }}>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text3)' }}>
          {row.session_id?.slice(0, 8)}
        </span>
      </td>
      <td style={{ padding: '8px 12px', minWidth: 120 }}>
        <MOSGauge mos={row.mos_mos || 0} />
      </td>
      <td style={{ padding: '8px 12px' }}>
        <Metric value={fmtPct(row.loss_pct)} color={lossColor(row.loss_pct)} />
      </td>
      <td style={{ padding: '8px 12px' }}>
        <Metric value={fmtMs(row.latency_ms)} color={latencyColor(row.latency_ms)} />
      </td>
      <td style={{ padding: '8px 12px' }}>
        <Metric value={fmtMs(row.jitter_ms)} color={jitterColor(row.jitter_ms)} />
      </td>
      <td style={{ padding: '8px 12px' }}>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text2)' }}>
          {(row.packets_recv || 0).toLocaleString()}
        </span>
      </td>
    </tr>
  )
}

function Metric({ value, color }) {
  return (
    <span style={{ fontFamily: 'var(--mono)', fontSize: 12, color: color || 'var(--text)' }}>
      {value}
    </span>
  )
}

function Empty() {
  return (
    <div style={{ padding: '32px 14px', textAlign: 'center', color: 'var(--text3)' }}>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 11 }}>NO ACTIVE STREAMS</div>
      <div style={{ fontSize: 11, marginTop: 4 }}>Start a test session to see live telemetry here.</div>
    </div>
  )
}
