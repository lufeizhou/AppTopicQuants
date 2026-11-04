/* ---------- research ---------- */
let rpoll=null;
async function loadRuns(){const r=await(await fetch("/api/research")).json();
 $("#runs").innerHTML=r.length?`<h3 style="margin:0 0 6px">Saved research</h3><table class="small"><thead><tr><th>Topic</th><th>When</th><th>Contents</th><th>Status</th><th></th></tr></thead><tbody>`+
  r.map(x=>`<tr><td><a href="#" class="kw" data-open="${esc(x.id)}">${esc(x.topic)}</a></td><td class="muted">${esc((x.started_at||"").replace("T"," ").slice(0,16))}</td>
   <td class="muted">${x.n_aspects?x.n_aspects+" aspects":"no aspects"}${x.has_market?" · market report":""}${x.has_reddit?" · Reddit":""} · ${esc(x.depth||"")}</td>
   <td>${x.status==="running"?`running ${x.pct}%`:x.status==="error"?`<span class="muted">${esc(x.step||"error")}</span>`:"done"}</td>
   <td style="text-align:right">${x.status==="running"?"":`<button class="rdel" data-id="${esc(x.id)}" data-t="${esc(x.topic)}" aria-label="Delete ${esc(x.topic)}">Delete</button>`}</td></tr>`).join("")+"</tbody></table>":"<p class='sub'>No saved research yet.</p>";
 $("#runs").querySelectorAll("[data-open]").forEach(a=>a.onclick=e=>{e.preventDefault();showRun(a.dataset.open)});
 $("#runs").querySelectorAll(".rdel").forEach(b=>b.onclick=async()=>{if(!confirm(`Delete the research “${b.dataset.t}”?\n\nThe report is removed. Keywords it found stay tracked. This cannot be undone.`))return;
  const res=await fetch("/api/research/"+b.dataset.id,{method:"DELETE"});if(!res.ok){alert("Could not delete: "+((await res.json()).error||res.status));return}
  if($("#rpanel").dataset.id===b.dataset.id){$("#rpanel").style.display="none"}loadRuns()})}
let ASP=[];
async function startResearch(aspects){const topic=$("#rtopic").value.trim();if(topic.length<2)return;
 const r=await(await fetch("/api/research",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({topic,app:$("#rapp").value.trim(),depth:$("#rdepth").value,aspects:aspects||[]})})).json();
 if(r.error){$("#raspects").style.display="block";$("#raspects").innerHTML=`<p class="sub">${esc(r.error)}</p>`;return}$("#raspects").style.display="none";showRun(r.id)}
let ASPMETA=null;
async function suggestAspects(){const topic=$("#rtopic").value.trim();if(topic.length<2)return;const box=$("#raspects");box.style.display="block";
 box.innerHTML=`<p class="sub" style="font-size:13px">Collecting real evidence about “${esc(topic)}”: App Store searches, competitors, reviews, Reddit… (2–4 minutes)</p>`;$("#rasp").disabled=true;
 try{const r0=await(await fetch("/api/research/aspects",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({topic,app:$("#rapp").value.trim()})})).json();
  if(r0.error){box.innerHTML=`<p class="sub">${esc(r0.error)}</p>`;return}
  let j;for(;;){await new Promise(z=>setTimeout(z,2000));j=await(await fetch("/api/research/aspects/"+r0.job)).json();
   if(j.error&&!j.status){box.innerHTML=`<p class="sub">${esc(j.error)}</p>`;return}
   if(j.status!=="running")break;
   box.innerHTML=`<p class="sub" style="font-size:13px">Collecting real evidence about “${esc(topic)}” · ${esc(j.step)}</p><div class="bar"><i style="width:${j.pct}%"></i></div>`}
  if(j.status==="error"){box.innerHTML=`<p class="sub">Could not build aspects: ${esc(j.error)}</p>`;return}
  ASPMETA=j.result;ASP=j.result.aspects.map(a=>({...a,off:!!a.idea_only}));drawAspects()}finally{$("#rasp").disabled=false}}
function aspMetaLine(m){const c=m.evidence_counts||{},k=m.cost||{};
 return `<p class="sub" style="margin:4px 0 10px">Grouped from real evidence: ${c.searches} App Store searches (${c.searches_grouped} used)${m.roots&&m.roots.length?` incl. ${m.roots.map(esc).join(", ")}`:""} · ${c.apps} competitors · ${c.reviews} happy reviews · ${c.reddit} Reddit posts. Cost $${((k.deepseek_usd||0)+(k.reddit_usd||0)).toFixed(3)}${k.reddit_credits?` (${k.reddit_credits} Reddit credits)`:""}. Aspects marked “idea only” have no evidence: unticked.${(m.notes||[]).length?"<br>"+m.notes.map(esc).join(" · "):""}</p>`}
function aspEv(e){const p=[];if(e.searches)p.push(`<b>~${e.searches_per_day.toLocaleString()}</b>/day in ${e.searches} searches`);if(e.apps)p.push(`${e.apps} apps`);if(e.reviews)p.push(`${e.reviews} reviews`);if(e.reddit)p.push(`${e.reddit} Reddit`);
 let h=`<br><span class="ev">${p.join(" · ")||"no evidence"}</span>`;
 if(e.top_searches&&e.top_searches.length)h+=`<br><span class="muted">e.g. ${e.top_searches.slice(0,3).map(esc).join(", ")}</span>`;
 if(e.review_quote)h+=`<br><span class="muted">“${esc(e.review_quote.split(": ").slice(1).join(": ")||e.review_quote).slice(0,110)}…”</span>`;
 else if(e.reddit_title)h+=`<br><span class="muted">Reddit: “${esc(e.reddit_title).slice(0,110)}”</span>`;return h}
