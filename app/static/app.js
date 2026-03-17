/* Download Proxy + Media Server v5.0 - Frontend JS */
let K=localStorage.getItem('dp_key')||'',U=localStorage.getItem('dp_url')||'',poll=null,hasAct=false;
let fmCurPath='',fmViewMode='grid',fmAllItems=[],fmSelected=new Set(),renameTarget='';
let trashCount=0,currentUser=localStorage.getItem('dp_user')||'',currentRole=localStorage.getItem('dp_role')||'';
let fmSort='name',fmSortDir=1,ctxTarget=null,previewPath='',previewDirty=false;
let _cmEditor=null; // CodeMirror instance
const TEXT_EXTS=['.txt','.md','.log','.csv','.json','.xml','.yaml','.yml','.ini','.cfg','.conf','.env','.toml','.py','.js','.ts','.html','.css','.jsx','.tsx','.sh','.bash','.bat','.ps1','.c','.cpp','.h','.java','.go','.rs','.rb','.php','.sql','.srt','.vtt','.ass','.ssa','.nfo'];
const IMG_EXTS=['.jpg','.jpeg','.png','.gif','.webp','.bmp','.svg'];
const VID_EXTS=['.mp4','.mkv','.avi','.mov','.wmv','.flv','.webm','.m4v','.ts'];
const AUD_EXTS=['.mp3','.flac','.aac','.wav','.ogg','.m4a'];

document.addEventListener('DOMContentLoaded',()=>{
  // Set default server URL placeholders
  const defUrl=location.origin;
  document.getElementById('aUrl').placeholder=defUrl;
  document.getElementById('jwtUrl').placeholder=defUrl;
  if(!K)showAuth();else init();
  document.getElementById('aKey').addEventListener('keypress',e=>{if(e.key==='Enter')auth()});
  document.getElementById('jwtPass').addEventListener('keypress',e=>{if(e.key==='Enter')jwtLogin()});
  // Drag-only upload zone (overlay)
  const body=document.body;
  let dragCount=0;
  body.addEventListener('dragenter',ev=>{ev.preventDefault();dragCount++;document.getElementById('uploadZone').classList.add('drag')});
  body.addEventListener('dragleave',ev=>{ev.preventDefault();dragCount--;if(dragCount<=0){dragCount=0;document.getElementById('uploadZone').classList.remove('drag')}});
  body.addEventListener('dragover',ev=>ev.preventDefault());
  body.addEventListener('drop',ev=>{ev.preventDefault();dragCount=0;document.getElementById('uploadZone').classList.remove('drag');if(ev.dataTransfer.files.length)fmUpload(ev.dataTransfer.files)});
});

function base(){return(U||location.origin).replace(/\/$/,'')}
function showAuth(){document.getElementById('authM').style.display='flex';document.getElementById('aKey').focus()}
async function auth(){
  const key=document.getElementById('aKey').value.trim();
  U=document.getElementById('aUrl').value.trim();
  if(!key){toast('Enter API key','err');return}
  // Validate key against server
  const serverBase=(U||location.origin).replace(/\/$/,'');
  try{
    const r=await fetch(serverBase+'/health',{headers:{'Authorization':'Bearer '+key}});
    if(!r.ok){toast('Invalid API key — check and try again','err');return}
  }catch(e){toast('Cannot connect to server — check URL','err');return}
  K=key;
  localStorage.setItem('dp_key',K);localStorage.setItem('dp_url',U);
  currentUser='Admin';currentRole='admin';localStorage.setItem('dp_user','Admin');localStorage.setItem('dp_role','admin');
  document.getElementById('authM').style.display='none';updateUserUI();init();
}
async function jwtLogin(){
  const u=document.getElementById('jwtUser').value.trim();
  const p=document.getElementById('jwtPass').value.trim();
  U=document.getElementById('jwtUrl').value.trim();
  if(!u||!p){toast('Enter username and password','err');return}
  try{
    const r=await fetch((U||location.origin).replace(/\/$/,'')+'/api/admin/login',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username:u,password:p})
    });
    if(!r.ok){const e=await r.json().catch(()=>({}));toast(e.detail||'Login failed','err');return}
    const d=await r.json();
    K=d.token;currentUser=d.username;currentRole=d.role;
    localStorage.setItem('dp_key',K);localStorage.setItem('dp_url',U);
    localStorage.setItem('dp_user',d.username);localStorage.setItem('dp_role',d.role);
    document.getElementById('authM').style.display='none';
    toast(`Welcome ${d.username} (${d.role})`,'ok');updateUserUI();init();
  }catch(e){toast('Login failed: '+e.message,'err')}
}
function logout(){
  K='';U='';currentUser='';currentRole='';
  localStorage.removeItem('dp_key');localStorage.removeItem('dp_url');
  localStorage.removeItem('dp_user');localStorage.removeItem('dp_role');
  showAuth();
}
function updateUserUI(){
  const el=document.getElementById('userInfo');
  if(el&&currentUser){
    const badge=currentRole==='admin'?'<span style="background:var(--pri);color:#fff;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:600">Admin</span>':'<span style="background:var(--bg3);color:var(--txt2);padding:2px 8px;border-radius:10px;font-size:10px;font-weight:600">'+esc(currentRole)+'</span>';
    el.innerHTML=`${badge} <button class="tb-btn" onclick="logout()" title="Logout" style="font-size:14px;padding:4px 8px">🚪</button>`;
  }
}
function init(){health();rAll();rFiles();rMedia();startPoll();updateUserUI()}
function startPoll(){if(poll)clearInterval(poll);poll=setInterval(()=>{rAll();health()},hasAct?1500:5000)}

async function api(m,p,b){
  const o={method:m,headers:{'Authorization':'Bearer '+K}};
  if(b&&!(b instanceof FormData)){o.headers['Content-Type']='application/json';o.body=JSON.stringify(b)}
  else if(b instanceof FormData){o.body=b}
  const r=await fetch(base()+p,o);
  if(r.status===401||r.status===403){showAuth();throw new Error('Unauthorized')}
  if(!r.ok){const e=await r.json().catch(()=>({detail:r.statusText}));throw new Error(typeof e.detail==='object'?e.detail.error||JSON.stringify(e.detail):e.detail||'Failed')}
  return r.json();
}

async function health(){
  try{
    const d=await api('GET','/health');
    document.getElementById('sDot').className='tb-dot';
    document.getElementById('sTxt').textContent='Online';
    document.getElementById('sDisk').textContent=d.disk_free_gb??'--';
    document.getElementById('sFiles').textContent=d.files_count??0;
    trashCount=d.trash_count||0;
    const tb=document.getElementById('trashBadge');
    if(tb){tb.textContent=trashCount;tb.style.display=trashCount?'inline':'none'}
    if(d.disk_total_gb&&d.disk_used_gb){
      const pct=(d.disk_used_gb/d.disk_total_gb*100).toFixed(0);
      document.getElementById('diskBar').style.width=pct+'%';
      document.getElementById('diskUsed').textContent=d.disk_used_gb.toFixed(1)+' GB';
      document.getElementById('diskTotal').textContent=d.disk_total_gb.toFixed(0)+' GB';
    }
  }catch{document.getElementById('sDot').className='tb-dot off';document.getElementById('sTxt').textContent='Offline'}
}

// Nav
function go(page,el){
  document.querySelectorAll('.sb-item').forEach(s=>s.classList.remove('active'));
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('p-'+page).classList.add('active');
  const titles={dl:'Downloads',fm:'File Manager',media:'Media',trash:'Recycle Bin',dedup:'Deduplication',share:'Share Links'};
  document.getElementById('pageTitle').textContent=titles[page]||'';
  if(page==='fm')rFiles();
  if(page==='media')rMedia();
  if(page==='trash')rTrash();
  if(page==='dedup')rDedup();
  if(page==='share')rShares();
}
function stab(n,el){
  el.parentElement.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  const container=el.closest('.card-b')||el.closest('.modal');
  if(container)container.querySelectorAll('.tc').forEach(t=>t.classList.remove('active'));
  el.classList.add('active');document.getElementById('tab-'+n).classList.add('active');
}

