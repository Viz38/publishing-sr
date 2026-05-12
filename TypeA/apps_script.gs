/**
 * TITLE: Type A Publishing Pipeline
 * BRIEF STEPS: 1. Select Device, 2. Set Config, 3. Run Pipeline
 * FEATURES: Fleet management, Real-time status probing, Modern Wizard UI
 * LAST EDITED DATE: 30th Apr 2026
 */

// --- Configuration ---
const CONFIG = {
  TYPE: 'A',
  WORKER_URLS: [
    { name: '4230-TypeA-Pipeline', url: 'https://4230-type-A.ecosuyaenergies.com' },
    { name: '4990-TypeA-Pipeline', url: 'https://4990-type-A.ecosuyaenergies.com' },
    { name: 'Rajath-TypeA-Pipeline', url: 'https://rajath-type-A.ecosuyaenergies.com' },
    { name: 'Vishnu-TypeA-Pipeline', url: 'https://vishnu-typea.ecosuyaenergies.com' }
  ],
  AUTH_TOKEN: 'YOUR_SECRET_TOKEN',
  DEFAULT_MODE: 'full',
  MAX_RETRIES: 3,
  ACTIVE_WORKER_PROP: 'TYPEA_ACTIVE_WORKER'
};

/**
 * Creates custom menu
 */
function onOpen() {
  const ui = SpreadsheetApp.getUi();
  ui.createMenu('🚀 Tracxn Menu')
    .addItem('▶️ Run Pipeline', 'uiStartRun')
    .addSeparator()
    .addItem('🚦 Check Status', 'uiCheckStatus')
    .addItem('📊 Check Health', 'uiCheckHealth')
    .addSeparator()
    .addItem("🧹 Clean Sheet", "cleanSheet")
    .addItem("⚠️ Force Reset", "forceReset")
    .addToUi();
}

/**
 * Main Wizard UI
 */
