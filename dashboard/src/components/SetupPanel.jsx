import { useState, useEffect } from 'react'
import { Card, CardHeader, Btn, Field, Input, NumericInput } from './UI.jsx'
import { api } from '../hooks/useController.js'

const DEFAULT_ROLES = ['Employee', 'HR', 'IOT', 'GUEST']

const DEFAULT_PLAN = {
  name:             'Lab test',
  voice_calls:      2,
  video_calls:      1,
  screen_shares:    1,
  web_users:        0,
  web_urls:         '',
  youtube_users:    0,
  youtube_url:      '',
  duration_s:       60,
  // RADIUS simulation
  radius_enabled:   false,
  radius_server_ip: '',        // empty string → resolved to controller IP server-side
  radius_secret:    'testing123',
  aruba_roles:      [...DEFAULT_ROLES],
  nas_identifier:   'netlab-controller',
  nas_port_type:    15,
}

const DEFAULT_YOUTUBE_URL = 'https://www.youtube.com/watch?v=UgHKb_7884o'

export function SetupPanel({ nodes, activeSession, onSessionStarted }) {
  const [plan,          setPlan]         = useState(DEFAULT_PLAN)
  const [urlInput,      setUrlInput]     = useState('https://example.com\nhttps://cloudflare.com')
  const [loading,       setLoading]      = useState(false)
  const [error,         setError]        = useState(null)
  const [ytOk,          setYtOk]         = useState(null)   // null=unchecked true/false
  const [controllerIp,  setControllerIp] = useState('')     // auto-filled from /api/health
  const [newRoleInput,  setNewRoleInput] = useState('')

  const canRun = nodes.length > 0 && !activeSession

  // YouTube reachability check + controller IP fetch
  useEffect(() => {
    api.checkYoutube().then(r => setYtOk(r.reachable)).catch(() => setYtOk(false))
    fetch('/api/health')
      .then(r => r.json())
      .then(d => {
        const ip = d.controller_ip || ''
        setControllerIp(ip)
        // Pre-fill radius_server_ip only if the user hasn't already changed it
        setPlan(p => p.radius_server_ip === '' ? { ...p, radius_server_ip: ip } : p)
      })
      .catch(() => {})
  }, [])

  function set(key, val) {
    setPlan(p => ({ ...p, [key]: val }))
  }

  async function handleStart() {
    setError(null)
    setLoading(true)
    try {
      const urls = urlInput.split('\n').map(u => u.trim()).filter(Boolean)
      // radius_server_ip: empty string → null so the controller auto-fills its own IP
      const radiusIp = plan.radius_server_ip.trim() || null
      const payload = {
        ...plan,
        web_urls:         urls,
        youtube_users:    ytOk ? plan.youtube_users : 0,
        youtube_url:      plan.youtube_url.trim() || '',
        radius_server_ip: radiusIp,
        aruba_roles:      plan.aruba_roles.filter(Boolean),
      }
      const res = await api.createSession(payload)
      onSessionStarted?.(res.session_id)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  // RADIUS role helpers
  function toggleRole(name) {
    setPlan(p => {
      const roles = p.aruba_roles.includes(name)
        ? p.aruba_roles.filter(r => r !== name)
        : [...p.aruba_roles, name]
      return { ...p, aruba_roles: roles.length ? roles : p.aruba_roles }
    })
  }

  function addCustomRole() {
    const name = newRoleInput.trim()
    if (!name) return
    setPlan(p => ({
      ...p,
      aruba_roles: p.aruba_roles.includes(name) ? p.aruba_roles : [...p.aruba_roles, name],
    }))
    setNewRoleInput('')
  }

  function resetRoles() {
    setPlan(p => ({ ...p, aruba_roles: [...DEFAULT_ROLES] }))
  }

  const isInternalMode = plan.radius_server_ip.trim() === '' ||
                         plan.radius_server_ip.trim() === controllerIp

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

        {/* RADIUS simulation */}
        <Section label="RADIUS simulation">
          {/* Enable toggle */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <button
              onClick={() => set('radius_enabled', !plan.radius_enabled)}
              style={{
                width: 38, height: 22, borderRadius: 11,
                background: plan.radius_enabled ? 'var(--accent)' : 'var(--bg3)',
                border: '1px solid ' + (plan.radius_enabled ? 'var(--accent2)' : 'var(--border2)'),
                position: 'relative', cursor: 'pointer', transition: 'background 0.2s',
                flexShrink: 0,
              }}
            >
              <div style={{
                width: 16, height: 16, borderRadius: '50%', background: '#fff',
                position: 'absolute', top: 2,
                left: plan.radius_enabled ? 18 : 2,
                transition: 'left 0.2s',
                boxShadow: '0 1px 2px rgba(0,0,0,0.2)',
              }} />
            </button>
            <span style={{ fontFamily: 'var(--mono)', fontSize: 12,
              color: plan.radius_enabled ? 'var(--text)' : 'var(--text3)' }}>
              {plan.radius_enabled ? 'Enabled' : 'Disabled'}
            </span>
            {plan.radius_enabled && (
              <span style={{
                fontFamily: 'var(--mono)', fontSize: 10, padding: '2px 8px',
                borderRadius: 10,
                background: isInternalMode ? '#14532d44' : '#78350f44',
                color: isInternalMode ? 'var(--green)' : 'var(--orange)',
                border: '1px solid ' + (isInternalMode ? 'var(--green)' : 'var(--orange)'),
              }}>
                {isInternalMode ? '● internal' : '● external / ClearPass'}
              </span>
            )}
          </div>

          {plan.radius_enabled && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginTop: 4 }}>

              {/* Mode description */}
              <div style={{
                fontFamily: 'var(--mono)', fontSize: 10, lineHeight: 1.5,
                color: 'var(--text3)', padding: '6px 10px',
                background: 'var(--bg2)', borderRadius: 'var(--radius)',
                border: '1px solid var(--border)',
              }}>
                {isInternalMode
                  ? '🖥  Controller will start a RADIUS listener on UDP 1812/1813 and respond to agent auth requests directly.'
                  : '🔀  Agents will send RADIUS packets to the external server. Controller assigns usernames and roles only.'}
              </div>

              {/* Server IP + secret */}
              <div style={{ display: 'grid', gridTemplateColumns: '1fr auto 1fr', gap: 10, alignItems: 'start' }}>
                <Field
                  label="RADIUS Server IP"
                  hint={isInternalMode ? `Auto-filled · controller address` : 'External server — ClearPass or similar'}
                >
                  <div style={{ position: 'relative' }}>
                    <Input
                      value={plan.radius_server_ip}
                      onChange={e => set('radius_server_ip', e.target.value)}
                      placeholder={controllerIp || '192.168.1.10'}
                      style={{ paddingRight: 80 }}
                    />
                    <span style={{
                      position: 'absolute', right: 8, top: '50%', transform: 'translateY(-50%)',
                      fontFamily: 'var(--mono)', fontSize: 10, fontWeight: 600,
                      padding: '2px 6px', borderRadius: 8, pointerEvents: 'none',
                      background: isInternalMode ? '#14532d44' : '#78350f44',
                      color: isInternalMode ? 'var(--green)' : 'var(--orange)',
                    }}>
                      {isInternalMode ? 'auto' : 'override'}
                    </span>
                  </div>
                </Field>

                <div style={{ display: 'grid', gridTemplateColumns: '70px 70px', gap: 8 }}>
                  <Field label="Auth Port"><Input value="1812" style={{ fontFamily: 'var(--mono)' }} readOnly /></Field>
                  <Field label="Acct Port"><Input value="1813" style={{ fontFamily: 'var(--mono)' }} readOnly /></Field>
                </div>

                <Field label="Shared Secret" hint="Plain text — lab use only">
                  <Input
                    value={plan.radius_secret}
                    onChange={e => set('radius_secret', e.target.value)}
                    placeholder="testing123"
                  />
                </Field>
              </div>

              {/* NAS options */}
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
                <Field label="NAS Identifier" hint="Sent in every RADIUS packet — use in ClearPass policy">
                  <Input
                    value={plan.nas_identifier}
                    onChange={e => set('nas_identifier', e.target.value)}
                  />
                </Field>
                <Field label="NAS Port Type" hint="15 = Ethernet  ·  19 = Wireless 802.11">
                  <select
                    value={plan.nas_port_type}
                    onChange={e => set('nas_port_type', +e.target.value)}
                    style={{
                      background: 'var(--bg2)', border: '1px solid var(--border)',
                      borderRadius: 'var(--radius)', color: 'var(--text)',
                      fontFamily: 'var(--mono)', fontSize: 12, padding: '6px 10px',
                      width: '100%', cursor: 'pointer',
                    }}
                  >
                    <option value={15}>15 — Ethernet</option>
                    <option value={19}>19 — Wireless IEEE 802.11</option>
                  </select>
                </Field>
              </div>

              {/* Role chips */}
              <div>
                <div style={{
                  fontFamily: 'var(--mono)', fontSize: 10, fontWeight: 600,
                  textTransform: 'uppercase', letterSpacing: '0.08em',
                  color: 'var(--text3)', marginBottom: 6,
                }}>
                  Aruba User Roles
                  <span style={{ fontWeight: 400, marginLeft: 8, textTransform: 'none' }}>
                    — assigned round-robin · {plan.aruba_roles.length} active
                  </span>
                </div>

                {/* All known roles as toggleable chips */}
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 8 }}>
                  {[...new Set([...DEFAULT_ROLES, ...plan.aruba_roles])].map(role => {
                    const active = plan.aruba_roles.includes(role)
                    return (
                      <button
                        key={role}
                        onClick={() => toggleRole(role)}
                        style={{
                          fontFamily: 'var(--mono)', fontSize: 11, fontWeight: 500,
                          padding: '4px 10px', borderRadius: 'var(--radius)',
                          cursor: 'pointer', transition: 'all 0.1s',
                          background: active ? 'var(--accent)' : 'var(--bg3)',
                          color:      active ? '#fff'          : 'var(--text3)',
                          border: '1px solid ' + (active ? 'var(--accent2)' : 'var(--border2)'),
                        }}
                      >
                        {role}
                      </button>
                    )
                  })}
                </div>

                {/* Add custom role */}
                <div style={{ display: 'flex', gap: 6 }}>
                  <Input
                    value={newRoleInput}
                    onChange={e => setNewRoleInput(e.target.value)}
                    placeholder="Custom role name…"
                    style={{ flex: 1 }}
                  />
                  <Btn small onClick={addCustomRole}>+ Add</Btn>
                  <Btn small variant="ghost" onClick={resetRoles}>Reset</Btn>
                </div>
              </div>

              {/* Lab warning */}
              <div style={{
                fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--orange)',
                display: 'flex', alignItems: 'center', gap: 6,
              }}>
                ⚠ For lab use only — do not use against production infrastructure.
              </div>
            </div>
          )}
        </Section>

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
