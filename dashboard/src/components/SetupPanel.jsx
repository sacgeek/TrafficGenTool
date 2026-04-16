import { useState, useEffect } from 'react'
import { Card, CardHeader, Btn, Field, Input, NumericInput } from './UI.jsx'
import { api } from '../hooks/useController.js'

const DEFAULT_PLAN = {
  name:          'Lab test',
  voice_calls:   2,
  video_calls:   1,
  screen_shares: 1,
  web_users:     0,
  web_urls:      '',
  youtube_users: 0,
  youtube_url:   '',
  duration_s:    60,
}

const DEFAULT_YOUTUBE_URL = 'https://www.youtube.com/watch?v=UgHKb_7884o'

export function SetupPanel({ nodes, activeSession, onSessionStarted }) {
  const [plan,      setPlan]      = useState(DEFAULT_PLAN)
  const [urlInput,  setUrlInput]  = useState('https://example.com\nhttps://cloudflare.com')
  const [loading,   setLoading]   = useState(false)
  const [error,     setError]     = useState(null)
  const [ytOk,      setYtOk]      = useState(null)  // null=unchecked true/false

  const canRun = nodes.length > 0 && !activeSession

  // YouTube reachability check
  useEffect(() => {
    api.checkYoutube().then(r => setYtOk(r.reachable)).catch(() => setYtOk(false))
  }, [])

  function set(key, val) {
    setPlan(p => ({ ...p, [key]: val }))
  }

  async function handleStart() {
    setError(null)
    setLoading(true)
    try {
      const urls = urlInput.split('\n').map(u => u.trim()).filter(Boolean)
      const payload = {
        ...plan,
        web_urls: urls,
        youtube_users: ytOk ? plan.youtube_users : 0,
        youtube_url: plan.youtube_url.trim() || '',   // empty → worker uses its built-in default
      }
      const res = await api.createSession(payload)
      onSessionStarted?.(res.session_id)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  const totalUdp = plan.voice_calls + plan.video_calls + plan.screen_shares
  const totalWeb = plan.web_users + (ytOk ? plan.youtube_users : 0)

  return (
    <Card>
      <CardHeader label="Test plan" sub="configure and launch" />
      <div style={{ padding: '14px', display: 'flex', flexDirection: 'column', gap: 16 }}>

        {/* Plan name */}
        <Field label="Plan name">
          <Input value={plan.name} onChange={e => set('name', e.target.value)} placeholder="My lab test" />
        </Field>

        {/* UDP call section */}
        <Section label="UDP calls · Teams ports" color="var(--accent)">
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12 }}>
            <NumericInput
              label="Voice calls" hint="Bidir · port 3478"
              value={plan.voice_calls} min={0} max={20}
              onChange={v => set('voice_calls', v)}
            />
            <NumericInput
              label="Video calls" hint="Bidir · port 3479"
              value={plan.video_calls} min={0} max={20}
              onChange={v => set('video_calls', v)}
            />
            <NumericInput
              label="Screen shares" hint="1→many · port 3480"
              value={plan.screen_shares} min={0} max={10}
              onChange={v => set('screen_shares', v)}
            />
          </div>
        </Section>

        {/* Web section */}
        <Section label="Web & streaming">
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 10 }}>
            <NumericInput
              label="Web users" hint="Playwright headless"
              value={plan.web_users} min={0} max={20}
              onChange={v => set('web_users', v)}
            />
            <NumericInput
              label="YouTube streams" hint={ytOk === false ? '⚠ unreachable' : ytOk ? '✓ reachable' : 'checking…'}
              value={plan.youtube_users} min={0} max={20}
              onChange={v => set('youtube_users', v)}
            />
          </div>
          {plan.youtube_users > 0 && ytOk !== false && (
            <Field label="YouTube video URL" hint={`Leave blank to use default — ${DEFAULT_YOUTUBE_URL}`}>
              <Input
                value={plan.youtube_url}
                onChange={e => set('youtube_url', e.target.value)}
                placeholder={DEFAULT_YOUTUBE_URL}
              />
            </Field>
          )}
          <Field label="URLs to surf" hint="One per line — each web user cycles through all URLs">
            <textarea
              value={urlInput}
              onChange={e => setUrlInput(e.target.value)}
              rows={4}
              style={{
                width: '100%', background: 'var(--bg2)', border: '1px solid var(--border)',
                borderRadius: 'var(--radius)', color: 'var(--text)', fontFamily: 'var(--mono)',
                fontSize: 11, padding: '7px 10px', outline: 'none', resize: 'vertical',
                lineHeight: 1.6,
              }}
              onFocus={e  => e.target.style.borderColor = 'var(--accent)'}
              onBlur={e   => e.target.style.borderColor = 'var(--border)'}
            />
          </Field>
        </Section>

        {/* Duration */}
        <Field label="Duration (seconds)" hint="0 = run until manually stopped">
          <Input
            type="number" value={plan.duration_s} min={0}
            onChange={e => set('duration_s', +e.target.value || 0)}
          />
        </Field>

        {/* Summary bar */}
        <SummaryBar plan={plan} totalUdp={totalUdp} totalWeb={totalWeb} nodes={nodes} />

        {/* Error */}
        {error && (
          <div style={{ background: '#7f1d1d22', border: '1px solid var(--red)',
            borderRadius: 'var(--radius)', padding: '8px 12px',
            color: 'var(--red)', fontFamily: 'var(--mono)', fontSize: 11 }}>
            {error}
          </div>
        )}

        {/* Launch button */}
        <Btn
          variant={canRun ? 'primary' : 'ghost'}
          disabled={!canRun || loading}
          onClick={handleStart}
          style={{ width: '100%', padding: '10px', fontSize: 13 }}
        >
          {loading ? 'STARTING…' : activeSession ? 'SESSION RUNNING' : nodes.length === 0 ? 'NO NODES CONNECTED' : '▶  START TEST'}
        </Btn>
      </div>
    </Card>
  )
}

function Section({ label, color = 'var(--text3)', children }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div style={{
        fontFamily: 'var(--mono)', fontSize: 10, fontWeight: 600,
        letterSpacing: '0.1em', textTransform: 'uppercase',
        color, display: 'flex', alignItems: 'center', gap: 8,
      }}>
        <span>{label}</span>
        <div style={{ flex: 1, height: 1, background: 'var(--border)' }} />
      </div>
      {children}
    </div>
  )
}

function SummaryBar({ plan, totalUdp, totalWeb, nodes }) {
  const items = [
    { label: 'UDP sessions', value: totalUdp, color: 'var(--accent)' },
    { label: 'Web sessions', value: totalWeb, color: 'var(--orange)' },
    { label: 'Nodes',        value: nodes.length, color: 'var(--green)' },
    { label: 'Duration',     value: plan.duration_s ? `${plan.duration_s}s` : '∞', color: 'var(--text2)' },
  ]
  return (
    <div style={{
      display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)',
      gap: 1, background: 'var(--border)',
      border: '1px solid var(--border)', borderRadius: 'var(--radius)',
      overflow: 'hidden',
    }}>
      {items.map(({ label, value, color }) => (
        <div key={label} style={{
          background: 'var(--bg2)', padding: '8px 10px',
          display: 'flex', flexDirection: 'column', gap: 2,
        }}>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text3)',
            textTransform: 'uppercase', letterSpacing: '0.05em' }}>
            {label}
          </span>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 16, fontWeight: 600, color }}>
            {value}
          </span>
        </div>
      ))}
    </div>
  )
}