function uiStartRun(resumeOnly = false) {
  const isResuming = resumeOnly === true;

  let htmlContent = '<html><head>';
  htmlContent += '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">';
  htmlContent += '<style>body{font-family:"Inter",sans-serif;background:#f8fafc;color:#1e2937;padding:20px;font-size:14px;}</style>';
  htmlContent += '</head><body>';

  htmlContent += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;padding-bottom:16px;border-bottom:1px solid #e2e8f0;">';
  htmlContent += '<h1 style="font-size:18px;font-weight:700;margin:0;">🚀 Type A Pipeline</h1>';
  htmlContent += '<div id="step-text" style="font-size:13px;font-weight:600;background:#f1f5f9;color:#64748b;padding:4px 12px;border-radius:20px;">Step 1 of 3</div>';
  htmlContent += '</div>';

  // Worker Selection View
  htmlContent += '<div id="worker-view">';
  htmlContent += '<div style="background:white;border:1px solid #e2e8f0;border-radius:12px;padding:18px;margin-bottom:20px;">';
  htmlContent += '<div style="font-size:14px;font-weight:600;color:#334155;margin-bottom:12px;">🖥️ 1. Select Worker Device</div>';
  htmlContent += '<div id="worker-list" style="max-height:240px;overflow-y:auto;display:flex;flex-direction:column;gap:8px;">';
  htmlContent += '<em style="color:#64748b;text-align:center;display:block;padding:30px 0;">Loading devices...</em>';
  htmlContent += '</div></div>';

  htmlContent += '<div style="display:flex;gap:10px;">';
  htmlContent += '<button id="fetch-btn" onclick="fetchStatuses()" style="flex:1;padding:12px;background:white;color:#475569;border:1px solid #cbd5e1;border-radius:8px;font-weight:600;cursor:pointer;">🔄 Refresh Status</button>';
  htmlContent += '<button id="next-btn" onclick="goToSetup()" disabled style="flex:1;padding:12px;background:#2563eb;color:white;border:none;border-radius:8px;font-weight:600;cursor:pointer;">Continue →</button>';
  htmlContent += '</div></div>';

  // Setup View
  htmlContent += '<div id="setup-view" style="display:none;">';
  htmlContent += '<div style="background:white;border:1px solid #e2e8f0;border-radius:12px;padding:18px;margin-bottom:20px;">';
  
  htmlContent += '<div id="selected-worker-label" style="background:#f0f9ff;color:#0369a1;padding:10px;border-radius:8px;font-weight:500;margin-bottom:16px;">✅ <span id="selected-name"></span></div>';
  
  htmlContent += '<label style="display:block;font-weight:500;font-size:13.5px;color:#475569;margin-bottom:6px;">🔢 Start Row</label>';
  htmlContent += '<input type="number" id="startRow" value="3" min="2" style="width:100%;padding:10px;border:1px solid #cbd5e1;border-radius:8px;font-size:14px;">';

  htmlContent += '<label style="display:block;font-weight:500;font-size:13.5px;color:#475569;margin:16px 0 6px 0;">⚙️ Pipeline Mode</label>';
  htmlContent += '<select id="mode" onchange="updatePhaseInfo()" style="width:100%;padding:10px;border:1px solid #cbd5e1;border-radius:8px;font-size:14px;">';
  htmlContent += '<option value="full" selected>Full Pipeline 🚀</option>';
  htmlContent += '<option value="phase1">Phase 1 — Web Scraping + LLM Prediction 🤖</option>';
  htmlContent += '<option value="phase2">Phase 2 — Tracxn Write APIs 🏛️</option>';
  htmlContent += '</select>';

  htmlContent += '<div style="margin-top:16px;">';
  htmlContent += '<label style="display:flex;align-items:center;gap:8px;font-weight:500;cursor:pointer;">';
  htmlContent += '<input type="checkbox" id="applyFormatting" checked style="width:18px;height:18px;">';
  htmlContent += '<span>Apply Formatting (Colors, Bold, etc.)</span>';
  htmlContent += '</label></div>';

  htmlContent += '<div id="phase-box" style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:10px;padding:14px;margin-top:16px;">';
  htmlContent += '<div id="phase-title" style="font-weight:600;color:#1e40af;">Full Pipeline 🚀</div>';
  htmlContent += '<div id="phase-desc" style="font-size:13px;color:#3b82f6;margin-top:4px;">Web Scraping + LLM Prediction + Tracxn Write APIs</div>';
  htmlContent += '</div></div>';

  htmlContent += '<div style="display:flex;gap:10px;margin-top:20px;">';
  htmlContent += '<button onclick="goToWorkers()" style="flex:1;padding:12px;background:white;color:#475569;border:1px solid #cbd5e1;border-radius:8px;font-weight:600;cursor:pointer;">← Back</button>';
  htmlContent += '<button onclick="runPipeline()" style="flex:2;padding:12px;background:#2563eb;color:white;border:none;border-radius:8px;font-weight:600;cursor:pointer;">▶️ Start Pipeline</button>';
  htmlContent += '</div></div>';

  // Progress View
  htmlContent += '<div id="progress-view" style="display:none;">';
  htmlContent += '<div style="background:white;border:1px solid #e2e8f0;border-radius:12px;padding:18px;">';
  htmlContent += '<div id="status-text" style="font-weight:600;margin-bottom:12px;">Connecting to worker...</div>';
  htmlContent += '<div style="height:10px;background:#e2e8f0;border-radius:10px;margin:16px 0;">';
  htmlContent += '<div id="progress-fill" style="height:100%;background:#2563eb;width:0%;border-radius:10px;"></div>';
  htmlContent += '</div>';
  htmlContent += '<div style="display:flex;justify-content:space-between;font-size:13px;font-weight:500;">';
  htmlContent += '<span id="count-text">0 / 0 rows</span>';
  htmlContent += '<span id="yield-text" style="color:#10b981;">0 Successful</span>';
  htmlContent += '</div>';
  htmlContent += '<div id="worker-info-progress" style="font-size:13px;color:#64748b;text-align:center;margin-top:12px;"></div>';
  htmlContent += '</div>';

  htmlContent += '<div id="completion-info" style="display:none;background:#f0fdf4;border:1px solid #86efac;border-radius:10px;padding:16px;text-align:center;margin-top:20px;">';
  htmlContent += '<strong style="color:#166534;">✨ Run Completed</strong>';
  htmlContent += '<div id="completion-msg" style="margin-top:8px;color:#15803d;"></div>';
  htmlContent += '</div>';

  htmlContent += '<div style="display:flex;gap:10px;margin-top:24px;">';
  htmlContent += '<button onclick="cancelRun()" style="flex:1;padding:12px;background:#fee2e2;color:#b91c1c;border:1px solid #fecaca;border-radius:8px;font-weight:600;cursor:pointer;">⏹️ Stop Pipeline</button>';
  htmlContent += '<button onclick="google.script.host.close()" style="flex:1;padding:12px;background:white;color:#475569;border:1px solid #cbd5e1;border-radius:8px;font-weight:600;cursor:pointer;">Close</button>';
  htmlContent += '</div></div>';

  htmlContent += '<script>';
  htmlContent += 'let pollTimer; let workersList = []; const isResuming = ' + isResuming + ';';

  htmlContent += 'function fitHeight(){setTimeout(()=>google.script.host.setHeight(Math.max(document.body.scrollHeight+60,380)),150);}';

  htmlContent += 'window.onload=function(){if(isResuming){showProgress();startPolling();}else{fitHeight();google.script.run.withSuccessHandler(initWorkers).getConfigWorkersForUi();}};';

  htmlContent += 'function initWorkers(data){workersList=[{url:"auto",name:"Auto-Select (Recommended)",statusHtml:"Finds idle worker"},...data.map(w=>({url:w.url,name:w.name,statusHtml:"Unknown"}))];renderWorkers();}';

  htmlContent += 'function renderWorkers(){let html="";workersList.forEach((w,i)=>{let dotColor=w.statusHtml.includes("Idle")||w.url==="auto"?"#10b981":w.statusHtml.includes("Busy")?"#f59e0b":"#94a3b8";';
  htmlContent += 'html+=`<div onclick="selectWorker(${i})" style="display:flex;align-items:center;gap:12px;padding:12px;background:#fafbfc;border:1px solid #e2e8f0;border-radius:10px;cursor:pointer;">';
  htmlContent += '<input type="radio" name="workerChoice" id="w${i}" value="${w.url}" style="margin:0;">';
  htmlContent += '<div><div style="font-weight:600;font-size:14px;">${w.name}</div><div style="font-size:12px;color:#64748b;"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${dotColor};margin-right:6px;"></span>${w.statusHtml}</div></div></div>`;});';
  htmlContent += 'document.getElementById("worker-list").innerHTML=html;fitHeight();}';

  htmlContent += 'function selectWorker(i){document.getElementById("w"+i).checked=true;document.getElementById("next-btn").disabled=false;}';

  htmlContent += 'function fetchStatuses(){const btn=document.getElementById("fetch-btn");btn.textContent="⏳ Probing...";btn.disabled=true;';
  htmlContent += 'google.script.run.withSuccessHandler((results)=>{results.forEach(res=>{const worker=workersList.find(w=>w.url===res.url);if(worker)worker.statusHtml=res.isOnline?(res.isIdle?"Idle ✅":"Busy ⚡"):"Offline";});';
  htmlContent += 'btn.textContent="🔄 Refresh Status";btn.disabled=false;renderWorkers();}).checkMultipleWorkerStatusesForUi(workersList.slice(1).map(w=>w.url));}';

  htmlContent += 'function goToSetup(){const selected=document.querySelector(\'input[name="workerChoice"]:checked\');if(!selected)return;';
  htmlContent += 'const worker=workersList.find(w=>w.url===selected.value);document.getElementById("selected-name").textContent=worker.name;';
  htmlContent += 'document.getElementById("worker-view").style.display="none";document.getElementById("setup-view").style.display="block";';
  htmlContent += 'document.getElementById("step-text").textContent="Step 2 of 3";fitHeight();}';

  htmlContent += 'function goToWorkers(){document.getElementById("setup-view").style.display="none";document.getElementById("worker-view").style.display="block";document.getElementById("step-text").textContent="Step 1 of 3";fitHeight();}';

  htmlContent += 'function updatePhaseInfo(){const mode=document.getElementById("mode").value;const t=document.getElementById("phase-title");const d=document.getElementById("phase-desc");';
  htmlContent += 'if(mode==="full"){t.textContent="Full Pipeline 🚀";d.textContent="Web Scraping + LLM Prediction + Tracxn Write APIs";}';
  htmlContent += 'else if(mode==="phase1"){t.textContent="Phase 1 — Web Scraping + LLM Prediction 🤖";d.textContent="Scrapes websites and runs LLM predictions.";}';
  htmlContent += 'else{t.textContent="Phase 2 — Tracxn Write APIs 🏛️";d.textContent="Writes enriched data to Tracxn using APIs.";} fitHeight();}';

  htmlContent += 'function runPipeline(){';
  htmlContent += '  const startRow = parseInt(document.getElementById("startRow").value)||3;';
  htmlContent += '  const mode = document.getElementById("mode").value;';
  htmlContent += '  const applyFormatting = document.getElementById("applyFormatting").checked;';
  htmlContent += '  const selectedUrl = document.querySelector(\'input[name="workerChoice"]:checked\').value;';
  htmlContent += '  const workerUrl = selectedUrl==="auto"?null:selectedUrl;';
  htmlContent += '  showProgress();';
  htmlContent += '  google.script.run.withSuccessHandler(startPolling).executeRunFromUi(startRow, mode, workerUrl, applyFormatting);';
  htmlContent += '}';

  htmlContent += 'function showProgress(){document.getElementById("worker-view").style.display="none";document.getElementById("setup-view").style.display="none";';
  htmlContent += 'document.getElementById("progress-view").style.display="block";document.getElementById("step-text").textContent="Running...";fitHeight();}';

  htmlContent += 'function startPolling(){pollProgress(); pollTimer=setInterval(pollProgress,4000);}';
  htmlContent += 'function pollProgress(){google.script.run.withSuccessHandler(updateProgressUI).getStatusJson();}';

  htmlContent += 'function updateProgressUI(s){if(!s)return;const pct=s.progress_total>0?Math.round((s.progress_current/s.progress_total)*100):0;';
  htmlContent += 'document.getElementById("progress-fill").style.width=pct+"%";';
  htmlContent += 'document.getElementById("status-text").textContent=s.active?"Processing rows...":"Pipeline Idle";';
  htmlContent += 'document.getElementById("count-text").textContent=s.progress_current+" / "+s.progress_total+" rows";';
  htmlContent += 'document.getElementById("yield-text").textContent=s.progress_success+" Successful";';
  htmlContent += 'document.getElementById("worker-info-progress").innerHTML="Worker: <strong>"+(s.workerName||"Unknown")+"</strong>";';
  htmlContent += 'if(!s.active&&s.status!=="running"&&s.status!=="idle"){clearInterval(pollTimer);';
  htmlContent += 'document.getElementById("completion-info").style.display="block";';
  htmlContent += 'document.getElementById("completion-msg").textContent=s.status||"Pipeline finished successfully.";fitHeight();}}';

  htmlContent += 'function cancelRun(){google.script.run.executeCancelFromUi();google.script.host.close();}';
  htmlContent += '</script></body></html>';

  const html = HtmlService.createHtmlOutput(htmlContent)
    .setWidth(400)
    .setHeight(580)
    .setTitle('Type A Pipeline');

  SpreadsheetApp.getUi().showModelessDialog(html, 'Type A Pipeline');
}

