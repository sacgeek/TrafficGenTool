import { useState } from 'react'
import { Card, CardHeader, Btn, StatusDot, MOSGauge } from './UI.jsx'
import { api } from '../hooks/useController.js'
import { fmtDuration } from '../lib/mos.js'

export function SessionHistory({ sessions, activeSession, onStop, onClear }) {
  const [stopping, setStopping] = useState(false)

  async function handleStop() {
    if (!activeSession) return
    setStopping(true)
    try { await onStop(activeSession.session_id) }
    finally { setStopping(false) }
  }

  const pastSessions = sessions.filter(
    s => s.status === 'stopped' || s.status === 'complete'
  )

  return (
    <Card>
      <CardHeader
        label="Sessions"
        right={
          <div style={{ display: 'flex', gap: 6 }}>
            {activeSession && (
              <Btn variant="danger" small disabled={stopping} onClick={handleStop}>
                {stopping ? 'STOPPING…' : '■ STOP'}
              </Btn>
            )}
            <Btn variant="ghost" small onClick={onClear}>
              CLEAR
            </Btn>
          </div>
        }
      />

      {/* Active session */}
      {activeSession && <ActiveSessionRow session={activeSession} />}

      {/* Past sessions */}
      {pastSessions.length === 0 && !activeSession && (
        <div style={{ padding: '20px 14px', color: 'var(--text3)', fontFamily: 'var(--mono)', fontSize: 11, textAlign: 'center' }}>
          NO SESSIONS YET
        </div>
      )}

      {pastSessions.map((s, i) => (
        <PastSessionRow key={s.session_id} session={s} last={i === pastSessions.length - 1} />
      ))}
    </Card>
  )
}

function ActiveSessionRow({ session }) {
  return (
    <div style={{
      padding: '12px 14px',
      background: '#1d2d4422',
      borderBottom: '1px solid var(--border)',
      borderLeft: '3px solid var(--accent)',
      animation: 'fade-in 0.3s ease',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
        <StatusDot status="running" />
        <span style={{ fontFamily: 'var(--mono)', fontWeight: 600, fontSize: 12 }}>
          {session.session_id}
        </span>
        <span style={{ color: 'var(--text3)', fontSize: 11 }}>{session.plan_name}</span>
        <span style={{ marginLeft: 'auto', fontFamily: 'var(--mono)', fontSize: 10,
          color: 'var(--accent)', letterSpacing: '0.05em' }}>
          RUNNING
        </span>
      </div>
      <div style={{ display: 'flex', gap: 16, fontSize: 11, color: 'var(--text2)', fontFamily: 'var(--mono)' }}>
        <span>{session.node_ids?.join(', ')}</span>
        <span>{session.snapshot_count || 0} snapshots</span>
        {session.start_time && (
          <span>↑ {new Date(session.start_time * 1000).toLocaleTimeString()}</span>
        )}
      </div>
    </div>
  )
}

function PastSessionRow({ session, last }) {
  const [exporting, setExporting] = useState(false)

  function download(url, filename) {
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
  }

  return (
    <div style={{
      padding: '10px 14px',
      borderBottom: last ? 'none' : '1px solid var(--border)',
      display: 'flex', alignItems: 'center', gap: 10,
    }}>
      <StatusDot status={session.status} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 12 }}>{session.session_id}</span>
          <span style={{ color: 'var(--text3)', fontSize: 11, overflow: 'hidden',
            textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {session.plan_name}
          </span>
        </div>
        <div style={{ display: 'flex', gap: 10, fontSize: 10, color: 'var(--text3)',
          fontFamily: 'var(--mono)', marginTop: 2 }}>
          <span>{fmtDuration(session.duration_s)}</span>
          <span>{session.snapshot_count || 0} snapshots</span>
          {session.end_time && (
            <span>{new Date(session.end_time * 1000).toLocaleTimeString()}</span>
          )}
        </div>
      </div>
      <div style={{ display: 'flex', gap: 4, flexShrink: 0 }}>
        <Btn small variant="ghost"
          onClick={() => download(api.exportCsv(session.session_id), `netlab-${session.session_id}.csv`)}>
          CSV
        </Btn>
        <Btn small variant="ghost"
          onClick={() => download(api.exportPdf(session.session_id), `netlab-${session.session_id}.pdf`)}>
          PDF
        </Btn>
      </div>
    </div>
  )
}
