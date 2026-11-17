"""Create a self-contained, explicitly offline design preview."""
import base64
import json
from pathlib import Path
from modern_desktop import Service, WEB

state = Service(preview=True).snapshot()
state['samples'] = ['bite.wav']
state['settings']['sample_file'] = 'bite.wav'
state['sample_description'] = 'Образец поклёвки · WAV'
state['logs'] = ['INFO | Автономный предпросмотр. Движок рыбалки не запущен.']
script = 'const previewState=' + json.dumps(state, ensure_ascii=False) + ';\n' + r'''
window.fetch=async function(url,options={}){
  let result=previewState,ok=true;
  if(url==='/api/bootstrap')result={token:'offline-preview'};
  else if(url==='/api/command'){
    const command=JSON.parse(options.body||'{}');
    if(command.action==='settings')Object.assign(previewState.settings,command.settings);
    else if(command.action==='reset_stats_confirmed')previewState.stats={casts:0,bites:0,catches:0};
    else if(command.action==='adjust_plus')previewState.stats.catches++;
    else if(command.action==='adjust_minus')previewState.stats.catches=Math.max(0,previewState.stats.catches-1);
    else if(!['save','refresh_devices','refresh_samples','refresh_wow_processes','check_components'].includes(command.action)){
      ok=false;result={error:'Это автономный предпросмотр дизайна. Для работы с игрой запусти NobrainModern.exe.'};
    }
  }
  return {ok,json:async()=>JSON.parse(JSON.stringify(result))};
};
'''
html = (WEB/'index.html').read_text(encoding='utf-8')
mark = 'data:image/svg+xml;base64,' + base64.b64encode((WEB/'mark.svg').read_bytes()).decode()
html = html.replace('mark.svg', mark)
if '<link rel="stylesheet" href="style.css">' in html:
    html = html.replace('<link rel="stylesheet" href="style.css">', '<style>'+(WEB/'style.css').read_text(encoding='utf-8')+'</style>')
html = html.replace('<script src="app.js" defer></script>', '<script>'+script+(WEB/'app.js').read_text(encoding='utf-8')+'</script>')
html = html.replace('<title>Nobrain Companion</title>', '<title>Nobrain Companion — предпросмотр</title>')
target = Path(__file__).resolve().parent.parent/'Nobrain-Modern-Preview.html'
target.write_text(html, encoding='utf-8')
print(target)

