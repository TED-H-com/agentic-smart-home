// Browser-only fictional household shared by the HA simulation and product frames.
export type DemoSurface = 'outer' | 'bridge';
export type DemoDevice = {
  entity_id:string; name:string; domain:string; area:string; state:string;
  brightness?:number; temperature?:number; current_temperature?:number;
};
export type DemoCandidate = {id:string; title:string; integration_domain:string};
export type DemoIntegration = {id:string; title:string; domain:string; state:string; device_count:number};
export type DemoAutomation = {id:string; title:string; enabled:boolean};
export type DemoHome = {
  devices:DemoDevice[]; candidates:DemoCandidate[];
  integrations:DemoIntegration[]; automations:DemoAutomation[];
};
export type DemoResponse = {status:number; body:unknown};
export interface DemoModel {
  getVersion:()=>number;
  subscribe:(listener:()=>void)=>()=>void;
  home:()=>DemoHome;
  control:(entityId:string,action:'toggle'|'brightness'|'temperature',value?:number)=>void;
  connectCandidate:(id:string)=>void;
  request:(path:string,method:string,body:Record<string,unknown>,surface:DemoSurface,key?:string)=>DemoResponse;
}
