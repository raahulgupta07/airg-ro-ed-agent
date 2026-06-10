<script lang="ts">
  import { onMount } from 'svelte';
  import { auth } from '$lib/stores/auth.svelte';
  import ChapterHeading from '$lib/components/ChapterHeading.svelte';
  import KpiCard from '$lib/components/KpiCard.svelte';
  import DataTable from '$lib/components/DataTable.svelte';
  import Badge from '$lib/components/Badge.svelte';
  import { getAccuracyColor } from '$lib/colors';

  let loading = $state(true);
  let costStats = $state<any>(null);
  let jobs = $state<any[]>([]);
  let dateFrom = $state('');
  let dateTo = $state('');
  let selectedUser = $state('');
  let users = $state<string[]>([]);
  let chartContainer: HTMLDivElement;

const filteredJobs = $derived(() => {
    let result = jobs;
    if (dateFrom) {
      result = result.filter(j => (j.created_at || '').split(' ')[0] >= dateFrom);
    }
    if (dateTo) {
      result = result.filter(j => (j.created_at || '').split(' ')[0] <= dateTo);
    }
    if (selectedUser) {
      result = result.filter(j => j.username === selectedUser);
    }
    return result;
  });

  const filteredTotalCost = $derived(() => {
    return filteredJobs().reduce((sum, j) => sum + (j.cost_usd || 0), 0);
  });

  const filteredAvgCost = $derived(() => {
    const fj = filteredJobs();
    return fj.length > 0 ? filteredTotalCost() / fj.length : 0;
  });

  const projection = $derived(() => {
    return costStats ? costStats.avg_per_pdf * 100 : 0;
  });

  const columns = [
    { key: 'created_at_short', label: 'Date' },
    { key: 'pdf_name', label: 'PDF Name' },
    { key: 'username', label: 'User' },
    { key: 'pipeline_mode', label: 'Pipeline' },
    { key: 'total_pages', label: 'Pages', align: 'right' as const },
    { key: 'items_display', label: 'Items', align: 'right' as const },
    { key: 'tokens_in_display', label: 'Tokens In', align: 'right' as const },
    { key: 'tokens_out_display', label: 'Tokens Out', align: 'right' as const },
    { key: 'processing_time_display', label: 'Time', align: 'right' as const },
    { key: 'accuracy_display', label: 'Accuracy', align: 'right' as const },
    { key: 'cost_display', label: 'Cost', align: 'right' as const },
  ];

  const tableRows = $derived(() => {
    return filteredJobs().map(j => ({
      ...j,
      created_at_short: (j.created_at || '').split(' ')[0],
      pipeline_mode: (j.pipeline_mode || j.pipeline_version || '—').toUpperCase(),
      items_display: j.items_count ?? '—',
      tokens_in_display: (j.tokens_in ?? 0).toLocaleString(),
      tokens_out_display: (j.tokens_out ?? 0).toLocaleString(),
      processing_time_display: j.processing_time_seconds ? j.processing_time_seconds.toFixed(0) + 's' : '—',
      accuracy_display: j.accuracy_percent != null ? j.accuracy_percent.toFixed(1) + '%' : '—',
      cost_display: '$' + (j.cost_usd || 0).toFixed(4),
    }));
  });

  function downloadCsv() {
    const rows = tableRows();
    if (!rows.length) return;
    const cols = ['Date','PDF Name','User','Pipeline','Pages','Items','Tokens In','Tokens Out','Time','Accuracy','Cost'];
    const fields = ['created_at_short','pdf_name','username','pipeline_mode','total_pages','items_display','tokens_in_display','tokens_out_display','processing_time_display','accuracy_display','cost_display'];
    const lines = [cols.join(',')];
    for (const r of rows) {
      lines.push(fields.map(f => {
        const v = (r as any)[f] ?? '';
        const s = String(v).replace(/"/g, '""');
        return `"${s}"`;
      }).join(','));
    }
    const blob = new Blob([lines.join('\n')], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `costs_${new Date().toISOString().slice(0,10)}.csv`; a.click();
    URL.revokeObjectURL(url);
  }

  async function downloadXlsx() {
    try {
      const res = await fetch('/api/data/jobs/excel', {
        headers: { 'Authorization': `Bearer ${auth.token}` },
      });
      if (!res.ok) { downloadCsv(); return; }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = `costs_${new Date().toISOString().slice(0,10)}.xlsx`; a.click();
      URL.revokeObjectURL(url);
    } catch {
      downloadCsv();
    }
  }

  async function initChart() {
    if (!chartContainer || !costStats?.daily_breakdown) return;
    // Modular import — pull only the line chart + the components actually used,
    // instead of the whole ~600KB echarts bundle.
    const echarts = await import('echarts/core');
    const { LineChart } = await import('echarts/charts');
    const { GridComponent, LegendComponent, TooltipComponent } = await import('echarts/components');
    const { CanvasRenderer } = await import('echarts/renderers');
    echarts.use([LineChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

    const dailyCost = costStats.daily_breakdown || {};
    const dailyDocs = costStats.daily_docs || {};
    const dailyTokIn = costStats.daily_tokens_in || {};
    const dailyTokOut = costStats.daily_tokens_out || {};
    const dates = [...new Set([
      ...Object.keys(dailyCost),
      ...Object.keys(dailyDocs),
      ...Object.keys(dailyTokIn),
    ])].sort();

    const costSeries = dates.map(d => +(dailyCost[d] || 0).toFixed(4));
    const docSeries = dates.map(d => dailyDocs[d] || 0);
    const tokSeries = dates.map(d => (dailyTokIn[d] || 0) + (dailyTokOut[d] || 0));

    const chart = echarts.init(chartContainer);
    chart.setOption({
      backgroundColor: 'transparent',
      grid: { top: 40, right: 80, bottom: 40, left: 70 },
      legend: {
        data: ['COST ($)', 'DOCS', 'TOKENS'],
        textStyle: { color: 'var(--on-surface)', fontSize: 10, fontFamily: 'Inter', fontWeight: 700 },
        top: 0,
      },
      xAxis: {
        type: 'category',
        data: dates.map(d => d.slice(5)),
        axisLabel: { color: 'var(--on-surface-muted)', fontSize: 10, fontFamily: 'Inter' },
        axisLine: { lineStyle: { color: 'var(--on-surface)' } },
      },
      yAxis: [
        { type: 'value', name: 'COST', position: 'left',
          axisLabel: { color: 'var(--warning)', fontSize: 9, formatter: (v:number) => '$' + v.toFixed(3) },
          splitLine: { lineStyle: { color: 'rgba(56,56,50,0.1)' } },
          axisLine: { lineStyle: { color: 'var(--warning)' } },
        },
        { type: 'value', name: 'DOCS / TOKENS', position: 'right',
          axisLabel: { color: 'var(--on-surface-muted)', fontSize: 9, formatter: (v:number) => v >= 1000 ? (v/1000).toFixed(1)+'K' : String(v) },
          splitLine: { show: false },
          axisLine: { lineStyle: { color: 'var(--on-surface)' } },
        },
      ],
      tooltip: {
        trigger: 'axis',
        formatter: (params: any[]) => {
          const lines = [`<b>${params[0]?.name ?? ''}</b>`];
          for (const p of params) {
            let v = p.value;
            if (p.seriesName === 'COST ($)') v = '$' + (+v).toFixed(4);
            else if (p.seriesName === 'TOKENS') v = (+v).toLocaleString();
            lines.push(`${p.marker} ${p.seriesName}: ${v}`);
          }
          return lines.join('<br/>');
        },
      },
      series: [
        { name: 'COST ($)', type: 'line', smooth: true, yAxisIndex: 0,
          data: costSeries,
          symbol: 'circle', symbolSize: 6,
          lineStyle: { color: 'var(--warning)', width: 2 },
          itemStyle: { color: 'var(--warning)', borderColor: 'var(--on-surface)', borderWidth: 1 },
          areaStyle: { color: 'rgba(251,191,36,0.15)' },
        },
        { name: 'DOCS', type: 'line', smooth: true, yAxisIndex: 1,
          data: docSeries,
          symbol: 'rect', symbolSize: 6,
          lineStyle: { color: 'var(--success)', width: 2 },
          itemStyle: { color: 'var(--success)' },
        },
        { name: 'TOKENS', type: 'line', smooth: true, yAxisIndex: 1,
          data: tokSeries,
          symbol: 'triangle', symbolSize: 6,
          lineStyle: { color: '#1d4ed8', width: 2, type: 'dashed' },
          itemStyle: { color: '#1d4ed8' },
        },
      ],
    });

    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(chartContainer);
  }

  function clearFilters() {
    dateFrom = '';
    dateTo = '';
    selectedUser = '';
  }

  onMount(async () => {
    try {
      const res = await fetch('/api/data/cost-stats', { headers: { 'Authorization': `Bearer ${auth.token}` } });
      if (res.ok) costStats = await res.text().then(t => JSON.parse(t));
    } catch {}
    try {
      const res2 = await fetch('/api/jobs/', { headers: { 'Authorization': `Bearer ${auth.token}` } });
      if (res2.ok) jobs = await res2.text().then(t => JSON.parse(t));
      users = [...new Set(jobs.map((j: any) => j.username).filter(Boolean))];
    } catch {}
    loading = false;

    // Init chart after data loads
    setTimeout(initChart, 100);
  });
</script>


<ChapterHeading
  icon="payments"
  title="COST_CONTROL_CENTER"
  subtitle="Monitor API spending, trends, and projections"
  question="How much are extractions costing?"
/>


{#if loading}
  <div class="skeleton h-64 w-full"></div>
{:else}
  <!-- Filters (above KPIs) -->
  <div class="flex flex-wrap gap-3 items-end mb-4">
    <div>
      <label class="cl-lbl" for="cost-from">From</label>
      <input id="cost-from" type="date" bind:value={dateFrom}
             class="cl-inp font-mono"
             style="width: auto;" />
    </div>
    <div>
      <label class="cl-lbl" for="cost-to">To</label>
      <input id="cost-to" type="date" bind:value={dateTo}
             class="cl-inp font-mono"
             style="width: auto;" />
    </div>
    <div>
      <label class="cl-lbl" for="cost-user">User</label>
      <select id="cost-user" bind:value={selectedUser}
              class="cl-inp font-mono"
              style="width: auto;">
        <option value="">ALL USERS</option>
        {#each users as u}
          <option value={u}>{u}</option>
        {/each}
      </select>
    </div>
    {#if dateFrom || dateTo || selectedUser}
      <button class="cl-btn sm"
              onclick={clearFilters}>Clear</button>
    {/if}
    <div class="flex-1"></div>
    {#if dateFrom || dateTo || selectedUser}
      <span class="pill warn">
        FILTERED: ${filteredTotalCost().toFixed(4)} ({filteredJobs().length} PDFs)
      </span>
    {/if}
    <button
      class="cl-btn sm primary"
      onclick={downloadXlsx}
    >↓ Download XLSX</button>
    <button
      class="cl-btn sm"
      onclick={downloadCsv}
    >↓ CSV</button>
  </div>

  <!-- KPI Row 1: cost -->
  {#if costStats}
    <div class="grid grid-cols-2 md:grid-cols-5 gap-3 mb-3">
      <KpiCard title="TOTAL_SPENT" value="${costStats.total_cost.toFixed(3)}" icon="payments" accent="var(--warning)"
               subtitle="{costStats.total_jobs} PDFs total" />
      <KpiCard title="AVG_PER_PDF" value="${costStats.avg_per_pdf.toFixed(4)}" icon="calculate" accent="var(--info)"
               subtitle="per extraction" />
      <KpiCard title="THIS_MONTH" value="${costStats.month_cost.toFixed(3)}" icon="calendar_month" accent="var(--success)"
               subtitle="{costStats.month_jobs} PDFs this month" />
      <KpiCard title="TODAY" value="${costStats.today_cost.toFixed(3)}" icon="today" accent="var(--tertiary)"
               subtitle="{costStats.today_jobs} PDFs today" />
      <KpiCard title="PROJECTED" value="${(costStats.avg_per_pdf * 100).toFixed(2)}" icon="trending_up" accent="var(--warning)"
               subtitle="per 100 PDFs/month" />
    </div>
    <!-- KPI Row 2: tokens -->
    <div class="grid grid-cols-2 md:grid-cols-5 gap-3 mb-6">
      <KpiCard title="TOTAL_TOKENS_IN" value="{((costStats.total_tokens_in||0)/1000).toFixed(1)}K" accent="#1d4ed8"
               subtitle="{(costStats.total_tokens_in||0).toLocaleString()} input" />
      <KpiCard title="TOTAL_TOKENS_OUT" value="{((costStats.total_tokens_out||0)/1000).toFixed(1)}K" accent="#7c3aed"
               subtitle="{(costStats.total_tokens_out||0).toLocaleString()} output" />
      <KpiCard title="TOTAL_TOKENS" value="{((costStats.total_tokens||0)/1000).toFixed(1)}K" accent="#6d28d9"
               subtitle="{(costStats.total_tokens||0).toLocaleString()} all" />
      <KpiCard title="AVG_TOKENS/PDF" value="{((costStats.avg_tokens_per_pdf||0)/1000).toFixed(1)}K" accent="#0e7490"
               subtitle="per extraction" />
      <KpiCard title="$/1K_TOKENS" value="${(costStats.cost_per_1k_tokens||0).toFixed(5)}" accent="#a16207"
               subtitle="effective rate" />
    </div>
  {/if}

  <!-- Daily Cost Chart -->
  <div class="cl-panel mb-6">
    <div class="cl-hd"><span class="dot">◉</span>Daily Cost Trend</div>
    <div class="cl-bd">
      <div bind:this={chartContainer} style="width: 100%; height: 250px;"></div>
    </div>
  </div>


  <!-- Per-PDF Cost Table -->
  <div class="mb-6">
    <DataTable title="COST_PER_PDF" count={filteredJobs().length} columns={columns} rows={tableRows()} />
  </div>

  <!-- Projections + Model Info -->
  <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
    <div class="cl-panel">
      <div class="cl-hd"><span class="dot">◉</span>Cost Projections</div>
      <div class="cl-bd space-y-3">
        {#if costStats}
          {@const avg = costStats.avg_per_pdf}
          {#each [
            { label: '50 PDFs/month', value: avg * 50 },
            { label: '100 PDFs/month', value: avg * 100 },
            { label: '500 PDFs/month', value: avg * 500 },
            { label: '1,000 PDFs/month', value: avg * 1000 },
            { label: '5,000 PDFs/month', value: avg * 5000 },
          ] as proj}
            <div class="flex items-center justify-between">
              <span class="text-[10px] font-bold uppercase" style="color: var(--on-surface-muted);">{proj.label}</span>
              <span class="text-sm font-mono font-bold" style="color: var(--warning);">${proj.value.toFixed(2)}</span>
            </div>
          {/each}
        {/if}
      </div>
    </div>

    <div class="cl-panel">
      <div class="cl-hd"><span class="dot">◉</span>Model Info</div>
      <div class="cl-bd space-y-3">
        <div class="flex justify-between">
          <span class="text-[10px] font-bold uppercase" style="color: var(--on-surface-muted);">MODEL</span>
          <span class="text-[11px] font-mono font-bold" style="color: var(--on-surface);">google/gemini-3.1-flash-lite</span>
        </div>
        <div class="flex justify-between">
          <span class="text-[10px] font-bold uppercase" style="color: var(--on-surface-muted);">INPUT PRICE</span>
          <span class="text-[11px] font-mono font-bold" style="color: var(--on-surface);">$0.30 / M tokens</span>
        </div>
        <div class="flex justify-between">
          <span class="text-[10px] font-bold uppercase" style="color: var(--on-surface-muted);">OUTPUT PRICE</span>
          <span class="text-[11px] font-mono font-bold" style="color: var(--on-surface);">$2.50 / M tokens</span>
        </div>
        <div class="flex justify-between">
          <span class="text-[10px] font-bold uppercase" style="color: var(--on-surface-muted);">AVG TOKENS/PDF</span>
          <span class="text-[11px] font-mono font-bold" style="color: var(--on-surface);">~15,000</span>
        </div>
        <div class="flex justify-between">
          <span class="text-[10px] font-bold uppercase" style="color: var(--on-surface-muted);">PIPELINE</span>
          <span class="text-[11px] font-mono font-bold" style="color: var(--on-surface);">10 agents / 4 API calls</span>
        </div>
        <div class="flex justify-between">
          <span class="text-[10px] font-bold uppercase" style="color: var(--on-surface-muted);">API PROVIDER</span>
          <span class="text-[11px] font-mono font-bold" style="color: var(--on-surface);">OpenRouter</span>
        </div>
      </div>
    </div>
  </div>
{/if}
