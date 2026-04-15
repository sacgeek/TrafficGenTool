import { useMemo } from 'react'
import { Card } from './UI.jsx'
import { MOSGauge } from './UI.jsx'
import { streamTypeColor, streamTypeIcon } from '../lib/mos.js'

const TYPES = ['voice', 'video', 'screenshare', 'web', 'youtube']

export function MOSSummaryBar({ snapshots }) {
  const byType = useMemo(() => {
    const map = {}
    for (const s of snapshots) {
      if (!s.mos_mos || !s.stream_type) continue
      if (!map[s.stream_type]) map[s.stream_type] = []
      map[s.stream_type].push(s.mos_mos)
    }
    const result = {}
    for (const [type, vals] of Object.entries(map)) {
      result[type] = vals.reduce((a, b) => a + b, 0) / vals.length
    }
    return result
  }, [snapshots])

  const activeTypes = TYPES.filter(t => byType[t] !== undefined)

  if (!activeTypes.length) return null

  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: `repeat(${activeTypes.length}, 1fr)`,
      gap: 1,
      background: 'var(--border)',
      border: '1px solid var(--border)',
      borderRadius: 'var(--radius2)',
      overflow: 'hidden',
    }}>
      {activeTypes.map(type => (
        <div key={type} style={{
          background: 'var(--bg1)', padding: '12px 16px',
          display: 'flex', flexDirection: 'column', gap: 8,
          animation: 'fade-in 0.3s ease',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
            <span style={{ color: streamTypeColor(type), fontSize: 10 }}>
              {streamTypeIcon(type)}
            </span>
            <span style={{ fontFamily: 'var(--mono)', fontSize: 10, fontWeight: 600,
              letterSpacing: '0.08em', textTransform: 'uppercase', color: 'var(--text2)' }}>
              {type}
            </span>
          </div>
          <MOSGauge mos={byType[type]} size="lg" />
        </div>
      ))}
    </div>
  )
}