// Downloads
async function subUrl(){
  const raw=document.getElementById('dlUrl').value.trim();
  if(!raw){toast('Enter URL(s)','err');return}
  const urls=raw.split('\n').map(s=>s.trim()).filter(s=>/^https?:\/\//i.test(s));
  if(!urls.length){toast('No valid URLs found','err');return}
  let h={};const ht=document.getElementById('dlH').value.trim();
  if(ht){try{h=JSON.parse(ht)}catch{toast('Invalid JSON headers','err');return}}
  const fn=urls.length===1?document.getElementById('dlFn').value.trim()||undefined:undefined;
  const eng=document.getElementById('dlEng').value;
  const b=document.getElementById('bUrl');b.disabled=true;b.innerHTML='<span class="spin"></span>';
  let ok=0;
  for(const url of urls){
    try{await api('POST','/api/download',{url,headers:h,filename:fn,engine:eng});ok++}catch(e){console.error(e)}
  }
  b.disabled=false;b.innerHTML='⬇ Download';
  if(ok)document.getElementById('dlUrl').value='';
  rAll();
}
function splitCurlCmds(text){
  const joined=text.replace(/\\\s*\n/g,' ');
  const items=[];
  const lines=joined.split('\n').map(s=>s.trim()).filter(Boolean);
  let i=0;
  while(i<lines.length){
    const line=lines[i];
    if(/^curl\s/i.test(line)){
      let cmd=line;i++;
      while(i<lines.length&&!/^curl\s/i.test(lines[i])&&!/^https?:\/\//i.test(lines[i])){
        cmd+=' '+lines[i];i++;
      }
      items.push({type:'curl',value:cmd.trim()});
    }else if(/^https?:\/\//i.test(line)){
      items.push({type:'url',value:line});i++;
    }else{i++}
  }
  return items;
}
async function subCurl(){
  const raw=document.getElementById('dlCurl').value.trim();
  if(!raw){toast('Paste cURL command(s)','err');return}
  const items=splitCurlCmds(raw);
  if(!items.length){toast('No valid commands found','err');return}
  const eng=document.getElementById('dlCE').value;
  const b=document.getElementById('bCurl');
  const info=document.getElementById('curlInfo');
  b.disabled=true;b.innerHTML='<span class="spin"></span>';
  let ok=0;
  if(items.length>1&&info)info.innerHTML=`⏳ 0/${items.length}`;
  for(let i=0;i<items.length;i++){
    if(items.length>1&&info)info.innerHTML=`⏳ ${i+1}/${items.length}`;
    try{
      const body=items[i].type==='curl'
        ?{url:'p',curl_command:items[i].value,engine:eng}
        :{url:items[i].value,engine:eng};
      await api('POST','/api/download',body);ok++;
    }catch(e){console.error(e)}
  }
  b.disabled=false;b.innerHTML='⬇ Download';
  if(info)info.innerHTML='';
  if(ok)document.getElementById('dlCurl').value='';
  rAll();
}
async function cancelDl(tid){
  try{await api('DELETE','/api/downloads/'+tid);rAll()}catch(e){toast(e.message,'err')}
}
async function resumeDl(tid){
  try{await api('POST','/api/downloads/'+tid+'/resume');rAll()}catch(e){toast(e.message,'err')}
}
async function clearDone(){
  try{const r=await api('DELETE','/api/downloads');rAll()}catch(e){toast(e.message,'err')}
}
async function delDownloadFile(tid,filename){
  if(!await dlgConfirm('🗑 Permanently Delete','Delete "'+filename.split('/').pop()+'" forever? This cannot be undone.'))return;
  try{
    // Delete the actual file
    await api('DELETE','/api/files/'+encodeURIComponent(filename)+'?permanent=true');
    // Remove download entry
    try{await api('DELETE','/api/downloads/'+tid)}catch(e){}
    toast('Deleted permanently','ok');rAll();rFiles();health();
  }catch(e){toast(e.message,'err')}
}

async function rAll(){
  try{const d=await api('GET','/api/downloads');renderDL(d.downloads||[])}catch{}
}
function renderDL(items){
  const el=document.getElementById('dlList');
  const act=items.filter(i=>['downloading','queued','extracting','compressing'].includes(i.status)).length;
  const done=items.filter(i=>i.status==='completed').length;
  document.getElementById('sAct').textContent=act;document.getElementById('sDone').textContent=done;
  const w=hasAct;hasAct=act>0;if(hasAct!==w)startPoll();
  if(!items.length){el.innerHTML='<div class="empty"><span>📭</span>No downloads</div>';return}
  const ord={downloading:0,extracting:1,compressing:1,queued:2,completed:3,cancelled:4,failed:5};
  items.sort((a,b)=>(ord[a.status]??6)-(ord[b.status]??6)||(b.created_at||'').localeCompare(a.created_at||''));
  el.innerHTML=items.map(d=>{
    const sc={queued:'st-q',downloading:'st-d',completed:'st-c',failed:'st-f',cancelled:'st-q',extracting:'st-e',compressing:'st-e'};
    const pct=d.percent||0;const isA=d.status==='downloading'||d.status==='extracting'||d.status==='compressing';
    const eng=d.engine||'';const eH=eng.includes('curl')?'<span class="eng eng-c">curl</span>':eng.includes('aria')?'<span class="eng eng-a">aria2c</span>':'';
    let pH='';
    if(isA)pH=`<div class="prog"><div class="prog-f on" style="width:${pct}%"></div></div><div class="dl-pi"><span>${pct.toFixed(1)}% · ${hs(d.downloaded||0)}${d.total_size?' / '+hs(d.total_size):''}</span><span class="dl-spd">${d.speed||'...'}</span></div>`;
    else if(d.status==='completed')pH='<div class="prog"><div class="prog-f" style="width:100%"></div></div>';
    // Action buttons
    let aH='';
    if(d.status==='downloading'||d.status==='queued')aH=`<button class="btn-d" onclick="cancelDl('${d.task_id}')" title="Cancel" style="font-size:10px;padding:2px 6px">✕</button>`;
    if(d.status==='cancelled'||d.status==='failed')aH=`<button class="btn-g" onclick="resumeDl('${d.task_id}')" title="Resume" style="font-size:10px;padding:2px 6px">▶</button>`;
    const fnExt=d.filename?'.'+d.filename.split('.').pop().toLowerCase():'';
    const isVid=VID_EXTS.includes(fnExt);
    if(d.status==='completed'&&d.download_url){
      aH='';
      if(isVid)aH+=`<button class="btn-g" onclick="event.stopPropagation();navigateToFile('${d.filename.replace(/'/g,"\\'")}'+'')" title="Play" style="font-size:10px;padding:2px 6px">▶️</button>`;
      aH+=`<a href="${d.download_url}" class="btn-s" target="_blank" style="font-size:10px">↓</a><button class="cp" onclick="cpL('${d.download_url}')">📋</button>`;
    }
    // Delete button for non-active downloads (permanent delete file)
    if(!['downloading','queued','extracting','compressing'].includes(d.status)&&d.filename){
      aH+=`<button class="btn-d" onclick="event.stopPropagation();delDownloadFile('${d.task_id}','${d.filename.replace(/'/g,"\\\'")}')" title="Delete file" style="font-size:10px;padding:2px 6px">🗑</button>`;
    }
    let eR='';if(d.status==='failed'&&d.error)eR=`<div style="font-size:10px;color:var(--red);margin-top:4px;word-break:break-all">❌ ${esc(d.error.substring(0,150))}</div>`;
    if(d.status==='cancelled')eR=`<div style="font-size:10px;color:var(--txt3);margin-top:4px">Cancelled — click ▶ to resume</div>`;
    const sz=d.status==='completed'&&d.file_size?`<span>${hs(d.file_size)}</span>`:'';
    const t=d.created_at?new Date(d.created_at).toLocaleTimeString():'';
    const ic={extracting:'📦',compressing:'🗜️',cancelled:'⏸'}[d.status]||'📄';
    const nameClick=d.status==='completed'&&d.filename?` onclick="navigateToFile('${d.filename.replace(/'/g,"\\'")}')"; style="cursor:pointer"`:'';
    return`<div class="dl-i"><div class="dl-top"><div class="dl-name"${nameClick}>${ic} ${esc(d.filename||'?')}</div><div class="dl-acts">${aH}</div></div><div class="dl-meta"><span class="st ${sc[d.status]||'st-q'}">${d.status}</span>${eH}${sz}<span>🕐 ${t}</span></div>${pH}${eR}</div>`;
  }).join('');
}
// Navigate from download list to file in File Manager
function navigateToFile(filename){
  const parts=filename.split('/');
  const dir=parts.length>1?parts.slice(0,-1).join('/'):'';
  const ext='.'+filename.split('.').pop().toLowerCase();
  // Media files → play directly
  if(VID_EXTS.includes(ext)||AUD_EXTS.includes(ext)){
    playMediaFile(filename);
    return;
  }
  // Other files → just navigate to folder in File Manager
  fmCurPath=dir;
  const fmTab=document.querySelector('.sb-item[onclick*="fm"]');
  if(fmTab)go('fm',fmTab);
}

// File Manager
async function rFiles(){
  try{const d=await api('GET','/api/files?path='+encodeURIComponent(fmCurPath));fmAllItems=d.items||[];renderFM()}catch(e){console.error(e)}
}
function renderFM(){
  fmSelected.clear();document.getElementById('fmBulkDel').style.display='none';
  document.getElementById('fmBulkCompress').style.display='none';
  document.getElementById('fmSelInfo').style.display='none';
  document.getElementById('fmBulkDiv').style.display='none';
  const pathEl=document.getElementById('fmPath');
  let crumbs='<span class="fm-crumb'+(fmCurPath?'':' current')+'" onclick="fmGo(\'\')">📁 /</span>';
  if(fmCurPath){
    const parts=fmCurPath.split('/');let acc='';
    parts.forEach((p,i)=>{acc+=(i?'/':'')+p;const a=acc;crumbs+=`<span class="fm-sep">›</span><span class="fm-crumb${i===parts.length-1?' current':''}" onclick="fmGo('${a}')">${esc(p)}</span>`});
  }
  pathEl.innerHTML=crumbs;
  let items=[...fmAllItems];
  const q=document.getElementById('fmSearch').value.toLowerCase();
  if(q)items=items.filter(f=>f.name.toLowerCase().includes(q));
  // Sort
  items.sort((a,b)=>{
    if(a.type==='folder'&&b.type!=='folder')return -1;
    if(a.type!=='folder'&&b.type==='folder')return 1;
    let v=0;
    if(fmSort==='name')v=a.name.localeCompare(b.name);
    else if(fmSort==='size')v=(a.size||0)-(b.size||0);
    else if(fmSort==='date')v=(a.modified||'').localeCompare(b.modified||'');
    else if(fmSort==='type')v=(a.ext||'').localeCompare(b.ext||'');
    return v*fmSortDir;
  });
  const el=document.getElementById('fmContent');
  if(!items.length){el.innerHTML='<div class="empty"><span>📂</span>Empty folder</div>';return}
  const icons={folder:'📁',video:'🎬',audio:'🎵',subtitle:'🔤',archive:'📦',image:'🖼',file:'📄'};
  if(fmViewMode==='grid'){
    el.innerHTML='<div class="fm-grid">'+items.map(f=>{
      const ic=icons[f.type]||'📄';
      const ext=(f.ext||'').toLowerCase();
      const isMedia=VID_EXTS.includes(ext)||AUD_EXTS.includes(ext);
      const dbl=f.type==='folder'?`ondblclick="fmGo('${esc(f.path)}')"`:
        isMedia?`ondblclick="playMediaFile('${esc(f.path)}')"`:
        (TEXT_EXTS.includes(ext)||IMG_EXTS.includes(ext))?`ondblclick="openPreview('${esc(f.path)}')"`:
        f.download_url?`ondblclick="window.open('${f.download_url}')"`:'';
      const archBtn=f.type==='archive'?`<button class="btn-o" style="position:absolute;bottom:4px;right:4px;font-size:9px;padding:2px 5px" onclick="event.stopPropagation();extractF('${esc(f.path)}')">📦</button>`:'';
      // Image thumbnail
      let thumb='';
      if(IMG_EXTS.includes(ext)&&f.download_url)thumb=`<img class="fm-thumb" src="${f.download_url}" loading="lazy" onerror="this.style.display='none'">`;
      return`<div class="fm-item" onclick="fmSel(this,'${esc(f.path)}')" ${dbl} data-path="${esc(f.path)}" oncontextmenu="fmCtx(event,'${esc(f.path)}')"><div class="fm-check">✓</div>${thumb||`<span class="fm-icon">${ic}</span>`}<div class="fm-fname">${esc(f.name)}</div><div class="fm-fsize">${f.size_human}${f.items!=null?' · '+f.items+' items':''}</div>${archBtn}</div>`;
    }).join('')+'</div>';
  }else{
    const sa=k=>fmSort===k?(fmSortDir>0?'▲':'▼'):'';
    el.innerHTML=`<div class="fm-list"><div class="fm-row fm-row-h"><div onclick="fmSetSort('name')" style="cursor:pointer">Name <span class="sort-arrow">${sa('name')}</span></div><div onclick="fmSetSort('size')" style="cursor:pointer">Size <span class="sort-arrow">${sa('size')}</span></div><div onclick="fmSetSort('type')" style="cursor:pointer">Type <span class="sort-arrow">${sa('type')}</span></div></div>`+items.map(f=>{
      const ic=icons[f.type]||'📄';
      const ext=(f.ext||'').toLowerCase();
      const isMedia=VID_EXTS.includes(ext)||AUD_EXTS.includes(ext);
      const dbl=f.type==='folder'?`ondblclick="fmGo('${esc(f.path)}')"`:
        isMedia?`ondblclick="playMediaFile('${esc(f.path)}')"`:
        (TEXT_EXTS.includes(ext)||IMG_EXTS.includes(ext))?`ondblclick="openPreview('${esc(f.path)}')"`
        :f.download_url?`ondblclick="window.open('${f.download_url}')"`:''
      return`<div class="fm-row" onclick="fmSel(this,'${esc(f.path)}')" ${dbl} data-path="${esc(f.path)}" oncontextmenu="fmCtx(event,'${esc(f.path)}')"><div class="fm-check">✓</div><div class="fm-row-name"><span>${ic}</span>${esc(f.name)}</div><div style="color:var(--txt3);font-size:12px">${f.size_human}</div><div style="color:var(--txt3);font-size:12px">${f.mime_type||f.type}</div></div>`;
    }).join('')+'</div>';
  }
}
function fmSetSort(key){
  if(fmSort===key)fmSortDir*=-1;else{fmSort=key;fmSortDir=1}
  renderFM();
}
function fmGo(path){fmCurPath=path;rFiles()}
function fmGoUp(){if(!fmCurPath)return;const p=fmCurPath.split('/');p.pop();fmGo(p.join('/'))}
function fmFilter(){renderFM()}
function fmToggleView(){fmViewMode=fmViewMode==='grid'?'list':'grid';document.getElementById('fmViewBtn').textContent=fmViewMode==='grid'?'☰':'▦';renderFM()}
function fmSortChange(v){fmSort=v;renderFM()}
function fmToggleSortDir(){fmSortDir*=-1;document.getElementById('fmSortDirBtn').textContent=fmSortDir>0?'↑':'↓';renderFM()}
function fmSel(el,path){
  if(fmSelected.has(path)){fmSelected.delete(path);el.classList.remove('selected')}
  else{fmSelected.add(path);el.classList.add('selected')}
  const cnt=fmSelected.size;
  const show=cnt>0;
  document.getElementById('fmBulkDel').style.display=show?'inline-flex':'none';
  document.getElementById('fmBulkCompress').style.display=show?'inline-flex':'none';
  document.getElementById('fmSelInfo').style.display=show?'inline-flex':'none';
  document.getElementById('fmBulkDiv').style.display=show?'block':'none';
  document.getElementById('fmSelCount').textContent=cnt;
}
async function fmMkdir(){
  const name=await dlgPrompt('📁 New Folder','Enter folder name:');if(!name)return;
  try{await api('POST','/api/files/mkdir?path='+encodeURIComponent(fmCurPath),{name});toast('Created '+name,'ok');rFiles()}
  catch(e){toast(e.message,'err')}
}
async function fmNewFile(){
  const TEMPLATES={
    '.html':'<!DOCTYPE html>\n<html lang="en">\n<head>\n  <meta charset="UTF-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n  <title>Document</title>\n</head>\n<body>\n  \n</body>\n</html>',
    '.css':'/* styles */\nbody {\n  margin: 0;\n  padding: 0;\n}\n',
    '.js':'// script\n',
    '.php':'<?php\n\n?>\n',
    '.py':'# -*- coding: utf-8 -*-\n',
    '.sh':'#!/bin/bash\n',
    '.json':'{\n  \n}\n',
    '.md':'# Title\n\n'
  };
  const name=await dlgPrompt('📄 New File','Enter filename with extension:','newfile.txt');if(!name)return;
  const ext='.'+name.split('.').pop().toLowerCase();
  const content=TEMPLATES[ext]||'';
  try{
    await api('POST','/api/files/create?path='+encodeURIComponent(fmCurPath),{filename:name,content});
    toast('Created '+name,'ok');rFiles();
    // Auto-open in editor
    const filePath=fmCurPath?fmCurPath+'/'+name:name;
    if(TEXT_EXTS.includes(ext))setTimeout(()=>openPreview(filePath),300);
  }catch(e){toast(e.message,'err')}
}
async function fmBulkDel(){
  if(!await dlgConfirm('🗑 Delete Files',`Move ${fmSelected.size} items to Recycle Bin?`))return;
  try{await api('POST','/api/files/delete-bulk',{filenames:[...fmSelected]});toast(`Deleted ${fmSelected.size} items`,'ok');rFiles();rMedia();health()}
  catch(e){toast(e.message,'err')}
}
async function fmBulkCompress(){
  const name=await dlgPrompt('🗜️ Compress','Archive name (e.g. files.zip, file.gz, file.bz2):','archive.zip');if(!name)return;
  let fmt='zip';
  if(name.endsWith('.tar.gz')||name.endsWith('.tgz'))fmt='tar.gz';
  else if(name.endsWith('.tar.bz2')||name.endsWith('.tbz2'))fmt='tar.bz2';
  else if(name.endsWith('.gz'))fmt='gzip';
  else if(name.endsWith('.bz2'))fmt='bzip2';
  try{await api('POST','/api/compress',{filenames:[...fmSelected],archive_name:name,format:fmt});toast('Compression started','ok');rAll()}
  catch(e){toast(e.message,'err')}
}
async function fmUpload(files){
  if(!files||!files.length)return;
  for(const f of files){
    const fd=new FormData();fd.append('file',f);fd.append('path',fmCurPath);
    try{await api('POST','/api/upload',fd);toast('Uploaded '+f.name,'ok')}
    catch(e){toast('Upload failed: '+e.message,'err')}
  }
  rFiles();
}
function showRename(path,name){
  renameTarget=path;
  document.getElementById('renameM').style.display='flex';
  document.getElementById('renameOld').textContent='Rename: '+name;
  document.getElementById('renameIn').value=name;
  document.getElementById('renameIn').select();
}
function closeRename(){document.getElementById('renameM').style.display='none'}
async function doRename(){
  const nn=document.getElementById('renameIn').value.trim();
  if(!nn){toast('Enter name','err');return}
  try{await api('POST','/api/files/rename/'+encodeURIComponent(renameTarget),{new_name:nn});toast('Renamed','ok');closeRename();rFiles()}
  catch(e){toast(e.message,'err')}
}
async function delF(path){
  if(!await dlgConfirm('🗑 Delete','Move "'+path.split('/').pop()+'" to Recycle Bin?'))return;
  try{await api('DELETE','/api/files/'+encodeURIComponent(path));toast('Moved to trash','ok');rFiles();rMedia();health()}
  catch(e){toast(e.message,'err')}
}

// Extract
let _extPollId=null;
async function extractF(path){
  const name=path.split('/').pop();
  let extractPath=path;
  let isMultipart=false;

  // Detect multipart: .partN.rar format
  const partMatch=name.match(/^(.+?)\.part(\d+)\.rar$/i);
  // Detect multipart: old RAR .r00, .r01
  const oldRarMatch=name.match(/^(.+?)\.r(\d{2,})$/i);
  // Detect split: .zip.001, .7z.001
  const splitMatch=name.match(/^(.+?\.(zip|7z))\.(\d{3,})$/i);

  if(partMatch||oldRarMatch||splitMatch){
    isMultipart=true;
    try{
      const check=await api('POST','/api/extract/check/'+encodeURIComponent(path));
      if(check.is_multipart&&!check.complete){
        const errMsg=check.error||`Missing parts: ${(check.missing_files||[]).join(', ')}`;
        if(check.zero_byte_parts&&check.zero_byte_parts.length)toast(`Empty files: ${check.zero_byte_parts.join(', ')}`,'err');
        toast(errMsg,'err');return;
      }
    }catch(e){}
    if(partMatch){
      const dir=path.substring(0,path.length-name.length);
      extractPath=dir+partMatch[1]+'.part1.rar';
      if(parseInt(partMatch[2])!==1)toast('Using part1 for extraction...','info');
    }
  }

  const archName=extractPath.split('/').pop();
  const msg=isMultipart
    ? `Extract "${archName}" and all related parts?\n\nChoose "OK" to delete archive files after extraction.\nChoose "Cancel" to keep them.`
    : `Extract "${archName}"?\n\nChoose "OK" to delete the archive after extraction.\nChoose "Cancel" to keep it.`;
  const del=await dlgConfirm('📦 Extract Archive', msg);
  await _doExtract(extractPath, del, archName);
}
async function _doExtract(extractPath, del, archName, password){
  try{
    const body={delete_after:del};
    if(password)body.password=password;
    const r=await api('POST','/api/extract/'+encodeURIComponent(extractPath),body);
    const dest=r.destination||'';
    toast(`Extracting → ${dest||archName}`,'ok');
    startExtractPoll();
  }catch(e){
    const errMsg=e.message||'';
    // If password error, prompt for password
    if(errMsg.toLowerCase().includes('password')){
      const pw=await dlgPrompt('🔐 Password Required','Enter archive password:','');
      if(pw)return _doExtract(extractPath, del, archName, pw);
    }
    toast(errMsg,'err');
  }
}
function startExtractPoll(){
  if(_extPollId)return;
  renderExtractBanner();
  _extPollId=setInterval(async()=>{
    try{
      const d=await api('GET','/api/extract-tasks');
      const tasks=d.tasks||[];
      const active=tasks.filter(t=>t.status==='extracting');
      renderExtractBanner(tasks);
      if(!active.length){
        clearInterval(_extPollId);_extPollId=null;
        setTimeout(()=>{
          const el=document.getElementById('extractBanner');
          if(el)el.innerHTML='';
          rFiles();
        },3000);
      }
    }catch(e){clearInterval(_extPollId);_extPollId=null}
  },1000);
}
async function cancelExtract(eid){
  try{await api('DELETE','/api/extract-tasks/'+eid);toast('Extraction cancelled','ok')}catch(e){toast(e.message,'err')}
}
function renderExtractBanner(tasks){
  let el=document.getElementById('extractBanner');
  if(!el){
    const c=document.getElementById('fmContent');
    el=document.createElement('div');el.id='extractBanner';
    c.parentNode.insertBefore(el,c);
  }
  if(!tasks||!tasks.length){el.innerHTML='';return}
  el.innerHTML=tasks.map(t=>{
    const pct=t.percent||0;
    const isActive=t.status==='extracting';
    const ic=t.status==='completed'?'✅':t.status==='failed'?'❌':t.status==='cancelled'?'⏹':'📦';
    const dest=t.destination?`→ 📁 ${esc(t.destination)}`:'';
    const cancelBtn=isActive?`<button class="btn-d" onclick="cancelExtract('${t.task_id}')" style="font-size:10px;padding:2px 8px;margin-left:8px" title="Cancel">✕ Cancel</button>`:'';
    // Status badge color
    const sc=t.status==='completed'?'color:var(--grn)':t.status==='failed'?'color:var(--red)':t.status==='cancelled'?'color:var(--txt3)':'color:var(--pri2)';
    // Progress info line
    let infoLine=t.progress||'';
    if(isActive&&t.elapsed)infoLine+=` · ⏱ ${t.elapsed}`;
    // Error detail
    const errLine=t.error?`<div style="font-size:11px;color:var(--red);margin-top:4px;word-break:break-all">⚠ ${esc(t.error)}</div>`:'';
    // Progress bar
    const bar=isActive||t.status==='cancelled'?`<div style="background:var(--bg2);border-radius:4px;height:6px;margin:6px 0 4px;overflow:hidden"><div style="height:100%;border-radius:4px;transition:width .3s;${isActive?'background:linear-gradient(90deg,var(--pri),#6a4ff0)':'background:var(--txt3)'};width:${pct}%"></div></div>`
      :t.status==='completed'?`<div style="background:var(--bg2);border-radius:4px;height:6px;margin:6px 0 4px;overflow:hidden"><div style="height:100%;border-radius:4px;background:var(--grn);width:100%"></div></div>`:'';
    return`<div style="background:var(--bg3);border:1px solid var(--bdr);border-radius:8px;padding:10px 14px;margin-bottom:6px">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span style="font-size:12px;font-weight:600">${ic} ${esc(t.filename)} ${dest}</span>
        <div style="display:flex;align-items:center">
          <span style="font-size:11px;font-weight:600;${sc}">${t.status}</span>
          ${cancelBtn}
        </div>
      </div>
      ${bar}
      <div style="font-size:11px;color:var(--txt3);margin-top:2px">${infoLine}</div>
      ${errLine}
    </div>`;
  }).join('');
}

// Detail Panel
async function showDetail(path){
  const p=document.getElementById('detailPanel');
  p.innerHTML='<h3>ℹ️ File Info <button class="cp" onclick="closeDetail()" style="font-size:14px">✕</button></h3><div class="empty"><span class="spin"></span></div>';
  p.classList.add('open');
  try{
    const info=await api('GET','/api/files/info/'+encodeURIComponent(path));
    let html='<h3>ℹ️ File Info <button class="cp" onclick="closeDetail()" style="font-size:14px">✕</button></h3>';
    html+=`<div class="detail-row"><span class="label">Path</span><span class="value">${esc(info.path)}</span></div>`;
    html+=`<div class="detail-row"><span class="label">MIME</span><span class="value">${esc(info.mime_type||'—')}</span></div>`;
    html+=`<div class="detail-row"><span class="label">Size</span><span class="value">${hs(info.size)}</span></div>`;
    html+=`<div class="detail-row"><span class="label">MD5</span><span class="value">${esc(info.hash_md5||'Not computed')}</span></div>`;
    html+=`<div class="detail-row"><span class="label">Created</span><span class="value">${info.created_at?new Date(info.created_at).toLocaleString():'—'}</span></div>`;
    html+=`<div class="detail-row"><span class="label">Modified</span><span class="value">${info.modified_at?new Date(info.modified_at).toLocaleString():'—'}</span></div>`;
    if(info.tags&&info.tags.length)html+=`<div class="detail-row"><span class="label">Tags</span><span class="value">${info.tags.join(', ')}</span></div>`;
    // Versions
    try{
      const v=await api('GET','/api/files/versions/'+encodeURIComponent(path));
      if(v.versions&&v.versions.length){
        html+='<h3 style="margin-top:16px;font-size:13px">📋 Versions ('+v.total+')</h3>';
        v.versions.forEach(ver=>{
          html+=`<div class="detail-row"><span class="label">v${ver.version} · ${hs(ver.size)}</span><span class="value"><button class="btn-g" onclick="restoreVer('${esc(path)}',${ver.version})">Restore</button></span></div>`;
        });
      }
    }catch{}
    p.innerHTML=html;
  }catch(e){p.innerHTML='<h3>Error <button class="cp" onclick="closeDetail()">✕</button></h3><p>'+esc(e.message)+'</p>'}
}
function closeDetail(){document.getElementById('detailPanel').classList.remove('open')}
async function restoreVer(path,v){
  if(!await dlgConfirm('📋 Restore','Restore to version '+v+'?'))return;
  try{await api('POST','/api/files/restore-version/'+encodeURIComponent(path)+'?version='+v);toast('Version restored','ok');rFiles();closeDetail()}
  catch(e){toast(e.message,'err')}
}

// Recycle Bin
async function rTrash(){
  try{
    const d=await api('GET','/api/trash');
    const el=document.getElementById('trashList');
    const items=d.items||[];
    if(!items.length){el.innerHTML='<div class="empty"><span>♻️</span>Recycle bin is empty</div>';return}
    el.innerHTML=items.map(t=>`<div class="trash-item"><div class="trash-info"><div class="trash-name">📄 ${esc(t.filename)}</div><div class="trash-meta">${hs(t.size)} · Deleted ${new Date(t.deleted_at).toLocaleString()} · Expires ${new Date(t.expires_at).toLocaleDateString()}</div></div><div class="trash-actions"><button class="btn-g" onclick="trashRestore(${t.id})">♻️ Restore</button><button class="btn-d" onclick="trashPurge(${t.id})">🗑 Delete</button></div></div>`).join('');
  }catch(e){console.error(e)}
}
async function trashRestore(id){
  try{await api('POST','/api/trash/restore/'+id);toast('Restored','ok');rTrash();rFiles();health()}
  catch(e){toast(e.message,'err')}
}
async function trashPurge(id){
  if(!await dlgConfirm('⚠️ Permanent Delete','This cannot be undone. Delete permanently?'))return;
  try{await api('DELETE','/api/trash/'+id);toast('Permanently deleted','ok');rTrash();health()}
  catch(e){toast(e.message,'err')}
}
async function trashPurgeAll(){
  if(!await dlgConfirm('⚠️ Empty Trash','Permanently delete ALL items? This cannot be undone.'))return;
  try{await api('DELETE','/api/trash/purge');toast('Trash emptied','ok');rTrash();health()}
  catch(e){toast(e.message,'err')}
}

// Dedup
async function rDedup(){
  const el=document.getElementById('dedupContent');
  el.innerHTML='<div class="empty"><span class="spin"></span> Scanning files...</div>';
  try{
    const d=await api('GET','/api/dedup/scan');
    if(!d.duplicate_groups||!d.duplicate_groups.length){
      el.innerHTML='<div class="empty"><span>✅</span>No duplicate files found</div>';return;
    }
    let html=`<div style="padding:12px;font-size:13px;color:var(--ylw);background:var(--ylw3);border-radius:var(--rs);margin-bottom:12px">⚠️ Found ${d.total_groups} duplicate groups · Wasted: ${d.total_wasted_human}<button class="btn-d" style="margin-left:auto;float:right" onclick="dedupClean()">🧹 Clean All</button></div>`;
    html+=d.duplicate_groups.map(g=>`<div class="dedup-group"><div class="dedup-group-header"><span>🔗 ${g.count} files · ${g.size_human} each</span><span style="color:var(--red)">Wasted: ${g.wasted_human}</span></div>${g.files.map(f=>`<div class="dedup-file">📄 ${esc(f)}</div>`).join('')}</div>`).join('');
    el.innerHTML=html;
  }catch(e){el.innerHTML='<div class="empty">Error: '+esc(e.message)+'</div>'}
}
async function dedupClean(){
  if(!await dlgConfirm('🧹 Clean Duplicates','Remove duplicate files? One copy of each will be kept.'))return;
  try{const r=await api('POST','/api/dedup/clean?strategy=first');toast(`Removed ${r.deleted_count} files · Freed ${r.freed_human}`,'ok');rDedup();rFiles();health()}
  catch(e){toast(e.message,'err')}
}

// Media
async function rMedia(){
  try{const d=await api('GET','/api/media');renderM(d.media||[])}catch{}
}
function renderM(items){
  const el=document.getElementById('mediaList');
  if(!items.length){el.innerHTML='<div class="empty"><span>🎬</span>No media files yet</div>';return}
  el.innerHTML=items.map(m=>{
    const isV=m.type==='video';const ic=isV?'🎬':'🎵';
    const badge=isV?'<span class="mc-badge mc-vid">VIDEO</span>':'<span class="mc-badge mc-aud">AUDIO</span>';
    const subH=m.subtitles?.length?`<span>🔤 ${m.subtitles.length} sub</span>`:'';
    const hlsS=m.hls?m.hls.status:'none';
    let hlsBtn='';
    if(isV){
      if(hlsS==='ready')hlsBtn=`<button class="btn-g" style="font-size:10px;margin-top:4px" onclick="event.stopPropagation();playHls('${esc(m.filename)}','${m.hls.master_url}',${JSON.stringify(m.subtitles||[]).replace(/"/g,'&quot;')})">▶ HLS</button>`;
      else if(hlsS==='transcoding')hlsBtn=`<div style="font-size:10px;color:var(--ylw);margin-top:4px"><span class="spin"></span> Transcoding ${m.hls.progress?.percent||0}%</div>`;
      else hlsBtn=`<button class="btn-s" style="font-size:10px;margin-top:4px" onclick="event.stopPropagation();startHls('${esc(m.path)}')">📡 Create HLS</button>`;
    }
    let shareBtn=`<button class="btn-s" style="font-size:10px;margin-top:4px;margin-left:4px" onclick="event.stopPropagation();shareFile('${esc(m.path)}')">🔗 Share</button>`;
    return`<div class="mc" id="mc-${esc(m.filename)}" onclick="playMediaFile('${esc(m.path)}')"><div class="mc-top"><div class="mc-icon">${ic}</div>${badge}</div><div class="mc-name">${esc(m.filename)}</div><div class="mc-meta"><span>${m.size_human}</span>${subH}</div>${hlsBtn}${shareBtn}</div>`;
  }).join('');
}
let hlsPlayer=null;

// === Custom Video Player Controller ===
const VP={
  v:null,wrap:null,playBtn:null,muteBtn:null,volSlider:null,speedBtn:null,
  fsBtn:null,timeCur:null,timeDur:null,played:null,buffered:null,scrubber:null,
  progressWrap:null,bigPlay:null,buffer:null,speedMenu:null,subWrap:null,subMenu:null,subBtn:null,
  qualityWrap:null,qualityMenu:null,qualityBtn:null,
  controls:null,nowP:null,playerE:null,
  _inited:false,_hideTimer:null,_currentName:'',_isSeeking:false,_savedVol:1,

  init(){
    if(this._inited)return;
    this.v=document.getElementById('vp');
    this.wrap=document.getElementById('vpWrapper');
    this.playBtn=document.getElementById('vpPlayBtn');
    this.muteBtn=document.getElementById('vpMuteBtn');
    this.volSlider=document.getElementById('vpVolSlider');
    this.speedBtn=document.getElementById('vpSpeedBtn');
    this.fsBtn=document.getElementById('vpFsBtn');
    this.timeCur=document.getElementById('vpTimeCur');
    this.timeDur=document.getElementById('vpTimeDur');
    this.played=document.getElementById('vpPlayed');
    this.buffered=document.getElementById('vpBuffered');
    this.scrubber=document.getElementById('vpScrubber');
    this.progressWrap=document.getElementById('vpProgressWrap');
    this.bigPlay=document.getElementById('vpBigPlay');
    this.buffer=document.getElementById('vpBuffer');
    this.speedMenu=document.getElementById('vpSpeedMenu');
    this.subWrap=document.getElementById('vpSubWrap');
    this.subMenu=document.getElementById('vpSubMenu');
    this.subBtn=document.getElementById('vpSubBtn');
    this.qualityWrap=document.getElementById('vpQualityWrap');
    this.qualityMenu=document.getElementById('vpQualityMenu');
    this.qualityBtn=document.getElementById('vpQualityBtn');
    this.controls=document.getElementById('vpControls');
    this.nowP=document.getElementById('nowP');
    this.playerE=document.getElementById('playerE');
    if(!this.v)return;

    const self=this;

    // Play/Pause
    this.playBtn.addEventListener('click',e=>{e.stopPropagation();self.togglePlay()});
    this.bigPlay.addEventListener('click',e=>{e.stopPropagation();self.togglePlay()});
    this.wrap.addEventListener('click',e=>{
      if(e.target.closest('.vp-controls,.vp-speed-menu,.vp-sub-menu'))return;
      self.togglePlay();
    });
    this.wrap.addEventListener('dblclick',e=>{
      if(e.target.closest('.vp-controls'))return;
      self.toggleFs();
    });

    // Video events
    this.v.addEventListener('play',()=>self._onPlayState());
    this.v.addEventListener('pause',()=>self._onPlayState());
    this.v.addEventListener('ended',()=>self._onEnded());
    this.v.addEventListener('timeupdate',()=>self._onTimeUpdate());
    this.v.addEventListener('loadedmetadata',()=>self._onMeta());
    this.v.addEventListener('progress',()=>self._onBuffer());
    this.v.addEventListener('waiting',()=>{self.buffer.classList.add('show')});
    this.v.addEventListener('playing',()=>{self.buffer.classList.remove('show')});
    this.v.addEventListener('canplay',()=>{self.buffer.classList.remove('show')});

    // Progress bar (seek)
    this.progressWrap.addEventListener('mousedown',e=>{self._startSeek(e)});
    this.progressWrap.addEventListener('mousemove',e=>{self._hoverSeek(e)});

    // Volume
    this.muteBtn.addEventListener('click',e=>{e.stopPropagation();self.toggleMute()});
    this.volSlider.addEventListener('input',()=>{
      self.v.volume=parseFloat(self.volSlider.value);
      self.v.muted=false;
      self._updateVolIcon();
      localStorage.setItem('vp_vol',self.volSlider.value);
    });

    // Restore saved volume
    const sv=localStorage.getItem('vp_vol');
    if(sv!==null){this.v.volume=parseFloat(sv);this.volSlider.value=sv}

    // Speed
    this.speedMenu.querySelectorAll('.vp-speed-opt').forEach(opt=>{
      opt.addEventListener('click',e=>{
        e.stopPropagation();
        const spd=parseFloat(opt.dataset.speed);
        self.v.playbackRate=spd;
        self.speedBtn.textContent=spd===1?'1x':spd+'x';
        self.speedMenu.querySelectorAll('.vp-speed-opt').forEach(o=>o.classList.remove('active'));
        opt.classList.add('active');
      });
    });

    // Fullscreen
    this.fsBtn.addEventListener('click',e=>{e.stopPropagation();self.toggleFs()});
    document.addEventListener('fullscreenchange',()=>self._onFsChange());

    // Controls auto-hide
    this.wrap.addEventListener('mousemove',()=>self._showControls());
    this.wrap.addEventListener('mouseleave',()=>self._scheduleHide());

    // Keyboard (only when media page active)
    document.addEventListener('keydown',e=>{
      if(e.target.matches('input,textarea,select'))return;
      if(document.querySelector('.modal-bg[style*="flex"]'))return;
      const mp=document.getElementById('p-media');
      if(!mp||!mp.classList.contains('active'))return;
      if(!self.v.src)return;
      switch(e.key){
        case ' ':case 'k':case 'K':e.preventDefault();self.togglePlay();break;
        case 'm':case 'M':e.preventDefault();self.toggleMute();break;
        case 'f':case 'F':e.preventDefault();self.toggleFs();break;
        case 'ArrowLeft':e.preventDefault();self.v.currentTime=Math.max(0,self.v.currentTime-10);break;
        case 'ArrowRight':e.preventDefault();self.v.currentTime=Math.min(self.v.duration||0,self.v.currentTime+10);break;
        case 'ArrowUp':e.preventDefault();self.v.volume=Math.min(1,self.v.volume+0.1);self.volSlider.value=self.v.volume;self._updateVolIcon();break;
        case 'ArrowDown':e.preventDefault();self.v.volume=Math.max(0,self.v.volume-0.1);self.volSlider.value=self.v.volume;self._updateVolIcon();break;
      }
    });

    this._inited=true;
  },

  load(name,url,subs,isHls,hlsMasterUrl){
    this.init();
    // Save position of previous video
    this._savePosition();

    if(hlsPlayer){hlsPlayer.destroy();hlsPlayer=null}
    this._currentName=name;
    this.v.querySelectorAll('track').forEach(t=>t.remove());

    // Show video, hide placeholder
    document.getElementById('playerW').style.display='block';
    this.playerE.style.display='none';
    this.v.style.display='block';
    this.bigPlay.classList.add('show');
    this.buffer.classList.remove('show');
    this.wrap.classList.add('paused');

    // Reset UI
    this.played.style.width='0%';
    this.buffered.style.width='0%';
    this.scrubber.style.left='0%';
    this.timeCur.textContent='0:00';
    this.timeDur.textContent='0:00';
    this.playBtn.textContent='▶';
    // Hide quality selector by default (only shown for HLS)
    this.qualityWrap.style.display='none';

    // Now playing bar
    const hlsBadge=isHls?' <span style="color:var(--grn);font-size:11px;margin-left:4px">📡 HLS Adaptive</span>':'';
    this.nowP.style.display='flex';
    this.nowP.innerHTML='🎬 <strong>'+esc(name)+'</strong>'+hlsBadge+' <button class="btn-s" style="margin-left:auto" onclick="window.open(\''+url+'\')">↗ Open</button>';

    // Load source
    if(isHls){
      this._loadHls(hlsMasterUrl);
    }else{
      this.v.src=url;
      this.v.load();
    }

    // Subtitles
    this._setupSubs(subs);

    // Restore position
    const savedTime=localStorage.getItem('vp_pos_'+name);
    if(savedTime){
      const t=parseFloat(savedTime);
      if(t>2){
        this.v.addEventListener('loadedmetadata',function once(){
          this.currentTime=t;
          this.removeEventListener('loadedmetadata',once);
        });
        toast(`Resuming from ${VP._fmtTime(t)}`,'info');
      }
    }

    // Highlight in list
    document.querySelectorAll('.mc').forEach(c=>c.classList.remove('playing'));
    const mc=document.getElementById('mc-'+name);if(mc)mc.classList.add('playing');
    document.getElementById('playerW').scrollIntoView({behavior:'smooth'});
  },

  _loadHls(masterUrl){
    const self=this;
    if(typeof Hls!=='undefined'&&Hls.isSupported()){
      hlsPlayer=new Hls({
        maxBufferLength:30,
        maxMaxBufferLength:60,
        startLevel:-1, // auto
        capLevelToPlayerSize:true
      });
      hlsPlayer.loadSource(masterUrl);
      hlsPlayer.attachMedia(self.v);
      hlsPlayer.on(Hls.Events.MANIFEST_PARSED,(e,data)=>{
        self._setupQuality(data.levels);
      });
      // Error recovery
      hlsPlayer.on(Hls.Events.ERROR,(e,data)=>{
        if(data.fatal){
          switch(data.type){
            case Hls.ErrorTypes.MEDIA_ERROR:
              hlsPlayer.recoverMediaError();
              break;
            case Hls.ErrorTypes.NETWORK_ERROR:
              toast('Network error — retrying...','err');
              setTimeout(()=>hlsPlayer.startLoad(),2000);
              break;
            default:
              toast('Fatal playback error','err');
              break;
          }
        }
      });
      // Track auto level switching for quality badge
      hlsPlayer.on(Hls.Events.LEVEL_SWITCHED,(e,data)=>{
        self._onLevelSwitch(data.level);
      });
    }else if(self.v.canPlayType('application/vnd.apple.mpegurl')){
      // Safari native HLS
      self.v.src=masterUrl;
      self.v.load();
    }
  },

  _setupQuality(levels){
    if(!levels||!levels.length){
      this.qualityWrap.style.display='none';
      return;
    }
    this.qualityWrap.style.display='block';
    const resLabels={2160:'4K',1440:'1440p',1080:'1080p',720:'720p',480:'480p',360:'360p',240:'240p'};
    let html='<div class="vp-quality-opt active" data-level="-1">Auto <span class="vp-quality-badge">ABR</span></div>';
    levels.forEach((lv,i)=>{
      const h=lv.height||0;
      const label=resLabels[h]||(h?h+'p':'Level '+(i+1));
      const br=lv.bitrate?Math.round(lv.bitrate/1000)+'k':'';
      html+=`<div class="vp-quality-opt" data-level="${i}">${label}${br?' <span style="color:var(--txt3);font-size:10px">'+br+'</span>':''}</div>`;
    });
    this.qualityMenu.innerHTML=html;
    this.qualityBtn.textContent='Auto';

    // Bind clicks
    const self=this;
    this.qualityMenu.querySelectorAll('.vp-quality-opt').forEach(opt=>{
      opt.addEventListener('click',e=>{
        e.stopPropagation();
        const lvl=parseInt(opt.dataset.level);
        if(hlsPlayer)hlsPlayer.currentLevel=lvl;
        self.qualityMenu.querySelectorAll('.vp-quality-opt').forEach(o=>o.classList.remove('active'));
        opt.classList.add('active');
        if(lvl===-1){
          self.qualityBtn.textContent='Auto';
        }else{
          const h=levels[lvl]?.height;
          self.qualityBtn.textContent=resLabels[h]||h+'p';
        }
      });
    });
  },

  _onLevelSwitch(level){
    // Update quality badge when auto-switching
    if(!hlsPlayer||hlsPlayer.currentLevel!==-1)return; // Only update in auto mode
    const levels=hlsPlayer.levels;
    if(levels&&levels[level]){
      const h=levels[level].height;
      const resLabels={2160:'4K',1440:'1440p',1080:'1080p',720:'720p',480:'480p',360:'360p'};
      this.qualityBtn.textContent='Auto';
      // Flash the current quality briefly
      this.qualityBtn.title='Quality: '+(resLabels[h]||h+'p');
    }
  },

  _setupSubs(subs){
    if(!subs||!subs.length){
      this.subWrap.style.display='none';
      return;
    }
    this.subWrap.style.display='block';
    // Build menu
    let html='<div class="vp-sub-opt active" data-idx="-1">Off</div>';
    subs.forEach((s,i)=>{
      // Add track to video
      const t=document.createElement('track');
      t.kind='subtitles';t.label=s.filename;t.src=s.url;
      t.srclang=s.language||'und';
      this.v.appendChild(t);
      html+=`<div class="vp-sub-opt" data-idx="${i}">${esc(s.filename)}</div>`;
    });
    this.subMenu.innerHTML=html;

    // Bind clicks
    const self=this;
    this.subMenu.querySelectorAll('.vp-sub-opt').forEach(opt=>{
      opt.addEventListener('click',e=>{
        e.stopPropagation();
        const idx=parseInt(opt.dataset.idx);
        self.subMenu.querySelectorAll('.vp-sub-opt').forEach(o=>o.classList.remove('active'));
        opt.classList.add('active');
        // Toggle tracks
        for(let i=0;i<self.v.textTracks.length;i++){
          self.v.textTracks[i].mode=(i===idx)?'showing':'hidden';
        }
        self.subBtn.style.color=(idx>=0)?'var(--pri2)':'';
      });
    });
  },

  togglePlay(){
    if(!this.v.src)return;
    if(this.v.paused||this.v.ended){this.v.play().catch(()=>{})}
    else{this.v.pause()}
  },

  toggleMute(){
    this.v.muted=!this.v.muted;
    this._updateVolIcon();
  },

  toggleFs(){
    const w=this.wrap;
    if(document.fullscreenElement===w){document.exitFullscreen().catch(()=>{})}
    else{w.requestFullscreen().catch(()=>{})}
  },

  _onPlayState(){
    const paused=this.v.paused;
    this.playBtn.textContent=paused?'▶':'⏸';
    this.bigPlay.classList.toggle('show',paused);
    this.wrap.classList.toggle('paused',paused);
    if(!paused)this._scheduleHide();
  },

  _onEnded(){
    this.playBtn.textContent='▶';
    this.bigPlay.classList.add('show');
    this.wrap.classList.add('paused');
    // Clear saved position on completion
    localStorage.removeItem('vp_pos_'+this._currentName);
  },

  _onTimeUpdate(){
    if(this._isSeeking)return;
    const v=this.v;
    if(!v.duration||isNaN(v.duration))return;
    const pct=(v.currentTime/v.duration)*100;
    this.played.style.width=pct+'%';
    this.scrubber.style.left=pct+'%';
    this.timeCur.textContent=this._fmtTime(v.currentTime);
    // Auto-save position every 5 seconds
    if(Math.floor(v.currentTime)%5===0&&this._currentName){
      localStorage.setItem('vp_pos_'+this._currentName,v.currentTime.toFixed(1));
    }
  },

  _onMeta(){
    this.timeDur.textContent=this._fmtTime(this.v.duration);
  },

  _onBuffer(){
    const v=this.v;
    if(v.buffered.length>0){
      const end=v.buffered.end(v.buffered.length-1);
      const pct=(end/(v.duration||1))*100;
      this.buffered.style.width=pct+'%';
    }
  },

  _startSeek(e){
    e.preventDefault();e.stopPropagation();
    this._isSeeking=true;
    const self=this;
    const seek=ev=>{
      const rect=self.progressWrap.getBoundingClientRect();
      const pct=Math.max(0,Math.min(1,(ev.clientX-rect.left)/rect.width));
      self.played.style.width=(pct*100)+'%';
      self.scrubber.style.left=(pct*100)+'%';
      if(self.v.duration)self.v.currentTime=pct*self.v.duration;
    };
    seek(e);
    const onMove=ev=>seek(ev);
    const onUp=()=>{
      self._isSeeking=false;
      document.removeEventListener('mousemove',onMove);
      document.removeEventListener('mouseup',onUp);
    };
    document.addEventListener('mousemove',onMove);
    document.addEventListener('mouseup',onUp);
  },

  _hoverSeek(e){
    const rect=this.progressWrap.getBoundingClientRect();
    const pct=Math.max(0,Math.min(1,(e.clientX-rect.left)/rect.width));
    const ht=document.getElementById('vpHoverTime');
    if(this.v.duration){
      ht.textContent=this._fmtTime(pct*this.v.duration);
      ht.style.left=(e.clientX-rect.left)+'px';
    }
  },

  _updateVolIcon(){
    if(this.v.muted||this.v.volume===0)this.muteBtn.textContent='🔇';
    else if(this.v.volume<0.5)this.muteBtn.textContent='🔉';
    else this.muteBtn.textContent='🔊';
    this.volSlider.value=this.v.muted?0:this.v.volume;
  },

  _showControls(){
    this.wrap.classList.add('show-controls');
    clearTimeout(this._hideTimer);
    if(!this.v.paused)this._scheduleHide();
  },

  _scheduleHide(){
    clearTimeout(this._hideTimer);
    this._hideTimer=setTimeout(()=>{
      if(!this.v.paused)this.wrap.classList.remove('show-controls');
    },2500);
  },

  _onFsChange(){
    this.fsBtn.textContent=document.fullscreenElement?'⛶':'⛶';
  },

  _savePosition(){
    if(this._currentName&&this.v&&this.v.currentTime>2){
      localStorage.setItem('vp_pos_'+this._currentName,this.v.currentTime.toFixed(1));
    }
  },

  _fmtTime(s){
    if(!s||isNaN(s))return'0:00';
    s=Math.floor(s);
    const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=s%60;
    if(h>0)return h+':'+String(m).padStart(2,'0')+':'+String(sec).padStart(2,'0');
    return m+':'+String(sec).padStart(2,'0');
  }
};

// Save position when leaving page
window.addEventListener('beforeunload',()=>VP._savePosition());

// === Play media file from File Manager ===
async function playMediaFile(path){
  // Navigate to Media tab using the app's go() function
  const mediaBtn=document.querySelector('.sb-item[onclick*="media"]');
  if(mediaBtn)go('media',mediaBtn);
  else{
    // Fallback: manually switch pages
    document.querySelectorAll('.sb-item').forEach(s=>s.classList.remove('active'));
    document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
    const mp=document.getElementById('p-media');
    if(mp)mp.classList.add('active');
    document.getElementById('pageTitle').textContent='Media';
    rMedia();
  }
  // Build stream URL - probe codec for browser compatibility
  const filename=path.split('/').pop();
  const directUrl=base()+'/stream/'+encodeURIComponent(path);
  // Check if video needs transcoding
  const ext='.'+filename.split('.').pop().toLowerCase();
  const browserNativeExts=['.mp4','.webm','.m4v'];
  let streamUrl=directUrl;
  if(VID_EXTS.includes(ext)&&!browserNativeExts.includes(ext)){
    // MKV/AVI/FLV etc — likely needs transcoding, probe to confirm
    try{
      const probe=await api('GET','/api/media/probe/'+encodeURIComponent(path));
      if(probe.needs_transcode)streamUrl=base()+'/stream-transcode/'+encodeURIComponent(path);
    }catch(e){
      // If probe fails, try transcode for non-native containers
      streamUrl=base()+'/stream-transcode/'+encodeURIComponent(path);
    }
  } else if(VID_EXTS.includes(ext)){
    // MP4/WebM — probe to check codec (could be HEVC in MP4)
    try{
      const probe=await api('GET','/api/media/probe/'+encodeURIComponent(path));
      if(probe.needs_transcode)streamUrl=base()+'/stream-transcode/'+encodeURIComponent(path);
    }catch(e){}
  }
  // Try to find subtitles from media API data
  api('GET','/api/media').then(d=>{
    const m=(d.media||[]).find(x=>x.path===path||x.filename===filename);
    playM(filename,streamUrl,m&&m.subtitles?m.subtitles:[]);
  }).catch(()=>playM(filename,streamUrl,[]));
}

function playM(name,url,subs){
  VP.load(name,url,subs,false,null);
}
function loadHlsLib(){return new Promise((res,rej)=>{if(window.Hls)return res();const s=document.createElement('script');s.src='https://cdn.jsdelivr.net/npm/hls.js@latest';s.onload=res;s.onerror=rej;document.head.appendChild(s)})}
async function playHls(name,masterUrl,subs){
  try{await loadHlsLib()}catch{toast('Failed to load HLS.js','err');return}
  VP.load(name,masterUrl,subs,true,masterUrl);
}
async function startHls(path){
  try{const r=await api('POST','/api/media/hls/'+encodeURIComponent(path));toast('HLS transcoding '+r.status,'ok');setTimeout(()=>rMedia(),2000)}
  catch(e){toast(e.message,'err')}
}

// Share
let shareTarget='';
function shareFile(filepath){
  shareTarget=filepath;
  document.getElementById('shareFileName').textContent='📄 '+filepath.split('/').pop();
  document.getElementById('shareHours').value='24';
  document.getElementById('sharePwd').value='';
  document.getElementById('shareResult').style.display='none';
  document.getElementById('shareGoBtn').style.display='inline-flex';
  document.getElementById('shareM').style.display='flex';
}
function closeShare(){document.getElementById('shareM').style.display='none'}
async function doShare(){
  const hours=parseInt(document.getElementById('shareHours').value)||0;
  const pw=document.getElementById('sharePwd').value.trim()||null;
  const btn=document.getElementById('shareGoBtn');btn.disabled=true;btn.innerHTML='<span class="spin"></span>';
  try{
    const r=await api('POST','/api/share',{filepath:shareTarget,expire_hours:hours,password:pw});
    document.getElementById('shareUrl').value=r.url;
    document.getElementById('shareResult').style.display='block';
    document.getElementById('shareGoBtn').style.display='none';
    toast('Share link created!','ok');rShares();
  }catch(e){toast(e.message,'err')}
  finally{btn.disabled=false;btn.innerHTML='🔗 Create'}
}
async function rShares(){
  try{
    const d=await api('GET','/api/shares');
    const el=document.getElementById('shareList');
    const items=d.shares||[];
    if(!items.length){el.innerHTML='<div class="empty"><span>🔗</span>No share links</div>';return}
    el.innerHTML=items.map(s=>`<div class="trash-item"><div class="trash-info"><div class="trash-name">🔗 ${esc(s.file_path)}</div><div class="trash-meta">${s.password_protected?'🔒 ':''}Downloads: ${s.download_count}${s.max_downloads?' / '+s.max_downloads:''} · Created ${new Date(s.created_at).toLocaleString()}${s.expires_at?' · Expires '+new Date(s.expires_at).toLocaleString():' · Never expires'}</div></div><div class="trash-actions"><button class="btn-s" onclick="cpL('${s.url}')">📋 Copy</button><button class="btn-d" onclick="delShare('${s.token}')">🗑</button></div></div>`).join('');
  }catch(e){console.error(e)}
}
async function delShare(token){
  if(!await dlgConfirm('🗑 Delete Share','Remove this share link?'))return;
  try{await api('DELETE','/api/share/'+token);toast('Share deleted','ok');rShares()}
  catch(e){toast(e.message,'err')}
}

// Utils
function cpL(u){
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(u).then(()=>toast('Copied!','info')).catch(()=>cpFallback(u));
  }else{cpFallback(u)}
}
function cpFallback(u){
  const t=document.createElement('textarea');t.value=u;t.style.cssText='position:fixed;opacity:0';
  document.body.appendChild(t);t.select();
  try{document.execCommand('copy');toast('Copied!','info')}catch{toast('Copy failed','err')}
  document.body.removeChild(t);
}
function hs(b){if(!b)return'0 B';const u=['B','KB','MB','GB','TB'];let i=0;while(b>=1024&&i<u.length-1){b/=1024;i++}return b.toFixed(1)+' '+u[i]}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
function toast(m,t='info'){
  const w=document.getElementById('toasts');const c=t==='ok'?'t-ok':t==='err'?'t-err':'t-info';
  const i=t==='ok'?'✓':t==='err'?'✕':'ℹ';const e=document.createElement('div');e.className='toast '+c;
  e.innerHTML=`<span>${i}</span> ${esc(m)}`;w.appendChild(e);
  setTimeout(()=>{e.style.animation='sIn .2s ease reverse';setTimeout(()=>e.remove(),200)},3000);
}

