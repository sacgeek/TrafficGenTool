import { StatusDot } from './UI.jsx'

/**
 * Sticky top bar showing:
 *   left  — app name
 *   centre — active session badge (if a test is running)
 *   right  — connected node count + WebSocket connection status
 *
 * Props
 *   connected      {boolean}  WebSocket connection state
 *   nodes          {Array}    connected worker nodes
 *   activeSession  {object|null}  running session summary (or null)
 */
export function Header({ connected, nodes = [], activeSession }) {
  const nodeCount  = nodes.length
  const runningCount = nodes.filter(n => n.status === 'running').length

  return (
    <header style={{
      display:         'flex',
      alignItems:      'center',
      justifyContent:  'space-between',
      height:          46,
      padding:         '0 16px',
      background:      'var(--bg1)',
      borderBottom:    '1px solid var(--border)',
      flexShrink:      0,
      position:        'sticky',
      top:             0,
      zIndex:          100,
    }}>

      {/* ── Left: brand ─────────────────────────────────────────── */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <span style={{ fontSize: 15, lineHeight: 1 }}>📡</span>
        <span style={{
          fontFamily:    'var(--mono)',
          fontSize:      13,
          fontWeight:    600,
          letterSpacing: '0.04em',
          color:         'var(--text)',
        }}>
          NetLab
        </span>
        <span style={{
          fontFamily: 'var(--mono)',
          fontSize:   11,
          color:      'var(--text3)',
          marginLeft: 2,
        }}>
          Traffic Generator
        </span>
      </div>

      {/* ── Centre: active session badge ────────────────────────── */}
      <div>
        {activeSession ? (
          <div style={{
            display:       'flex',
            alignItems:    'center',
            gap:           8,
            background:    'var(--bg3)',
            border:        '1px solid var(--border2)',
            borderRadius:  'var(--radius)',
            padding:       '3px 10px',
          }}>
            <StatusDot status="running" />
            <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text2)' }}>
              {activeSession.plan_name || 'Unnamed test'}
            </span>
            <span style={{
              fontFamily:  'var(--mono)',
              fontSize:    10,
              color:       'var(--accent)',
              fontWeight:  600,
              letterSpacing: '0.05em',
            }}>
              RUNNING
            </span>
          </div>
        ) : null}
      </div>

      {/* ── Right: node count + WS status ───────────────────────── */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>

        {/* Node count */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{
            fontFamily: 'var(--mono)',
            fontSize:   11,
            color:      'var(--text3)',
            letterSpacing: '0.04em',
          }}>
            NODES
          </span>
          <span style={{
            fontFamily: 'var(--mono)',
            fontSize:   13,
            fontWeight: 600,
            color:      nodeCount > 0 ? 'var(--text)' : 'var(--text3)',
          }}>
            {runningCount > 0 ? `${runningCount}/${nodeCount}` : nodeCount}
          </span>
          {runningCount > 0 && (
            <span style={{ fontSize: 10, color: 'var(--text3)' }}>running</span>
          )}
        </div>

        {/* Divider */}
        <div style={{ width: 1, height: 16, background: 'var(--border)' }} />

        {/* WS connection pill */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
          <StatusDot status={connected ? 'connected' : 'offline'} />
          <span style={{
            fontFamily: 'var(--mono)',
            fontSize:   11,
            color:      connected ? 'var(--text2)' : 'var(--text3)',
          }}>
            {connected ? 'Connected' : 'Disconnected'}
          </span>
        </div>
      </div>
    </header>
  )
}
