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
    { key: 'total_pages', label: 'Pages', align: 'right' as const },
    { key: 'items_display', label: 'Items', align: 'right' as const },
    { key: 'processing_time_display', label: 'Time', align: 'right' as const },
    { key: 'accuracy_display', label: 'Accuracy', align: 'right' as const },
    { key: 'cost_display', label: 'Cost', align: 'right' as const },
  ];

  const tableRows = $derived(() => {
    return filteredJobs().map(j => ({
      ...j,
      created_at_short: (j.created_at || '').split(' ')[0],
      items_display: j.items_count ?? '—',
      processing_time_display: j.processing_time_seconds ? j.processing_time_seconds.toFixed(0) + 's' : '—',
      accuracy_display: j.accuracy_percent != null ? j.accuracy_percent.toFixed(1) + '%' : '—',
      cost_display: '$' + (j.cost_usd || 0).toFixed(4),
    }));
  });

  async function initChart() {
    if (!chartContainer || !costStats?.daily_breakdown) return;
    const echarts = await import('echarts');

    const daily = costStats.daily_breakdown;
    const dates = Object.keys(daily).sort();
    const values = dates.map(d => daily[d]);

    const chart = echarts.init(chartContainer);
    chart.setOption({
      backgroundColor: 'transparent',
      grid: { top: 20, right: 20, bottom: 40, left: 60 },
      xAxis: {
        type: 'category',
        data: dates.map(d => d.slice(5)),
        axisLabel: { color: '#65655e', fontSize: 10, fontFamily: 'Space Grotesk' },
        axisLine: { lineStyle: { color: '#383832' } },
      },
      yAxis: {
        type: 'value',
        axisLabel: {
          color: '#65655e', fontSize: 10, fontFamily: 'Space Grotesk',
          formatter: (v: number) => '$' + v.toFixed(3),
        },
        splitLine: { lineStyle: { color: 'rgba(56,56,50,0.15)' } },
        axisLine: { lineStyle: { color: '#383832' } },
      },
      tooltip: {
        trigger: 'axis',
        formatter: (params: any) => {
          const p = params[0];
          return `<b>${p.name}</b><br/>Cost: $${p.value.toFixed(4)}`;
        },
      },
      series: [{
        type: 'bar',
        data: values,
        itemStyle: {
          color: {
            type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: '#fbbf24' },
              { offset: 1, color: '#f59e0b' },
            ],
          },
          borderColor: '#383832',
          borderWidth: 1,
        },
        barWidth: '60%',
      }],
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
  </div>

  <!-- KPI Row -->
  {#if costStats}
    <div class="grid grid-cols-2 md:grid-cols-5 gap-3 mb-6">
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
