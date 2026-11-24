<template>
  <section class="closure-panel" data-testid="page-closure-panel">
    <header class="closure-head">
      <div>
        <h3>采集页面独立结案 / 页面结案明细</h3>
        <p>
          回执必须携带或关联已通过解析检查的实际响应；预约重采不算成功，重复回执和对照采样均不会第二次计入该页。
        </p>
      </div>
      <div class="toolbar">
        <label>
          值班人
          <input v-model="worker" data-testid="closure-worker" />
        </label>
        <button class="btn" type="button" data-testid="reopen-dispatch" @click="reopenDispatch">
          乙重新打开派工
        </button>
        <button class="btn" type="button" data-testid="restore-dispatch" @click="restoreDispatch">
          恢复派工
        </button>
        <button class="btn primary" type="button" data-testid="claim-next" @click="claimPage('')">
          领取下一页
        </button>
      </div>
    </header>

    <div class="scenario-bar">
      <button class="btn" type="button" data-testid="reset-baseline" @click="resetBaseline">
        重置第6/7/8页
      </button>
      <button class="btn" type="button" data-testid="report-timeout-6-7" @click="reportTimeout">
        报第6、7页读取超时
      </button>
      <button class="btn" type="button" data-testid="claim-6-before-fetch" @click="claimPage('P6')">
        乙领取第6页但尚未取页
      </button>
      <button class="btn primary" type="button" data-testid="run-canonical" @click="runCanonical">
        一键回放：P6迟到有效回执 + P7网关正文
      </button>
      <span v-if="message" class="closure-message" data-testid="closure-message">{{ message }}</span>
    </div>

    <div class="check-grid" data-testid="narrative-checks">
      <article><span>P6</span><strong :data-ok="checks.p6Verified">{{ checks.p6Verified ? '已核验' : '未核验' }}</strong><small>回执+解析后独立结案</small></article>
      <article><span>P6</span><strong :data-ok="checks.p6Exempt">{{ checks.p6Exempt ? '本次已免重采' : '仍需重采' }}</strong><small>已领取未取页也可豁免</small></article>
      <article><span>P6</span><strong :data-ok="checks.p6SampleOnce">{{ checks.p6SampleOnce ? '对照采样仅一次' : '可确认一次对照采样' }}</strong><small>核对结果不第二次计入</small></article>
      <article><span>P7</span><strong :data-ok="!checks.p7Closed">{{ checks.p7Pending ? '继续待补' : '已结案' }}</strong><small>网关正文不能结案</small></article>
      <article><span>P7</span><strong :data-ok="checks.p7AlarmOpen">{{ checks.p7AlarmOpen ? '报警未关闭' : '报警已关闭' }}</strong><small>失败类型按尝试保留</small></article>
      <article><span>P8</span><strong :data-ok="checks.p8Claimable">{{ checks.p8Pending ? '仍待补/可领取' : '状态已变化' }}</strong><small>其他页面不受影响</small></article>
    </div>

    <div class="closure-layout">
      <div class="page-cards" data-testid="closure-cards">
        <article
          v-for="page in pages"
          :key="page.page_key"
          class="page-card"
          :data-testid="`closure-card-${page.page_key}`"
          :data-status="page.status"
        >
          <header>
            <div>
              <h4>{{ page.page_key }} · {{ page.页面名称 }}</h4>
              <span class="status-pill" :data-status="page.status" data-testid="closure-status">
                {{ page.status }}
              </span>
            </div>
            <div class="mini-flags">
              <em v-if="page.retry_exempt" data-testid="retry-exempt">本次已免重采</em>
              <em v-if="page.comparison_confirmed">对照采样已确认</em>
              <em v-if="page.claimed">{{ page.领取人 || page.assignee }} 已领取；本次派工实际取页：{{ page.worker_fetched ? '是' : '否' }}</em>
              <em v-if="page.late_response_recovered" data-testid="late-page-recovered">迟到原页/正文已补回</em>
            </div>
          </header>

          <dl class="detail-grid">
            <div><dt>结案明细</dt><dd>{{ page.closure_detail_id }}</dd></div>
            <div><dt>派工状态</dt><dd>{{ page.dispatch_status }} / {{ page.领取人 || '未领取' }}</dd></div>
            <div><dt>报警状态</dt><dd>{{ page.alarm_status }}（{{ page.open_alarm_count }}）</dd></div>
            <div><dt>成功次数</dt><dd data-testid="success-count">{{ page.success_count }}</dd></div>
            <div><dt>实际数据</dt><dd data-testid="data-count">{{ page.actual_data_count }} 条</dd></div>
            <div><dt>对照采样</dt><dd>{{ page.对照采样状态 }}</dd></div>
            <div><dt>当前尝试</dt><dd>{{ page.current_attempt_id || '—' }}</dd></div>
            <div><dt>回执</dt><dd>{{ page.receipt_id || page.receipt_status }}</dd></div>
            <div><dt>原始页</dt><dd>{{ page.raw_response_id || '—' }}</dd></div>
            <div><dt>提取结果</dt><dd>{{ page.extraction_id || '—' }}</dd></div>
          </dl>

          <label class="body-editor">
            <span>实际响应文字 / 原始页正文</span>
            <textarea
              v-model="bodies[page.page_key]"
              :data-testid="`response-body-${page.page_key}`"
              rows="5"
              :placeholder="`粘贴 ${page.page_key} 的实际响应正文，解析规则将直接消费这段文字`"
            />
          </label>
          <div class="body-actions">
            <button class="btn ghost" type="button" @click="fillFixture(page.page_key)">填入题设正文</button>
            <button
              v-if="page.page_key === 'P6'"
              class="btn primary"
              type="button"
              data-testid="supply-original-page-P6"
              @click="supplyFixture(page.page_key)"
            >
              补到它的原有效页
            </button>
            <button
              v-else-if="page.page_key === 'P7'"
              class="btn primary"
              type="button"
              data-testid="supply-gateway-page-P7"
              @click="supplyFixture(page.page_key)"
            >
              补网关错误正文
            </button>
            <button
              v-else
              class="btn"
              type="button"
              :data-testid="`supply-fixture-${page.page_key}`"
              @click="supplyFixture(page.page_key)"
            >
              填入并提交正文
            </button>
            <button
              class="btn"
              type="button"
              :data-testid="`submit-body-${page.page_key}`"
              @click="submitBody(page.page_key)"
            >
              补响应正文并解析
            </button>
            <button
              class="btn"
              type="button"
              :data-testid="`receipt-body-${page.page_key}`"
              @click="receiveReceipt(page.page_key)"
            >
              收回执并解析
            </button>
            <button
              v-if="page.page_key === 'P6'"
              class="btn"
              type="button"
              data-testid="late-success-receipt-P6"
              @click="lateReceipt6"
            >
              第6页成功回执迟到
            </button>
          </div>

          <div class="action-list" :data-testid="`actions-${page.page_key}`">
            <button
              v-for="action in page.available_actions"
              :key="action.code"
              type="button"
              class="link action-button"
              :data-testid="`page-action-${page.page_key}-${action.code}`"
              :data-enabled="action.enabled"
              :disabled="!action.enabled"
              :title="action.reason"
              @click="runPageAction(page.page_key, action.code)"
            >
              {{ action.label }}
            </button>
          </div>
        </article>
      </div>

      <aside class="side-panel">
        <section data-testid="page-closure-detail">
          <h4>页面结案明细</h4>
          <div data-testid="closure-detail" />
          <table class="mini-table" data-testid="closure-detail-table">
            <thead><tr><th>页面</th><th>状态</th><th>尝试</th><th>原始→提取</th><th>数据</th></tr></thead>
            <tbody>
              <tr v-for="page in pages" :key="`row-${page.page_key}`" :data-testid="`closure-row-${page.page_key}`">
                <td>{{ page.page_key }}</td>
                <td>{{ page.status }}</td>
                <td>{{ page.current_attempt_id }}</td>
                <td>{{ page.raw_response_id || '—' }} → {{ page.extraction_id || '—' }}</td>
                <td>{{ page.actual_data_count }}</td>
              </tr>
            </tbody>
          </table>
        </section>

        <section>
          <h4>错误记录（按尝试关联）</h4>
          <table class="mini-table" data-testid="error-table">
            <thead><tr><th>页面</th><th>尝试</th><th>失败类型</th><th>结案明细</th></tr></thead>
            <tbody>
              <tr v-for="error in board?.errors || []" :key="error.error_record_id" :data-attempt="error.attempt_id">
                <td>{{ error.page_key }}</td>
                <td>{{ error.attempt_id }}</td>
                <td data-testid="error-failure-type">{{ error.failure_type }} / {{ error.错误类型 }}</td>
                <td>{{ error.closure_detail_id }}</td>
              </tr>
            </tbody>
          </table>
        </section>

        <section>
          <h4>报警</h4>
          <ul class="record-list" data-testid="alarm-list">
            <li v-for="alarm in board?.alarms || []" :key="alarm.alarm_id" :data-open="alarm.open">
              <strong>{{ alarm.page_key }} · {{ alarm.status }}</strong>
              <span>{{ alarm.attempt_id }} · {{ alarm.failure_type }}</span>
              <button class="link" type="button" @click="closeAlarm(alarm.page_key)">尝试关闭报警</button>
            </li>
          </ul>
        </section>

        <section>
          <h4>实际取得且只确认一次的数据</h4>
          <ul class="record-list data-list" data-testid="actual-data-list">
            <li v-for="datum in board?.actual_data || []" :key="datum.data_id">
              <strong>{{ datum.data_id }} · {{ datum.page_key }}</strong>
              <code>{{ datum.raw_response_id }} → {{ datum.extraction_id }}</code>
              <pre>{{ JSON.stringify(datum.payload, null, 2) }}</pre>
            </li>
          </ul>
        </section>
      </aside>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type AnyBoard = Record<string, any>