// === Custom Dialog System (replaces alert/confirm/prompt) ===
let _dlgResolve=null,_dlgMode='confirm';
function dlgConfirm(title,msg){
  return new Promise(res=>{
    _dlgResolve=res;_dlgMode='confirm';
    document.getElementById('dlgTitle').textContent=title;
    document.getElementById('dlgMsg').innerHTML=esc(msg).replace(/\n/g,'<br>');
    document.getElementById('dlgInput').style.display='none';
    document.getElementById('dlgCancel').style.display='inline-flex';
    document.getElementById('dlgOk').textContent='OK';
    document.getElementById('dialogM').style.display='flex';
  });
}
function dlgPrompt(title,msg,def){
  return new Promise(res=>{
    _dlgResolve=res;_dlgMode='prompt';
    document.getElementById('dlgTitle').textContent=title;
    document.getElementById('dlgMsg').textContent=msg;
    document.getElementById('dlgInput').style.display='block';
    const inp=document.getElementById('dlgIn');inp.value=def||'';inp.focus();
    document.getElementById('dlgCancel').style.display='inline-flex';
    document.getElementById('dlgOk').textContent='OK';
    document.getElementById('dialogM').style.display='flex';
    inp.addEventListener('keypress',function h(e){if(e.key==='Enter'){inp.removeEventListener('keypress',h);dlgOk()}});
  });
}
function dlgAlert(title,msg){
  return new Promise(res=>{
    _dlgResolve=res;_dlgMode='alert';
    document.getElementById('dlgTitle').textContent=title;
    document.getElementById('dlgMsg').textContent=msg;
    document.getElementById('dlgInput').style.display='none';
    document.getElementById('dlgCancel').style.display='none';
    document.getElementById('dlgOk').textContent='OK';
    document.getElementById('dialogM').style.display='flex';
  });
}
function dlgOk(){
  document.getElementById('dialogM').style.display='none';
  if(!_dlgResolve)return;
  if(_dlgMode==='prompt')_dlgResolve(document.getElementById('dlgIn').value.trim()||null);
  else _dlgResolve(true);
  _dlgResolve=null;
}
function dlgResolve(v){
  document.getElementById('dialogM').style.display='none';
  if(_dlgResolve)_dlgResolve(v);
  _dlgResolve=null;
}

