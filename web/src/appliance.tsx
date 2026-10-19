type ApplianceObservation={
  network?:{state:string;address?:string;mdns_hostname?:string;mac?:string;address_changed?:boolean;previous_address?:string;router_reservation_hint?:string};
  lid?:{state:string;message:string};
  suspend?:{state:string;warning?:string};
};

export function ApplianceStatus({value,unavailable=false}:{value?:ApplianceObservation;unavailable?:boolean}){
  const network=!unavailable?value?.network:undefined;
  const lid=!unavailable?value?.lid:undefined;
  const suspend=!unavailable?value?.suspend:undefined;
  return <section className="settings-section" aria-label="主機運作"><h2>主機運作</h2>
    <dl><div><dt>區網位址</dt><dd>{network?.state==='observed'?network.address:'尚無資料'}</dd></div>
      <div><dt>區網名稱</dt><dd>{network?.mdns_hostname||'尚無資料'}</dd></div>
      <div><dt>合上上蓋</dt><dd>{lid?.message||'尚未確認合蓋後會繼續運作。'}</dd></div></dl>
    {network?.state==='observed'&&network.address_changed&&<p className="notice" role="status">主機位址已變更：{network.previous_address} → {network.address}。</p>}
    {suspend?.warning&&<p className="notice" role="status">{suspend.warning}</p>}
    {suspend?.state==='unavailable'&&<p className="helper-text">暫時無法讀取睡眠紀錄。</p>}
    {!value&&!unavailable&&<p className="helper-text">主機尚未提供運作檢查，請透過正式更新取得支援。</p>}
    <details><summary>固定主機位址</summary><p>{network?.router_reservation_hint||'在路由器的「DHCP 位址保留」中，選這台主機並保留目前位址。'}</p>
      {network?.state==='observed'&&network.mac&&<p className="helper-text">網卡位址：<code>{network.mac}</code></p>}
      <p className="helper-text">區網名稱需要網路支援本機名稱探索；遠端管理請使用既有的安全連線方式。</p></details>
  </section>;
}
