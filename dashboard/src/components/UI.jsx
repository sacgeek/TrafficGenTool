import { mosColor, mosLabel, mosBarWidth } from '../lib/mos.js'

// ── Layout shell ──────────────────────────────────────────────────────────────

export function Card({ children, style, className = '' }) {
  return (
    <div className={className} style={{
      background: 'var(--bg1)',
      border: '1px solid var(--border)',
      borderRadius: 'var(--radius2)',
      ...style,
    }}>
      {children}
    </div>
  )
}

export function CardHeader({ label, sub, right }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      padding: '10px 14px', borderBottom: '1px solid var(--border)',
    }}>
      <div>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 11, fontWeight: 600,
          letterSpacing: '0.08em', textTransform: 'uppercase', color: 'var(--text2)' }}>
          {label}
        </span>
        {sub && <span style={{ marginLeft: 8, color: 'var(--text3)', fontSize: 11 }}>{sub}</span>}
      </div>
      {right && <div>{right}</div>}
    </div>
  )
}

// ── Status indicators ─────────────────────────────────────────────────────────

export function StatusDot({ status }) {
  const colors = {
    connected: 'var(--green)',
    running:   'var(--accent)',
    idle:      'var(--green)',
    error:     'var(--red)',
    offline:   'var(--border2)',
    pending:   'var(--yellow)',
    complete:  'var(--text2)',
    stopped:   'var(--text3)',
  }
  const animate = ['connected','idle','running','pending'].includes(status)
  return (
    <span style={{
      display: 'inline-block', width: 7, height: 7, borderRadius: '50%',
      background: colors[status] || 'var(--text3)',
      animation: animate ? 'pulse-dot 1.8s ease-in-out infinite' : 'none',
      flexShrink: 0,
    }} />
  )
}

export function Badge({ label, color = 'var(--text2)', bg = 'var(--bg3)' }) {
  return (
    <span style={{
      fontFamily: 'var(--mono)', fontSize: 10, fontWeight: 500,
      letterSpacing: '0.06em', textTransform: 'uppercase',
      color, background: bg, border: `1px solid ${color}22`,
      borderRadius: 3, padding: '2px 6px',
    }}>
      {label}
    </span>
  )
}

// ── MOS gauge ─────────────────────────────────────────────────────────────────

export function MOSGauge({ mos, size = 'md' }) {
  const color  = mosColor(mos)
  const label  = mosLabel(mos)
  const width  = mosBarWidth(mos)
  const isLg   = size === 'lg'

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
        <span style={{
          fontFamily: 'var(--mono)', fontWeight: 600,
          fontSize: isLg ? 28 : 18, color,
          lineHeight: 1,
        }}>
          {mos > 0 ? mos.toFixed(2) : '—'}
        </span>
        {mos > 0 && (
          <span style={{ fontSize: 11, color: 'var(--text2)' }}>{label}</span>
        )}
      </div>
      <div style={{
        height: isLg ? 5 : 3, background: 'var(--bg3)',
        borderRadius: 99, overflow: 'hidden', width: isLg ? 140 : 80,
      }}>
        <div style={{
          height: '100%', width: `${width}%`,
          background: color, borderRadius: 99,
          transition: 'width 0.6s ease, background 0.4s ease',
        }} />
      </div>
    </div>
  )
}

// ── Metric cell ───────────────────────────────────────────────────────────────

export function MetricCell({ label, value, color, unit = '' }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      <span style={{ fontSize: 10, color: 'var(--text3)', fontFamily: 'var(--mono)',
        letterSpacing: '0.05em', textTransform: 'uppercase' }}>
        {label}
      </span>
      <span style={{ fontFamily: 'var(--mono)', fontSize: 14, fontWeight: 500, color: color || 'var(--text)' }}>
        {value}{unit && <span style={{ fontSize: 10, color: 'var(--text3)', marginLeft: 2 }}>{unit}</span>}
      </span>
    </div>
  )
}