// === Context Menu ===
const ARCHIVE_EXTS=['.rar','.zip','.7z','.tar','.gz','.tgz','.tar.gz','.bz2'];
const CODE_EXTS=['.html','.css','.js','.jsx','.ts','.tsx','.php','.py','.sh','.bash','.bat','.ps1','.c','.cpp','.h','.java','.go','.rs','.rb','.sql','.json','.xml','.yaml','.yml','.md','.txt','.log','.ini','.cfg','.conf','.env','.toml','.csv','.nfo','.srt','.vtt'];
function isArchiveFile(path){
  const n=path.toLowerCase();
  if(/\.part\d+\.rar$/i.test(n))return true;
  return ARCHIVE_EXTS.some(e=>n.endsWith(e));
}
function isCodeFile(path){
  const n=path.toLowerCase();
  return CODE_EXTS.some(e=>n.endsWith(e));
}
function fmCtx(ev,path){
  ev.preventDefault();ev.stopPropagation();
  ctxTarget=path;
  const m=document.getElementById('ctxMenu');
  const extItem=document.getElementById('ctxExtract');
  const editItem=document.getElementById('ctxEdit');
  if(extItem)extItem.style.display=isArchiveFile(path)?'block':'none';
  if(editItem)editItem.style.display=isCodeFile(path)?'block':'none';
  m.style.display='block';
  m.style.left=Math.min(ev.clientX,window.innerWidth-200)+'px';
  m.style.top=Math.min(ev.clientY,window.innerHeight-280)+'px';
}
document.addEventListener('click',()=>{document.getElementById('ctxMenu').style.display='none'});
function ctxAction(act){
  document.getElementById('ctxMenu').style.display='none';
  if(!ctxTarget)return;
  const f=fmAllItems.find(i=>i.path===ctxTarget);
  const ext='.'+ctxTarget.split('.').pop().toLowerCase();
  const isMedia=VID_EXTS.includes(ext)||AUD_EXTS.includes(ext);
  switch(act){
    case 'open':
      if(f&&f.type==='folder')fmGo(f.path);
      else if(isMedia)playMediaFile(ctxTarget);
      else openPreview(ctxTarget);
      break;
    case 'edit':openPreview(ctxTarget);break;
    case 'copy':copyFile(ctxTarget);break;
    case 'rename':if(f)showRename(ctxTarget,f.name);break;
    case 'share':shareFile(ctxTarget);break;
    case 'download':if(f&&f.download_url)window.open(f.download_url);break;
    case 'extract':extractF(ctxTarget);break;
    case 'info':showDetail(ctxTarget);break;
    case 'delete':if(fmSelected.size>1){fmBulkDel()}else{delF(ctxTarget)}break;
  }
}

