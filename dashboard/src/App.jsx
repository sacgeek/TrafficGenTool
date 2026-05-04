import { useState, useCallback, useRef } from 'react'
import { useControllerWS, api } from './hooks/useController.js'
import { Header }         from './components/Header.jsx'
import { NodePanel }      from './components/NodePanel.jsx'
import { SetupPanel }     from './components/SetupPanel.jsx'
import { LiveStreamTable } from './components/LiveStreamTable.jsx'
import { MOSChart }       from './components/MOSChart.jsx'
import { MOSSummaryBar }  from './components/MOSSummaryBar.jsx'
import { SessionHistory } from './components/SessionHistory.jsx'

// ── Alert banner ──────────────────────────────────────────────────────────────

function AlertBanner({ alerts }) {
  if (!alerts || alerts.length === 0) return null
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 10 }}>
      {alerts.map(a => (
        <div key={a.alert_id} style={{
          display: 'flex', alignItems: 'center', gap: 10,
          background: '#3a1a1a', border: '1px solid #c0392b',
          borderRadius: 'var(--radius)', padding: '8px 14px',
          color: '#e74c3c', fontFamily: 'var(--mono)', fontSize: 12,
        }}>
          <span style={{ fontWeight: 700, letterSpacing: '0.05em' }}>⚠ ALERT</span>
          <span style={{ color: 'var(--text2)' }}>
            {a.stream_type.toUpperCase()} · {a.node_id}
          </span>
          <span>
            MOS <strong style={{ color: '#e74c3c' }}>{a.mos_at_alert.toFixed(2)}</strong>
            {' '}below floor <strong>{a.floor.toFixed(1)}</strong>
            {' '}for &gt;{Math.round((Date.now() / 1000) - a.fired_at)}s
          </span>
        </div>
      ))}
    </div>
  )
}

export default function App() {
  const { nodes, sessions, snapshots, activeAlerts, activeSession, connected, refreshSessions, clearSnapshots } = useControllerWS()
  const [tab, setTab] = useState('live')  // 'live' | 'setup'

  const handleStop = useCallback(async (sessionId) => {
    await api.stopSession(sessionId)
  }, [])

  const handleClear = useCallback(async () => {
    await api.clearSessions()
    await refreshSessions()
    clearSnapshots()
  }, [refreshSessions, clearSnapshots])

  const handleSessionStarted = useCallback((sessionId) => {
    setTab('live')
  }, [])

  // Show all accumulated snapshots in the live view
  const liveSnaps = snapshots

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh', overflow: 'hidden' }}>
      <Header connected={connected} nodes={nodes} activeSession={activeSession} />

      <div style={{ flex: 1, overflow: 'auto', padding: '16px' }}>

        {/* Tab bar */}
        <div style={{ display: 'flex', gap: 2, marginBottom: 14,
          background: 'var(--bg1)', border: '1px solid var(--border)',
          borderRadius: 'var(--radius)', padding: 3, width: 'fit-content' }}>
          {[['live', 'Live Monitor'], ['setup', 'Test Setup']].map(([key, label]) => (
            <button key={key} onClick={() => setTab(key)} style={{
              fontFamily: 'var(--mono)', fontSize: 11, fontWeight: 500,
              letterSpacing: '0.06em', textTransform: 'uppercase',
              padding: '5px 14px', border: 'none', borderRadius: 3, cursor: 'pointer',
              background: tab === key ? 'var(--bg3)' : 'transparent',
              color: tab === key ? 'var(--text)' : 'var(--text3)',
              transition: 'all 0.15s',
            }}>
              {label}
            </button>
          ))}
        </div>

        <AlertBanner alerts={activeAlerts} />

        {tab === 'live' && (
          <LiveTab
            nodes={nodes}
            snapshots={liveSnaps}
            sessions={sessions}
            activeSession={activeSession}
            onStop={handleStop}
            onClear={handleClear}
          />
        )}
        {tab === 'setup' && (
          <SetupTab
            nodes={nodes}
            activeSession={activeSession}
            onSessionStarted={handleSessionStarted}
            sessions={sessions}
            onStop={handleStop}
            onClear={handleClear}
          />
        )}
      </div>
    </div>
  )
}

// ── Live tab layout ───────────────────────────────────────────────────────────

function LiveTab({ nodes, snapshots, sessions, activeSession, onStop, onClear }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {/* MOS aggregate bar - only visible when there are streams */}
      <MOSSummaryBar snapshots={snapshots} />

      {/* Two-column: chart left, nodes right */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 280px', gap: 12 }}>
        <MOSChart snapshots={snapshots} />
        <NodePanel nodes={nodes} />
      </div>

      {/* Stream table full width */}
      <LiveStreamTable snapshots={snapshots} activeSession={activeSession} />

      {/* Session history */}
      <SessionHistory
        sessions={sessions}
        activeSession={activeSession}
        onStop={onStop}
        onClear={onClear}
      />
    </div>
  )
}

// ── Setup tab layout — resizable split ───────────────────────────────────────

function SetupTab({ nodes, activeSession, onSessionStarted, sessions, onStop, onClear }) {
  // Default to roughly half the viewport; clamp between 300 px and viewport−300 px
  const [leftWidth, setLeftWidth] = useState(
    () => Math.max(300, Math.floor((window.innerWidth - 48) / 2))
  )
  const containerRef = useRef(null)

  function onDragHandlePointerDown(e) {
    e.preventDefault()
    const container = containerRef.current
    if (!container) return

    function onMove(ev) {
      const rect    = container.getBoundingClientRect()
      const newWidth = ev.clientX - rect.left - 8   // 8 = half handle width
      setLeftWidth(Math.max(300, Math.min(newWidth, rect.width - 300)))
    }
    function onUp() {
      document.removeEventListener('pointermove', onMove)
      document.removeEventListener('pointerup',   onUp)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }

    document.body.style.cursor     = 'col-resize'
    document.body.style.userSelect = 'none'
    document.addEventListener('pointermove', onMove)
    document.addEventListener('pointerup',   onUp)
  }

  return (
    <div ref={containerRef} style={{ display: 'flex', alignItems: 'start', gap: 0 }}>

      {/* Left column — SetupPanel + NodePanel */}
      <div style={{ width: leftWidth, flexShrink: 0, display: 'flex', flexDirection: 'column', gap: 12 }}>
        <SetupPanel nodes={nodes} activeSession={activeSession} onSessionStarted={onSessionStarted} />
        <NodePanel nodes={nodes} />
      </div>

      {/* Drag handle */}
      <div
        onPointerDown={onDragHandlePointerDown}
        title="Drag to resize"
        style={{
          width: 16, flexShrink: 0, alignSelf: 'stretch',
          cursor: 'col-resize',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          // subtle visual affordance — brightens on hover
        }}
        onMouseEnter={e => e.currentTarget.querySelector('div').style.background = 'var(--accent)'}
        onMouseLeave={e => e.currentTarget.querySelector('div').style.background = 'var(--border2)'}
      >
        <div style={{
          width: 3, height: 48, borderRadius: 2,
          background: 'var(--border2)', transition: 'background 0.15s',
          pointerEvents: 'none',
        }} />
      </div>

      {/* Right column — Session history */}
      <div style={{ flex: 1, minWidth: 0 }}>
        <SessionHistory
          sessions={sessions}
          activeSession={activeSession}
          onStop={onStop}
          onClear={onClear}
        />
      </div>

    </div>
  )
}
