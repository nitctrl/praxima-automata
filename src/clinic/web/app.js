/* Tokens never enter browser storage. Database content is rendered only as text. */
"use strict";
const $=id=>document.getElementById(id);
const pages={
 today:["Home","What is live for the agent right now."],
 documents:["Knowledge","Stable clinic information from reviewed Markdown or Word documents."],
 temporary_notices:["Live Updates","Temporary closures, availability changes, and announcements."],
 test:["Agent Test","Ask the same published knowledge used by new phone calls."],
 requests:["Requests","Appointment and callback requests requiring human follow-up."],
 call_sessions:["Calls","Sanitized call outcomes; recordings and transcripts are not stored."],
 settings:["Settings","Language and approved safety messages."]
};
const categories=["about","vision","story","achievements","doctor_bio","facilities","policies","registration","other"];
let csrf="",memberships=[],current="today";
function node(tag,text,cls){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;}
function button(text,fn,cls="secondary"){const el=node("button",text,cls);el.type="button";el.addEventListener("click",()=>Promise.resolve().then(fn).catch(showError));return el;}
function base(){return `/api/clinics/${$("clinic").value}`;}
function role(){return memberships.find(row=>row.clinic_id===$("clinic").value)?.role;}
function manager(){return ["owner","manager"].includes(role());}
function showError(error){
 if(error.status===401){csrf="";memberships=[];$("workspace").hidden=true;$("login-panel").hidden=false;$("logout").hidden=true;$("navigation").replaceChildren();}
 $("message").textContent=error.status===401?"Your session ended. Please sign in again.":error.message||"Operation unavailable.";
}
async function api(path,body){
 const options={headers:{}};
 if(body!==undefined){options.method="POST";options.headers={"Content-Type":"application/json","X-CSRF-Token":csrf};options.body=JSON.stringify(body);}
 const response=await fetch(path,options),value=await response.json();
 if(!response.ok){const error=new Error(typeof value.detail==="string"?value.detail:`Request failed (${response.status}).`);error.status=response.status;throw error;}
 return value;
}
async function publishChanges(){await api(base()+"/preview",{});return api(base()+"/publish",{});}
async function changeAndPublish(path,next,previous){await api(path,next);try{return await publishChanges();}catch(error){try{await api(path,previous);}catch{}throw error;}}
async function signedIn(){
 const me=await api("/api/me");csrf=me.csrf;memberships=me.memberships;
 $("login-panel").hidden=true;$("workspace").hidden=false;$("logout").hidden=false;$("clinic").replaceChildren();
 for(const membership of memberships){const option=node("option",membership.clinics?.name||membership.clinic_id);option.value=membership.clinic_id;$("clinic").append(option);}
 $("clinic").disabled=false;$("navigation").replaceChildren();
 for(const [key,[label]] of Object.entries(pages))$("navigation").append(button(label,()=>load(key),"nav-item"));
 if(memberships.length)await load("today");else $("message").textContent="No active clinic membership.";
}
$("login-form").addEventListener("submit",async event=>{event.preventDefault();const submit=event.target.querySelector("button");submit.disabled=true;try{await api("/api/login",{email:$("email").value,password:$("password").value});$("password").value="";await signedIn();}catch(error){showError(error);}finally{submit.disabled=false;}});
$("logout").addEventListener("click",async()=>{try{await api("/api/logout",{});}finally{location.reload();}});
$("clinic").addEventListener("change",()=>load(current).catch(showError));
window.addEventListener("unhandledrejection",event=>{event.preventDefault();showError(event.reason);});
function section(title){const el=node("section",undefined,"card");el.append(node("h2",title));return el;}
function formCard(title){const el=node("form",undefined,"card");el.append(node("h2",title));return el;}
function status(text,live){return node("span",text,`status ${live?"published":"draft"}`);}
function picker(values,selected,label){const el=node("select");el.setAttribute("aria-label",label);for(const [value,text] of values){const option=node("option",text);option.value=value;option.selected=value===selected;el.append(option);}return el;}
function table(rows,actions,empty="No records yet."){
 if(!rows.length)return node("div",empty,"empty");
 const wrap=node("div",undefined,"table-wrap"),element=node("table"),head=node("tr"),keys=Object.keys(rows[0]);
 for(const key of keys)head.append(node("th",key.replaceAll("_"," ")));if(actions)head.append(node("th","Actions"));
 const thead=node("thead"),body=node("tbody");thead.append(head);element.append(thead);
 for(const row of rows){const tr=node("tr");for(const key of keys)tr.append(node("td",row[key]===null?"—":typeof row[key]==="object"?JSON.stringify(row[key]):String(row[key])));if(actions){const td=node("td");actions(row,td);tr.append(td);}body.append(tr);}
 element.append(body);wrap.append(element);return wrap;
}
function renderHome(value){
 const grid=node("div",undefined,"metric-grid"),knowledge=section("Published knowledge"),updates=section("Active live updates"),requests=section("New requests"),calls=section("Calls needing review");
 knowledge.append(node("p",`${value.knowledge_chunks} searchable chunks · version ${value.version.number}`));
 for(const title of value.documents)knowledge.append(node("p",title));if(!value.documents.length)knowledge.append(node("p","No published documents."));knowledge.append(button("Manage knowledge",()=>load("documents")));
 for(const message of value.live_updates)updates.append(node("p",message));if(!value.live_updates.length)updates.append(node("p","No live updates are active now."));updates.append(button("Manage live updates",()=>load("temporary_notices")));
 const pending=value.pending_requests||{},requestCount=(pending.appointment_requests||[]).length+(pending.callback_requests||[]).length;
 requests.append(node("p",`${requestCount} new requests`),button("Review requests",()=>load("requests")));calls.append(node("p",`${(value.unresolved_calls||[]).length} unresolved calls`),button("Review calls",()=>load("call_sessions")));
 grid.append(knowledge,updates,requests,calls);$("content").append(grid);
}
async function upload(file,category){
 const response=await fetch(base()+`/documents/upload?filename=${encodeURIComponent(file.name)}&category=${encodeURIComponent(category)}`,{method:"POST",headers:{"Content-Type":"application/octet-stream","X-CSRF-Token":csrf},body:file});
 const value=await response.json();if(!response.ok){const error=new Error(value.detail||"Upload failed.");error.status=response.status;throw error;}return value;
}
function uploadEditor(){
 const form=formCard("Upload document"),file=node("input"),category=picker(categories.map(value=>[value,value.replaceAll("_"," ")]),"about","Document type");file.type="file";file.accept=".docx,.md";file.required=true;
 form.append(node("p","Upload Markdown or Word, review the extracted text, then publish."),file,category,node("button","Upload for review"));
 form.addEventListener("submit",async event=>{event.preventDefault();const created=await upload(file.files[0],category.value);await reviewDocument(created.id);});$("content").replaceChildren(form);$("actions").replaceChildren(button("Back to knowledge",()=>load("documents")));
}
async function reviewDocument(id){
 const doc=await api(base()+`/documents/${id}`),form=formCard("Review document"),title=node("input"),category=picker(categories.map(value=>[value,value.replaceAll("_"," ")]),doc.document_category,"Document type");title.value=doc.title||"";title.required=true;
 form.append(node("p",doc.original_filename),node("label","Title"),title,node("label","Document type"),category);
 const edited=(doc.sections||[]).map(source=>{const block=node("div",undefined,"table-wrap"),heading=node("input"),text=node("textarea"),remove=node("input");heading.value=source.heading||"";text.value=source.text||"";text.rows=5;remove.type="checkbox";block.append(node("label","Heading"),heading,node("label","Published text"),text,node("label","Remove section"),remove);form.append(block);return {source,heading,text,remove};});
 form.append(node("button","Save reviewed text"));form.addEventListener("submit",async event=>{event.preventDefault();const sections=edited.filter(row=>!row.remove.checked&&row.text.value.trim()).map((row,position)=>({id:row.source.id,position,heading:row.heading.value,text:row.text.value.trim(),doctor_id:null,keywords:row.source.keywords||[]}));await api(base()+`/documents/${id}`,{title:title.value,document_category:category.value,sections});const result=doc.status==="published"?await publishChanges():null;await load("documents");$("message").textContent=result?`Published document updated · ${result.indexed??0} chunks indexed.`:"Saved. Publish when ready.";});
 $("content").replaceChildren(form);$("actions").replaceChildren(button("Back to knowledge",()=>load("documents")));
}
function renderDocuments(rows){
 const list=node("div",undefined,"item-list");if(!rows.length)list.append(node("div","No documents yet.","empty"));
 for(const row of rows){const live=row.status==="published",item=section(row.title||row.original_filename),head=node("div",undefined,"item-head"),actions=node("div",undefined,"item-actions");head.append(node("span",row.original_filename),status(live?"Published":"Not published",live));item.prepend(head);if(manager())actions.append(button("Review",()=>reviewDocument(row.id)),button(live?"Unpublish":"Publish",async()=>{if(!confirm(`${live?"Unpublish":"Publish"} this document?`))return;const path=base()+`/documents/${row.id}/status`,result=await changeAndPublish(path,{status:live?"archived":"published"},{status:row.status});await load("documents");$("message").textContent=`Knowledge updated · ${result.indexed??0} chunks indexed.`;},live?"secondary":""));item.append(actions);list.append(item);}$("content").append(list);
}
function localDateTime(value){const date=new Date(value),shifted=new Date(date.getTime()-date.getTimezoneOffset()*60000);return shifted.toISOString().slice(0,16);}
function updateState(row){const now=Date.now(),start=Date.parse(row.starts_at),end=Date.parse(row.expires_at);if(row.publication_status!=="published")return ["Not published",false];if(now<start)return ["Scheduled",true];if(now>=end)return ["Expired",false];return ["Active now",true];}
function liveUpdateEditor(row={}){
 const form=formCard(row.id?"Edit live update":"Add live update"),types=[["information","General, doctor, hours, or service update"],["closure","Clinic closure"],["no_walk_ins","No walk-ins"]],kind=picker(types,types.some(value=>value[0]===row.notice_type)?row.notice_type:"information","Update type"),message=node("textarea"),start=node("input"),end=node("input"),now=new Date();
 message.value=row.public_message||"";message.required=true;message.placeholder="Example: Dr Mahto is unavailable today after 4 PM.";start.type=end.type="datetime-local";start.required=end.required=true;start.value=localDateTime(row.starts_at||now);end.value=localDateTime(row.expires_at||new Date(now.getTime()+86400000));
 form.append(node("p","This overrides document information while it is active."),node("label","Type"),kind,node("label","What should patients know?"),message,node("label","Starts"),start,node("label","Expires"),end,node("button","Save update"));
 form.addEventListener("submit",async event=>{event.preventDefault();const starts=new Date(start.value),expires=new Date(end.value);if(expires<=starts)throw new Error("Expiry must be after the start time.");const values={notice_type:kind.value,public_message:message.value.trim(),internal_note:"",starts_at:starts.toISOString(),expires_at:expires.toISOString(),priority:100,publication_status:row.publication_status||"draft",location_id:null,doctor_id:null,service_id:null};if(row.id)values.id=row.id;await api(base()+"/rows/temporary_notices",values);const result=row.publication_status==="published"?await publishChanges():null;await load("temporary_notices");$("message").textContent=result?`Live update changed · ${result.indexed??0} chunks indexed.`:"Saved. Publish when ready.";});
 $("content").replaceChildren(form);$("actions").replaceChildren(button("Back to live updates",()=>load("temporary_notices")));
}
function renderLiveUpdates(rows){
 const list=node("div",undefined,"item-list");if(!rows.length)list.append(node("div","No live updates yet.","empty"));
 for(const row of rows){const [label,active]=updateState(row),published=row.publication_status==="published",item=section(row.public_message),head=node("div",undefined,"item-head"),actions=node("div",undefined,"item-actions");head.append(node("span",`${new Date(row.starts_at).toLocaleString()} — ${new Date(row.expires_at).toLocaleString()}`),status(label,active));item.prepend(head);if(manager())actions.append(button("Edit",()=>liveUpdateEditor(row)),button(published?"Unpublish":"Publish",async()=>{if(!confirm(`${published?"Unpublish":"Publish"} this update?`))return;const path=base()+"/rows/temporary_notices",result=await changeAndPublish(path,{id:row.id,publication_status:published?"archived":"published"},{id:row.id,publication_status:row.publication_status});await load("temporary_notices");$("message").textContent=`Live updates published · ${result.indexed??0} chunks indexed.`;},published?"secondary":""));item.append(actions);list.append(item);}$("content").append(list);
}
function requestActions(kind){return (row,cell)=>{cell.append(button("View contact",async()=>{const details=await api(base()+`/requests/${kind}/${row.id}/detail`,{}),card=section("Contact — access audited");card.append(node("p",details.erased?"Contact erased.":`${details.name} · ${details.phone}`),button("Hide",()=>card.remove()));$("content").prepend(card);}));const select=picker(["new","contacted",...(kind==="appointment"?["confirmed_externally"]:[]),"closed","cancelled"].map(value=>[value,value]),row.status,"Status");cell.append(select,button("Update",async()=>{if(select.value==="confirmed_externally"&&!confirm("Has a human confirmed this outside the system?"))return;await api(base()+`/requests/${kind}/${row.id}/status`,{status:select.value});await load("requests");}));};}
async function renderRequests(){for(const [tableName,label,kind] of [["appointment_requests","Appointment requests","appointment"],["callback_requests","Callback requests","callback"]]){$("content").append(node("h2",label),table(await api(base()+`/rows/${tableName}?offset=0`),requestActions(kind)));}}
async function renderTest(){
 const form=formCard("Ask the agent"),question=node("input"),submit=node("button","Ask");question.required=true;question.maxLength=500;question.placeholder="Is Dr Mahto available tomorrow?";form.append(question,node("p","Uses the latest published documents and active live updates."),submit);
 for(const sample of ["Which doctors work here?","What qualification does Dr Mahto have?","Is the clinic open tomorrow?","Are walk-ins allowed today?"])form.append(button(sample,()=>{question.value=sample;form.requestSubmit();}));
 form.addEventListener("submit",async event=>{event.preventDefault();submit.disabled=true;try{const result=await api(base()+"/test",{question:question.value});$("content").querySelector(".test-result")?.remove();const card=section("Answer"),sources=node("details");card.classList.add("test-result");card.append(node("p",result.answer),node("small",`Published version ${result.published_version} · ${result.result.retrieval}`));sources.append(node("summary","Sources and retrieval details"),node("pre",JSON.stringify(result.result.data.passages,null,2)));card.append(sources);$("content").append(card);}finally{submit.disabled=false;}});$("content").append(form);
}
async function renderSettings(){
 const rows=await api(base()+"/settings"),row=rows[0],card=section("Clinic settings");card.append(node("p",`Language: ${row.default_language}`),node("p",row.greeting),node("p",row.emergency_message));$("content").append(card);if(!manager())return;
 $("actions").append(button("Edit settings",()=>{const form=formCard("Edit settings"),greeting=node("textarea"),emergency=node("textarea"),fallback=node("textarea"),language=node("input"),supported=node("input");greeting.value=row.greeting;emergency.value=row.emergency_message;fallback.value=row.fallback_message;language.value=row.default_language;supported.value=(row.supported_languages||[]).join(", ");form.append(node("label","Greeting"),greeting,node("label","Emergency message"),emergency,node("label","Fallback message"),fallback,node("label","Default language"),language,node("label","Supported languages"),supported,node("button","Save and publish"));form.addEventListener("submit",async event=>{event.preventDefault();await api(base()+"/settings",{greeting:greeting.value,emergency_message:emergency.value,fallback_message:fallback.value,default_language:language.value,supported_languages:supported.value.split(",").map(value=>value.trim()).filter(Boolean)});await publishChanges();await load("settings");$("message").textContent="Settings published for new calls.";});$("content").replaceChildren(form);$("actions").replaceChildren(button("Back",()=>load("settings")));}));
}
async function load(key){
 current=key;$("message").textContent="";$("title").textContent=pages[key][0];$("description").textContent=pages[key][1];$("breadcrumb").textContent=`${(role()||"clinic").toUpperCase()} WORKSPACE`;$("content").replaceChildren();$("actions").replaceChildren();
 [...$("navigation").children].forEach((item,index)=>item.classList.toggle("active",Object.keys(pages)[index]===key));
 if(key==="today")renderHome(await api(base()+"/today"));
 if(key==="documents"){if(manager())$("actions").append(button("＋ Upload document",uploadEditor,""));renderDocuments(await api(base()+"/documents"));}
 if(key==="temporary_notices"){if(manager())$("actions").append(button("＋ Add live update",()=>liveUpdateEditor({}),""));renderLiveUpdates(await api(base()+"/rows/temporary_notices?offset=0"));}
 if(key==="test")await renderTest();if(key==="requests")await renderRequests();if(key==="call_sessions")$("content").append(table(await api(base()+"/rows/call_sessions?offset=0")));if(key==="settings")await renderSettings();
}
signedIn().catch(error=>{if(error.status!==401)showError(error);});
