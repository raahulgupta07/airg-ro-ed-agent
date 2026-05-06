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
    const echarts = await import('echarts');

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
        textStyle: { color: '#383832', fontSize: 10, fontFamily: 'Space Grotesk', fontWeight: 700 },
        top: 0,
      },
      xAxis: {
        type: 'category',
        data: dates.map(d => d.slice(5)),
        axisLabel: { color: '#65655e', fontSize: 10, fontFamily: 'Space Grotesk' },
        axisLine: { lineStyle: { color: '#383832' } },
      },
      yAxis: [
        { type: 'value', name: 'COST', position: 'left',
          axisLabel: { color: '#fbbf24', fontSize: 9, formatter: (v:number) => '$' + v.toFixed(3) },
          splitLine: { lineStyle: { color: 'rgba(56,56,50,0.1)' } },
          axisLine: { lineStyle: { color: '#fbbf24' } },
        },
        { type: 'value', name: 'DOCS / TOKENS', position: 'right',
          axisLabel: { color: '#65655e', fontSize: 9, formatter: (v:number) => v >= 1000 ? (v/1000).toFixed(1)+'K' : String(v) },
          splitLine: { show: false },
          axisLine: { lineStyle: { color: '#383832' } },
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
          lineStyle: { color: '#fbbf24', width: 2 },
          itemStyle: { color: '#fbbf24', borderColor: '#383832', borderWidth: 1 },
          areaStyle: { color: 'rgba(251,191,36,0.15)' },
        },
        { name: 'DOCS', type: 'line', smooth: true, yAxisIndex: 1,
          data: docSeries,
          symbol: 'rect', symbolSize: 6,
          lineStyle: { color: '#007518', width: 2 },
          itemStyle: { color: '#007518' },
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
      <div class="tag-label mb-1 text-[8px]">FROM</div>
      <input type="date" bind:value={dateFrom}
             class="text-[10px] font-mono px-2 py-1.5 focus:outline-none"
             style="border: 2px solid var(--on-surface); background: white; color: var(--on-surface);" />
    </div>
    <div>
      <div class="tag-label mb-1 text-[8px]">TO</div>
      <input type="date" bind:value={dateTo}
             class="text-[10px] font-mono px-2 py-1.5 focus:outline-none"
             style="border: 2px solid var(--on-surface); background: white; color: var(--on-surface);" />
    </div>
    <div>
      <div class="tag-label mb-1 text-[8px]">USER</div>
      <select bind:value={selectedUser}
              class="text-[10px] font-mono font-bold uppercase px-2 py-1.5 focus:outline-none"
              style="border: 2px solid var(--on-surface); background: white; color: var(--on-surface);">
        <option value="">ALL USERS</option>
        {#each users as u}
          <option value={u}>{u}</option>
        {/each}
      </select>
    </div>
    {#if dateFrom || dateTo || selectedUser}
      <button class="text-[8px] font-black uppercase px-2 py-1.5 cursor-pointer"
              style="border: 1px solid var(--outline); color: var(--outline); background: transparent;"
              onclick={clearFilters}>CLEAR</button>
    {/if}
    <div class="flex-1"></div>
    {#if dateFrom || dateTo || selectedUser}
      <div class="text-xs font-mono font-bold" style="color: #fbbf24;">
        FILTERED: ${filteredTotalCost().toFixed(4)} ({filteredJobs().length} PDFs)
      </div>
    {/if}
    <button
      class="text-[10px] font-black uppercase px-3 py-1.5 cursor-pointer border-2 press-effect"
      style="border-color: var(--on-surface); background: var(--primary-container); color: var(--on-surface); box-shadow: 2px 2px 0px 0px var(--on-surface);"
      onclick={downloadXlsx}
    >↓ DOWNLOAD XLSX</button>
    <button
      class="text-[10px] font-black uppercase px-3 py-1.5 cursor-pointer border-2"
      style="border-color: var(--on-surface); background: var(--surface); color: var(--on-surface);"
      onclick={downloadCsv}
    >↓ CSV</button>
  </div>

  <!-- KPI Row 1: cost -->
  {#if costStats}
    <div class="grid grid-cols-2 md:grid-cols-5 gap-3 mb-3">
      <KpiCard title="TOTAL_SPENT" value="${costStats.total_cost.toFixed(3)}" icon="payments" accent="#fbbf24"
               subtitle="{costStats.total_jobs} PDFs total" />
      <KpiCard title="AVG_PER_PDF" value="${costStats.avg_per_pdf.toFixed(4)}" icon="calculate" accent="#006f7c"
               subtitle="per extraction" />
      <KpiCard title="THIS_MONTH" value="${costStats.month_cost.toFixed(3)}" icon="calendar_month" accent="#007518"
               subtitle="{costStats.month_jobs} PDFs this month" />
      <KpiCard title="TODAY" value="${costStats.today_cost.toFixed(3)}" icon="today" accent="#9d4867"
               subtitle="{costStats.today_jobs} PDFs today" />
      <KpiCard title="PROJECTED" value="${(costStats.avg_per_pdf * 100).toFixed(2)}" icon="trending_up" accent="#ff9d00"
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
  <div class="border-2 stamp-shadow mb-6" style="border-color: var(--on-surface);">
    <div class="dark-bar">DAILY_COST_TREND</div>
    <div class="bg-white p-4">
      <div bind:this={chartContainer} style="width: 100%; height: 250px;"></div>
    </div>
  </div>


  <!-- Per-PDF Cost Table -->
  <div class="mb-6">
    <DataTable title="COST_PER_PDF" count={filteredJobs().length} columns={columns} rows={tableRows()} />
  </div>

  <!-- Projections + Model Info -->
  <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
    <div class="border-2 stamp-shadow" style="border-color: var(--on-surface);">
      <div class="dark-bar text-xs">COST_PROJECTIONS</div>
      <div class="bg-white p-4 space-y-3">
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
              <span class="text-[10px] font-bold uppercase" style="color: var(--outline);">{proj.label}</span>
              <span class="text-sm font-mono font-bold" style="color: #fbbf24;">${proj.value.toFixed(2)}</span>
            </div>
          {/each}
        {/if}
      </div>
    </div>

    <div class="border-2 stamp-shadow" style="border-color: var(--on-surface);">
      <div class="dark-bar text-xs">MODEL_INFO</div>
      <div class="bg-white p-4 space-y-3">
        <div class="flex justify-between">
          <span class="text-[10px] font-bold uppercase" style="color: var(--outline);">MODEL</span>
          <span class="text-[11px] font-mono font-bold" style="color: var(--on-surface);">google/gemini-3.1-flash-lite</span>
        </div>
        <div class="flex justify-between">
          <span class="text-[10px] font-bold uppercase" style="color: var(--outline);">INPUT PRICE</span>
          <span class="text-[11px] font-mono font-bold" style="color: var(--on-surface);">$0.30 / M tokens</span>
        </div>
        <div class="flex justify-between">
          <span class="text-[10px] font-bold uppercase" style="color: var(--outline);">OUTPUT PRICE</span>
          <span class="text-[11px] font-mono font-bold" style="color: var(--on-surface);">$2.50 / M tokens</span>
        </div>
        <div class="flex justify-between">
          <span class="text-[10px] font-bold uppercase" style="color: var(--outline);">AVG TOKENS/PDF</span>
          <span class="text-[11px] font-mono font-bold" style="color: var(--on-surface);">~15,000</span>
        </div>
        <div class="flex justify-between">
          <span class="text-[10px] font-bold uppercase" style="color: var(--outline);">PIPELINE</span>
          <span class="text-[11px] font-mono font-bold" style="color: var(--on-surface);">10 agents / 4 API calls</span>
        </div>
        <div class="flex justify-between">
          <span class="text-[10px] font-bold uppercase" style="color: var(--outline);">API PROVIDER</span>
          <span class="text-[11px] font-mono font-bold" style="color: var(--on-surface);">OpenRouter</span>
        </div>
      </div>
    </div>
  </div>
{/if}