// ── Btn ───────────────────────────────────────────────────────────────────────

export function Btn({ children, onClick, variant = 'default', disabled, style, small }) {
  const variants = {
    default:  { background: 'var(--bg3)', border: '1px solid var(--border2)', color: 'var(--text)' },
    primary:  { background: 'var(--accent)', border: '1px solid var(--accent2)', color: '#fff' },
    danger:   { background: '#7f1d1d', border: '1px solid var(--red)', color: 'var(--red)' },
    ghost:    { background: 'transparent', border: '1px solid var(--border)', color: 'var(--text2)' },
    success:  { background: '#14532d', border: '1px solid var(--green)', color: 'var(--green)' },
  }
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      style={{
        ...variants[variant],
        borderRadius: 'var(--radius)',
        padding: small ? '4px 10px' : '6px 14px',
        fontSize: small ? 11 : 12,
        fontFamily: 'var(--mono)',
        fontWeight: 500,
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.45 : 1,
        transition: 'opacity 0.15s, background 0.15s',
        whiteSpace: 'nowrap',
        ...style,
      }}
    >
      {children}
    </button>
  )
}

// ── Form fields ───────────────────────────────────────────────────────────────

export function Field({ label, hint, children, error }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      <label style={{ fontSize: 11, color: 'var(--text2)', fontFamily: 'var(--mono)',
        letterSpacing: '0.05em', textTransform: 'uppercase' }}>
        {label}
      </label>
      {children}
      {hint  && <span style={{ fontSize: 10, color: 'var(--text3)' }}>{hint}</span>}
      {error && <span style={{ fontSize: 10, color: 'var(--red)' }}>{error}</span>}
    </div>
  )
}

export function Input({ value, onChange, type = 'text', min, max, placeholder, style }) {
  return (
    <input
      type={type} value={value} onChange={onChange}
      min={min} max={max} placeholder={placeholder}
      style={{
        background: 'var(--bg2)', border: '1px solid var(--border)',
        borderRadius: 'var(--radius)', color: 'var(--text)',
        fontFamily: 'var(--mono)', fontSize: 12,
        padding: '6px 10px', outline: 'none', width: '100%',
        transition: 'border-color 0.15s',
        ...style,
      }}
      onFocus={e => e.target.style.borderColor = 'var(--accent)'}
      onBlur={e  => e.target.style.borderColor = 'var(--border)'}
    />
  )
}

export function NumericInput({ value, onChange, min = 0, max, label, hint }) {
  return (
    <Field label={label} hint={hint}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
        <button
          onClick={() => onChange(Math.max(min, value - 1))}
          style={{ width: 26, height: 28, background: 'var(--bg3)', border: '1px solid var(--border)',
            borderRadius: 'var(--radius)', color: 'var(--text)', fontSize: 14, cursor: 'pointer' }}>
          −
        </button>
        <input
          type="number" value={value}
          onChange={e => onChange(Math.max(min, Math.min(max || 9999, +e.target.value || 0)))}
          min={min} max={max}
          style={{
            width: 52, textAlign: 'center',
            background: 'var(--bg2)', border: '1px solid var(--border)',
            borderRadius: 'var(--radius)', color: 'var(--text)',
            fontFamily: 'var(--mono)', fontSize: 13, padding: '4px 6px',
            outline: 'none',
          }}
        />
        <button
          onClick={() => onChange(Math.min(max || 9999, value + 1))}
          style={{ width: 26, height: 28, background: 'var(--bg3)', border: '1px solid var(--border)',
            borderRadius: 'var(--radius)', color: 'var(--text)', fontSize: 14, cursor: 'pointer' }}>
          +
        </button>
      </div>
    </Field>
  )
}

// ── Divider ───────────────────────────────────────────────────────────────────

export function Divider() {
  return <div style={{ height: 1, background: 'var(--border)', margin: '4px 0' }} />
}
