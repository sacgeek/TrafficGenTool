import { useState, useCallback } from 'react'
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
  const { nodes, sessions, snapshots, activeAlerts, activeSession, connected, refreshSessions } = useControllerWS()
  const [tab, setTab] = useState('live')  // 'live' | 'setup'

  const handleStop = useCallback(async (sessionId) => {
    await api.stopSession(sessionId)
  }, [])

  const handleClear = useCallback(async () => {
    await api.clearSessions()
    await refreshSessions()
  }, [refreshSessions])

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

// ── Setup tab layout ──────────────────────────────────────────────────────────

function SetupTab({ nodes, activeSession, onSessionStarted, sessions, onStop, onClear }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '380px 1fr', gap: 12, alignItems: 'start' }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <SetupPanel nodes={nodes} activeSession={activeSession} onSessionStarted={onSessionStarted} />
        <NodePanel nodes={nodes} />
      </div>
      <SessionHistory
        sessions={sessions}
        activeSession={activeSession}
        onStop={onStop}
        onClear={onClear}
      />
    </div>
  )
}
