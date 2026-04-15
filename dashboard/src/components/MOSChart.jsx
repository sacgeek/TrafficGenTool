import { useMemo } from 'react'
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer, ReferenceLine } from 'recharts'
import { Card, CardHeader } from './UI.jsx'
import { streamTypeColor } from '../lib/mos.js'

const STREAM_TYPES = ['voice', 'video', 'screenshare', 'web', 'youtube']

export function MOSChart({ snapshots }) {
  // Build a time series: bucket by 2-second windows, average MOS per stream type
  const chartData = useMemo(() => {
    if (!snapshots.length) return []

    // Group into 2s buckets
    const buckets = {}
    for (const s of snapshots) {
      if (!s.mos_mos) continue
      const bucket = Math.floor(s.timestamp / 2) * 2
      if (!buckets[bucket]) buckets[bucket] = {}
      if (!buckets[bucket][s.stream_type]) buckets[bucket][s.stream_type] = []
      buckets[bucket][s.stream_type].push(s.mos_mos)
    }

    const times = Object.keys(buckets).sort()
    if (!times.length) return []

    const t0 = +times[0]
    return times.map(t => {
      const row = { t: Math.round((+t - t0)) }
      for (const [type, vals] of Object.entries(buckets[t])) {
        row[type] = +(vals.reduce((a, b) => a + b, 0) / vals.length).toFixed(2)
      }
      return row
    })
  }, [snapshots])

  const activeTypes = useMemo(() => {
    const seen = new Set()
    for (const s of snapshots) if (s.stream_type) seen.add(s.stream_type)
    return STREAM_TYPES.filter(t => seen.has(t))
  }, [snapshots])

  const CustomTooltip = ({ active, payload, label }) => {
    if (!active || !payload?.length) return null
    return (
      <div style={{
        background: 'var(--bg2)', border: '1px solid var(--border)',
        borderRadius: 'var(--radius)', padding: '8px 12px', fontSize: 11,
        fontFamily: 'var(--mono)',
      }}>
        <div style={{ color: 'var(--text3)', marginBottom: 4 }}>t+{label}s</div>
        {payload.map(p => (
          <div key={p.dataKey} style={{ color: p.color, display: 'flex', gap: 8 }}>
            <span>{p.dataKey}</span>
            <span style={{ color: 'var(--text)' }}>{p.value?.toFixed(2)}</span>
          </div>
        ))}
      </div>
    )
  }

  return (
    <Card>
      <CardHeader label="MOS over time" sub="2-second averages" />
      <div style={{ padding: '8px 8px 4px' }}>
        {chartData.length < 2 ? (
          <div style={{ height: 160, display: 'flex', alignItems: 'center', justifyContent: 'center',
            color: 'var(--text3)', fontFamily: 'var(--mono)', fontSize: 11 }}>
            AWAITING DATA
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={160}>
            <LineChart data={chartData} margin={{ top: 4, right: 8, bottom: 0, left: -16 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
              <XAxis
                dataKey="t"
                tick={{ fontFamily: 'var(--mono)', fontSize: 9, fill: 'var(--text3)' }}
                tickFormatter={v => `${v}s`}
                stroke="var(--border)"
              />
              <YAxis
                domain={[1, 5]}
                ticks={[1, 2, 3, 4, 5]}
                tick={{ fontFamily: 'var(--mono)', fontSize: 9, fill: 'var(--text3)' }}
                stroke="var(--border)"
              />
              <Tooltip content={<CustomTooltip />} />
              <ReferenceLine y={4.0} stroke="var(--green)"   strokeDasharray="4 4" strokeOpacity={0.4} />
              <ReferenceLine y={3.6} stroke="var(--yellow)"  strokeDasharray="4 4" strokeOpacity={0.4} />
              <ReferenceLine y={3.1} stroke="var(--orange)"  strokeDasharray="4 4" strokeOpacity={0.4} />
              {activeTypes.map(type => (
                <Line
                  key={type}
                  type="monotone"
                  dataKey={type}
                  stroke={streamTypeColor(type)}
                  strokeWidth={1.5}
                  dot={false}
                  activeDot={{ r: 3, fill: streamTypeColor(type) }}
                  connectNulls
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        )}
        <div style={{ display: 'flex', gap: 10, padding: '4px 8px', flexWrap: 'wrap' }}>
          {[
            { label: '≥4.0 Good',  color: 'var(--green)' },
            { label: '≥3.6 Fair',  color: 'var(--yellow)' },
            { label: '≥3.1 Poor',  color: 'var(--orange)' },
            { label: '<3.1 Bad',   color: 'var(--red)' },
          ].map(({ label, color }) => (
            <span key={label} style={{ fontFamily: 'var(--mono)', fontSize: 9, color }}>
              {label}
            </span>
          ))}
        </div>
      </div>
    </Card>
  )
}
