// Run with FUSION_PLAYWRIGHT_MODULE pointing to an installed playwright package.
// Uses a local fixture server; no model or Fusion geometry calls are made.
const { chromium } = require(process.env.FUSION_PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname,'../static/index.html'));
const out = process.env.FUSION_UI_SCREENSHOTS || '/tmp/fusion-ui-validation';
fs.mkdirSync(out,{recursive:true});
const model = 'test/cad-model';
let requestBody;
let liveLog=[];
let selectedFaces=[];let selectionDocument="doc-one";let highlighted=[];let rejectFaces=false;
const chats = [{id:'one',title:'New chat',model,busy:false},{id:'two',title:'Drivebase layout',model,busy:false}];
const history = [
 {kind:'user',data:{text:'Move the mounting holes inward by 5 mm. Keep the outside dimensions.'}},
 {kind:'thinking_start',data:{}}, {kind:'thinking',data:{text:'Checking the placement and available references. '.repeat(90)}},
 {kind:'tool_start',data:{id:'t1',name:'inspect_entity',arguments:{entity:'mounting plate'}}},
 {kind:'tool_end',data:{id:'t1',ok:true,result:{dimensions:'200 × 100 mm'}}},
 {kind:'text',data:{text:'Moved the mounting holes **5 mm inward**.\n\n- Outside dimensions remain 200 × 100 mm.\n- All four holes keep their original diameter.\n\nThe updated hole spacing is **170 × 70 mm**.'}},
 {kind:'usage',data:{input_tokens:8200,output_tokens:362,session_cost_usd:0.0023}}
];
const server = http.createServer((req,res)=>{res.setHeader('Content-Type','text/html');res.end(source)});
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 let executablePath = process.env.FUSION_UI_BROWSER;
 if (!executablePath && !fs.existsSync(chromium.executablePath()) && process.platform === 'darwin') {
  const cached = fs.globSync(path.join(require('node:os').homedir(),'Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell'));
  executablePath = cached.sort().at(-1);
 }
 const browser = await chromium.launch({headless:true,executablePath});
 try {
  const page = await browser.newPage({viewport:{width:420,height:780},deviceScaleFactor:1});
  const errors=[];page.on('pageerror',e=>errors.push(String(e)));
  await page.route('**/api/**',async route=>{
   const u = new URL(route.request().url()); let body;
   if(u.pathname==='/api/bootstrap') body={presets:{[model]:{provider:'test',label:'CAD model',model}},backends:{test:{sdk_installed:true,key_present:true}},current:{model,effort:'low'},session:chats[0],sessions:chats,bridge:'connected',fusion:{active_document:{name:'Drivebase assembly'}},model_ok:true,model_status:'Ready'};
   else if(u.pathname==='/api/sessions') {
    if(route.request().method()==='POST') {const chat={id:'three',title:'New chat',model,busy:false};chats.unshift(chat);body=chat;}
    else body={sessions:chats};
   } else if(u.pathname.startsWith('/api/session/')) {const id=u.pathname.split('/').pop();body={...chats.find(c=>c.id===id),updated:Date.now()/1000,log:id==='two'?history:liveLog};}
   else if(u.pathname==='/api/face-selection') {
    const request=route.request().postDataJSON();
    if(request.action==='highlight'){highlighted=request.refs;body={count:highlighted.length};}
    else body={faces:selectedFaces,document_id:selectionDocument,ignored_non_faces:0};
   }
   else if(u.pathname==='/api/chat') {
    requestBody=route.request().postDataJSON();
    if(rejectFaces){await route.fulfill({status:400,contentType:'application/json',body:JSON.stringify({error:'The attached face was deleted. Select it again.'})});return;}
    await route.fulfill({status:200,contentType:'text/event-stream',body:'event: text\ndata: {"text":"I can see your **layout drawing**."}\n\nevent: done\ndata: {}\n\n'});return;
   } else if(u.pathname==='/api/stop') {chats[0].busy=false;liveLog.push({kind:'stopped',data:{}},{kind:'done',data:{}});body={ok:true};}
   else if(u.pathname==='/api/capture') body={url:'/fixture.png'};
   else body={ok:true};
   await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
  });
  await page.route('**/fixture.png',route=>route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="1400" height="1000"><rect width="1400" height="1000" fill="#f3f4f6"/><rect x="200" y="180" width="1000" height="620" rx="20" fill="#b2bbc6" stroke="#5c6979" stroke-width="5"/><rect x="290" y="270" width="820" height="440" fill="#f3f4f6"/><circle cx="245" cy="225" r="15" fill="#fff"/><circle cx="1155" cy="225" r="15" fill="#fff"/></svg>'}));
  const url=`http://127.0.0.1:${server.address().port}/?embedded=1`;
  await page.goto(url);await page.waitForSelector('#welcome');
  async function contained(selector){
   // ResizeObserver positions the canvas on the next rendering frame.
   await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
   assert(await page.locator(selector).isVisible(),selector+' visible');
   const r=await page.locator(selector).boundingBox(), v=page.viewportSize();
   assert(r.x>=-1&&r.y>=-1&&r.x+r.width<=v.width+1&&r.y+r.height<=v.height+1,selector+' fits '+JSON.stringify(v)+': '+JSON.stringify(r));
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'no page overflow');
  }
  for(const size of [{width:320,height:520},{width:360,height:640},{width:420,height:780},{width:480,height:740},{width:1000,height:800}]){
   await page.setViewportSize(size);await contained('#composer');await contained('#send');await contained('#capture-markup');
   await page.click('#settings-toggle');await contained('#preset');await contained('#reset');await page.click('#settings-close');
  }
  await page.setViewportSize({width:420,height:780});await page.screenshot({path:path.join(out,'chat-empty.png')});
  await page.fill('#text','Place four modules at these corners.');
  await page.click('#scratchpad');await page.waitForFunction(()=>document.querySelector('#shot').naturalWidth===1200);
  await contained('#canvas');await contained('#attach');
  const c=await page.locator('#canvas').boundingBox();
  await page.click('[data-tool="rect"]');
  await page.mouse.move(c.x+c.width*.15,c.y+c.height*.2);await page.mouse.down();await page.mouse.move(c.x+c.width*.85,c.y+c.height*.8);await page.mouse.up();
  await page.click('[data-tool="arrow"]');
  await page.mouse.move(c.x+c.width*.4,c.y+c.height*.1);await page.mouse.down();await page.mouse.move(c.x+c.width*.2,c.y+c.height*.3);await page.mouse.up();
  await page.click('[data-tool="text"]');await page.fill('#label-text','28 in');await page.mouse.click(c.x+c.width*.42,c.y+c.height*.5);
  assert.equal(await page.evaluate(()=>strokes.length),3);
  await page.setViewportSize({width:320,height:520});await contained('#canvas');await contained('#attach');await contained('#ink-colour');
  const align=await page.evaluate(()=>{const a=document.querySelector('#canvas').getBoundingClientRect(),b=document.querySelector('#shot').getBoundingClientRect();return Math.abs(a.x-b.x)+Math.abs(a.y-b.y)+Math.abs(a.width-b.width)+Math.abs(a.height-b.height)});assert(align<1,'canvas overlay aligned');
  await page.setViewportSize({width:420,height:780});await page.screenshot({path:path.join(out,'drawing.png')});
  await page.click('#back-chat');assert.equal(await page.inputValue('#text'),'Place four modules at these corners.');
  await page.reload();await page.waitForSelector('#resume-drawing:not(.hide)');await page.click('#resume-drawing');
  assert.equal(await page.evaluate(()=>strokes.length),3,'drawing survives page reload');
  await page.click('#attach');assert.equal(await page.locator('.thumb').count(),1);
  await page.screenshot({path:path.join(out,'chat-attachment.png')});
  await page.click('#send');await page.waitForSelector('.prose strong');assert.equal(requestBody.images.length,1);assert(requestBody.images[0].startsWith('data:image/png;base64,'));assert.equal(requestBody.text,'Place four modules at these corners.');
  assert.equal(await page.locator('.thumb').count(),0);
  await page.click('#rail-toggle');await page.getByText('Drivebase layout',{exact:true}).click();
  await page.waitForFunction(()=>CHAT==='two'&&!document.querySelector('main').classList.contains('show-rail')&&document.querySelector('#log').textContent.includes('Moved the mounting holes'));
  assert.equal(await page.locator('.think[open]').count(),0);assert.equal(await page.locator('.tool[open]').count(),0);await contained('#composer');
  await page.screenshot({path:path.join(out,'chat-history.png')});
  await page.click('#capture-markup');await page.waitForFunction(()=>document.querySelector('#shot').naturalWidth===1400);await contained('#canvas');
  await page.screenshot({path:path.join(out,'capture.png')});
  await page.click('#attach');assert.equal(await page.locator('.thumb').count(),1);
  // Reload an active chat, then observe new calls without waiting for completion.
  chats[0].busy=true;liveLog=[{kind:'turn_start',data:{}}];
  const repeatedCall=i=>[{kind:'tool_start',data:{id:'repeat'+i,name:'list_documents',arguments:{}}},{kind:'tool_end',data:{id:'repeat'+i,name:'list_documents',ok:true,result:{documents:[]}}}];
  for(let i=0;i<3;i++) liveLog.push(...repeatedCall(i));
  await page.reload();await page.waitForFunction(()=>document.querySelector('#run-warning').textContent.includes('3 times'));
  assert(await page.locator('#stop').isVisible());await contained('#run-state');
  liveLog.push(...repeatedCall(3));
  await page.waitForFunction(()=>document.querySelector('#run-meta').textContent.includes('4 tool calls'));
  assert.equal(await page.locator('.tool').count(),4,'reopened active chat updates its transcript');
  await page.screenshot({path:path.join(out,'run-progress.png')});
  await page.evaluate(()=>{progress.last=Date.now()-45000;progress.warning='';paintProgress()});
  assert((await page.textContent('#run-warning')).includes('No new activity for 45s'));
  await page.click('#stop');await page.waitForFunction(()=>document.querySelector('#run-label').textContent==='Stopped');
  assert(await page.locator('#send').isVisible());assert(await page.locator('#stop').isHidden());
  const retained=await page.evaluate(async()=>{
    const db=await draftDB;
    return await new Promise(resolve=>{const r=db.transaction('drafts').objectStore('drafts').getAll();r.onsuccess=()=>resolve(r.result.filter(x=>x.sent).map(x=>({text:x.text,images:x.images.length})));});
  });
  assert(retained.some(x=>x.text==='Place four modules at these corners.'&&x.images===1),'sent drawings retained separately from cleared draft');
  const prefill={text:'Recovered design prompt',images:[requestBody.images[0]]};
  await page.goto(url+'&session=two#draft='+encodeURIComponent(JSON.stringify(prefill)));
  await page.waitForFunction(()=>document.querySelector('#text').value==='Recovered design prompt');
  assert.equal(await page.locator('.thumb').count(),1);assert.equal(await page.evaluate(()=>CHAT),'two');
  assert.equal(await page.evaluate(()=>location.hash),'');
  await page.goto(url+'&retry_session=one');
  await page.waitForFunction(()=>document.querySelector('#text').value==='Place four modules at these corners.');
  assert.equal(await page.locator('.thumb').count(),1,'restart retry restores retained images');
  await page.evaluate(()=>paintDesignState({entries:[{id:'spacing',statement:'Mount spacing comes from the purchased component',kind:'derived',status:'stale'},{id:'plate',statement:'Plate depends on measured spacing',kind:'derived',status:'blocked',depends_on:['spacing']}],next_action:'Measure the mounting interfaces'}));
  assert(await page.locator('#design-state').isVisible());
  await page.click('#design-state summary');
  assert((await page.textContent('#design-entries')).includes('Needs rechecking'));
  assert((await page.textContent('#design-entries')).includes('Dependency unresolved'));
  await page.locator('#text').fill('');await page.click('#correct-design');
  assert((await page.inputValue('#text')).includes('Correction to the design intent'));
  for(const size of [{width:320,height:520},{width:480,height:740}]){await page.setViewportSize(size);await contained('#design-state');await contained('#composer');}
  await page.screenshot({path:path.join(out,'design-state.png')});
  await page.evaluate(()=>paintDesignState({entries:[]}));assert(await page.locator('#design-state').isHidden());
  await page.click('#attach-faces');await page.waitForFunction(()=>document.querySelector('#face-hint').textContent.includes('Select faces in Fusion'));
  selectedFaces=[{ref:'picked_one',body:'Complex housing',occurrence:'Module:2',selector:{face_ref:'picked_one'}},{ref:'picked_two',body:'Mount',occurrence:'Module:4',selector:{face_ref:'picked_two'}}];
  await page.click('#attach-faces');await page.waitForFunction(()=>faceAttachments.length===2);
  await page.locator('[aria-label="Label Face 1"]').fill('Tube socket <A>');
  await page.click('#attach-faces');await page.waitForFunction(()=>!document.querySelector('#attach-faces').disabled);assert.equal(await page.locator('.face-chip').count(),2,'repeated capture does not duplicate faces');
  await page.reload();await page.waitForSelector('.face-chip');
  assert.equal(await page.locator('[aria-label="Label Face 1"]').inputValue(),'Tube socket <A>','face labels survive draft reload');
  await page.locator('.face-chip button').first().click();await page.waitForTimeout(30);assert.deepEqual(highlighted,['picked_one']);
  selectionDocument='doc-other';await page.click('#attach-faces');await page.waitForFunction(()=>document.querySelector('#face-hint').textContent.includes('another document'));assert.equal(await page.locator('.face-chip').count(),2);selectionDocument='doc-one';
  await page.click('[aria-label="Remove Face 1"]');selectedFaces=[{ref:'picked_three',body:'Socket',selector:{face_ref:'picked_three'}}];
  await page.click('#attach-faces');await page.waitForFunction(()=>faceAttachments.some(f=>f.tag==='Face 3'));assert.equal(await page.locator('[aria-label="Label Face 1"]').count(),0,'removed face numbers are not reassigned');
  rejectFaces=true;await page.locator('#text').fill('Measure Face 2 to Face 3');const rejection=page.waitForResponse(r=>r.url().includes('/api/chat'));await page.click('#send');await rejection;
  await page.waitForFunction(()=>document.querySelector('#text').value==='Measure Face 2 to Face 3'&&faceAttachments.length===2);
  assert(requestBody.faces.every(face=>face.ref.startsWith('picked_')));assert.equal(requestBody.faces[1].tag,'Face 3');
  rejectFaces=false;await page.waitForFunction(()=>!sending);await page.evaluate(()=>{clearAttachments();document.querySelector('#text').value='';});const faceReply=page.waitForResponse(r=>r.url().includes('/api/chat'));await page.click('#send');await faceReply;
  await page.waitForFunction(()=>faceAttachments.length===0);await page.waitForFunction(()=>!sending);assert.equal(requestBody.text,'');assert.equal(requestBody.faces.length,2,'face-only messages are submitted');
  const faceHistory=await page.evaluate(async()=>{const db=await draftDB;return new Promise(resolve=>{const r=db.transaction('drafts').objectStore('drafts').getAll();r.onsuccess=()=>resolve(r.result.filter(x=>x.sent&&x.faces?.length).map(x=>x.faces));});});
  assert(faceHistory.some(faces=>faces.length===2&&faces[1].tag==='Face 3'),'sent faces retained for retries');
  await page.evaluate(()=>{faceAttachments=Array.from({length:12},(_,i)=>({ref:'picked_'+i,tag:'Face '+(i+1),label:'Mount surface',body:'Complex module'}));renderFaceAttachments();paintDesignState({entries:[{id:'mounts',kind:'unknown',status:'open',statement:'Resolve mounting interfaces'}],next_action:'Inspect the selected faces'});});
  for(const size of [{width:320,height:520},{width:480,height:740}]){await page.setViewportSize(size);await contained('#composer');await contained('#send');await contained('#text');}
  await page.screenshot({path:path.join(out,'face-attachments.png')});
  await page.evaluate(()=>{
    faceAttachments=[];renderFaceAttachments();paintDesignState({entries:[]});
    renderEvent(null,'face_selection_request',{
      question:'Which face supports the rail?',reason:'The mounting interface is ambiguous.',
      faces:['Rail support surface','Insertion stop surface']},{pending:{}});
  });
  const faceRequest=page.locator('.face-request').last();
  await faceRequest.scrollIntoViewIfNeeded();
  assert(await faceRequest.isVisible(),'face-selection question stays visible');
  assert((await faceRequest.innerText()).includes('Insertion stop surface'));
  await faceRequest.getByRole('button',{name:'Attach selected faces'}).click();
  await page.waitForFunction(()=>faceAttachments.length>0);
  assert(await page.locator('#face-hint').isVisible(),'request button invokes native selection flow');
  await page.screenshot({path:path.join(out,'face-selection-request.png')});
  assert.deepEqual(errors,[]);console.log('PASS: five viewport sizes, drawing, attachment submission, draft recovery, active-chat polling, repeat/quiet warnings and stop acknowledgement.');
 } finally {await browser.close();server.close();}
})().catch(e=>{console.error(e);server.close();process.exitCode=1});