const board = ref<AnyBoard | null>(null)
const message = ref('')
const worker = ref('乙')
const bodies = reactive<Record<string, string>>({ P6: '', P7: '', P8: '' })

const pages = computed<any[]>(() => board.value?.pages || [])
const checks = computed(() => {
  const byKey = Object.fromEntries(pages.value.map((page) => [page.page_key, page]))
  const p6 = byKey.P6 || {}
  const p7 = byKey.P7 || {}
  const p8 = byKey.P8 || {}
  return {
    p6Verified: p6.status === '已核验',
    p6Exempt: Boolean(p6.retry_exempt),
    p6SampleOnce: p6.comparison_confirmed && p6.comparison_count === 1,
    p7Pending: p7.status === '待补采',
    p7Closed: p7.status === '已核验',
    p7AlarmOpen: Number(p7.open_alarm_count || 0) > 0,
    p8Pending: p8.status === '待补采',
    p8Claimable: p8.status === '待补采' && !p8.claimed,
  }
})

async function reload() {
  try {
    const response = await request(`/api/retry-console/state?worker=${encodeURIComponent(worker.value)}`)
    board.value = await response.json()
    for (const page of pages.value) {
      if (!(page.page_key in bodies)) bodies[page.page_key] = ''
    }
  } catch (error) {
    message.value = error instanceof Error ? error.message : '结案工作台读取失败'
  }
}

