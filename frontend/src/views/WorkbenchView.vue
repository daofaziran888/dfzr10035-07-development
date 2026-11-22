<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <!-- 数据版本：退役清单与在途投递核定结果 -->
    <section v-if="meta.detail === 'retirement'" class="detail-panel" data-testid="retirement-panel">
      <header class="detail-head">
        <h3>退役清单</h3>
        <button class="btn" type="button" data-testid="detail-refresh" @click="loadDetail">刷新退役状态</button>
      </header>
      <p v-if="detailHint" class="detail-hint">{{ detailHint }}</p>
      <template v-if="retirement">
        <dl class="detail-grid">
          <div><dt>退役记录</dt><dd data-testid="retirement-id">{{ retirement['记录编号'] }}</dd></div>
          <div><dt>清单状态</dt><dd>{{ retirement['status'] }}</dd></div>
          <div><dt>关联投递</dt><dd>{{ retirement['关联投递'] }}</dd></div>
          <div><dt>待核定</dt><dd>{{ retirement['待核定'] }}</dd></div>
          <div><dt>已交付</dt><dd>{{ retirement['已交付'] }}</dd></div>
          <div><dt>已终止</dt><dd>{{ retirement['已终止'] }}</dd></div>
        </dl>
        <table class="data-table">
          <thead>
            <tr><th>投递ID</th><th>管道编号</th><th>接收方</th><th>清单状态</th><th>投递终态</th></tr>
          </thead>
          <tbody>
            <tr v-for="ref in retirementRefs" :key="String(ref['投递ID'])" :data-testid="`ref-${ref['投递ID']}`">
              <td>{{ ref['投递ID'] }}</td>
              <td>{{ ref['管道编号'] }}</td>
              <td>{{ ref['接收方'] }}</td>
              <td>{{ ref['状态'] }}</td>
              <td>{{ ref['终态'] || '待核定' }}</td>
            </tr>
            <tr v-if="!retirementRefs.length">
              <td colspan="5" class="empty-state">暂无关联投递</td>
            </tr>
          </tbody>
        </table>
      </template>
    </section>

    <!-- 数据投递：按退役清单核定下一步 + 接班人凭据复核 -->
    <section v-if="meta.detail === 'delivery_terminal'" class="detail-panel" data-testid="delivery-panel">
      <header class="detail-head">
        <h3>核定投递下一步</h3>
        <label class="detail-inline">
          投递ID
          <input v-model="detailId" data-testid="detail-id" placeholder="如 1" />
        </label>
        <button class="btn" type="button" data-testid="detail-refresh" @click="loadDetail">核定</button>
      </header>
      <p v-if="detailHint" class="detail-hint">{{ detailHint }}</p>
      <dl v-if="deliveryReview" class="detail-grid">
        <div><dt>下一步</dt><dd data-testid="delivery-decision">{{ deliveryReview['decision'] ?? '—' }}</dd></div>
        <div><dt>投递终态</dt><dd>{{ deliveryReview['terminal_state'] || '未结案' }}</dd></div>
        <div><dt>可否重发</dt><dd>{{ deliveryReview['can_redeliver'] ? '可以' : '不可以' }}</dd></div>
        <div><dt>采集凭据</dt><dd>{{ deliveryReview['receipt'] || '无' }}</dd></div>
        <div class="detail-wide"><dt>说明</dt><dd>{{ deliveryReview['message'] }}</dd></div>
      </dl>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  loadDeliveryNextStep,
  loadRetirementStatus,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})

// 领域明细：退役清单 / 投递核定。
const detailId = ref('')
const detailHint = ref('')
const retirement = ref<Record<string, unknown> | null>(null)
const retirementRefs = ref<Record<string, unknown>[]>([])
const deliveryReview = ref<Record<string, unknown> | null>(null)

async function loadDetail() {
  detailHint.value = ''
  try {
    if (meta.detail === 'retirement') {
      const target = Number(detailId.value || rows.value[0]?.id || 0)
      if (!target) {
        detailHint.value = '请先选择一条数据版本，或在投递ID框内填入版本ID'
        return
      }
      detailId.value = String(target)
      const payload = await loadRetirementStatus(target)
      const record = payload.retirement as Record<string, unknown> | null
      retirement.value = record
      retirementRefs.value = (record?.投递引用 as Record<string, unknown>[]) ?? []
      detailHint.value = String(payload.message ?? '')
    } else if (meta.detail === 'delivery_terminal') {
      const target = Number(detailId.value || rows.value[0]?.id || 0)
      if (!target) {
        detailHint.value = '请先选择一条投递管道，或填入投递ID'
        return
      }
      detailId.value = String(target)
      const payload = await loadDeliveryNextStep(target)
      deliveryReview.value = {
        ...(payload.next as Record<string, unknown>),
        ...(payload.credentials as Record<string, unknown>),
      }
    }
  } catch (error) {
    detailHint.value = error instanceof Error ? error.message : '明细读取失败'
  }
}
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(() => {
  void reload().then(() => {
    if (meta.detail) void loadDetail()
  })
})

</script>
