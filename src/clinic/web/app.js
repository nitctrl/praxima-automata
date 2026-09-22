/* Tokens never enter browser storage. Render all database content as text, not HTML. */
"use strict";
const $ = id => document.getElementById(id);
const pages = {
  today: ["Today", "Published information for your front desk."],
  doctors: ["Doctors", "Manage public doctor information and effective dates."],
  services: ["Services", "Approved administrative service information."],
  doctor_services: ["Fees", "Append-only fee history. Overlapping effective intervals are rejected."],
  weekly_schedules: ["Weekly schedule", "Local clinic time. Split overnight hours into separate entries."],
  special_date_schedules: ["Special dates", "Date-specific schedules override weekly hours."],
  schedule_exceptions: ["Leave & exceptions", "Public messages and internal notes stay separate."],
  temporary_notices: ["Quick daily info", "Add or expire short-lived facts rendered into the Jinja voice prompt and RAG index."],
  locations: ["Locations", "Approved addresses, directions and parking information."],
  approved_faqs: ["Knowledge & review", "Approved question and answer wording for the phone assistant."],
  documents: ["Clinic documents", "Upload .docx or .md background text, review every line, then approve it for the next publication."],
  appointment_requests: ["Appointment requests", "Requests need human follow-up. Nothing here guarantees an available slot."],
  callback_requests: ["Callbacks", "Minimal administrative callback requests. Contact details require audited access."],
  call_sessions: ["Calls", "Sanitized outcomes only. No recordings or transcripts."],
  test: ["Agent test", "Hybrid semantic + lexical RRF search over the published clinic knowledge."],
  configuration_versions: ["Publish & history", "Review a preview before publishing. Existing calls retain their pinned version."],
  settings: ["Settings", "Language and approved messages are drafts until published."],
  clinic_users: ["Team", "Only owners can assign existing Auth users. You cannot change your own membership."],
  usage_records: ["Usage", "Raw provider units. Unpriced rates are not billing estimates; test calls are not production traffic."],
  audit_logs: ["Audit trail", "Append-only administrative actions and sensitive access events."],
  platform: ["Platform", "Requires a separately provisioned platform administrator identity."]
};
const fields = {
 doctors:"display_name,speciality,aliases,languages,short_public_bio,accepts_new_patients,effective_from,effective_until,status",
 services:"name,aliases,short_approved_description,appointment_required,active,effective_from,effective_until",
 locations:"name,address,landmark,directions,map_url,parking_information,status,effective_from,effective_until",
 doctor_services:"doctor_id,service_id,current_fee,currency,effective_from,effective_until,status",
 weekly_schedules:"doctor_id,location_id,day_of_week,start_time,end_time,availability_type,status,effective_from,effective_until",
 special_date_schedules:"doctor_id,location_id,schedule_date,start_time,end_time,publication_status",
 schedule_exceptions:"doctor_id,location_id,exception_date,status,start_time,end_time,public_message,internal_note,publication_status",
 temporary_notices:"location_id,doctor_id,service_id,notice_type,public_message,internal_note,starts_at,expires_at,priority,publication_status",
 approved_faqs:"category,canonical_question,alternative_phrasings,approved_answer,effective_from,effective_until,publication_status",
 settings:"greeting,emergency_message,fallback_message,default_language,supported_languages"
};
const arrays=new Set(["aliases","languages","alternative_phrasings","supported_languages"]);
const booleans=new Set(["accepts_new_patients","appointment_required","active"]);
const numbers=new Set(["current_fee","day_of_week","priority"]);
let csrf="", memberships=[], current="today", offset=0, editing=null;
function node(tag,text,cls){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;}
function button(text,fn,cls="secondary"){const el=node("button",text,cls);el.type="button";el.addEventListener("click",()=>Promise.resolve().then(fn).catch(showError));return el;}
function showError(error){
 if(error.status===401){csrf="";memberships=[];$("workspace").hidden=true;$("login-panel").hidden=false;$("logout").hidden=true;$("content").replaceChildren();$("actions").replaceChildren();$("navigation").replaceChildren();$("clinic").replaceChildren(node("option","Sign in to select"));$("clinic").disabled=true;$("editor").close();$("fields").replaceChildren();editing=null;$("title").textContent="Welcome to Reception";$("breadcrumb").textContent="A CALMER FRONT DESK";}
 $("message").textContent=error.status===401?"Your session ended. Please sign in again.":error.message||"Operation unavailable.";
}
function base(){return `/api/clinics/${$("clinic").value}`;}
function role(){return memberships.find(m=>m.clinic_id===$("clinic").value)?.role;}
function manager(){return ["owner","manager"].includes(role());}
async function api(path,body){const options={headers:{}};if(body!==undefined){options.method="POST";options.headers={"Content-Type":"application/json","X-CSRF-Token":csrf};options.body=JSON.stringify(body);}const res=await fetch(path,options);const value=await res.json();if(!res.ok){const error=new Error(typeof value.detail==="string"?value.detail:`Request failed (${res.status}).`);error.status=res.status;throw error;}return value;}
async function signedIn(){const me=await api("/api/me");csrf=me.csrf;memberships=me.memberships;$("login-panel").hidden=true;$("workspace").hidden=false;$("logout").hidden=false;$("clinic").replaceChildren();for(const m of memberships){const opt=node("option",m.clinics?.name||m.clinic_id);opt.value=m.clinic_id;$("clinic").append(opt);}$("clinic").disabled=false;$("navigation").replaceChildren();for(const [key,[label]] of Object.entries(pages))$("navigation").append(button(label,()=>{offset=0;return load(key);},"nav-item"));if(!memberships.length){$("message").textContent="No active clinic membership. Ask a clinic owner to assign access. Platform administrators may use Platform.";return;}await load("today");}
$("login-form").addEventListener("submit",async e=>{e.preventDefault();$("message").textContent="";const submit=e.target.querySelector("button");submit.disabled=true;try{await api("/api/login",{email:$("email").value,password:$("password").value});$("password").value="";await signedIn();}catch(err){showError(err);}finally{submit.disabled=false;}});
$("logout").addEventListener("click",async()=>{try{await api("/api/logout",{});}catch(err){showError(err);}finally{location.reload();}});
$("clinic").addEventListener("change",()=>{offset=0;load(current).catch(showError);});
$("previous").addEventListener("click",()=>{offset=Math.max(0,offset-50);load(current).catch(showError);});
$("next").addEventListener("click",()=>{offset+=50;load(current).catch(showError);});
function display(value){return value===null?"—":typeof value==="object"?JSON.stringify(value):String(value);}
function table(rows,actions){if(!rows.length){$("content").append(node("div","No records yet. New records will appear here.","empty"));return;}const wrap=node("div",undefined,"table-wrap"), t=node("table"),head=node("tr");const keys=Object.keys(rows[0]);keys.forEach(k=>head.append(node("th",k.replaceAll("_"," "))));if(actions)head.append(node("th","Actions"));const thead=node("thead");thead.append(head);t.append(thead);const tbody=node("tbody");for(const row of rows){const tr=node("tr");for(const key of keys)tr.append(node("td",display(row[key])));if(actions){const td=node("td");actions(row,td);tr.append(td);}tbody.append(tr);}t.append(tbody);wrap.append(t);$("content").append(wrap);}
function card(title,value){const c=node("section",undefined,"card");c.append(node("h2",title),node("pre",JSON.stringify(value,null,2)));return c;}
const categories=["about","vision","story","achievements","doctor_bio","facilities","policies","registration","other"];
function picker(options,selected,label){const el=node("select");el.setAttribute("aria-label",label);for(const [value,text] of options){const opt=node("option",text);opt.value=value;opt.selected=String(selected)===String(value);el.append(opt);}return el;}
async function upload(file,category){
 const url=base()+`/documents/upload?filename=${encodeURIComponent(file.name)}&category=${encodeURIComponent(category)}`;
 const res=await fetch(url,{method:"POST",headers:{"Content-Type":"application/octet-stream","X-CSRF-Token":csrf},body:file});
 const value=await res.json();if(!res.ok){const e=new Error(typeof value.detail==="string"?value.detail:`Upload failed (${res.status}).`);e.status=res.status;throw e;}return value;
}
async function reviewDocument(id){
 const doc=await api(base()+`/documents/${id}`),people=await api(base()+"/rows/doctors?offset=0");
 const options=[["","Not about one doctor"],...people.map(p=>[p.id,p.display_name])];
 const form=node("form",undefined,"card"),title=node("input"),category=picker(categories.map(c=>[c,c]),doc.document_category,"Document type"),doctor=picker(options,doc.doctor_id||"","Doctor");
 title.value=doc.title||"";title.required=true;title.maxLength=200;title.setAttribute("aria-label","Document title");
 form.append(node("h2","Review before approving"),node("p",`${doc.original_filename} · version ${doc.version} · ${doc.status}`),node("label","Title"),title,node("label","Document type"),category,node("label","About which doctor"),doctor);
 for(const warning of doc.extraction_warnings||[])form.append(node("p",`Note: ${warning}`));
 const edited=(doc.sections||[]).map(section=>{
  const block=node("div",undefined,"table-wrap"),heading=node("input"),text=node("textarea"),who=picker(options,section.doctor_id||"","Section doctor"),drop=node("input");
  heading.value=section.heading||"";heading.maxLength=200;heading.setAttribute("aria-label","Section heading");
  text.value=section.text||"";text.rows=4;text.setAttribute("aria-label","Section text");drop.type="checkbox";drop.setAttribute("aria-label","Remove this section");
  block.append(heading,text,who,node("label","Remove this section"),drop);form.append(block);
  return {section,heading,text,who,drop};
 });
 const checks=node("details");checks.append(node("summary","Before you approve"));
 for(const line of ["You have the right to publish this text.","No patient names, records or identifiable stories remain.","No guaranteed outcomes, medical advice or claims you cannot support.","Credentials and achievements are accurate and current."])checks.append(node("p",line));
 form.append(checks,node("button","Save reviewed text"));
 form.addEventListener("submit",async e=>{
  e.preventDefault();
  const sections=edited.filter(row=>!row.drop.checked&&row.text.value.trim()).map((row,position)=>({id:row.section.id,position,heading:row.heading.value,text:row.text.value.trim(),doctor_id:row.who.value||null,keywords:row.section.keywords||[]}));
  try{await api(base()+`/documents/${id}`,{title:title.value,document_category:category.value,doctor_id:doctor.value||null,sections});await load("documents");$("message").textContent="Reviewed text saved. Approve it, then publish a configuration version.";}catch(err){showError(err);}
 });
 $("content").replaceChildren(form);$("actions").replaceChildren(button("Back to documents",()=>load("documents")));$("pagination").hidden=true;
}
function uploadForm(){
 const form=node("form",undefined,"card"),file=node("input"),category=picker(categories.map(c=>[c,c]),"about","Document type");
 file.type="file";file.accept=".docx,.md";file.required=true;file.setAttribute("aria-label","Document file");
 form.append(node("h2","Upload background text"),node("p","Word (.docx) or Markdown (.md), up to 5 MB. Images, macros and tracked-change history are ignored."),file,category,node("button","Upload for review"));
 form.addEventListener("submit",async e=>{
  e.preventDefault();const chosen=file.files[0];if(!chosen)return;
  try{const created=await upload(chosen,category.value);await reviewDocument(created.id);$("message").textContent="Uploaded. Nothing reaches callers until you approve and publish.";}catch(err){showError(err);}
 });
 return form;
}
function summaryCard(title){const c=node("section",undefined,"card");c.append(node("h2",title));return c;}
function hoursText(hours){return (hours||[]).map(h=>`${h.start.slice(11,16)}–${h.end.slice(11,16)}`).join(", ");}
function renderToday(value){
 const grid=node("div",undefined,"metric-grid"),status=summaryCard("Clinic status"),data=value.status?.data||{};
 status.append(node("h3",value.status?.status==="success"?(data.status==="open"?"Open now":"Closed now"):"Status needs clarification"));
 if(data.local_time)status.append(node("p",`${data.local_time.slice(0,10)} · ${data.timezone}`));
 status.append(node("p",data.hours?.length?`Published hours: ${hoursText(data.hours)}`:"No confirmed opening hours to display."));
 if(data.walk_ins_restricted)status.append(node("p","Walk-ins are currently restricted."));
 for(const notice of data.notices||[])status.append(node("p",notice));
 const doctors=summaryCard("Doctors & effective hours"),names=value.doctors?.data?.doctors||[];
 if(!names.length)doctors.append(node("p","No effective published doctors today."));
 for(const doctor of names){
   const info=(value.doctor_hours||[]).find(r=>r.data?.doctor?.reference===doctor.reference)?.data;
   doctors.append(node("h3",doctor.name),node("p",doctor.speciality));
   doctors.append(node("p",info?(info.hours?.length?`${hoursText(info.hours)} · ${info.timezone}`:"No remaining published hours today."):"Hours need clarification; check the schedule and location."));
   if(info?.walk_in_restricted_hours?.length)doctors.append(node("p",`Walk-ins restricted: ${hoursText(info.walk_in_restricted_hours)}`));
   for(const notice of info?.notices||[])doctors.append(node("p",notice));
 }
 doctors.append(node("small","Working hours are not appointment slots. Staff must confirm requests."));
 const requests=summaryCard("New requests"),pending=value.pending_requests||{};
 for(const [key,label] of [["appointment_requests","Appointment requests"],["callback_requests","Callbacks"]]){
   const count=(pending[key]||[]).length;requests.append(node("p",`${label}: ${count}${count===value.limit?"+":""}`),button(`Review ${label.toLowerCase()}`,()=>{offset=0;return load(key);}));
 }
 requests.append(node("small",`Showing up to ${value.limit||50} new requests per category, not a lifetime total.`));
 const calls=summaryCard("Unresolved production calls"),count=(value.unresolved_calls||[]).length;
 calls.append(node("p",count?`${count}${count===value.limit?"+":""} calls need review.`:"No unresolved production calls returned."),node("small","Test calls are excluded from this summary."),button("Review calls",()=>{offset=0;return load("call_sessions");}));
 grid.append(status,doctors,requests,calls);$("content").append(grid);
}
async function load(key){current=key;$("message").textContent="";$("title").textContent=pages[key][0];$("description").textContent=pages[key][1];$("breadcrumb").textContent=role()?`${role().toUpperCase()} WORKSPACE`:"WORKSPACE";$("content").replaceChildren();$("actions").replaceChildren();$("pagination").hidden=true;[...$("navigation").children].forEach((el,i)=>el.classList.toggle("active",Object.keys(pages)[i]===key));
 if(key==="platform"){try{$("content").append(card("Platform overview",await api("/api/platform")));}catch(error){if(error.status!==403)throw error;const c=summaryCard("Platform access is separate");c.append(node("p","This account does not have platform-administrator access. Clinic owners can manage only their assigned clinics."));$("content").append(c);}return;}
 if(!$("clinic").value)throw new Error("Select an authorized clinic first.");
 if(key==="today"){const endpoint=base()+"/today";$("content").append(node("p","Loading published clinic information…"));try{const value=await api(endpoint);if(current!==key||base()+"/today"!==endpoint)return;$("content").replaceChildren();renderToday(value);}catch(error){if(current!==key||base()+"/today"!==endpoint)return;$("content").replaceChildren();if(error.status===401)throw error;const c=summaryCard("Today is unavailable");c.append(node("p",error.message||"Could not reach the clinic service. Check your connection and try again."),button("Retry Today",()=>load("today")));$("content").append(c);}return;}
 if(key==="test"){
  const form=node("form",undefined,"card"),label=node("label","Ask about published clinic information"),question=node("input");
  question.required=true;question.maxLength=500;question.id="test-question";question.placeholder="Where would Dr Sharma be available?";label.htmlFor=question.id;
  const submit=node("button","Ask"),clinicBase=base();
  form.append(label,question,node("p","Every question uses hybrid RRF retrieval over the published version. Unsaved drafts are never searched."),submit);
  form.addEventListener("submit",async e=>{
   e.preventDefault();submit.disabled=true;const asked=question.value;
   try{
    const result=await api(clinicBase+"/test",{question:asked});
    if(current!=="test"||base()!==clinicBase||!form.isConnected)return;
    $("content").querySelector(".test-result")?.remove();
    const c=node("section",undefined,"card test-result");c.append(node("h2","Test answer — not a live call"),node("p",result.answer||"See the structured result below."));
    const details=node("details");details.append(node("summary","Source facts and lookup details"),node("pre",JSON.stringify(result,null,2)));c.append(details);$("content").append(c);
   }catch(err){showError(err);}finally{submit.disabled=false;}
  });$("content").append(form);return;
 }
 if(key==="settings"){const rows=await api(base()+"/settings");$("content").append(card("Clinic settings",rows[0]));if(manager())$("actions").append(button("Edit messages & languages",()=>edit(rows[0])));return;}
 if(key==="documents"){
  if(!manager()){$("content").append(node("div","Only clinic owners and managers can review uploaded documents.","empty"));return;}
  $("content").append(uploadForm());
  table(await api(base()+"/documents"),(row,td)=>{
   td.append(button("Review",()=>reviewDocument(row.id)));
   if(row.status!=="published")td.append(button("Approve for publication",async()=>{if(!confirm("Approve this reviewed text? It reaches callers only after you publish a configuration version."))return;await api(base()+`/documents/${row.id}/status`,{status:"published"});await load("documents");$("message").textContent="Approved. Publish a configuration version to reach new calls.";}));
   else td.append(button("Withdraw",async()=>{await api(base()+`/documents/${row.id}/status`,{status:"archived"});await load("documents");}));
  });
  return;
 }
 const rows=await api(base()+`/rows/${key}?offset=${offset}`);
 if(fields[key]&&manager())$("actions").append(button("＋ Add draft",()=>edit({}),""));
 if(key==="configuration_versions"&&manager())$("actions").append(button("Preview current drafts",()=>preview(),""));
 if(key==="clinic_users"&&role()==="owner")$("actions").append(button("Assign existing user",()=>membershipEditor()));
 table(rows,(row,td)=>{
   if(fields[key]&&manager()&&key!=="doctor_services")td.append(button("Edit draft",()=>edit(row)));
   if(key==="temporary_notices"&&manager()){
    const included=row.publication_status==="published";
    td.append(button(included?"Remove from next publish":"Include in next publish",async()=>{
     await api(base()+"/rows/temporary_notices",{id:row.id,publication_status:included?"archived":"published"});
     await load("temporary_notices");
     $("message").textContent="Quick daily information updated. Publish the configuration to apply it to calls.";
    }));
   }
   if(key==="configuration_versions"&&manager()&&[2,3].includes(row.schema_version)&&["published","superseded"].includes(row.status))td.append(button("Preview rollback",()=>preview(row.id)));
   if(["appointment_requests","callback_requests"].includes(key)&&["owner","manager","receptionist"].includes(role())){const kind=key==="appointment_requests"?"appointment":"callback";td.append(button("View contact (audited)",async()=>{const details=await api(base()+`/requests/${kind}/${row.id}/detail`,{});$("content").querySelector(".sensitive")?.remove();const c=card("Contact — authorized access recorded",details);c.classList.add("sensitive");c.append(button("Hide details",()=>c.remove()));$("content").prepend(c);}));const status=node("select");status.setAttribute("aria-label","Request status");for(const s of ["new","contacted",...(kind==="appointment"?["confirmed_externally"]:[]),"closed","cancelled"]){const opt=node("option",s);opt.selected=row.status===s;status.append(opt);}td.append(status,button("Update status",async()=>{if(status.value==="confirmed_externally"&&!confirm("Has a human confirmed this booking outside this system?"))return;await api(base()+`/requests/${kind}/${row.id}/status`,{status:status.value});await load(current);}));}
 });$("pagination").hidden=false;$("previous").disabled=offset===0;$("next").disabled=rows.length<50;$("page-number").textContent=`Page ${offset/50+1}`;
}
async function preview(source){const result=await api(base()+"/preview",source?{source}:{});$("content").replaceChildren(card(source?"Rollback preview — review before publishing":"Draft preview — review before publishing",result.snapshot));$("actions").replaceChildren(button("Publish reviewed version",async()=>{if(!confirm("Publish this reviewed configuration for NEW calls? Existing calls keep their version."))return;const published=await api(base()+"/publish",{});await load("configuration_versions");$("message").textContent=`Configuration published. ${published.indexed??0} knowledge chunks indexed for semantic search. Existing calls are unchanged.`;},""));$("pagination").hidden=true;}
function edit(row){editing={row,key:current};$("edit-title").textContent=current==="settings"?"Edit approved messages":row.id?"Edit draft":"New draft";$("fields").replaceChildren();$("edit-error").textContent="";for(const name of fields[current].split(",")){const wrap=node("div"),label=node("label",name.replaceAll("_"," ")+(arrays.has(name)?" (comma separated)":""));const input=node(name.includes("message")||name.includes("description")||name.includes("answer")||name==="internal_note"?"textarea":"input");input.name=name;input.id=`field-${name}`;label.htmlFor=input.id;if(booleans.has(name)){input.type="checkbox";input.checked=row[name]??true;}else{input.value=arrays.has(name)?(row[name]||[]).join(", "):row[name]??"";if(numbers.has(name)){input.type="number";input.step="any";}else if(name.endsWith("_date")||name.startsWith("effective_"))input.type="date";else if(name.endsWith("_time"))input.type="time";else input.placeholder=name.endsWith("_id")?"UUID from the relevant list":name.endsWith("_at")?"2026-09-17T09:00:00+05:30":"";}wrap.append(label,input);$("fields").append(wrap);}$("editor").showModal();}
$("close-editor").addEventListener("click",()=>$("editor").close());
$("edit-form").addEventListener("submit",async e=>{e.preventDefault();const values={};for(const input of $("fields").querySelectorAll("input,textarea")){const name=input.name;if(booleans.has(name))values[name]=input.checked;else if(arrays.has(name))values[name]=input.value.split(",").map(x=>x.trim()).filter(Boolean);else if(input.value!=="")values[name]=numbers.has(name)?Number(input.value):input.value;else if(name in editing.row)values[name]=null;}if(editing.row.id)values.id=editing.row.id;try{await api(base()+(editing.key==="settings"?"/settings":`/rows/${editing.key}`),values);$("editor").close();await load(current);$("message").textContent="Draft saved. Review and publish to affect new calls.";}catch(err){$("edit-error").textContent=err.message;}});
function membershipEditor(){const c=node("form",undefined,"card"),user=node("input"),r=node("select"),active=node("input");user.placeholder="Existing Supabase Auth user UUID";user.required=true;user.setAttribute("aria-label","Auth user ID");for(const value of ["viewer","receptionist","manager","owner"])r.append(node("option",value));r.setAttribute("aria-label","Role");active.type="checkbox";active.checked=true;active.setAttribute("aria-label","Active");c.append(node("h2","Assign team member"),user,r,node("label","Active membership"),active,node("button","Save membership"));c.addEventListener("submit",async e=>{e.preventDefault();try{await api(base()+"/membership",{user_id:user.value,new_role:r.value,active:active.checked});await load("clinic_users");}catch(err){showError(err);}});$("content").prepend(c);}
signedIn().catch(error=>{if(error.status!==401)showError(error);});