function drawAspects(){const box=$("#raspects");
 box.innerHTML=`<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px"><h3 style="margin:0">Pick the aspects to research</h3><span class="sub"><a href="#" id="aall">All</a> · <a href="#" id="anone">None</a></span></div>
 ${ASPMETA?aspMetaLine(ASPMETA):""}<div class="asp">${ASP.map((a,i)=>`<label><input type="checkbox" data-i="${i}" ${a.off?"":"checked"}><div><b>${esc(a.name)}</b>${a.idea_only?` <span class="tag idea">idea only</span>`:""}<span>${esc(a.description)}</span>${a.evidence?aspEv(a.evidence):`<br><span class="muted">${esc(a.audience)} · e.g. ${a.seeds.slice(0,3).map(esc).join(", ")}</span>`}</div></label>`).join("")}</div>
 <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center"><input id="aown" type="text" placeholder="Add your own aspect, e.g. protein for small appetite" style="flex:1;min-width:260px"><button id="aadd">Add</button>
 <button class="primary" id="ago"></button></div>`;
 const upd=()=>{const n=box.querySelectorAll(".asp input:checked").length;$("#ago").textContent=`Research ${n} aspect${n===1?"":"s"}`;$("#ago").disabled=!n};
 box.querySelectorAll(".asp input").forEach(c=>c.onchange=()=>{ASP[+c.dataset.i].off=!c.checked;upd()});upd();
 $("#aall").onclick=e=>{e.preventDefault();ASP.forEach(a=>a.off=false);drawAspects()};$("#anone").onclick=e=>{e.preventDefault();ASP.forEach(a=>a.off=true);drawAspects()};
 $("#aadd").onclick=()=>{const v=$("#aown").value.trim();if(!v)return;ASP.push({name:v,description:"Added by you",audience:"",seeds:[v.toLowerCase()]});drawAspects()};
 $("#ago").onclick=()=>startResearch(ASP.filter(a=>!a.off).map(({off,...a})=>a))}
/* ---------- research report: verdict first, every section collapsed behind its one-line conclusion ---------- */
const usd0=v=>v==null?"–":"$"+Math.round(v).toLocaleString();
const usdk=v=>v==null?"–":v>=1000?"$"+(v/1000).toFixed(1)+"k":"$"+Math.round(v);
const fmtV=v=>v==null?"–":v>=1e6?(v/1e6).toFixed(1)+"M":v>=1e3?Math.round(v/1e3)+"k":String(v);
const GQ=g=>(g||"E").replace("?","Q");
const chip=(g,s)=>`<span class="gb g${GQ(g)}">${g||"–"}</span>${s!=null?`<span class="sc">${Math.round(s)}</span>`:""}`;
const bar=(f,o={})=>{const w=o.w||120,h=o.h||8,c=o.c||"var(--blue-d)";return`<span class="hb" style="width:${w}px;height:${h}px"><i style="width:${Math.max(0,Math.min(100,Math.round((f||0)*100)))}%;background:${c}"></i></span>`};
const PCOL=["#1D4ED8","#60A5FA","#047857","#C2410C","#9CA3AF"],PKEYS=["opening","people_care","newcomers_win","someone_pays","white_space"];
const PLAB={opening:"Opening",people_care:"People care",newcomers_win:"Newcomers win",someone_pays:"Someone pays ads",white_space:"White space"};
const PTIP={opening:"Crowdedness of the aspect: Emerging 1.0 · Empty 0.7 (nobody serves it yet: unproven, not bad) · Crowded 0.35 · Saturated 0.1",
 people_care:"TikTok + YouTube views about the aspect on a log scale: 10k views = 0, 10M = 1",newcomers_win:"Competitors under 2 years old already earning $5k+/month: 4 = 1",
 someone_pays:"Competitors of this aspect with verified, active Meta ads: 3 = 1",white_space:"Wishes users voice for this aspect that no competitor offers: 3 = 1"};
const stk=(parts,W,w)=>`<span class="stk" style="width:${w||220}px">${PKEYS.map((k,i)=>parts[k]==null?"":`<i style="width:${(W[k]*parts[k]*100).toFixed(1)}%;background:${PCOL[i]}" data-tip="${PLAB[k]} ${Math.round(W[k]*100)}% × ${parts[k].toFixed(2)} = ${(W[k]*parts[k]*100).toFixed(0)} points"></i>`).join("")}</span>`;
const lgd=W=>`<span class="lgd">${PKEYS.map((k,i)=>`<span data-tip="${esc(PTIP[k])}"><i style="background:${PCOL[i]}"></i>${PLAB[k]} <b>${Math.round(W[k]*100)}%</b></span>`).join("")}</span>`;
const sec=(id,title,one,chips,body,open)=>`<details class="sec" id="sec-${id}"${open?" open":""}><summary><span class="chev"></span><b>${esc(title)}</b><span class="one">${one}</span><span class="chips">${(chips||[]).map(c=>`<span class="tag">${esc(c)}</span>`).join("")}</span></summary><div class="body">${body}</div></details>`;
const srcTags=x=>(x.sources||[]).map(s=>`<span class="tag">${esc(s)}</span>`).join("");
const seclink=(name,id)=>`<a href="#" class="golink" data-sec="${id}">${esc(name)} →</a>`;
const SECID={"Aspects":"aspects","Competitors":"comp","TikTok and YouTube":"social","TikTok & YouTube":"social","Reddit":"reddit","Who buys ads":"ads","Prices":"prices","Feature map":"features","Voice of users":"voice","Searches":"kw","Searches and keywords":"kw","Positioning":"pos"};
function secIdOf(name){const k=Object.keys(SECID).find(k=>(name||"").toLowerCase().includes(k.toLowerCase()));return k?SECID[k]:"kw"}

