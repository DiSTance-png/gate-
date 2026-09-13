import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

const SESSION_TOKEN_KEY = 'gate.admin.session.id'
const SESSION_USER_KEY = 'gate.admin.session.user'

export interface AdminUser {
  username: string
  role: string
}

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string>('')
  const user = ref<AdminUser | null>(null)
  const error = ref<string>('')

  const isAuthenticated = computed(() => !!token.value)
  const isSuperadmin = computed(() => user.value?.role === 'superadmin')

  async function login(username: string, password: string): Promise<boolean> {
    error.value = ''
    try {
      const resp = await fetch('/api/v1/admin/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      })
      const data = await resp.json()
      if (!resp.ok) {
        error.value = data.detail || `登录失败 (HTTP ${resp.status})`
        return false
      }
      token.value = data.session_token
      user.value = { username: data.user?.username || username, role: data.user?.role || 'admin' }
      localStorage.setItem(SESSION_TOKEN_KEY, token.value)
      localStorage.setItem(SESSION_USER_KEY, JSON.stringify(user.value))
      return true
    } catch (e: any) {
      error.value = e.message || '网络错误'
      return false
    }
  }

  async function validateSession(): Promise<boolean> {
    if (!token.value) return false
    try {
      const resp = await fetch('/api/v1/admin/auth/me', {
        headers: { 'X-R20-Session': token.value },
      })
      if (!resp.ok) {
        logout()
        return false
      }
      const data = await resp.json()
      if (data.user) {
        user.value = { username: data.user.username, role: data.user.role }
        localStorage.setItem(SESSION_USER_KEY, JSON.stringify(user.value))
      }
      return true
    } catch {
      return false
    }
  }

  function restoreSession() {
    const savedToken = localStorage.getItem(SESSION_TOKEN_KEY)
    const savedUser = localStorage.getItem(SESSION_USER_KEY)
    if (savedToken && savedUser) {
      token.value = savedToken
      try {
        user.value = JSON.parse(savedUser)
      } catch {
        user.value = null
      }
      validateSession()
    }
  }

  function logout() {
    // Best-effort server-side logout (don't block)
    if (token.value) {
      fetch('/api/v1/admin/logout', {
        method: 'POST',
        headers: { 'X-R20-Session': token.value },
      }).catch(() => {})
    }
    token.value = ''
    user.value = null
    localStorage.removeItem(SESSION_TOKEN_KEY)
    localStorage.removeItem(SESSION_USER_KEY)
  }

  return {
    token,
    user,
    error,
    isAuthenticated,
    isSuperadmin,
    login,
    logout,
    restoreSession,
  }
})