/**
 * IMPROVED Check Health Screen
 */
function uiCheckHealth() {
  let htmlContent = `<html>
    <head>
      <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
      <style>
        body { font-family: 'Inter', sans-serif; background: #f8fafc; color: #1e2937; padding: 20px; }
        .health-card {
          background: white;
          border: 1px solid #e2e8f0;
          border-radius: 12px;
          padding: 16px;
          display: flex;
          align-items: center;
          gap: 16px;
          margin-bottom: 12px;
        }
      </style>
    </head>
    <body>
      <div style="margin-bottom:24px;">
        <h1 style="font-size:18px;font-weight:700;margin:0;">📊 Fleet Health Status</h1>
        <p style="color:#64748b;margin-top:4px;">Real-time status of all Type A workers</p>
      </div>

      <div id="health-list" style="display:flex;flex-direction:column;gap:12px;">
        <div style="text-align:center;padding:40px;color:#64748b;">Checking health of all workers...</div>
      </div>

      <div style="margin-top:24px;text-align:center;">
        <button onclick="refreshHealth()" style="padding:12px 24px;background:#2563eb;color:white;border:none;border-radius:8px;font-weight:600;cursor:pointer;">🔄 Refresh Health</button>
      </div>

      <script>
        function refreshHealth() {
          const container = document.getElementById("health-list");
          container.innerHTML = '<div style="text-align:center;padding:40px;color:#64748b;">Checking...</div>';
          google.script.run.withSuccessHandler(showHealthResults).uiCheckHealthServer();
        }

        function showHealthResults(results) {
          let html = "";
          results.forEach(r => {
            const dotColor = r.isOnline ? "#10b981" : "#ef4444";
            const statusText = r.isOnline ? "Online" : "Offline";
            const emoji = r.isOnline ? "✅" : "❌";

            html += \`
              <div class="health-card">
                <div style="width:12px;height:12px;border-radius:50%;background:\${dotColor};flex-shrink:0;"></div>
                <div style="flex:1;">
                  <div style="font-weight:600;font-size:15px;">\${r.name}</div>
                  <div style="color:#64748b;font-size:13px;">\${statusText}</div>
                </div>
                <div style="font-size:24px;">\${emoji}</div>
              </div>
            \`;
          });
          document.getElementById("health-list").innerHTML = html;
        }

        window.onload = refreshHealth;
      </script>
    </body>
  </html>`;

  const html = HtmlService.createHtmlOutput(htmlContent)
    .setWidth(420)
    .setHeight(520)
    .setTitle('Fleet Health');

  SpreadsheetApp.getUi().showModelessDialog(html, '📊 Fleet Health');
}