function verdictCard(r,m){const v=m.verdict;const av=m.aspect_verdicts||[];
 if(!v)return`<section class="card vwrap"><div class="kicker">Verdict</div><p style="margin:6px 0 10px">No verdict yet for this report (it was made before verdicts existed).</p><button class="primary" id="mkverdict" data-id="${esc(r.id)}">Compute the verdict</button> <span class="sub">Rescores the report's searches with the social evidence, grades each aspect and writes the call (a few cents, ~1 minute).</span></section>`;
 const best=av.find(a=>a.aspect===v.best_aspect)||av[0];
 const reasons=(v.reasons||[]).map(x=>`<li><b>${esc(x.title)}</b> <span class="why">${esc(x.detail)}</span> ${seclink(x.section||"",secIdOf(x.section))}</li>`).join("");
 const risks=(v.risks||[]).map(x=>`<li><b>${esc(x.title)}</b> <span class="why">${esc(x.detail)}</span></li>`).join("");
 const how=best?`<details class="how"><summary>How the grade ${v.grade} is built</summary><div class="why" style="margin:6px 0 8px">The grade is the verdict of the best proven aspect, <b>${esc(best.aspect)}</b> (${best.score} points): five parts, each 0–1, weighted. Unproven aspects (nobody serves them yet) do not set the grade but count as upside.</div>
   <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">${stk(best.parts,best.weights,260)}<b>${best.score}</b> ${lgd(best.weights)}</div>
   <div class="why" style="margin-top:8px">Grades: A ≥ 80 · B ≥ 65 · C ≥ 50 · D ≥ 35 · E below. ${esc(best.why||"")}</div></details>`:"";
 return`<section class="card vwrap"><div class="vcard">
  <div><div class="kicker">Verdict${r.app?" · "+esc(r.app.split(":")[0]):""}</div>
   <div style="display:flex;align-items:center;gap:14px;margin-top:8px"><span class="big g${GQ(v.grade)}">${esc(v.grade||"–")}</span><div><h2>${esc(v.call||"")}</h2><div class="sub" style="font-size:13px">${esc(v.not||"")}</div></div></div>
   ${v.one_liner?`<p style="margin:12px 0 0">${esc(v.one_liner)}</p>`:""}
   ${v.next_step?`<div class="next"><b>Next step</b> ${esc(v.next_step)}</div>`:""}
   ${v.growth?`<div class="why" style="margin-top:8px"><b>Where users come from:</b> ${esc(v.growth)}</div>`:""}</div>
  <div><div class="lbl">Why, in three facts</div><ol>${reasons}</ol></div>
  <div><div class="lbl">Risks</div><ul>${risks}</ul>${how}</div></div></section>`}

function aspectCard(m){const av=m.aspect_verdicts||[],rows=m.aspects||[];const byName=Object.fromEntries(rows.map(a=>[a.aspect,a]));
 const crowd=a=>`<span class="crowd"><span class="m"><i style="width:${a.crowd_index}%;background:${{Empty:"#9CA3AF",Emerging:"#2563EB",Crowded:"#F59E0B",Saturated:"#EA580C"}[a.band]}"></i></span><span class="band ${a.band}" data-tip="${esc(a.verdict)}">${a.band}</span></span>`;
 const head=`<div class="maphead"><h2 style="margin:0;font-size:16px">Aspects: where to play${info("One verdict per aspect: opening (crowdedness) 30% · people care (video views) 25% · newcomers win (young apps earning $5k+) 20% · someone pays ads 15% · white space (unmet wishes) 10%. Each part is 0–1; the bar shows weight × value. ? = nobody serves the aspect yet: unproven, judged from views and wishes.")}</h2><span class="sub">${av.length?lgd(av[0].weights):""}</span></div>`;
 if(!av.length)return`<section class="card">${head}<div class="tablewrap" style="max-height:none;margin-top:10px"><table class="small"><thead><tr><th>Aspect</th><th>Crowdedness</th><th class="num">Searches/day</th><th class="num">Apps</th><th class="num">Newcomers</th><th class="num">Small apps earn</th><th>Best search</th></tr></thead><tbody>${rows.map(a=>`<tr><td><b>${esc(a.aspect)}</b></td><td>${crowd(a)}</td><td class="num">${n0(a.searches_day)}</td><td class="num">${a.apps_real} · ${a.established} · ${a.giants}</td><td class="num">${pc(a.newcomer_share)}</td><td class="num">${usd0(a.small_apps_earn)}</td><td>${a.best_search?`<span class="kw" onclick="openKw('${esc(a.best_search)}')">${esc(a.best_search)}</span> ${chip(a.best_grade,a.best_score)}`:"–"}</td></tr>`).join("")}</tbody></table></div></section>`;
 const tr=av.map(v=>{const a=byName[v.aspect]||{},e=v.evidence||{};return`<tr><td style="white-space:normal;min-width:200px;max-width:260px"><b>${esc(v.aspect)}</b><br><span class="why">${esc(v.why||"")}</span></td>
  <td><span class="gb g${v.unproven?"Q":v.grade}">${v.unproven?"?":v.grade}</span><b class="sc">${v.score}</b>${v.unproven?'<span class="pill unproven">unproven</span>':""}</td>
  <td>${stk(v.parts,v.weights,170)}</td><td>${a.band?`<span class="band ${a.band}" data-tip="${esc(a.verdict)}">${a.band}</span>`:"–"}</td><td class="num">${n0(e.searches_day)}</td><td class="num">${fmtV(e.views)}</td>
  <td class="num" data-tip="${esc((e.young_winners||[]).map(w=>`${w.name} ${usdk(w.est)} ${w.age_months}m`).join(" · ")||"none")}">${(e.young_winners||[]).length}</td>
  <td class="num" data-tip="${esc((e.advertisers||[]).map(w=>`${w.name} ${w.days} days`).join(" · ")||"none")}">${(e.advertisers||[]).length}</td>
  <td class="num">${usd0(e.small_apps_earn)}</td><td>${e.best_search?`<span class="kw" onclick="openKw('${esc(e.best_search)}')">${esc(e.best_search)}</span> ${chip(e.best_grade)}`:"–"}</td></tr>`}).join("");
 return`<section class="card">${head}<div class="tablewrap" style="max-height:none;margin-top:10px"><table class="small"><thead><tr><th>Aspect</th><th>Verdict</th><th>How it is built</th><th>Opening</th><th class="num">Store /day${info("App Store searches per day across the aspect's searches")}</th><th class="num">Views${info("TikTok + YouTube views of videos about the aspect")}</th><th class="num">Young $5k+${info("Competitors under 2 years old estimated at $5k+/month (hover for names)")}</th><th class="num">Ads${info("Competitors of this aspect with verified, active Meta ads (hover for names)")}</th><th class="num">Small apps${info(TIP.attainable)}</th><th>Best search${info("The best search-led keyword score inside the aspect: asks only whether an App Store search would carry an app")}</th></tr></thead><tbody>${tr}</tbody></table></div>
 <p class="why" style="margin:10px 0 0">Hover a bar segment or a count for the detail. “Best search” is the search-led keyword score: it only asks whether an App Store search would carry an app, so it can be low where the aspect verdict is good.</p></section>`}

