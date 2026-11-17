// Unit checks for view generation and state transitions; not layout tests.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes=new Map();
function element(key){if(!nodes.has(key))nodes.set(key,{innerHTML:'',textContent:'',hidden:false,disabled:false,open:false,value:0,dataset:{},classList:{toggle(){}},setAttribute(){},addEventListener(){},showModal(){this.open=true},close(){this.open=false},focus(){},checkValidity(){return true}});return nodes.get(key);}
const document={querySelector:element,querySelectorAll:()=>[],getElementById:element,addEventListener(){}};
const context=vm.createContext({document,window:{addEventListener(){}},location:{hash:''},setTimeout:()=>1,clearTimeout(){},AbortSignal,console,navigator:{clipboard:{writeText:async()=>{}}}});
const source=fs.readFileSync('desktop_web/app.js','utf8').replace(/boot\(\);\s*$/,'');
vm.runInContext(source,context);
const preview=fs.readFileSync('../Nobrain-Modern-Preview.html','utf8');
const initial=JSON.parse(preview.match(/const previewState=(.*);\r?\n/)[1]);
context.initial=initial;
vm.runInContext('data=initial;draft={...data.settings};',context);
for(const page of ['fishing','audio','vision','remote','stats','components','logs']){
 const html=vm.runInContext(`views['${page}']()`,context);
 assert.ok(html.includes('class="card'));
 assert.ok(!html.includes('undefined'));
 vm.runInContext(`page='${page}';render()`,context);
 assert.ok(nodes.get('#navigation').innerHTML.includes('aria-current="page"'));
}
assert.equal(vm.runInContext(`esc('<script>"&')`,context),'&lt;script&gt;&quot;&amp;');
vm.runInContext("data.running=true;data.score='Splash similarity: 0.780 / 0.650';syncDynamic()",context);
assert.equal(nodes.get('#match-value').textContent,'78%');
assert.equal(nodes.get('#start').disabled,true);
assert.equal(nodes.get('#stop').disabled,false);
vm.runInContext('connected=false;syncDynamic()',context);
assert.equal(nodes.get('#stop').disabled,true);
vm.runInContext("dirty=true;draft.threshold=.91;accept({...data,settings:{...data.settings,threshold:.20}})",context);
assert.equal(vm.runInContext('draft.threshold',context),.91);
const requests=[];
context.fetch=async(url,options)=>{requests.push(JSON.parse(options.body));return {ok:true,json:async()=>initial};};
vm.runInContext('busy=true;connected=true;',context);
(async()=>{
 await vm.runInContext("act('stop')",context);
 assert.deepEqual(requests.map(x=>x.action),['stop']);
 assert.equal(vm.runInContext('dirty',context),true);
 requests.length=0;
 await vm.runInContext("act('save')",context);
 assert.deepEqual(requests.map(x=>x.action),['settings','save']);
 assert.equal(vm.runInContext('dirty',context),false);
 console.log('PASS: seven views, escaping, score display, button states, unsaved edits, urgent stop, settings/save dispatch.');
})().catch(e=>{console.error(e);process.exitCode=1});

