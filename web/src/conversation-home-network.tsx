import {HomeLanSettings} from './ha-lan';
import {Button} from './ui';
import './conversation-home-network.css';

/** Host management remains outside the Ingress capability boundary. */
export function ConversationHomeNetwork({hostMode,bridge,storageScope,disabled,open,onToggle,onContinue}:{
  hostMode:boolean;bridge:boolean;storageScope?:string;disabled:boolean;open:boolean;onToggle:()=>void;onContinue:()=>void;
}){
  return <div className={`conversation-home-network${open?' is-open':''}`}>
    <Button secondary type="button" disabled={disabled} aria-expanded={open} onClick={onToggle}>接通家庭網路</Button>
    {open&&<section className="conversation-home-network-panel" aria-label="接通家庭網路">
      {hostMode&&!bridge?<HomeLanSettings key={storageScope} storageScope={storageScope} disabled={disabled} active={open} onContinue={onContinue}/>:<p>{bridge?'請在主機桌面開啟 Agentic Home，進入智能管家後選「接通家庭網路」；完成後回到這段對話探索設備。':'這個安裝方式未提供主機家庭網路接入，請在管理這台家庭系統的主機確認網路。'}</p>}
      <Button secondary type="button" onClick={onToggle}>收起家庭網路</Button>
    </section>}
  </div>;
}
