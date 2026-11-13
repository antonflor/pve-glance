const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('pve-glance.py', 'utf8');
const script = source.split('<script>')[2].split('</script>')[0];
const functions = script.slice(script.indexOf('const GiB'), script.indexOf('function sectionFor'));
const context = vm.createContext({});
vm.runInContext(functions, context);
const guest = {status:'running',type:'qemu',vmid:100,name:'test',maxmem:100,mem:20,
  filesystems:[{mountpoint:'/<img src=x>',disk:0,maxdisk:100}],disk_status:'ok'};
const html = context.guestCard(guest);
assert(html.includes('DISK /&lt;img src=x&gt;'));
assert(!html.includes('n/a'));
assert(html.includes('width:0%'));
assert(context.guestCard({...guest,filesystems:[],disk_status:'agent unavailable'}).includes('agent unavailable'));
assert(!context.guestCard({...guest,status:'stopped'}).includes('DISK'));
assert(!context.guestCard({...guest,type:'lxc',disk:0,maxdisk:100}).includes('n/a'));
console.log('Guest card rendering checks passed');