// ====================== SERVER SIDE FUNCTIONS ======================

function getConfigWorkersForUi() { 
  return CONFIG.WORKER_URLS; 
}

function checkMultipleWorkerStatusesForUi(urls) {
  return urls.map(url => {
    try {
      const res = UrlFetchApp.fetch(url + '/typea/status', {
        headers: { 'Authorization': 'Bearer ' + CONFIG.AUTH_TOKEN },
        muteHttpExceptions: true
      });
      if (res.getResponseCode() === 200) {
        const s = JSON.parse(res.getContentText());
        return { url: url, isOnline: true, isIdle: !s.active };
      }
    } catch(e) {}
    return { url: url, isOnline: false };
  });
}

function executeRunFromUi(startRow, mode, workerUrl, applyFormatting = true) {
  const url = workerUrl || findIdleWorker_();
  const worker = CONFIG.WORKER_URLS.find(w => w.url === url);
  
  PropertiesService.getDocumentProperties().setProperty(CONFIG.ACTIVE_WORKER_PROP, url);
  PropertiesService.getDocumentProperties().setProperty(CONFIG.ACTIVE_WORKER_PROP + '_NAME', worker ? worker.name : 'Unknown');

  return UrlFetchApp.fetch(url + '/typea/start', {
    method: 'post',
    contentType: 'application/json',
    headers: { 'Authorization': 'Bearer ' + CONFIG.AUTH_TOKEN },
    payload: JSON.stringify({ 
      start_row: startRow, 
      mode: mode, 
      sheet_id: SpreadsheetApp.getActiveSpreadsheet().getId(),
      apply_formatting: applyFormatting
    })
  });
}