function posBody(m){return`<div class="opts">${(m.highlights||[]).map(o=>`<div class="opt"><h4>${esc(o.name)}</h4><div class="one">${esc(o.one_liner)}</div><div class="sub">For: ${esc(o.audience)} · ${(o.aspects||[]).map(esc).join(", ")}</div>
 <b class="lbl" style="margin-top:8px">Signature features</b><ul>${(o.signature_features||[]).map(f=>`<li><b>${esc(f.feature)}</b>${f.coverage?` <span class="muted">(competitors: ${esc(f.coverage)})</span>`:""}<br><span class="quote">${esc(f.why)}</span></li>`).join("")}</ul>
 <div style="font-size:13px"><b>Price</b> $${(o.price?.monthly??0).toFixed(2)}/mo · $${(o.price?.yearly??0).toFixed(2)}/yr <span class="quote">${esc(o.price?.reason||"")}</span></div>
 <div style="font-size:13px;margin-top:6px"><b>Why now</b> <span class="quote">${esc(o.why_now||"")}</span></div>
 <div style="font-size:12px;margin-top:6px">${(o.keywords||[]).map(k=>`<span class="kw" onclick="openKw('${esc(k)}')">${esc(k)}</span>`).join(" · ")}</div>
 <div class="quote" style="margin-top:6px">Risks: ${(o.risks||[]).map(esc).join(" · ")}</div></div>`).join("")}</div>`}

function voiceBody(V){const Q=x=>x.quote?`<br><span class="quote">“${esc(String(x.quote).slice(0,180))}” <span class="muted">(${esc(x.quote_source||"")})</span></span>`:"";
 const LI=(arr,k,extra)=>(arr||[]).slice(0,8).map(x=>`<li>${bar(x.strength,{w:48,h:6})} <b>${esc(x[k])}</b> ${srcTags(x)}${x.aspect?` <span class="muted">${esc(x.aspect)}</span>`:""}${extra?extra(x):""}${Q(x)}</li>`).join("");
 return`${V.summary?`<p style="font-size:14px;max-width:900px;margin:0 0 12px">${esc(V.summary)}</p>`:""}<div class="grid2">
  <div><div class="lbl">What they struggle with</div><ul class="vl">${LI(V.pains,"pain")}</ul></div>
  <div><div class="lbl">What they wish for</div><ul class="vl">${LI(V.wishes,"wish",x=>x.unmet?' <span class="band Emerging">no app does this</span>':"")}</ul></div>
  <div><div class="lbl">What they love today</div><ul class="vl">${LI(V.loved,"what")}</ul><div class="lbl">Who they are</div><ul class="vl">${(V.audiences||[]).slice(0,6).map(x=>`<li>${esc(x.who)} ${srcTags(x)}</li>`).join("")}</ul></div></div>
 ${(V.phrases||[]).length?`<p style="font-size:13px"><b>Their words</b>${info("Phrases people actually use: raw material for titles, subtitles and screenshots.")} ${V.phrases.slice(0,16).map(p=>`<span class="tag">${esc(p)}</span>`).join(" ")}</p>`:""}
 <p class="why">Bar = how often it comes up across ${V.n_app_reviews||0} App Store 4–5★ reviews, ${V.n_reddit_posts||0} Reddit posts and ${V.n_reddit_comments||0} comments${V.n_videos?`, ${V.n_videos} TikTok/YouTube videos and ${V.n_video_comments||0} video comments`:""} (merged by DeepSeek; quotes are real, tags say who said it).</p>`}

function socialBody(S,m){const A=S.aspects||[];const views=a=>((a.tiktok||{}).views||0)+((a.youtube||{}).views||0);const mx=Math.max(1,...A.map(views));const spd=Object.fromEntries((m.aspects||[]).map(a=>[a.aspect,a.searches_day]));
 const top=(x,p)=>x&&x.top?`<a href="${esc(x.top.url||"#")}" target="_blank" rel="noopener">${esc((x.top.title||"").slice(0,64))}…</a> <span class="muted">${fmtV(x.top.views)} ${p}</span>`:"";
 return`<div class="tablewrap" style="max-height:none"><table class="small"><thead><tr><th>Aspect</th><th>TikTok + YouTube views</th><th class="num">Store searches/day</th><th class="num">Videos</th><th class="num">Median views</th><th>Most watched</th></tr></thead><tbody>${A.map(a=>`<tr><td style="white-space:normal"><b>${esc(a.aspect)}</b></td><td>${bar(views(a)/mx,{w:160,h:10})} <b>${fmtV(views(a))}</b></td><td class="num">${n0(spd[a.aspect])}</td><td class="num">${((a.tiktok||{}).videos||0)+((a.youtube||{}).videos||0)}</td><td class="num">${fmtV(Math.max((a.tiktok||{}).median_views||0,(a.youtube||{}).median_views||0))}</td><td style="white-space:normal">${top(a.tiktok,"TikTok")}${top(a.tiktok,"")&&top(a.youtube,"")?"<br>":""}${top(a.youtube,"YouTube")}</td></tr>`).join("")}</tbody></table></div>
 <p class="why">Views say people care; store searches say they do not look for an app yet. TikTok: last 6 months; YouTube: this year. Median views = the typical video, so one viral clip does not mislead. ${S.credits_used} ScrapeCreators credits ($${(S.usd||0).toFixed(2)}); commenter names are not stored, posts kept 90 days.</p>`}

function redditBody(R){return`<div class="tablewrap" style="max-height:none"><table class="small"><thead><tr><th>Aspect</th><th class="num">Posts</th><th class="num">Last 90 days</th><th class="num">Comments read</th><th class="num">Median upvotes</th><th>Where they gather</th></tr></thead><tbody>${(R.aspects||[]).map(a=>`<tr><td><b>${esc(a.aspect)}</b></td><td class="num">${n0(a.posts)}</td><td class="num">${n0(a.posts_last_90d)}</td><td class="num">${n0(a.comments)}</td><td class="num">${n0(a.median_score)}</td><td style="white-space:normal">${(a.top_subreddits||[]).map(t=>`<a href="https://www.reddit.com/r/${encodeURIComponent(t.subreddit)}" target="_blank" rel="noopener">r/${esc(t.subreddit)}</a>${t.weekly_active_users?` <span class="muted">${n0(t.weekly_active_users)} weekly active</span>`:""}`).join(" · ")}</td></tr>`).join("")}</tbody></table></div>
 <p class="why">Posts and comments from the past year through ScrapeCreators (${R.credits_used} credits, $${(R.usd||0).toFixed(2)}). Usernames are not stored; posts are kept 90 days.</p>`}

