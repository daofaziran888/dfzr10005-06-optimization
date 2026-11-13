<template>
  <section class="page" data-module="cost">
    <header class="page-head">
      <div>
        <h2>存储成本优化管理</h2>
        <p class="page-desc">维护存储成本的扫描范围、冷数据识别与优化建议追踪，为存储成本优化提供可审计的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记成本分析报告</button>
        <button class="btn" type="button" @click="exportRows">导出存储成本优化清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" @click="focusScan(row)">分段扫描</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无存储成本优化数据，可先登记成本分析报告</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条存储成本优化记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section class="scan-panel">
      <header class="scan-head">
        <div>
          <h3>分段扫描进度</h3>
          <p class="page-desc">
            分段粒度：表 + 分区，每个分段独立重试；刷新页面会按批次编号恢复同一进度。
          </p>
        </div>
        <div class="scan-controls">
          <label class="filter-item">
            <span>成本分析报告</span>
            <select v-model="scanReportId">
              <option :value="''">不绑定报告</option>
              <option v-for="row in rows" :key="String(row.id)" :value="String(row.id)">
                {{ row['报告编号'] }}
              </option>
            </select>
          </label>
          <label class="filter-item">
            <span>分析范围</span>
            <input v-model="scanScope" placeholder="例如 dwd_order_detail 的 2026-09 分区" />
          </label>
          <label class="filter-item">
            <span>采样批次</span>
            <input v-model.number="samplingVersion" type="number" min="1" />
          </label>
          <button class="btn primary" type="button" :disabled="scanBusy" @click="submitScan">
            {{ scanBusy ? '扫描中…' : '提交扫描' }}
          </button>
        </div>
      </header>

      <p v-if="scanMessage" class="scan-message">{{ scanMessage }}</p>
      <p v-if="scanError" class="error-text">{{ scanError }}</p>

      <div v-if="!scan" class="empty-state scan-empty">
        暂无扫描批次。填写分析范围后点击「提交扫描」；同一范围重复提交不会新建批次。
      </div>

      <template v-else>
        <div class="stat-row">
          <article class="stat-card">
            <span class="stat-label">批次 / 状态</span>
            <strong class="stat-value small">{{ scan['批次编号'] }} · {{ scan['状态'] }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">进度</span>
            <strong class="stat-value small">
              已完成 {{ scan['进度']['已完成'] }} / {{ scan['进度']['总数'] }}，失败 {{ scan['进度']['失败'] }}，待扫描 {{ scan['进度']['待扫描'] }}
            </strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">容量摘要</span>
            <strong class="stat-value small">{{ scan['已发布摘要'] || '尚未发布' }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">本地重算分段</span>
            <strong class="stat-value small">{{ scan['本轮重算分段'].length }}</strong>
          </article>
        </div>

        <p v-if="scan['中断原因']" class="scan-warn">{{ scan['中断原因'] }}</p>

        <div class="scan-actions">
          <button class="btn" type="button" :disabled="scanBusy" @click="resumeScan">继续扫描</button>
          <button class="btn" type="button" :disabled="scanBusy" @click="retryFailed">重试失败段</button>
          <button class="btn ghost" type="button" @click="loadScan(scan['批次编号'])">刷新进度</button>
        </div>

        <dl class="caliber">
          <template v-for="(value, key) in scan['当前成本口径']" :key="key">
            <dt>{{ key }}</dt><dd>{{ value }}</dd>
          </template>
        </dl>

        <table class="data-table segment-table">
          <thead>
            <tr>
              <th>表名称</th>
              <th>分区</th>
              <th>数据源路径</th>
              <th>存储字节</th>
              <th>最近访问天数</th>
              <th>状态</th>
              <th>尝试</th>
              <th>失败原因 / 处理建议</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="segment in scan['分段']" :key="segment['分段键']">
              <td>{{ segment['表名称'] }}</td>
              <td>{{ segment['分区'] }}</td>
              <td class="path-cell">{{ segment['数据源路径'] }}</td>
              <td>{{ segment['存储字节'] }}</td>
              <td>{{ segment['最近访问天数'] }}</td>
              <td>{{ segment['状态'] }}</td>
              <td>{{ segment['尝试次数'] }}</td>
              <td>
                <template v-if="segment['失败原因'] && segment['失败原因']['原因']">
                  <div class="error-text">{{ segment['失败原因']['错误码'] }}：{{ segment['失败原因']['原因'] }}</div>
                  <div class="hint">{{ segment['失败原因']['处理建议'] }}</div>
                </template>
                <span v-else>—</span>
              </td>
            </tr>
            <tr v-if="!scan['分段'].length">
              <td colspan="8" class="empty-state">该范围没有可扫描的表或分区（暂无数据）</td>
            </tr>
          </tbody>
        </table>
      </template>
    </section>

    <section class="scan-panel">
      <header class="scan-head">
        <div>
          <h3>容量摘要</h3>
          <p class="page-desc">扫描完成后发布，查询性能分析按「容量摘要编号」引用；历史摘要编号不会被重扫替换。</p>
        </div>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th>摘要编号</th>
            <th>扫描范围</th>
            <th>存储总量</th>
            <th>冷数据占比</th>
            <th>预计节省</th>
            <th>覆盖状态</th>
            <th>采样批次</th>
            <th>生成时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="summary in summaries" :key="summary['摘要编号']">
            <td>{{ summary['摘要编号'] }}</td>
            <td>{{ summary['扫描范围'] }}</td>
            <td>{{ summary['存储总量'] }}</td>
            <td>{{ summary['冷数据占比'] }}</td>
            <td>{{ summary['预计节省'] }}</td>
            <td>{{ summary['覆盖状态'] }}</td>
            <td>{{ summary['采样批次'] }}</td>
            <td>{{ summary['生成时间'] }}</td>
          </tr>
          <tr v-if="!summaries.length">
            <td colspan="8" class="empty-state">暂无容量摘要，完成一次分段扫描后会生成</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type Segment = Record<string, any>
type Scan = Record<string, any>
type Summary = Record<string, any>

const ENDPOINT = '/api/cost'
const STORAGE_KEY = 'cost:scan:last'
const columns = ["报告编号", "分析范围", "存储总量", "冷数据占比", "优化建议", "预计节省", "执行状态", "生成时间"]
const actions = ["开始分析", "执行优化", "归档报告"]
const statuses = ["草稿", "分析中", "已生成", "已执行"]
const stats = [{"label": "成本分析报告总数", "value": 0}, {"label": "已生成报告", "value": 0}, {"label": "已执行报告", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const scan = ref<Scan | null>(null)
const summaries = ref<Summary[]>([])
const scanReportId = ref<string>('')
const scanScope = ref('')
const samplingVersion = ref(1)
const scanBusy = ref(false)
const scanMessage = ref('')
const scanError = ref('')

const scanPending = computed(() => {
  const progress = scan.value?.['进度']
  return Boolean(progress && (progress['待扫描'] > 0 || progress['失败'] > 0))
})

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '成本分析报告登记入口尚未接入审批流'
}

async function postJson(path: string, values: Record<string, unknown>) {
  const response = await request(path, { method: 'POST', body: JSON.stringify({ values }) })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.message || payload.detail || `接口返回 ${response.status}`)
  }
  return payload
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('存储成本优化动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '存储成本优化操作失败'
  }
}

function focusScan(row: Row) {
  scanReportId.value = String(row.id ?? '')
  scanScope.value = String(row['分析范围'] ?? '')
  const batchId = row['扫描批次']
  if (typeof batchId === 'string' && batchId) {
    void loadScan(batchId)
  }
}

async function loadScan(batchId: string) {
  scanError.value = ''
  try {
    const response = await request(`${ENDPOINT}/scans/${batchId}`)
    if (!response.ok) {
      throw new Error(`扫描批次 ${batchId} 读取失败`)
    }
    const batch = await response.json()
    scan.value = batch
    scanScope.value = batch['分析范围']
    samplingVersion.value = batch['采样批次'] ?? 1
    window.localStorage.setItem(STORAGE_KEY, batchId)
    await loadSummaries()
  } catch (error) {
    scanError.value = error instanceof Error ? error.message : '扫描进度读取失败'
  }
}

async function loadSummaries() {
  try {
    const response = await request(`${ENDPOINT}/capacity-summaries?size=50`)
    if (response.ok) {
      const payload = await response.json()
      summaries.value = payload.items ?? []
    }
  } catch {
    summaries.value = []
  }
}

async function submitScan() {
  scanError.value = ''
  scanMessage.value = ''
  if (!scanScope.value.trim()) {
    scanError.value = '请先填写分析范围，例如 dwd_order_detail 的 2026-09 分区'
    return
  }
  scanBusy.value = true
  try {
    const values: Record<string, unknown> = {
      '分析范围': scanScope.value.trim(),
      '采样批次': Number(samplingVersion.value) || 1,
    }
    if (scanReportId.value) {
      values.report_id = Number(scanReportId.value)
    }
    const payload = await postJson(`${ENDPOINT}/scans`, values)
    scanMessage.value = payload.message
    scan.value = payload.entry
    window.localStorage.setItem(STORAGE_KEY, payload.entry['批次编号'])
    await Promise.all([reload(), loadSummaries()])
  } catch (error) {
    scanError.value = error instanceof Error ? error.message : '提交扫描失败'
  } finally {
    scanBusy.value = false
  }
}

async function resumeScan() {
  if (!scan.value) return
  scanBusy.value = true
  scanError.value = ''
  try {
    const payload = await postJson(`${ENDPOINT}/scans/${scan.value['批次编号']}/resume`, {})
    scanMessage.value = payload.message
    scan.value = payload.entry
    await Promise.all([reload(), loadSummaries()])
  } catch (error) {
    scanError.value = error instanceof Error ? error.message : '继续扫描失败'
  } finally {
    scanBusy.value = false
  }
}

async function retryFailed() {
  if (!scan.value) return
  scanBusy.value = true
  scanError.value = ''
  try {
    const payload = await postJson(`${ENDPOINT}/scans/${scan.value['批次编号']}/retry`, {})
    scanMessage.value = payload.message
    scan.value = payload.entry
    await Promise.all([reload(), loadSummaries()])
  } catch (error) {
    scanError.value = error instanceof Error ? error.message : '重试失败段失败'
  } finally {
    scanBusy.value = false
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('成本分析报告列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '存储成本优化列表读取失败'
  }
}

onMounted(async () => {
  await reload()
  await loadSummaries()
  const last = window.localStorage.getItem(STORAGE_KEY)
  if (last) {
    await loadScan(last)
  }
})
</script>

<style scoped>
.scan-panel { margin-top: 20px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; }
.scan-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; flex-wrap: wrap; }
.scan-head h3 { margin: 0; font-size: 15px; }
.scan-controls { display: flex; gap: 8px; align-items: flex-end; flex-wrap: wrap; }
.scan-controls input, .scan-controls select { padding: 5px 8px; border: 1px solid var(--border); border-radius: 6px; }
.scan-message { color: #1f6feb; font-size: 13px; }
.scan-warn { color: #b54708; font-size: 13px; }
.scan-actions { display: flex; gap: 8px; margin: 8px 0; }
.scan-empty { padding: 16px; border: 1px dashed var(--border); border-radius: 6px; }
.stat-value.small { font-size: 14px; }
.caliber { display: grid; grid-template-columns: 120px 1fr; gap: 4px 12px; font-size: 12px; color: var(--muted); margin: 8px 0; }
.caliber dt { font-weight: 600; }
.caliber dd { margin: 0; }
.segment-table { margin-top: 8px; }
.path-cell { max-width: 280px; word-break: break-all; font-size: 12px; }
.hint { color: var(--muted); font-size: 12px; }
</style>