function findIdleWorker_() {
  for (const w of CONFIG.WORKER_URLS) {
    try {
      const res = UrlFetchApp.fetch(w.url + '/typea/status', {
        headers: { 'Authorization': 'Bearer ' + CONFIG.AUTH_TOKEN },
        muteHttpExceptions: true
      });
      if (res.getResponseCode() === 200 && !JSON.parse(res.getContentText()).active) {
        return w.url;
      }
    } catch(e) {}
  }
  throw new Error('All workers are busy or offline.');
}

function getStatusJson() {
  const url = PropertiesService.getDocumentProperties().getProperty(CONFIG.ACTIVE_WORKER_PROP);
  if (!url) return { active: false, status: 'idle' };

  try {
    const res = UrlFetchApp.fetch(url + '/typea/status', {
      headers: { 'Authorization': 'Bearer ' + CONFIG.AUTH_TOKEN },
      muteHttpExceptions: true
    });
    if (res.getResponseCode() !== 200) {
        return { active: true, status: 'running', progress_current: 0, progress_total: 0, progress_success: 0, workerName: "Tunnel Reconnecting..." };
    }
    const s = JSON.parse(res.getContentText());
    s.workerName = PropertiesService.getDocumentProperties().getProperty(CONFIG.ACTIVE_WORKER_PROP + '_NAME');
    return s;
  } catch(e) { 
    // Ignore temporary network/tunnel drops instead of killing the UI
    return { active: true, status: 'running', progress_current: 0, progress_total: 0, progress_success: 0, workerName: "Network Blip - Waiting..." }; 
  }
}

function executeCancelFromUi() {
  const url = PropertiesService.getDocumentProperties().getProperty(CONFIG.ACTIVE_WORKER_PROP);
  if (url) {
    UrlFetchApp.fetch(url + '/typea/cancel', { 
      method: 'post', 
      headers: { 'Authorization': 'Bearer ' + CONFIG.AUTH_TOKEN } 
    });
  }
}

function uiCheckStatus() { 
  uiStartRun(true); 
}

function uiCheckHealthServer() {
  return CONFIG.WORKER_URLS.map(w => {
    try {
      const res = UrlFetchApp.fetch(w.url + '/typea/health', { 
        muteHttpExceptions: true,
        headers: { 'Authorization': 'Bearer ' + CONFIG.AUTH_TOKEN }
      });
      return { 
        name: w.name, 
        isOnline: res.getResponseCode() === 200 
      };
    } catch(e) { 
      return { name: w.name, isOnline: false }; 
    }
  });
}

function cleanSheet() {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  const lastRow = sheet.getLastRow();
  if (lastRow >= 3) sheet.deleteRows(3, lastRow - 2);
  SpreadsheetApp.getActiveSpreadsheet().toast("🧹 Sheet cleaned successfully.");
}

function forceReset() {
  PropertiesService.getDocumentProperties().deleteAllProperties();
  SpreadsheetApp.getActiveSpreadsheet().toast("✅ All cache cleared.");
}