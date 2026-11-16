import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/cleanse'
const columns = [
  "规则编号",
  "关联数据集",
  "空值策略",
  "格式校验",
  "异常过滤",
  "去重字段",
  "规则版本",
  "生效日期"
]
const actions = [
  "提交校验",
  "启用规则",
  "停用规则"
]
const statuses = [
  "草稿",
  "待校验",
  "已启用",
  "已停用"
]

type Row = Record<string, unknown>
type Notice = { kind: 'ok' | 'error'; text: string } | null

type DetailState = {
  entry: Row
  results: Row[]
  abnormal: Row[]
  batches: Row[]
  ledger: Row[]
}

function cellText(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function unionKeys(items: Row[], exclude: string[] = []): string[] {
  const keys: string[] = []
  for (const item of items) {
    for (const key of Object.keys(item)) {
      if (!exclude.includes(key) && !keys.includes(key)) keys.push(key)
    }
  }
  return keys
}

async function postJson(path: string, values: Record<string, unknown>): Promise<{ ok: boolean; message: string; entry?: Row | null }> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  const payload = (await response.json()) as { ok: boolean; message: string; entry?: Row | null }
  return payload
}

async function fetchList(path: string): Promise<Row[]> {
  const response = await request(path)
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  const payload = (await response.json()) as { items?: Row[] }
  return payload.items ?? []
}

