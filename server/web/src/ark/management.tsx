import React, { useEffect, useRef, useState } from 'react'
import { ChevronLeft, ChevronRight, Mail, RefreshCw, ShieldCheck, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import './management.css'

async function request(path: string, value?: unknown, signal?: AbortSignal) {
  const response = await fetch(path, value === undefined ? { signal } : {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Ark-Request': '1' },
    body: JSON.stringify(value), signal,
  })
  const data = await response.json()
  if (!response.ok) throw Error(data.error || '请求失败，请重试。')
  return data
}
const errorText = (error: unknown) => error instanceof Error ? error.message : '网络连接失败，请重试。'
const time = (value: string) => new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })

export function NotificationsPanel() {
  const [value, setValue] = useState<{ email: string; enabled: boolean; mail_ready?: boolean } | null>(null)
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('')
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    setError('')
    request('/api/notifications', undefined, controller.signal).then(setValue).catch(e => {
      if (!controller.signal.aborted) setError(errorText(e))
    })
    return () => controller.abort()
  }, [retry])
  async function save(event: React.FormEvent) {
    event.preventDefault()
    if (!value) return
    setBusy(true); setError(''); setNotice('')
    try {
      const result = await request('/api/notifications', { email: value.email, enabled: value.enabled })
      setValue(old => ({ ...old, ...result })); setNotice(result.message)
    } catch (e) { setError(errorText(e)) } finally { setBusy(false) }
  }
  return <section className="panel account-panel management-panel" aria-labelledby="notification-title">
    <div className="panel-head"><div><h2 id="notification-title">异常邮件提醒</h2><p>定时或手动执行失败时，将提醒发送到你的邮箱。</p></div><Mail size={20} aria-hidden="true" /></div>
    {error && <p className="management-error" role="alert">{error}{!value && <button className="text-btn" onClick={() => setRetry(n => n + 1)}>重新加载</button>}</p>}
    {notice && <p className="management-success" role="status">{notice}</p>}
    {!value ? !error && <div className="management-loading" role="status">正在读取邮件提醒设置…</div> : <form className="schedule-form management-form" onSubmit={save}>
      <fieldset disabled={busy}>
        <label className="enable-row"><input type="checkbox" checked={value.enabled} onChange={e => setValue({ ...value, enabled: e.target.checked })} /><span><b>接收执行异常提醒</b><small>任务成功、没有可领取奖励或其他操作占用时不会发信。</small></span></label>
        <label className="schedule-field" htmlFor="notification-email">收件邮箱<input id="notification-email" type="email" autoComplete="email" maxLength={254} required={value.enabled} value={value.email} onChange={e => setValue({ ...value, email: e.target.value })} placeholder="输入接收提醒的邮箱" /><small>只用于你自己的任务异常提醒，可随时关闭。</small></label>
        {!value.mail_ready && <p className="management-warning">管理员尚未启用 QQ 发信。你可以先保存收件邮箱，启用后生效。</p>}
        <Button className="save-schedule" type="submit" disabled={busy}>{busy ? '正在保存…' : '保存提醒设置'}</Button>
      </fieldset>
    </form>}
  </section>
}

type MailSettings = { enabled: boolean; sender: string; has_authorization_code: boolean; ready: boolean }
function MailSettingsPanel() {
  const [value, setValue] = useState<MailSettings | null>(null), [secret, setSecret] = useState('')
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('')
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    request('/api/admin/mail', undefined, controller.signal).then(result => { setValue(result); setError('') }).catch(e => {
      if (!controller.signal.aborted) setError(errorText(e))
    })
    return () => controller.abort()
  }, [retry])
  async function save(event: React.FormEvent) {
    event.preventDefault()
    if (!value) return
    setBusy(true); setError(''); setNotice('')
    try {
      const result = await request('/api/admin/mail', { enabled: value.enabled, sender: value.sender, authorization_code: secret })
      setValue(result); setSecret(''); setNotice(result.message)
    } catch (e) { setSecret(''); setError(errorText(e)) } finally { setBusy(false) }
  }
  return <details className="panel mail-settings">
    <summary><Mail size={19} aria-hidden="true" /><span>QQ 发信设置<small>管理员配置发件邮箱，用户各自设置收件邮箱</small></span></summary>
    {error && <p className="management-error" role="alert">{error}{!value && <button className="text-btn" onClick={() => setRetry(n => n + 1)}>重新加载</button>}</p>}
    {notice && <p className="management-success" role="status">{notice}</p>}
    {!value ? !error && <div className="management-loading" role="status">正在读取发信设置…</div> : <form className="schedule-form management-form" onSubmit={save}>
      <fieldset disabled={busy}>
        <label className="enable-row"><input type="checkbox" checked={value.enabled} onChange={e => setValue({ ...value, enabled: e.target.checked })} /><span><b>启用 QQ 邮箱发信</b><small>通过 QQ 邮箱向已开启提醒的用户发送执行异常通知。</small></span></label>
        <label className="schedule-field" htmlFor="smtp-sender">QQ 发件邮箱<input id="smtp-sender" type="email" required autoComplete="off" value={value.sender} onChange={e => setValue({ ...value, sender: e.target.value })} placeholder="你的 QQ 邮箱地址" /></label>
        <label className="schedule-field" htmlFor="smtp-secret">SMTP 授权码<input id="smtp-secret" type="password" autoComplete="new-password" minLength={16} maxLength={128} required={!value.has_authorization_code} value={secret} onChange={e => setSecret(e.target.value)} placeholder={value.has_authorization_code ? '已保存；留空保留原授权码' : '填写 QQ 邮箱生成的授权码'} /><small>在 QQ 邮箱设置中开启 SMTP 服务并生成授权码。此处填写授权码，保存后不回显；更换发件邮箱需重新填写。</small></label>
        <Button className="save-schedule" type="submit" disabled={busy}>{busy ? '正在保存…' : '保存发信设置'}</Button>
      </fieldset>
    </form>}
  </details>
}

