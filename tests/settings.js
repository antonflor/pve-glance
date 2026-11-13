const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync('pve-glance.py', 'utf8');
const script = source.split('<script>')[2].split('</script>')[0];
const settingsCode = script.slice(script.indexOf('const THEMES'), script.indexOf('function clock()'));
function setup(saved, blocked=false){
  const nodes = {};
  for(const id of ['brand','tag','settings-status','settings','settings-open','settings-close','settings-reset']) {
    nodes[id] = {textContent:'',events:{},addEventListener(name,fn){this.events[name]=fn;}};
  }
  nodes.settings.showModal=()=>{nodes.settings.open=true;};
  nodes.settings.close=()=>{nodes.settings.open=false;};
  let focused;
  const inputs = ['light','dark','monty','stars','tricolor'].map(value=>({value,checked:false,events:{},
    addEventListener(name,fn){this.events[name]=fn;},focus(){focused=value;}}));
  const storage = new Map([['unrelated','keep'],['pgtheme',saved]]);
  let theme;
  const context = vm.createContext({document:{
    documentElement:{setAttribute(k,v){theme=v;}},
    querySelectorAll:()=>inputs,getElementById:id=>nodes[id]},
    localStorage:{getItem(k){if(blocked) throw Error('blocked');return storage.get(k);},
      setItem(k,v){if(blocked) throw Error('blocked');storage.set(k,v);}}});
  vm.runInContext(settingsCode,context);
  return {nodes,inputs,storage,get theme(){return theme;},get focused(){return focused;}};
}
for(const theme of ['light','dark','monty','stars','tricolor']){
  const state = setup(theme);
  assert.equal(state.theme,theme);
  assert.equal(state.inputs.filter(x=>x.checked)[0].value,theme);
  state.nodes['settings-open'].events.click();assert.equal(state.nodes.settings.open,true);
  assert.equal(state.focused,theme);
  state.nodes['settings-close'].events.click();assert.equal(state.nodes.settings.open,false);
  const next=state.inputs.find(x=>x.value==='tricolor');next.checked=true;next.events.change();
  assert.equal(state.theme,'tricolor');assert.equal(state.storage.get('pgtheme'),'tricolor');
  state.nodes['settings-reset'].events.click();assert.equal(state.theme,'dark');
  assert.equal(state.storage.get('unrelated'),'keep');
}
assert.equal(setup('invalid').theme,'dark');
const blocked=setup(null,true);
blocked.inputs[3].checked=true;blocked.inputs[3].events.change();
assert.equal(blocked.theme,'stars');
assert.match(blocked.nodes['settings-status'].textContent,/unavailable/);
console.log('Appearance settings migration, selection, focus, reset and blocked-storage checks passed');
