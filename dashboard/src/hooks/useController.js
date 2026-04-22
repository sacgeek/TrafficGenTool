import { useState, useEffect, useRef, useCallback } from 'react'

const WS_URL = `ws://${window.location.host}/ws/dashboard`
const API    = '/api'

// ── REST helpers ──────────────────────────────────────────────────────────────

export async function apiFetch(path, opts = {}) {
  const res = await fetch(`${API}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || `HTTP ${res.status}`)
  }
  return res.json()
}

export const api = {
  nodes:          ()           => apiFetch('/nodes'),
  sessions:       ()           => apiFetch('/sessions'),
  activeSession:  ()           => apiFetch('/sessions/active'),
  session:        (id)         => apiFetch(`/sessions/${id}`),
  snapshots:      (id, params) => apiFetch(`/sessions/${id}/snapshots?${new URLSearchParams(params || {})}`),
  checkYoutube:   ()           => apiFetch('/check/youtube'),
  createSession:  (plan)       => apiFetch('/sessions', { method: 'POST', body: JSON.stringify(plan) }),
  stopSession:    (id)         => apiFetch(`/sessions/${id}/stop`, { method: 'POST' }),
  clearSessions:  ()           => apiFetch('/sessions', { method: 'DELETE' }),
  health:         ()           => apiFetch('/health'),
  exportCsv:      (id)         => `${API}/sessions/${id}/export/csv`,
  exportPdf:      (id)         => `${API}/sessions/${id}/export/pdf`,
}

// ── WebSocket hook ────────────────────────────────────────────────────────────

export function useControllerWS() {
  const [nodes,       setNodes]       = useState([])
  const [sessions,    setSessions]    = useState([])
  const [snapshots,   setSnapshots]   = useState([])  // rolling last 200
  const [alerts,      setAlerts]      = useState([])  // active + recently cleared
  const [connected,   setConnected]   = useState(false)
  const [lastEvent,   setLastEvent]   = useState(null)

  const wsRef     = useRef(null)
  const retryRef  = useRef(null)
  const mountedRef = useRef(true)

  const connect = useCallback(() => {
    if (!mountedRef.current) return
    const ws = new WebSocket(WS_URL)
    wsRef.current = ws

    ws.onopen = () => {
      if (!mountedRef.current) return
      setConnected(true)
      clearTimeout(retryRef.current)
    }

    ws.onmessage = (ev) => {
      if (!mountedRef.current) return
      try {
        const { event, data } = JSON.parse(ev.data)
        setLastEvent({ event, data, ts: Date.now() })

        if (event === 'init') {
          setNodes(data.nodes || [])
          setSessions(data.sessions || [])
        } else if (event === 'node_update') {
          setNodes(prev => {
            const idx = prev.findIndex(n => n.node_id === data.node_id)
            if (data.status === 'offline') return prev.filter(n => n.node_id !== data.node_id)
            if (idx >= 0) { const next = [...prev]; next[idx] = { ...next[idx], ...data }; return next }
            return [...prev, data]
          })
        } else if (event === 'session_update') {
          setSessions(prev => {
            const idx = prev.findIndex(s => s.session_id === data.session_id)
            if (idx >= 0) { const next = [...prev]; next[idx] = { ...next[idx], ...data }; return next }
            return [data, ...prev]
          })
        } else if (event === 'snapshot') {
          setSnapshots(prev => {
            const next = [...prev, data]
            return next.length > 500 ? next.slice(-500) : next
          })
        } else if (event === 'alert') {
          setAlerts(prev => [...prev, { ...data, is_active: true }])
        } else if (event === 'alert_cleared') {
          // Mark the matching alert as cleared
          setAlerts(prev => prev.map(a =>
            a.alert_id === data.alert_id
              ? { ...a, is_active: false, cleared_at: data.cleared_at }
              : a
          ))
        }
      } catch (e) {
        // ignore parse errors
      }
    }

    ws.onclose = () => {
      if (!mountedRef.current) return
      setConnected(false)
      retryRef.current = setTimeout(connect, 3000)
    }

    ws.onerror = () => ws.close()
  }, [])

  useEffect(() => {
    mountedRef.current = true
    connect()
    return () => {
      mountedRef.current = false
      clearTimeout(retryRef.current)
      if (wsRef.current) wsRef.current.close()
    }
  }, [connect])

  const refreshSessions = useCallback(async () => {
    try {
      const data = await api.sessions()
      if (mountedRef.current) setSessions(data)
    } catch (e) {
      // ignore fetch errors
    }
  }, [])

  const clearSnapshots = useCallback(() => setSnapshots([]), [])

  const activeSession  = sessions.find(s => s.status === 'running' || s.status === 'pending') || null
  const activeAlerts   = alerts.filter(a => a.is_active)

  return { nodes, sessions, snapshots, alerts, activeAlerts, activeSession, connected, lastEvent, refreshSessions, clearSnapshots }
}