function adsBody(ads){const A=ads.filter(a=>a.active_ads).sort((x,y)=>(y.oldest_active_days||0)-(x.oldest_active_days||0));
 return(A.length?`<div class="tablewrap" style="max-height:none"><table class="small"><thead><tr><th>App</th><th>Ads running since</th><th class="num">Active ads</th><th>Where</th><th>Their message</th></tr></thead><tbody>${A.map(a=>`<tr><td><b>${esc(a.name.split(":")[0])}</b>${a.verified?` <span data-tip="the ads link to this exact App Store app">✓</span>`:""}<br><span class="muted">${esc(a.page||"")}</span></td><td>${bar((a.oldest_active_days||0)/150,{w:120})} ${a.oldest_active_days!=null?a.oldest_active_days+" days":"–"}</td><td class="num">${a.active_ads}${a.more?"+":""}</td><td class="muted" style="white-space:normal">${(a.platforms||[]).map(p=>p[0]+p.slice(1).toLowerCase().replace("_"," ")).join(", ")}</td><td style="white-space:normal">${(a.titles||[]).slice(0,1).map(t=>`<b>${esc(t)}</b><br>`).join("")}${(a.messages||[]).slice(0,1).map(t=>`<span class="muted">${esc(t.slice(0,160))}</span>`).join("")}</td></tr>`).join("")}</tbody></table></div>`:"")+
 `<p class="why">${A.length} of ${ads.length} top competitors run Meta ads now${ads.length-A.length?`; the others have none or no Facebook page we could match`:""}. ✓ = the ads link to that exact App Store app. Ads that run for months pay for themselves: the app earns from paid users, not from search.</p>`}

function pricesBody(P){const per=P.periods||{},S=P.suggested||{};const lab={week:"Weekly",month:"Monthly",year:"Yearly",lifetime:"Lifetime"};
 const scale=(k,you)=>{const p=per[k];if(!p)return"";const lo=p.min,hi=p.max,f=v=>hi>lo?Math.max(0,Math.min(100,(v-lo)/(hi-lo)*100)):50;
  return`<div class="pscale"><div class="row"><b>${lab[k]}</b><span class="why">${p.n} apps (${pc(p.share)}) · median $${p.median.toFixed(2)}</span></div><div class="trk"><i class="iqr" style="left:${f(p.p25)}%;width:${f(p.p75)-f(p.p25)}%"></i><i class="med" style="left:${f(p.median)}%"></i>${you?`<i class="you" style="left:${f(you)}%" data-tip="suggested $${you.toFixed(2)}"></i>`:""}</div><div class="row why"><span>$${p.min.toFixed(2)}</span><span>$${p.max.toFixed(2)}</span></div></div>`};
 return`<div class="grid2">${scale("month",S.monthly)}${scale("year",S.yearly)}${scale("week",S.weekly)}</div>
 <div class="verdict build" style="margin:10px 0 0"><div><b>Suggested: ${S.monthly?`$${S.monthly.toFixed(2)}/month`:""}${S.yearly?` · $${S.yearly.toFixed(2)}/year`:""}${S.weekly?` · $${S.weekly.toFixed(2)}/week`:""}</b><div class="why">${esc(S.reason||"")}${S.yearly_discount!=null?` Yearly saves ${Math.round(S.yearly_discount*100)}% against monthly.`:""} ${P.apps_with_prices||0} of ${P.apps_profiled||0} competitors list prices.</div></div></div>
 <p class="why">Blue band = middle half of competitors, dark line = median, orange dot = the suggested price. From each competitor's App Store page (its top in-app purchases); each app counts once per plan type.</p>`}

function featuresBody(F,C){const low=[...F].sort((a,b)=>a.coverage-b.coverage).slice(0,8);const top=C.slice(0,16);
 return`<div class="lbl">White space: what few competitors offer</div>${low.map(f=>`<div class="frow"><span>${esc(f.name)} <span class="muted">${esc(f.group||"")}</span></span>${bar(f.coverage,{w:200,c:f.coverage<=.2?"#C2410C":"var(--blue-d)"})}<span class="num why">${pc(f.coverage)}</span><span class="why">${(f.apps||[]).slice(0,3).map(esc).join(", ")}</span></div>`).join("")}
 <p class="why">Coverage = share of the ${C.length} profiled competitors whose App Store description claims it (read by DeepSeek). Orange = 20% or less.</p>
 <details><summary>Full feature matrix (${F.length} features × ${top.length} apps)</summary><div class="tablewrap" style="max-height:none;margin-top:8px"><table class="small fm"><thead><tr><th>Feature</th><th>Coverage</th>${top.map(c=>`<th class="v" title="${esc(c.name)}">${esc(c.name.split(/[:\-–—|]/)[0].slice(0,22))}</th>`).join("")}</tr></thead><tbody>${F.map(f=>`<tr class="${f.coverage<=0.2?'gap':''}"><td><b>${esc(f.name)}</b></td><td><span class="cov"><i style="width:${Math.round(f.coverage*100)}%"></i></span>${pc(f.coverage)}</td>${top.map(c=>`<td class="c">${(c.features||[]).includes(f.id)?"●":""}</td>`).join("")}</tr>`).join("")}</tbody></table></div></details>`}

function compBody(C){return`<div class="tablewrap" style="max-height:none"><table class="small"><thead><tr><th>App</th><th>Aspects</th><th class="num">Ratings</th><th class="num">★</th><th class="num">Est. $/mo${info("Our calibrated estimate from ratings growth; typical error ×2–3 either way.")}</th><th class="num">Age</th><th>Updated</th><th>Model</th><th>Prices</th></tr></thead><tbody>${C.map(c=>{const young=c.age_months!=null&&c.age_months<=24&&(c.est||0)>=5000;return`<tr><td style="white-space:normal;min-width:220px"><b>${esc(c.name)}</b>${young?' <span class="pill build">young winner</span>':""}<br><span class="quote">${esc(c.what||c.developer||"")}</span></td><td class="muted" style="white-space:normal;max-width:180px">${(c.aspects||[]).map(esc).join(", ")}</td><td class="num">${n0(c.ratings)}</td><td class="num">${c.stars??"–"}</td><td class="num"><b>${usd0(c.est)}</b></td><td class="num">${c.age_months!=null?(c.age_months>=24?Math.round(c.age_months/12)+"y":c.age_months+"m"):"–"}</td><td>${esc(c.updated||"–")}${c.stale?' <span class="tag">stale</span>':""}</td><td class="muted">${esc(c.model||"")}</td><td class="muted" style="white-space:normal;min-width:150px">${["week","month","year","lifetime"].filter(k=>c.prices?.[k]).map(k=>`${{week:"wk",month:"mo",year:"yr",lifetime:"life"}[k]} $${c.prices[k].map(v=>v.toFixed(2)).join("/")}`).join(" · ")||(c.upfront_price?"$"+c.upfront_price+" upfront":"free / no IAP listed")}</td></tr>`}).join("")}</tbody></table></div>
 <p class="why">Apps that serve this market's searches, ranked by estimated revenue. Young winner = under 2 years old and estimated at $5k+/month. Stale = no update for 12+ months.</p>`}

