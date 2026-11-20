export const materializeViewTemplate = `
<div class="card wide" v-show="tab===3">
  <h2>物化台账</h2>
  <p class="muted">物化单挂窗口世代；窗重开（作废重算）后旧世代物化作废，新物化挂新世代。分区失败策略为空桩（整窗回滚 vs 保留成功分区）。</p>
  <button class="ghost" @click="partitionStub">登记分区空桩</button>
  <table>
    <thead><tr><th>窗口</th><th>状态</th><th>世代</th><th>行数</th><th>金额</th><th>分区</th><th>时间</th></tr></thead>
    <tbody>
      <tr v-for="m in mats" :key="m.id">
        <td>{{m.window_name}}</td>
        <td>{{m.status}}</td>
        <td>{{m.generation}}</td>
        <td>{{m.row_count}}</td>
        <td>{{m.total_amount}}</td>
        <td>{{m.partition_key}}</td>
        <td>{{m.created_at}}</td>
      </tr>
    </tbody>
  </table>
  <h2>重开台账</h2>
  <p class="muted">甲路径·作废重算：重开同事务作废旧世代物化与窗内事件并抬世代；台账与窗口世代同源。</p>
  <table>
    <thead><tr><th>窗口</th><th>从世代</th><th>到世代</th><th>口径</th><th>状态</th><th>时间</th></tr></thead>
    <tbody>
      <tr v-for="r in reopenLedger" :key="r.id">
        <td>{{r.window_name}}</td>
        <td>{{r.from_generation}}</td>
        <td>{{r.to_generation}}</td>
        <td>{{r.mode}}</td>
        <td>{{r.status}}</td>
        <td>{{r.created_at}}</td>
      </tr>
    </tbody>
  </table>
</div>
`;
