export function mosColor(mos) {
  if (mos >= 4.0) return 'var(--green)'
  if (mos >= 3.6) return 'var(--yellow)'
  if (mos >= 3.1) return 'var(--orange)'
  return 'var(--red)'
}

export function mosLabel(mos) {
  if (mos >= 4.3) return 'Excellent'
  if (mos >= 4.0) return 'Good'
  if (mos >= 3.6) return 'Fair'
  if (mos >= 3.1) return 'Poor'
  return 'Bad'
}

export function mosBarWidth(mos) {
  // 1.0–5.0 → 0–100%
  return Math.max(0, Math.min(100, ((mos - 1) / 4) * 100))
}

export function lossColor(pct) {
  if (pct < 1)  return 'var(--green)'
  if (pct < 3)  return 'var(--yellow)'
  if (pct < 8)  return 'var(--orange)'
  return 'var(--red)'
}

export function latencyColor(ms) {
  if (ms < 100) return 'var(--green)'
  if (ms < 200) return 'var(--yellow)'
  if (ms < 400) return 'var(--orange)'
  return 'var(--red)'
}

export function jitterColor(ms) {
  if (ms < 30)  return 'var(--green)'
  if (ms < 60)  return 'var(--yellow)'
  return 'var(--red)'
}

export function fmtMs(ms) {
  if (!ms && ms !== 0) return '—'
  return `${ms.toFixed(1)} ms`
}

export function fmtPct(pct) {
  if (!pct && pct !== 0) return '—'
  return `${pct.toFixed(2)}%`
}

export function fmtDuration(s) {
  if (!s) return '—'
  const m = Math.floor(s / 60)
  const sec = Math.floor(s % 60)
  return m > 0 ? `${m}m ${sec}s` : `${sec}s`
}

export function streamTypeIcon(type) {
  const icons = {
    voice:       '◆',
    video:       '▶',
    screenshare: '⊡',
    web:         '○',
    youtube:     '▷',
  }
  return icons[type] || '·'
}

export function streamTypeColor(type) {
  const colors = {
    voice:       '#60a5fa',
    video:       '#a78bfa',
    screenshare: '#34d399',
    web:         '#fb923c',
    youtube:     '#f87171',
  }
  return colors[type] || 'var(--text2)'
}