function waterfall(i){const p=i.parts;if(!p)return`<p class="why">No breakdown stored for this search.</p>`;const I=p.in||{};
 const steps=[["Start",null,100]];let run=100;const add=(l,v,pw)=>{const mult=pw?Math.pow(v,pw):v;run*=mult;steps.push([l,mult,run])};
 add(`Money ${p.money.toFixed(2)}<sup>0.45</sup>`,p.money,0.45);add(`Reach ${p.reach.toFixed(2)}<sup>0.35</sup>`,p.reach,0.35);add(`Demand ${p.demand.toFixed(2)}<sup>0.20</sup>`,p.demand,0.20);
 add("Concentration",p.conc_adj);add("Buildability",p.build_adj);add("Trend",p.trend_adj);
 let prev=100;const rows=steps.map(([l,m,v])=>{const h=`<div class="row"><span class="why">${l}</span><span><i class="b" style="width:${(v*2.2).toFixed(0)}px;background:${m==null?"var(--blue-d)":"#93C5FD"}"></i>${m==null?"":`<i class="b" style="width:${((prev-v)*2.2).toFixed(0)}px;background:${m>1?"#A7F3D0":"#FED7AA"};margin-left:2px"></i>`}</span><span class="num why">${m==null?"":"× "+m.toFixed(2)}</span><b class="num">${v.toFixed(1)}</b></div>`;prev=v;return h}).join("");
 const money=`p5–p20 apps earn ${usd0(I.band_usd)} → ${(0.5*Math.max(0,Math.min(1,(Math.log10(Math.max(I.band_usd||1,1))-3)/1.477))).toFixed(2)} · P(≥$5k) ${pc(I.p5k)}${I.newcomers!=null?` · ~${Math.round(I.newcomers)} young apps earn $5k+`:""}${I.ad_activity!=null?` · ad activity ${I.ad_activity.toFixed(2)}`:""}${I.cpc!=null?` · Google CPC $${I.cpc.toFixed(2)}`:""}`;
 const reach=`difficulty ${n0(I.difficulty)} · ${n0(I.weak_spots)} weak spots · newcomers ${pc(I.entrant_share)}`;
 const demand=`popularity ${n0(I.popularity)} (≈ ${n0(i.searches_day)} searches/day)${I.google_volume!=null?` · Google ${n0(I.google_volume)}/mo`:""}${I.video_views!=null?` · ${fmtV(I.video_views)} video views`:""}`;
 return`<div class="wfhead"><b>How “${esc(i.keyword)}” gets ${n0(i.opp)} (${esc(i.opp_grade)})</b> <span class="why">score = 100 × money<sup>0.45</sup> × reach<sup>0.35</sup> × demand<sup>0.20</sup> × concentration × buildability × trend. Orange = what each step takes away, green = adds.</span></div>
 <div class="wf">${rows}</div>
 <div class="in3"><div><span class="why">Money (weight 0.45)</span><b>${p.money.toFixed(2)}</b><span class="why">${money}</span></div><div><span class="why">Reach (weight 0.35)</span><b>${p.reach.toFixed(2)}</b><span class="why">${reach}</span></div><div><span class="why">Demand (weight 0.20)</span><b style="${p.demand<0.2?"color:#C2410C":""}">${p.demand.toFixed(2)}</b><span class="why">${demand}</span></div></div>
 <p class="why" style="margin:10px 0 0">Multiplying means one weak part cannot be bought back by the others. ${p.demand<0.2?"Here store demand is the part that holds the score down: the aspect verdict above adds off-store demand (views) and young winners, which this search-led score weighs only lightly.":""}</p>`}

function kwBody(x){const items=(x.build_now&&x.build_now.length?x.build_now:x.top_opportunities||[]).slice(0,20);const kwl=k=>`<span class="kw" onclick="openKw('${esc(k)}')">${esc(k)}</span>`;
 const row=i=>`<tr class="ksel" data-kw="${esc(i.keyword)}"><td>${kwl(i.keyword)}</td><td>${chip(i.opp_grade,i.opp)}<span class="pill ${actCls(i.opp_action)}">${esc(i.opp_action)}</span></td><td class="num">${i.band_usd!=null?"$"+n0(i.band_usd):"–"}</td><td class="num">${pc(i.p5k)}</td><td class="num">${n0(i.difficulty)}</td><td class="num">${n0(i.weak_spots)}</td><td class="num">${n0(i.searches_day)}</td><td class="num">${i.trend==null?"–":(i.trend>0?"+":"")+i.trend.toFixed(0)}</td><td class="muted" style="white-space:normal">${esc(i.opp_reason)}</td></tr>`;
 let h=`<div class="lbl">${x.build_now&&x.build_now.length?"Build now":"Best new-app searches (search-led score)"}${info(TIP.opp_score)} <span class="why" style="font-weight:400">Click a row to see how its score is built.</span></div>
 <div class="tablewrap" style="max-height:none"><table class="small" id="kwt"><thead><tr><th>Search</th><th>Score</th><th class="num">p5–p20 $/mo${info(TIP.attainable)}</th><th class="num">P(≥$5k)${info(TIP.breadth)}</th><th class="num">Difficulty${info(TIP.difficulty)}</th><th class="num">Weak spots${info(TIP.weak_spots)}</th><th class="num">Searches/day</th><th class="num">Trend</th><th>Why</th></tr></thead><tbody>${items.map(row).join("")}</tbody></table></div>
 <div id="wf" class="wfbox">${items.length?waterfall(items[0]):""}</div>`;
 if((x.unproven||[]).length)h+=`<details><summary>${x.unproven.length} searches too thin to judge (fewer than 30 apps serve them)</summary><p class="why" style="margin:6px 0">Not bad, unknown: the store has no market to measure yet. They count through the aspect verdict instead.</p><p style="font-size:13px">${x.unproven.map(i=>kwl(i.keyword)).join(" · ")}</p></details>`;
 if((x.clusters||[]).length)h+=`<details><summary>Topics (${x.clusters.length})</summary><div class="tablewrap" style="max-height:none;margin-top:8px"><table class="small"><thead><tr><th>Topic</th><th class="num">Searches</th><th>Best entry</th><th>Grade</th><th class="num">Searches/day (all)</th><th>Members</th></tr></thead><tbody>${x.clusters.map(c=>`<tr><td data-tip="${esc(c.description)}">${esc(c.label)}</td><td class="num">${c.n}</td><td>${kwl(c.best_keyword)}</td><td>${chip(c.best_grade,c.best_opp)}</td><td class="num">${n0(c.total_searches_day)}</td><td class="muted" style="white-space:normal">${c.keywords.map(esc).join(", ")}</td></tr>`).join("")}</tbody></table></div></details>`;
 h+=`<details><summary>ASO targets for an existing app (${(x.top_aso||[]).length})${info(TIP.aso_score)}</summary><div class="tablewrap" style="max-height:none;margin-top:8px"><table class="small"><thead><tr><th>Search</th><th>ASO</th><th class="num">Searches/day</th><th class="num">Weak spots</th><th>Top apps (ratings)</th></tr></thead><tbody>${(x.top_aso||[]).slice(0,20).map(i=>`<tr><td>${kwl(i.keyword)}</td><td>${n0(i.aso)}<span class="pill ${actCls(i.aso_action)}">${esc(i.aso_action)}</span></td><td class="num">${n0(i.searches_day)}</td><td class="num">${n0(i.weak_spots)}</td><td class="muted" style="white-space:normal">${(i.top_apps||[]).map(esc).join(" · ")}</td></tr>`).join("")}</tbody></table></div></details>`;
 if((x.rising||[]).length)h+=`<p style="font-size:13px"><b>Rising searches</b> ${x.rising.map(i=>`${kwl(i.keyword)} (+${i.trend.toFixed(0)})`).join(" · ")}</p>`;
 return h}

function conclusions(r,x,m){const C=m.competitors||[],V=m.voice||{},S=m.social||{},R=m.reddit||{},ads=(m.ads||[]).filter(a=>a.active_ads),P=m.prices||{},F=m.features||[];
 const views=a=>((a.tiktok||{}).views||0)+((a.youtube||{}).views||0);const totV=(S.aspects||[]).reduce((s,a)=>s+views(a),0),totS=(m.aspects||[]).reduce((s,a)=>s+(a.searches_day||0),0);
 const young=C.filter(c=>c.age_months!=null&&c.age_months<=24&&(c.est||0)>=5000);const pains=[...(V.pains||[])].sort((a,b)=>(b.strength||0)-(a.strength||0));const unmet=(V.wishes||[]).filter(w=>w.unmet);
 const low=[...F].sort((a,b)=>a.coverage-b.coverage).slice(0,3);const per=P.periods||{},sug=P.suggested||{};const items=x.top_opportunities||[];const abc=items.filter(i=>"ABC".includes(i.opp_grade||"")).length;
 const subs=[...new Set((R.aspects||[]).flatMap(a=>(a.top_subreddits||[]).map(s=>s.subreddit)))].slice(0,3);
 return{pos:(m.highlights||[]).length?`${m.highlights.map(o=>esc(o.name)).join(" · ")}. ${esc(m.highlights[0].one_liner||"")}`:"",
  voice:pains.length?`${pains.slice(0,2).map(p=>`${esc(p.pain)} (${(p.strength||0).toFixed(1)})`).join("; ")}.${unmet.length?` ${unmet.length} unmet wish${unmet.length>1?"es":""}: ${unmet.slice(0,2).map(w=>esc(w.wish)).join(", ")}.`:""}`:"",
  social:`${fmtV(totV)} views across ${(S.aspects||[]).length} aspects vs ${n0(totS)} store searches a day${totV>1e6&&totS<100?": people care, they do not search the store yet":""}.`,
  reddit:`${n0((R.aspects||[]).reduce((s,a)=>s+(a.posts_last_90d||0),0))} posts in the last 90 days${subs.length?` · ${subs.map(s=>"r/"+esc(s)).join(", ")}`:""}.`,
  ads:ads.length?`${ads.sort((a,b)=>(b.oldest_active_days||0)-(a.oldest_active_days||0)).slice(0,4).map(a=>`${esc(a.name.split(":")[0])}${a.verified?" ✓":""} (${a.oldest_active_days} days)`).join(", ")} run Meta ads now: paid users feed this market.`:`None of the ${(m.ads||[]).length} top competitors runs Meta ads we could find.`,
  prices:`${per.month?`Median $${per.month.median.toFixed(2)}/month`:""}${per.year?` and $${per.year.median.toFixed(2)}/year`:""}${per.week&&per.week.share>=.2?`; ${pc(per.week.share)} sell a weekly plan`:""}. Suggested ${sug.monthly?`$${sug.monthly.toFixed(2)}`:"–"} / ${sug.yearly?`$${sug.yearly.toFixed(2)}`:"–"}.`,
  features:low.length?`White space: ${low.map(f=>`${esc(f.name)} ${pc(f.coverage)}`).join(", ")}.`:"",
  comp:`${young.length} app${young.length!==1?"s":""} under 2 years old earn $5k+${C[0]?` · top earner ${esc(C[0].name.split(":")[0])} ${usdk(C[0].est)}`:""}.`,
  kw:`${x.found} real searches: ${abc} graded A–C on search alone${(x.unproven||[]).length?`, ${x.unproven.length} too thin to judge`:""}, the rest D/E${(x.top_aso||[]).length?` · best ASO target “${esc(x.top_aso[0].keyword)}”`:""}.`}}

function renderReport(r){const x=r.result||{},m=x.market||{};const c=conclusions(r,x,m);const V=m.voice,S=m.social,R=m.reddit,A=m.ads||[],P=m.prices,F=m.features||[],C=m.competitors||[];
 let h=verdictCard(r,m);if((m.aspects||[]).length)h+=aspectCard(m);
 let s="";
 if((m.highlights||[]).length)s+=sec("pos","Three ways to position",c.pos,[`${m.highlights.length} options`],posBody(m));
 if(V)s+=sec("voice","Voice of users",c.voice,[`${V.n_app_reviews||0} reviews`,`${V.n_reddit_posts||0} Reddit`,...(V.n_videos?[`${V.n_videos} videos`]:[])],voiceBody(V));
 if(S&&(S.aspects||[]).length)s+=sec("social","On TikTok and YouTube",c.social,[`${S.credits_used} credits`],socialBody(S,m));
 if(R)s+=sec("reddit","On Reddit",c.reddit,[`${(R.aspects||[]).reduce((n,a)=>n+(a.posts||0),0)} posts`],redditBody(R));
 if(A.length)s+=sec("ads","Who buys ads (Meta)",c.ads,[`${A.filter(a=>a.active_ads).length} advertisers`],adsBody(A));
 if(P)s+=sec("prices","Prices",c.prices,[`${P.apps_with_prices||0} apps with prices`],pricesBody(P));
 if(F.length)s+=sec("features","Feature map",c.features,[`${F.length} features`],featuresBody(F,C));
 if(C.length)s+=sec("comp","Competitors",c.comp,[`${C.length} profiled`],compBody(C));
 if(x.metadata_suggestion){const md=x.metadata_suggestion;s+=sec("meta","Suggested metadata for your app",`${esc(md.title)} · ${esc(md.subtitle)}`,[],`<div class="meta-box"><b>Title</b> (${(md.title||"").length}/30): ${esc(md.title)}<br><b>Subtitle</b> (${(md.subtitle||"").length}/30): ${esc(md.subtitle)}<br><b>Keyword field</b> (${(md.keywords||"").length}/100): ${esc(md.keywords)}<br><span class="sub">Covers: ${esc((md.covers||[]).join(", "))}. ${esc(md.why||"")}</span></div>`,true)}
 s+=sec("kw","Searches and keywords",c.kw,[`${x.found} searches`],kwBody(x));
 s+=sec("log","Run log and cost",`${esc(r.depth||"")} run · started ${esc((r.started_at||"").replace("T"," ").slice(0,16))}${m.deepseek_usd!=null?` · DeepSeek $${(m.deepseek_usd||0).toFixed(2)}`:""}${R?` · ScrapeCreators ${(R.credits_used||0)+((S||{}).credits_used||0)} credits`:""}`,[],`<pre class="sub" style="white-space:pre-wrap;margin:0">${esc((r.log||[]).join("\n"))}</pre>`);
 return h+`<section class="card secwrap">${s}</section>`}

function wireReport(el,id){el.querySelectorAll(".golink").forEach(a=>a.onclick=e=>{e.preventDefault();const d=el.querySelector("#sec-"+a.dataset.sec);if(d){d.open=true;d.scrollIntoView({behavior:"smooth",block:"start"})}});
 const RR=window._RR||{};el.querySelectorAll("#kwt tr.ksel").forEach(tr=>tr.onclick=e=>{if(e.target.classList.contains("kw"))return;const i=(RR.items||[]).find(i=>i.keyword===tr.dataset.kw);if(!i)return;el.querySelectorAll("#kwt tr.ksel").forEach(t=>t.classList.remove("ysel"));tr.classList.add("ysel");$("#wf").innerHTML=waterfall(i)});
 const b=el.querySelector("#mkverdict");if(b)b.onclick=async()=>{b.disabled=true;b.textContent="Computing…";const res=await fetch("/api/research/"+id+"/verdict",{method:"POST"});if(!res.ok){alert("Could not start: "+((await res.json()).error||res.status));b.disabled=false;return}showRun(id,true)}}

async function showRun(id,poll){clearTimeout(rpoll);const r=await(await fetch("/api/research/"+id)).json();const el=$("#rpanel");if(poll&&el.dataset.id!==id)return;el.style.display="block";const fresh=el.dataset.id!==id||!document.body.classList.contains("reading");el.dataset.id=id;
 document.body.classList.add("reading");if(fresh)scrollTo(0,0);
 let h=`<div class="rback"><button id="rexit">← Saved research</button><span class="sub">${esc(r.topic)} · full screen (Esc to go back)</span></div>`;
 h+=`<div class="rhead"><h1>${esc(r.topic)}</h1><span class="sub">${r.status==="done"?`${(r.result||{}).found||0} real searches · ${((r.result||{}).market||{}).competitors?.length||0} competitors · ${(r.aspects||[]).length} aspects · ${esc(r.depth||"")} · ${esc((r.started_at||"").slice(0,10))}${r.app?` · ${esc(r.app)}`:""}`:`${esc(r.status)} · ${esc(r.step)}`}</span></div>`;
 if(r.status==="running"||(r.status==="done"&&r.step&&r.step!=="done")){h+=`<div class="card"><div class="bar"><i style="width:${r.pct}%"></i></div><p class="sub">${(r.log||[]).slice(-4).map(esc).join("<br>")}</p></div>`;el.innerHTML=h;rpoll=setTimeout(()=>showRun(id,true),3000);loadRuns();return}
 if(r.status==="error"){el.innerHTML=h+`<pre class="sub">${esc((r.log||[]).join("\n"))}</pre>`;return}
 const x=r.result||{};window._RR={items:(x.build_now&&x.build_now.length?x.build_now:x.top_opportunities||[])};
 el.innerHTML=h+renderReport(r);wireReport(el,id);loadRuns()}

function exitReading(){document.body.classList.remove("reading");clearTimeout(rpoll);$("#rpanel").style.display="none";$("#rpanel").dataset.id="";loadRuns();scrollTo(0,0)}
document.addEventListener("click",e=>{if(e.target.id==="rexit")exitReading()});
document.addEventListener("keydown",e=>{if(e.key==="Escape"&&document.body.classList.contains("reading")&&!document.querySelector("#wkdlg[style*='block']"))exitReading()});
function openKw(k){document.body.classList.remove("reading");document.querySelector('.tabs button[data-v="opp"]').click();$("#excl").checked=true;$("#q").value=k;load().then(()=>detail(k))}
$("#rasp").onclick=suggestAspects;$("#rgo").onclick=e=>{e.preventDefault();startResearch()};$("#rtopic").addEventListener("keydown",e=>{if(e.key==="Enter")suggestAspects()});