type AdminUser = { id: number; username: string; created: string; is_admin: boolean; configured: boolean;
  delete_allowed: boolean; delete_reason: string;
  schedule: { enabled: boolean; mode: string; minute: number; times: string[] } | null;
  execution: { scheduled_runs: number; successes: number; failures: number; interrupted: number; running: number;
    stats_available: boolean; last_run: { started: string; outcome: string } | null } }
type Users = { items: AdminUser[]; total: number; offset: number; limit: number; next_offset: number | null }
const outcomes: Record<string, string> = { success: '成功', error: '失败', interrupted: '中断', running: '执行中', blocked: '需确认' }
type DeleteResult = { message: string; cleanup_pending: boolean }
function DeleteUserDialog({ user, onCancel, onDeleted }: { user: AdminUser; onCancel: () => void; onDeleted: (result: DeleteResult) => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [confirmation, setConfirmation] = useState(''), [busy, setBusy] = useState(false), [error, setError] = useState('')
  useEffect(() => {
    const opener = document.activeElement
    dialog.current?.showModal()
    return () => { dialog.current?.close(); if (opener instanceof HTMLElement && opener.isConnected) opener.focus() }
  }, [])
  async function remove(event: React.FormEvent) {
    event.preventDefault()
    if (busy || confirmation !== user.username) return
    setBusy(true); setError('')
    try {
      const result: DeleteResult = await request('/api/admin/users/delete', { id: user.id, username: confirmation })
      onDeleted(result)
    } catch (e) { setError(errorText(e)); setBusy(false) }
  }
  return <dialog ref={dialog} className="delete-user-dialog" aria-labelledby="delete-user-title" aria-describedby="delete-user-description" aria-busy={busy} onCancel={event => { event.preventDefault(); if (!busy) onCancel() }}>
    <h2 id="delete-user-title">删除账号 {user.username}？</h2>
    <p id="delete-user-description">此操作无法撤销。账号的登录会话、token、定时计划、运行记录和邮件提醒设置将一并删除，其他账号不受影响。</p>
    <form className="management-form" onSubmit={remove}>
      <label className="schedule-field" htmlFor="delete-confirmation">输入用户名确认删除<input id="delete-confirmation" autoFocus autoComplete="off" autoCapitalize="none" spellCheck={false} required value={confirmation} disabled={busy} onChange={event => setConfirmation(event.target.value)} placeholder={user.username} /></label>
      {error && <p className="delete-error" role="alert">{error}</p>}
      <div className="delete-dialog-actions"><Button type="button" className="refresh" disabled={busy} onClick={onCancel}>取消</Button><Button type="submit" className="delete-user-button delete-confirm-button" disabled={busy || confirmation !== user.username}>{busy ? '正在删除…' : '确认删除账号'}</Button></div>
    </form>
  </dialog>
}
export function AdminPanel() {
  const [data, setData] = useState<Users | null>(null), [offset, setOffset] = useState(0), [retry, setRetry] = useState(0)
  const [busy, setBusy] = useState(true), [error, setError] = useState('')
  const [deleting, setDeleting] = useState<AdminUser | null>(null), [notice, setNotice] = useState<DeleteResult | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    setBusy(true); setError('')
    request('/api/admin/users?offset=' + offset, undefined, controller.signal).then(setData).catch(e => {
      if (!controller.signal.aborted) setError(errorText(e))
    }).finally(() => { if (!controller.signal.aborted) setBusy(false) })
    return () => controller.abort()
  }, [offset, retry])
  function deleted(result: DeleteResult) {
    setDeleting(null); setNotice(result)
    if (data && data.items.length === 1 && offset > 0) setOffset(Math.max(0, offset - data.limit))
    else setRetry(n => n + 1)
  }
  return <div className="management-stack">
    <section className="panel admin-panel" aria-labelledby="admin-users-title">
      <div className="panel-head"><div><h2 id="admin-users-title">注册用户{data && <span className="admin-total">共 {data.total} 人</span>}</h2><p>查看凭据录入、定时计划和执行结果。所有时间均为北京时间。</p></div><Button className="refresh" disabled={busy} onClick={() => setRetry(n => n + 1)}><RefreshCw size={15} />刷新列表</Button></div>
      <p className="admin-explainer"><ShieldCheck size={16} aria-hidden="true" />仅管理员可见。凭据状态表示是否已录入；统计仅包含定时执行，不包含手动操作和只查询验证。</p>
      {notice && <p className={notice.cleanup_pending ? 'management-error' : 'management-success'} role="status">{notice.message}</p>}
      {error ? <div className="empty" role="alert">{error}<button className="text-btn" onClick={() => setRetry(n => n + 1)}>重新加载列表</button></div> : busy ? <div className="management-loading" role="status">正在读取用户及执行记录…</div> : !data?.items.length ? <div className="empty"><b>当前没有注册用户</b><span>用户注册后会自动出现在这里。</span></div> : <>
        <div className="admin-table-wrap"><table className="admin-table"><caption className="sr-only">注册用户及定时任务执行概览</caption><thead><tr><th scope="col">用户</th><th scope="col">Token</th><th scope="col">定时计划</th><th scope="col">定时执行</th><th scope="col">最近结果</th><th scope="col">操作</th></tr></thead><tbody>{data.items.map(user => <tr key={user.id}>
          <td data-label="用户"><strong className="admin-username">{user.username}</strong>{user.is_admin && <span className="admin-role">管理员</span>}<small>注册于 {time(user.created)}</small></td>
          <td data-label="Token"><span className={'badge ' + (user.configured ? 'yes' : '')}>{user.configured ? '已录入' : '未录入'}</span></td>
          <td data-label="定时计划">{!user.schedule ? '暂不可读取' : <><span>{!user.schedule.enabled ? '已暂停' : user.schedule.mode === 'hourly' ? `每小时第 ${user.schedule.minute} 分钟` : `每天 ${user.schedule.times.join('、')}`}</span>{!user.configured && user.schedule.enabled && <small>等待录入凭据</small>}</>}</td>
          <td data-label="定时执行">{!user.execution.stats_available ? '暂不可读取' : <><b>{user.execution.scheduled_runs} 次</b><small>成功 {user.execution.successes} · 失败 {user.execution.failures}{user.execution.interrupted > 0 && ` · 中断 ${user.execution.interrupted}`}{user.execution.running > 0 && ` · 执行中 ${user.execution.running}`}</small></>}</td>
          <td data-label="最近结果">{!user.execution.stats_available ? '暂不可读取' : user.execution.last_run ? <><span className={'run-outcome ' + user.execution.last_run.outcome}>{outcomes[user.execution.last_run.outcome] || '未知'}</span><small>{time(user.execution.last_run.started)}</small></> : '尚未定时执行'}</td>
          <td data-label="操作">{user.delete_allowed ? <Button className="delete-user-button" aria-label={`删除账号 ${user.username}`} onClick={() => setDeleting(user)}><Trash2 size={14} aria-hidden="true" />删除</Button> : <small className="delete-protected">{user.delete_reason}</small>}</td>
        </tr>)}</tbody></table></div>
        <div className="admin-pagination"><span>{data.offset + 1}–{data.offset + data.items.length} / {data.total} 人</span><Button className="refresh" disabled={busy || offset === 0} onClick={() => setOffset(Math.max(0, offset - data.limit))}><ChevronLeft size={15} />上一页</Button><Button className="refresh" disabled={busy || data.next_offset === null} onClick={() => setOffset(data.next_offset!)}>下一页<ChevronRight size={15} /></Button></div>
      </>}
    </section>
    <MailSettingsPanel />
    {deleting && <DeleteUserDialog user={deleting} onCancel={() => setDeleting(null)} onDeleted={deleted} />}
  </div>
}