// === Copy File ===
async function copyFile(path){
  try{const d=await api('POST','/api/files/copy/'+encodeURIComponent(path));toast('Copied → '+d.new_name,'ok');rFiles()}
  catch(e){toast(e.message,'err')}
}

// === Preview / Editor ===
async function openPreview(path){
  previewPath=path;previewDirty=false;
  if(_cmEditor){_cmEditor.destroy();_cmEditor=null}
  const ext='.'+path.split('.').pop().toLowerCase();
  const title=path.split('/').pop();
  document.getElementById('prevTitle').textContent='📄 '+title;
  document.getElementById('prevSaveBtn').style.display='none';
  const body=document.getElementById('prevBody');
  body.innerHTML='<div class="empty"><span class="spin"></span> Loading...</div>';
  document.getElementById('previewM').style.display='flex';

  // Image
  if(IMG_EXTS.includes(ext)){
    body.innerHTML=`<img class="prev-img" src="${base()}/files/${path}" alt="${esc(title)}">`;
    return;
  }
  // Video — redirect to Media tab with custom player
  if(VID_EXTS.includes(ext)){
    document.getElementById('previewM').style.display='none';
    playMediaFile(path);
    return;
  }
  // Audio — redirect to Media tab with custom player
  if(AUD_EXTS.includes(ext)){
    document.getElementById('previewM').style.display='none';
    playMediaFile(path);
    return;
  }
  // Text / Code
  try{
    const d=await api('GET','/api/files/content/'+encodeURIComponent(path));
    const encBadge=d.encoding_warning?`<span style="color:var(--orn);font-weight:600" title="${esc(d.encoding_warning)}">⚠ ${d.encoding}</span>`:`<span>🔤 ${d.encoding}</span>`;
    const meta=`<div class="prev-meta"><span>📏 ${d.lines} lines</span><span>📦 ${hs(d.size)}</span>${encBadge}<span>🏷 ${d.language}</span></div>`;
    if(d.encoding_warning)toast(d.encoding_warning,'warn');
    document.getElementById('prevSaveBtn').style.display='inline-flex';
    // Try CodeMirror (with full fallback if ANY extension fails)
    let cmOk=false;
    if(window._cmReady){
      try{
        body.innerHTML=meta+'<div id="cmContainer" style="border:1px solid var(--bdr);border-radius:6px;overflow:hidden;flex:1;min-height:300px"></div>';
        const CM=window._CM;
        const langExt=_cmLang(ext,CM);
        const exts=[CM.basicSetup,CM.oneDark,CM.keymap.of([{key:'Mod-s',run:()=>{savePreview();return true}}])];
        if(langExt)exts.push(langExt);
        const updateListener=CM.EditorView.updateListener.of(u=>{if(u.docChanged)previewDirty=true});
        exts.push(updateListener);
        _cmEditor=new CM.EditorView({
          state:CM.EditorState.create({doc:d.content||'',extensions:exts}),
          parent:document.getElementById('cmContainer')
        });
        cmOk=true;
      }catch(cmErr){
        console.warn('CodeMirror init failed, using textarea:',cmErr);
        if(_cmEditor){try{_cmEditor.destroy()}catch(e){}; _cmEditor=null}
      }
    }
    if(!cmOk){
      // Fallback textarea
      body.innerHTML=meta+`<textarea class="prev-textarea" id="prevEditor" spellcheck="false">${esc(d.content)}</textarea>`;
      document.getElementById('prevEditor').addEventListener('input',()=>{previewDirty=true});
      document.getElementById('prevEditor').addEventListener('keydown',e=>{
        if(e.key==='Tab'){e.preventDefault();const t=e.target;const s=t.selectionStart,en=t.selectionEnd;t.value=t.value.substring(0,s)+'  '+t.value.substring(en);t.selectionStart=t.selectionEnd=s+2}
        if(e.ctrlKey&&e.key==='s'){e.preventDefault();savePreview()}
      });
    }
  }catch(e){
    body.innerHTML=`<div class="empty"><span>⚠️</span>${esc(e.message||'Cannot preview this file')}</div>`;
  }
}
function _cmLang(ext,CM){
  const L=CM.langs;
  const m={'.js':L.javascript,'.jsx':L.javascript,'.ts':L.javascript,'.tsx':L.javascript,
    '.html':L.html,'.htm':L.html,'.php':L.php,
    '.css':L.css,'.scss':L.css,'.less':L.css,
    '.json':L.json,'.xml':L.xml,'.svg':L.xml,
    '.py':L.python,'.sql':L.sql,'.md':L.markdown,
    '.yaml':null,'.yml':null,'.sh':null,'.bash':null,'.bat':null,
    '.txt':null,'.log':null,'.csv':null,'.ini':null,'.cfg':null,'.conf':null,'.env':null,'.toml':null,
    '.c':null,'.cpp':null,'.h':null,'.java':null,'.go':null,'.rs':null,'.rb':null,
    '.srt':null,'.vtt':null,'.ass':null,'.ssa':null,'.nfo':null};
  const fn=m[ext];
  if(!fn)return null;
  try{return fn()}catch(e){console.warn('CodeMirror lang failed for',ext,e);return null}
}
function closePreview(){
  if(previewDirty){dlgConfirm('⚠️ Unsaved Changes','Discard changes and close?').then(ok=>{if(ok){previewDirty=false;if(_cmEditor){_cmEditor.destroy();_cmEditor=null}document.getElementById('previewM').style.display='none';const v=document.querySelector('#prevBody video');if(v)v.pause();const a=document.querySelector('#prevBody audio');if(a)a.pause()}});return}
  if(_cmEditor){_cmEditor.destroy();_cmEditor=null}
  document.getElementById('previewM').style.display='none';
  previewDirty=false;
  const v=document.querySelector('#prevBody video');if(v)v.pause();
  const a=document.querySelector('#prevBody audio');if(a)a.pause();
}
async function savePreview(){
  let content='';
  if(_cmEditor){
    content=_cmEditor.state.doc.toString();
  }else{
    const ta=document.getElementById('prevEditor');
    if(!ta)return;
    content=ta.value;
  }
  // Code validation (warn but don't block)
  const ext='.'+previewPath.split('.').pop().toLowerCase();
  if(ext==='.json'){
    try{JSON.parse(content)}catch(e){
      const m=e.message.match(/position (\d+)/);
      let info='JSON syntax error';
      if(m){const pos=parseInt(m[1]);const before=content.substring(0,pos);const line=before.split('\n').length;const col=pos-before.lastIndexOf('\n');info+=` at line ${line}, column ${col}`}
      else{info+=': '+e.message}
      toast(info,'warn');
    }
  }
  if(['.html','.htm'].includes(ext)){
    const openTags=[...content.matchAll(/<([a-z][a-z0-9]*)[^>]*(?<!\/\s*)>/gi)].map(m=>m[1].toLowerCase());
    const closeTags=[...content.matchAll(/<\/([a-z][a-z0-9]*)\s*>/gi)].map(m=>m[1].toLowerCase());
    const selfClose=new Set(['br','hr','img','input','meta','link','area','base','col','embed','source','track','wbr']);
    const stack=[];
    openTags.forEach(t=>{if(!selfClose.has(t))stack.push(t)});
    closeTags.forEach(t=>{const i=stack.lastIndexOf(t);if(i>=0)stack.splice(i,1)});
    if(stack.length>0)toast(`HTML: unclosed tags: <${stack.slice(0,5).join('>, <')}>`,'warn');
  }
  try{
    await api('PUT','/api/files/content/'+encodeURIComponent(previewPath),{content});
    toast('Saved! (version backed up)','ok');previewDirty=false;
  }catch(e){toast(e.message,'err')}
}