async function postJson(path: string, payload: Record<string, unknown> = {}) {
  message.value = '处理中…'
  try {
    const response = await request(path, { method: 'POST', body: JSON.stringify(payload) })
    const result = await response.json()
    if (result.ok === false) {
      message.value = result.message || '动作未被接受'
    } else {
      message.value = result.message || '已执行'
    }
    await reload()
    return result
  } catch (error) {
    message.value = error instanceof Error ? error.message : '请求失败'
    return null
  }
}

function resetBaseline() {
  return postJson('/api/retry-console/reset', { seed: 'baseline', worker: worker.value })
}

function runCanonical() {
  return postJson('/api/retry-console/scenarios/canonical', { worker: worker.value })
}

function reportTimeout() {
  return postJson('/api/retry-console/command', {
    command: 'report_timeout', pages: [6, 7], error_type: '读取超时', worker: '系统',
  })
}

function claimPage(page: string) {
  return postJson('/api/retry-console/command', {
    command: 'claim_dispatch', page: page || undefined, worker: worker.value,
  })
}

function reopenDispatch() {
  return postJson('/api/retry-console/command', { command: 'reopen_dispatch', worker: worker.value })
}

function restoreDispatch() {
  return postJson('/api/retry-console/command', { command: 'restore_dispatch', worker: worker.value })
}

function submitBody(page: string, purpose?: string) {
  return postJson(`/api/page-closures/${page}/responses`, {
    text: bodies[page] || '', worker: worker.value, purpose,
  })
}

function receiveReceipt(page: string) {
  return postJson(`/api/page-closures/${page}/receipts`, {
    receipt_id: `RCP-${page}-${Date.now()}`, success: true,
    text: bodies[page] || '', worker: worker.value,
  })
}

function supplyFixture(page: string) {
  fillFixture(page)
  return submitBody(page)
}

function lateReceipt6() {
  fillFixture('P6')
  return postJson('/api/page-closures/P6/receipts', {
    receipt_id: 'RCP-P6-LATE', success: true,
    text: bodies.P6, worker: worker.value, source: 'late_original',
  })
}

function closeAlarm(page: string) {
  return postJson(`/api/page-closures/${page}/close-alarm`, { worker: worker.value })
}

async function runPageAction(page: string, code: string) {
  if (code === 'submit_response') {
    await submitBody(page)
    return
  }
  if (code === 'confirm_comparison_sample') {
    await postJson(`/api/page-closures/${page}/comparison`, {
      text: bodies[page] || '', worker: worker.value,
    })
    return
  }
  if (code === 'close_alarm') return closeAlarm(page)
  if (code === 'handle_alarm') {
    return postJson(`/api/page-closures/${page}/handle-alarm`, { worker: worker.value })
  }
  if (code === 'claim_dispatch') return claimPage(page)
  if (code === 'release_dispatch') {
    return postJson(`/api/page-closures/${page}/release`, { worker: worker.value })
  }
  if (code === 'schedule_retry') {
    return postJson(`/api/page-closures/${page}/schedule-retry`, { worker: worker.value })
  }
  return postJson(`/api/page-closures/${page}/actions`, { action: code, worker: worker.value })
}