export default function CleanseView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [stats, setStats] = useState({ total: 0, enabled: 0, checking: 0 })
  const [notice, setNotice] = useState<Notice>(null)
  const [filters, setFilters] = useState<{ keyword: string; status: string }>({ keyword: '', status: '' })
  const [registerOpen, setRegisterOpen] = useState(false)
  const [registerValues, setRegisterValues] = useState<Record<string, string>>({})
  const [detail, setDetail] = useState<DetailState | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [executeOptions, setExecuteOptions] = useState({ 批次日期: '', 中断演练: false, 重新执行: false })

  const reload = useCallback(async () => {
    const query = new URLSearchParams()
    if (filters.keyword) query.set('keyword', filters.keyword)
    if (filters.status) query.set('status', filters.status)
    try {
      const response = await request(`${ENDPOINT}?${query.toString()}`)
      if (!response.ok) throw new Error('清洗规则列表读取失败')
      const payload = (await response.json()) as { items?: Row[]; total?: number }
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setNotice({ kind: 'error', text: error instanceof Error ? error.message : '数据清洗列表读取失败' })
    }
  }, [filters])

  const reloadStats = useCallback(async () => {
    try {
      const items = await fetchList(`${ENDPOINT}?size=200`)
      setStats({
        total: items.length,
        enabled: items.filter((item) => item.status === '已启用').length,
        checking: items.filter((item) => item.status === '待校验').length,
      })
    } catch {
      // 统计卡片失败不阻断主流程
    }
  }, [])

  useEffect(() => {
    void reload()
    void reloadStats()
  }, [reload, reloadStats])

  async function runAction(action: string, row: Row) {
    setNotice(null)
    try {
      const payload = await postJson(`${ENDPOINT}/${String(row.id)}/actions`, { action })
      setNotice({ kind: payload.ok ? 'ok' : 'error', text: payload.message })
      await reload()
      await reloadStats()
    } catch (error) {
      setNotice({ kind: 'error', text: error instanceof Error ? error.message : '数据清洗操作失败' })
    }
  }

  async function submitRegister() {
    setNotice(null)
    try {
      const payload = await postJson(ENDPOINT, registerValues)
      setNotice({ kind: payload.ok ? 'ok' : 'error', text: payload.message })
      if (payload.ok) {
        setRegisterValues({})
        setRegisterOpen(false)
      }
      await reload()
      await reloadStats()
    } catch (error) {
      setNotice({ kind: 'error', text: error instanceof Error ? error.message : '清洗规则登记失败' })
    }
  }

  const openDetail = useCallback(async (row: Row) => {
    setDetailLoading(true)
    setNotice(null)
    try {
      const entryResponse = await request(`${ENDPOINT}/${String(row.id)}`)
      if (!entryResponse.ok) throw new Error(`清洗规则 ${String(row.id)} 读取失败`)
      const entry = (await entryResponse.json()) as Row
      const dataset = encodeURIComponent(String(row['关联数据集'] ?? ''))
      const [results, abnormal, batches, ledger] = await Promise.all([
        fetchList(`${ENDPOINT}/results?dataset=${dataset}`),
        fetchList(`${ENDPOINT}/abnormal?dataset=${dataset}`),
        fetchList(`${ENDPOINT}/batches?dataset=${dataset}`),
        fetchList(`${ENDPOINT}/ledger?dataset=${dataset}`),
      ])
      setDetail({ entry, results, abnormal, batches, ledger })
    } catch (error) {
      setNotice({ kind: 'error', text: error instanceof Error ? error.message : '规则详情读取失败' })
    } finally {
      setDetailLoading(false)
    }
  }, [])

  async function runExecute(row: Row) {
    setNotice(null)
    try {
      const values: Record<string, unknown> = {}
      if (executeOptions.批次日期) values['批次日期'] = executeOptions.批次日期
      if (executeOptions.中断演练) values['中断演练'] = true
      if (executeOptions.重新执行) values['重新执行'] = true
      const payload = await postJson(`${ENDPOINT}/${String(row.id)}/execute`, values)
      setNotice({ kind: payload.ok ? 'ok' : 'error', text: payload.message })
      await openDetail(row)
    } catch (error) {
      setNotice({ kind: 'error', text: error instanceof Error ? error.message : '清洗执行失败' })
    }
  }

  const statCards = useMemo(() => [
    { label: '清洗规则总数', value: stats.total },
    { label: '已启用规则', value: stats.enabled },
    { label: '待校验规则', value: stats.checking },
  ], [stats])

  const detailEntry = detail?.entry
  const summary = (detailEntry?.['执行摘要'] ?? {}) as Row
  const resultColumns = useMemo(() => unionKeys(detail?.results ?? [], ['id']), [detail])
  const ledgerColumns = useMemo(() => unionKeys(detail?.ledger ?? [], ['id']), [detail])

  return (
    <section className="page" data-module="cleanse">
      <header className="page-head">
        <div>
          <h2>数据清洗管理</h2>
          <p className="page-desc">维护数据清洗规则的输入、执行约束与结果记录，为数据清洗提供可验证的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setRegisterOpen((prev) => !prev)}>
            登记清洗规则
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出数据清洗清单
          </button>
        </div>
      </header>

      {registerOpen && (
        <form
          className="panel"
          onSubmit={(event) => { event.preventDefault(); void submitRegister() }}
        >
          <h3 className="panel-title">登记清洗规则（规则版本、空值策略、格式校验一起落库）</h3>
          <div className="form-grid">
            {columns.map((field) => (
              <label key={field} className="filter-item">
                <span>{field}{['规则编号', '关联数据集', '空值策略'].includes(field) ? ' *' : ''}</span>
                <input
                  value={registerValues[field] ?? ''}
                  placeholder={`请输入${field}`}
                  onChange={(event) => setRegisterValues((prev) => ({ ...prev, [field]: event.target.value }))}
                />
              </label>
            ))}
          </div>
          <div className="panel-actions">
            <button className="btn primary" type="submit">提交登记</button>
            <button className="btn ghost" type="button" onClick={() => setRegisterOpen(false)}>取消</button>
          </div>
        </form>
      )}

      <div className="stat-row">
        {statCards.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        <label className="filter-item">
          <span>规则编号</span>
          <input
            value={filters.keyword}
            placeholder="按规则编号检索"
            onChange={(event) => setFilters((prev) => ({ ...prev, keyword: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>状态</span>
          <select
            value={filters.status}
            onChange={(event) => setFilters((prev) => ({ ...prev, status: event.target.value }))}
          >
            <option value="">全部状态</option>
            {statuses.map((status) => <option key={status} value={status}>{status}</option>)}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({ keyword: '', status: '' })}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>状态</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{cellText(row[column])}</td>)}
              <td>{cellText(row.status)}</td>
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void openDetail(row)}>
                  详情
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无数据清洗数据，可先登记清洗规则</td>
            </tr>
          )}
        </tbody>
      </table>

      {detailLoading && <p className="page-desc">规则详情加载中…</p>}

      {detail && detailEntry && (
        <section className="panel">
          <h3 className="panel-title">
            规则详情：{cellText(detailEntry['规则编号'])}（{cellText(detailEntry['规则版本'])}，{cellText(detailEntry.status)}）
          </h3>
          <div className="form-grid">
            {columns.map((field) => (
              <div key={field} className="detail-item">
                <span className="stat-label">{field}</span>
                <span>{cellText(detailEntry[field])}</span>
              </div>
            ))}
          </div>

          <div className="panel-actions">
            <label className="filter-item">
              <span>批次日期（版本仲裁基准）</span>
              <input
                type="date"
                value={executeOptions.批次日期}
                onChange={(event) => setExecuteOptions((prev) => ({ ...prev, 批次日期: event.target.value }))}
              />
            </label>
            <label className="check-item">
              <input
                type="checkbox"
                checked={executeOptions.中断演练}
                onChange={(event) => setExecuteOptions((prev) => ({ ...prev, 中断演练: event.target.checked }))}
              />
              中断演练（下一批次超时）
            </label>
            <label className="check-item">
              <input
                type="checkbox"
                checked={executeOptions.重新执行}
                onChange={(event) => setExecuteOptions((prev) => ({ ...prev, 重新执行: event.target.checked }))}
              />
              重新执行（清空既有结果）
            </label>
            <button className="btn primary" type="button" onClick={() => void runExecute(detailEntry)}>
              执行清洗 / 断点续传
            </button>
          </div>

          <p className="page-desc">
            执行摘要：已清洗 {cellText(summary['已清洗条数'])} 条，异常过滤 {cellText(summary['异常过滤条数'])} 条，
            最近批次状态 {cellText(summary['最近批次状态'])}
            {summary['最近批次原因'] ? `（${cellText(summary['最近批次原因'])}）` : ''}
          </p>

          <h4 className="section-title">批次与断点</h4>
          <table className="data-table">
            <thead>
              <tr>
                <th>来源批次</th><th>状态</th><th>源记录数</th><th>清洗条数</th><th>异常条数</th><th>规则版本</th><th>原因</th>
              </tr>
            </thead>
            <tbody>
              {detail.batches.map((batch, index) => (
                <tr key={index}>
                  <td>{cellText(batch['来源批次'])}</td>
                  <td>{cellText(batch['状态'])}</td>
                  <td>{cellText(batch['源记录数'])}</td>
                  <td>{cellText(batch['清洗条数'])}</td>
                  <td>{cellText(batch['异常条数'])}</td>
                  <td>{cellText(batch['规则版本'])}</td>
                  <td>{cellText(batch['原因'])}</td>
                </tr>
              ))}
              {detail.batches.length === 0 && (
                <tr><td className="empty-state" colSpan={7}>暂无批次记录，执行清洗后此处展示断点状态</td></tr>
              )}
            </tbody>
          </table>

          <h4 className="section-title">清洗后的数据集（可复核，不应含空值）</h4>
          <table className="data-table">
            <thead>
              <tr>{resultColumns.map((column) => <th key={column}>{column}</th>)}</tr>
            </thead>
            <tbody>
              {detail.results.map((item, index) => (
                <tr key={index}>
                  {resultColumns.map((column) => <td key={column}>{cellText(item[column])}</td>)}
                </tr>
              ))}
              {detail.results.length === 0 && (
                <tr><td className="empty-state" colSpan={Math.max(resultColumns.length, 1)}>暂无清洗结果</td></tr>
              )}
            </tbody>
          </table>

          <h4 className="section-title">异常过滤记录（来源批次 + 规则版本可追溯）</h4>
          <table className="data-table">
            <thead>
              <tr>
                <th>来源批次</th><th>规则版本</th><th>过滤原因</th><th>原始记录</th><th>过滤时间</th>
              </tr>
            </thead>
            <tbody>
              {detail.abnormal.map((item, index) => (
                <tr key={index}>
                  <td>{cellText(item['来源批次'])}</td>
                  <td>{cellText(item['规则版本'])}</td>
                  <td>{cellText(item['过滤原因'])}</td>
                  <td>{cellText(item['原始记录'])}</td>
                  <td>{cellText(item['过滤时间'])}</td>
                </tr>
              ))}
              {detail.abnormal.length === 0 && (
                <tr><td className="empty-state" colSpan={5}>暂无异常过滤记录</td></tr>
              )}
            </tbody>
          </table>

          <h4 className="section-title">清洗字段台账（后续查询只读取已标准化记录）</h4>
          <table className="data-table">
            <thead>
              <tr>{ledgerColumns.map((column) => <th key={column}>{column}</th>)}</tr>
            </thead>
            <tbody>
              {detail.ledger.map((item, index) => (
                <tr key={index}>
                  {ledgerColumns.map((column) => <td key={column}>{cellText(item[column])}</td>)}
                </tr>
              ))}
              {detail.ledger.length === 0 && (
                <tr><td className="empty-state" colSpan={Math.max(ledgerColumns.length, 1)}>暂无台账记录，清洗完成后自动同步</td></tr>
              )}
            </tbody>
          </table>

          <div className="panel-actions">
            <button className="btn ghost" type="button" onClick={() => setDetail(null)}>收起详情</button>
          </div>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条数据清洗记录</span>
        {notice && <span className={notice.kind === 'error' ? 'error-text' : 'ok-text'}>{notice.text}</span>}
      </footer>
    </section>
  )
}