// === Drag Select (Rubber Band) ===
(function(){
  let dragging=false,startX=0,startY=0,box=null;
  const fmBody=()=>document.getElementById('fmContent');
  function createBox(){
    box=document.createElement('div');
    box.id='dragSelectBox';
    box.style.cssText='position:fixed;border:2px solid var(--pri);background:rgba(99,102,241,0.12);border-radius:3px;pointer-events:none;z-index:999;display:none';
    document.body.appendChild(box);
  }
  document.addEventListener('mousedown',e=>{
    // Only allow drag-select when File Manager page is active and has items
    const fmPage=document.getElementById('p-fm');
    if(!fmPage||!fmPage.classList.contains('active'))return;
    if(!fmAllItems||!fmAllItems.length)return;
    const body=fmBody();if(!body||!body.contains(e.target))return;
    // Don't start drag on buttons, inputs, context menu, modals
    if(e.target.closest('button,input,textarea,select,.ctx-menu,.modal-bg,.btn-s,.btn-d,.cp'))return;
    if(e.button!==0)return;
    dragging=false;startX=e.clientX;startY=e.clientY;
    if(!box)createBox();
    function onMove(ev){
      const dx=ev.clientX-startX,dy=ev.clientY-startY;
      if(!dragging&&(Math.abs(dx)>5||Math.abs(dy)>5)){
        dragging=true;box.style.display='block';
        // Clear previous selection
        fmSelected.clear();
        document.querySelectorAll('.fm-item.selected,.fm-row.selected').forEach(el=>el.classList.remove('selected'));
      }
      if(!dragging)return;
      const x=Math.min(startX,ev.clientX),y=Math.min(startY,ev.clientY);
      const w=Math.abs(dx),h=Math.abs(dy);
      box.style.left=x+'px';box.style.top=y+'px';
      box.style.width=w+'px';box.style.height=h+'px';
      // Check intersections
      const rect={left:x,top:y,right:x+w,bottom:y+h};
      fmSelected.clear();
      document.querySelectorAll('.fm-item,.fm-row:not(.fm-row-h)').forEach(el=>{
        const p=el.dataset.path;if(!p)return;
        const r=el.getBoundingClientRect();
        const hit=!(r.right<rect.left||r.left>rect.right||r.bottom<rect.top||r.top>rect.bottom);
        if(hit){fmSelected.add(p);el.classList.add('selected')}
        else{el.classList.remove('selected')}
      });
      // Update bulk bar
      const cnt=fmSelected.size;const show=cnt>0;
      document.getElementById('fmBulkDel').style.display=show?'inline-flex':'none';
      document.getElementById('fmBulkCompress').style.display=show?'inline-flex':'none';
      document.getElementById('fmSelInfo').style.display=show?'inline-flex':'none';
      document.getElementById('fmBulkDiv').style.display=show?'block':'none';
      document.getElementById('fmSelCount').textContent=cnt;
    }
    function onUp(){
      document.removeEventListener('mousemove',onMove);
      document.removeEventListener('mouseup',onUp);
      if(box)box.style.display='none';
      dragging=false;
    }
    document.addEventListener('mousemove',onMove);
    document.addEventListener('mouseup',onUp);
  });
})();