function fillFixture(page: string) {
  if (page === 'P6') {
    bodies.P6 = `<!doctype html><html lang="zh-CN"><head><title>第6页商品列表-原有效页</title></head>
<body data-page="6" data-valid-page="true"><article class="product-item" data-sku="ORIG-P6-001">
<h2 class="product-title">第6页迟到回执对应的原有效商品</h2><span class="product-price">¥66.00</span>
</article></body></html>`
  } else if (page === 'P7') {
    bodies.P7 = `<html><head><title>502 Bad Gateway</title></head><body>
<h1>502 Bad Gateway</h1><p>网关错误：上游采集通道返回无效响应。</p></body></html>`
  } else {
    bodies[page] = '<html><body><article class="product-item" data-sku="P8-1"><h2 class="product-title">第8页商品</h2><span class="product-price">¥8.00</span></article></body></html>'
  }
}

async function init() {
  await reload()
  // 界面首次打开时自动摆出第 6/7/8 页；后端测试仍可从空工作台开始。
  if (board.value && Number(board.value.total || 0) === 0) {
    await resetBaseline()
  }
}

onMounted(init)
</script>

<style scoped>
.closure-panel { margin: 18px 0 28px; padding: 18px; border: 1px solid #d9e2ef; border-radius: 16px; background: #f8fafc; }
.closure-head, .scenario-bar, .toolbar, .body-actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.closure-head { justify-content: space-between; }
.closure-head h3 { margin: 0 0 6px; font-size: 20px; }
.closure-head p { margin: 0; color: #5b6b82; max-width: 820px; line-height: 1.6; }
.scenario-bar { margin: 14px 0; padding: 12px; background: #fff; border-radius: 12px; border: 1px solid #e4eaf3; }
.closure-message { color: #1d4ed8; font-size: 14px; }
.check-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; margin: 12px 0 16px; }
.check-grid article { background: #fff; border: 1px solid #e1e8f2; border-radius: 12px; padding: 12px; display: grid; gap: 5px; }
.check-grid span { color: #64748b; font-weight: 700; }
.check-grid strong[data-ok='true'] { color: #047857; }
.check-grid strong[data-ok='false'] { color: #b45309; }
.check-grid small { color: #64748b; line-height: 1.4; }
.closure-layout { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(320px, .65fr); gap: 16px; }
.page-cards { display: grid; gap: 14px; }
.page-card { background: #fff; border: 1px solid #dce5f0; border-radius: 14px; padding: 14px; }
.page-card header { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
.page-card h4 { margin: 0 0 6px; }
.status-pill { display: inline-flex; padding: 3px 9px; border-radius: 999px; background: #fef3c7; color: #92400e; font-size: 12px; font-weight: 700; }
.status-pill[data-status='已核验'] { background: #d1fae5; color: #065f46; }
.mini-flags { display: flex; flex-wrap: wrap; gap: 6px; justify-content: flex-end; }
.mini-flags em { font-style: normal; font-size: 12px; padding: 3px 7px; border-radius: 8px; background: #e0f2fe; color: #075985; }
.detail-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 12px; margin: 12px 0; }
.detail-grid div { display: grid; gap: 2px; min-width: 0; }
.detail-grid dt { font-size: 12px; color: #64748b; }
.detail-grid dd { margin: 0; font-size: 13px; word-break: break-all; }
.body-editor { display: grid; gap: 6px; font-size: 13px; color: #334155; }
.body-editor textarea { width: 100%; border: 1px solid #cbd5e1; border-radius: 10px; padding: 9px; font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
.body-actions { margin: 8px 0; }
.action-list { display: flex; flex-wrap: wrap; gap: 10px; border-top: 1px dashed #dbe4f0; padding-top: 10px; }
.action-button:disabled { color: #94a3b8; cursor: not-allowed; text-decoration: none; }
.side-panel { display: grid; gap: 14px; align-content: start; }
.side-panel section { background: #fff; border: 1px solid #dce5f0; border-radius: 14px; padding: 12px; overflow: auto; }
.side-panel h4 { margin: 0 0 10px; }
.mini-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.mini-table th, .mini-table td { border-bottom: 1px solid #edf2f7; padding: 6px; text-align: left; vertical-align: top; }
.record-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 9px; }
.record-list li { display: grid; gap: 4px; border-bottom: 1px solid #edf2f7; padding-bottom: 8px; font-size: 12px; }
.record-list code { word-break: break-all; color: #475569; }
.record-list pre { margin: 0; white-space: pre-wrap; word-break: break-all; background: #f8fafc; padding: 7px; border-radius: 8px; }
@media (max-width: 1100px) { .closure-layout { grid-template-columns: 1fr; } }
</style>