// === Keyboard Shortcuts ===
document.addEventListener('keydown', e => {
  // Skip if typing in input/textarea or modal open
  if(e.target.matches('input,textarea,select')) return;
  if(document.querySelector('.modal-bg[style*="flex"]')) return;
  const tab = document.getElementById('fileManager');
  if(!tab || tab.style.display === 'none') return;

  // Ctrl+A — Select All files
  if(e.ctrlKey && e.key === 'a') {
    e.preventDefault();
    fmSelected.clear();
    document.querySelectorAll('.fm-item,.fm-row:not(.fm-row-h)').forEach(el => {
      const p = el.dataset.path; if(!p) return;
      fmSelected.add(p); el.classList.add('selected');
    });
    const cnt = fmSelected.size;
    document.getElementById('fmBulkDel').style.display = cnt ? 'inline-flex' : 'none';
    document.getElementById('fmBulkCompress').style.display = cnt ? 'inline-flex' : 'none';
    document.getElementById('fmSelInfo').style.display = cnt ? 'inline-flex' : 'none';
    document.getElementById('fmBulkDiv').style.display = cnt ? 'block' : 'none';
    document.getElementById('fmSelCount').textContent = cnt;
  }

  // Delete — Delete selected files
  if(e.key === 'Delete' && fmSelected.size > 0) {
    e.preventDefault();
    fmBulkDel();
  }

  // Escape — Clear selection
  if(e.key === 'Escape') {
    fmSelected.clear();
    document.querySelectorAll('.fm-item.selected,.fm-row.selected').forEach(el => el.classList.remove('selected'));
    document.getElementById('fmBulkDel').style.display = 'none';
    document.getElementById('fmBulkCompress').style.display = 'none';
    document.getElementById('fmSelInfo').style.display = 'none';
    document.getElementById('fmBulkDiv').style.display = 'none';
  }

  // Enter — Open selected file (single selection only)
  if(e.key === 'Enter' && fmSelected.size === 1) {
    e.preventDefault();
    const path = [...fmSelected][0];
    const ext = '.'+path.split('.').pop().toLowerCase();
    if(VID_EXTS.includes(ext)||AUD_EXTS.includes(ext))playMediaFile(path);
    else openPreview(path);
  }

  // F2 — Rename selected file (single selection only)
  if(e.key === 'F2' && fmSelected.size === 1) {
    e.preventDefault();
    const path = [...fmSelected][0];
    fmCtxFile = path;
    ctxAction('rename');
  }
});
