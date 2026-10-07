/**
 * TITLE: TechCrawler Publishing Pipeline
 * BRIEF STEPS: 1. Select Device, 2. Set Config, 3. Run Pipeline
 * FEATURES: Fleet management, Real-time status probing, Modern Wizard UI
 * LAST EDITED DATE: 7th Oct 2026
 */

// --- Configuration ---
const CONFIG = {
  TYPE: 'TechCrawler',
  WORKER_URLS: [
    { name: '4230-TechCrawler-Pipeline', url: 'https://4230-techcrwler.ecosuyaenergies.com' },
    { name: '4990-TechCrawler-Pipeline', url: 'https://4990-techcrwler.ecosuyaenergies.com' },
    { name: 'Rajath-TechCrawler-Pipeline', url: 'https://rajath-techcrwler.ecosuyaenergies.com' },
    { name: 'Vishnu-TechCrawler-Pipeline', url: 'https://vishnu-techcrwler.ecosuyaenergies.com' },
    { name: 'Keshav-TechCrawler-Pipeline', url: 'https://keshava-techcrwler.ecosuyaenergies.com' }
  ],
  AUTH_TOKEN: 'Tracxn@SR',
  DEFAULT_MODE: 'full',
  MAX_RETRIES: 3,
  ACTIVE_WORKER_PROP: 'TECHCRWLER_ACTIVE_WORKER'
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
  htmlContent += '<h1 style="font-size:18px;font-weight:700;margin:0;">🚀 TechCrawler Pipeline</h1>';
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

  htmlContent += '<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:14px;margin-top:16px;">';
  htmlContent += '<div style="font-weight:600;color:#334155;font-size:13px;">🚀 TechCrawler AI Extraction</div>';
  htmlContent += '<div style="font-size:12.5px;color:#64748b;margin-top:4px;">Crawls domains and extracts tech stack, subpages, and metadata into Google Sheets.</div>';
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
  htmlContent += '<button id="stop-btn" onclick="cancelRun()" style="flex:1;padding:12px;background:#fee2e2;color:#b91c1c;border:1px solid #fecaca;border-radius:8px;font-weight:600;cursor:pointer;">⏹️ Stop Pipeline</button>';
  htmlContent += '<button onclick="google.script.host.close()" style="flex:1;padding:12px;background:white;color:#475569;border:1px solid #cbd5e1;border-radius:8px;font-weight:600;cursor:pointer;">Close</button>';
  htmlContent += '</div></div>';

  htmlContent += '<script>';
  htmlContent += 'let pollTimer; let isStopping = false; let workersList = []; const isResuming = ' + isResuming + ';';

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

  htmlContent += 'function runPipeline(){';
  htmlContent += '  const startRow = parseInt(document.getElementById("startRow").value)||3;';
  htmlContent += '  const selectedUrl = document.querySelector(\'input[name="workerChoice"]:checked\').value;';
  htmlContent += '  const workerUrl = selectedUrl==="auto"?null:selectedUrl;';
  htmlContent += '  showProgress();';
  htmlContent += '  google.script.run.withSuccessHandler(startPolling).executeRunFromUi(startRow, workerUrl);';
  htmlContent += '}';

  htmlContent += 'function showProgress(){document.getElementById("worker-view").style.display="none";document.getElementById("setup-view").style.display="none";';
  htmlContent += 'document.getElementById("progress-view").style.display="block";document.getElementById("step-text").textContent="Running...";fitHeight();}';

  htmlContent += 'function startPolling(){pollProgress(); pollTimer=setInterval(pollProgress,4000);}';
  htmlContent += 'function pollProgress(){google.script.run.withSuccessHandler(updateProgressUI).getStatusJson();}';

  htmlContent += 'function updateProgressUI(s){';
  htmlContent += '  if(!s) return;';
  htmlContent += '  const pct = s.progress_total > 0 ? Math.round((s.progress_current / s.progress_total) * 100) : 0;';
  htmlContent += '  const pFill = document.getElementById("progress-fill");';
  htmlContent += '  const sText = document.getElementById("status-text");';
  htmlContent += '  const stepText = document.getElementById("step-text");';
  htmlContent += '  const stopBtn = document.getElementById("stop-btn");';
  htmlContent += '  const isFinished = (!s.active && (s.status === "succeeded" || s.status === "stopped" || s.status === "failed" || (s.status === "idle" && s.progress_total > 0 && s.progress_current >= s.progress_total)));';
  htmlContent += '  if(s.is_stopping || s.status === "stopping"){';
  htmlContent += '    isStopping = true;';
  htmlContent += '    if(stopBtn && stopBtn.textContent !== "⚡ Force Killing..."){';
  htmlContent += '      stopBtn.textContent = "⏹️ Stopping (Click for Force Kill)";';
  htmlContent += '    }';
  htmlContent += '    stepText.textContent = "Stopping...";';
  htmlContent += '    stepText.style.background = "#fef3c7";';
  htmlContent += '    stepText.style.color = "#92400e";';
  htmlContent += '    sText.innerHTML = "⏳ <strong>Stopping pipeline gracefully...</strong><br><span style=\\"font-size:12px;color:#b45309;\\">Saving progress to sheet...</span>";';
  htmlContent += '    pFill.style.background = "#f59e0b";';
  htmlContent += '    pFill.style.width = pct + "%";';
  htmlContent += '  } else if(isFinished){';
  htmlContent += '    clearInterval(pollTimer);';
  htmlContent += '    if(stopBtn) stopBtn.disabled = true;';
  htmlContent += '    document.getElementById("completion-info").style.display = "block";';
  htmlContent += '    const compMsg = document.getElementById("completion-msg");';
  htmlContent += '    if(s.status === "stopped"){';
  htmlContent += '      stepText.textContent = "Stopped";';
  htmlContent += '      stepText.style.background = "#fee2e2";';
  htmlContent += '      stepText.style.color = "#b91c1c";';
  htmlContent += '      sText.textContent = "Pipeline Stopped";';
  htmlContent += '      pFill.style.background = "#f59e0b";';
  htmlContent += '      pFill.style.width = pct + "%";';
  htmlContent += '      compMsg.innerHTML = "⏹️ <strong>Pipeline stopped cleanly.</strong><br>All extracted data saved to sheet.";';
  htmlContent += '    } else if(s.status === "failed"){';
  htmlContent += '      stepText.textContent = "Failed";';
  htmlContent += '      stepText.style.background = "#fee2e2";';
  htmlContent += '      stepText.style.color = "#b91c1c";';
  htmlContent += '      sText.textContent = "Pipeline Failed";';
  htmlContent += '      pFill.style.background = "#ef4444";';
  htmlContent += '      pFill.style.width = pct + "%";';
  htmlContent += '      compMsg.textContent = "Pipeline encountered an error. Check server logs.";';
  htmlContent += '    } else {';
  htmlContent += '      stepText.textContent = "Completed";';
  htmlContent += '      stepText.style.background = "#dcfce7";';
  htmlContent += '      stepText.style.color = "#15803d";';
  htmlContent += '      sText.textContent = "Pipeline Completed";';
  htmlContent += '      pFill.style.background = "#10b981";';
  htmlContent += '      pFill.style.width = "100%";';
  htmlContent += '      compMsg.textContent = "Pipeline completed successfully. All data written to sheet.";';
  htmlContent += '    }';
  htmlContent += '  } else {';
  htmlContent += '    pFill.style.background = "#2563eb";';
  htmlContent += '    pFill.style.width = pct + "%";';
  htmlContent += '    if(s.active){';
  htmlContent += '      stepText.textContent = "Running...";';
  htmlContent += '      stepText.style.background = "#eff6ff";';
  htmlContent += '      stepText.style.color = "#1d4ed8";';
  htmlContent += '      sText.textContent = "Processing rows...";';
  htmlContent += '    } else {';
  htmlContent += '      stepText.textContent = "Idle";';
  htmlContent += '      stepText.style.background = "#f1f5f9";';
  htmlContent += '      stepText.style.color = "#64748b";';
  htmlContent += '      sText.textContent = "Pipeline Idle";';
  htmlContent += '    }';
  htmlContent += '  }';
  htmlContent += '  document.getElementById("count-text").textContent = s.progress_current + " / " + s.progress_total + " rows";';
  htmlContent += '  document.getElementById("yield-text").textContent = s.progress_success + " Successful";';
  htmlContent += '  document.getElementById("worker-info-progress").innerHTML = "Worker: <strong>" + (s.workerName || "Unknown") + "</strong>";';
  htmlContent += '  fitHeight();';
  htmlContent += '}';

  htmlContent += 'function cancelRun(){';
  htmlContent += '  const btn = document.getElementById("stop-btn");';
  htmlContent += '  const sText = document.getElementById("status-text");';
  htmlContent += '  if(!isStopping){';
  htmlContent += '    isStopping = true;';
  htmlContent += '    if(btn){ btn.textContent = "⏹️ Stopping (Click for Force Kill)"; btn.style.background = "#fef2f2"; }';
  htmlContent += '    if(sText){ sText.innerHTML = "⏳ <strong>Stopping pipeline gracefully...</strong><br><span style=\\"font-size:12px;color:#64748b;\\">Halting crawler and saving to sheet...</span>"; }';
  htmlContent += '    google.script.run.withSuccessHandler(function(){ pollProgress(); }).executeCancelFromUi();';
  htmlContent += '  } else {';
  htmlContent += '    if(btn){ btn.textContent = "⚡ Force Killing..."; btn.disabled = true; }';
  htmlContent += '    google.script.run.withSuccessHandler(function(){ setTimeout(() => google.script.host.close(), 1000); }).executeCancelFromUi();';
  htmlContent += '  }';
  htmlContent += '  fitHeight();';
  htmlContent += '}';
  htmlContent += '</script></body></html>';

  const html = HtmlService.createHtmlOutput(htmlContent)
    .setWidth(400)
    .setHeight(580)
    .setTitle('TechCrawler Pipeline');

  SpreadsheetApp.getUi().showModelessDialog(html, 'TechCrawler Pipeline');
}

/**
 * Check Health Screen for TechCrawler Fleet
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
        <h1 style="font-size:18px;font-weight:700;margin:0;">📊 TechCrawler Fleet Health Status</h1>
        <p style="color:#64748b;margin-top:4px;">Real-time status of all TechCrawler workers</p>
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
    .setTitle('TechCrawler Fleet Health');

  SpreadsheetApp.getUi().showModelessDialog(html, '📊 TechCrawler Fleet Health');
}

// ====================== SERVER SIDE FUNCTIONS ======================

function getConfigWorkersForUi() { 
  return CONFIG.WORKER_URLS; 
}

function checkMultipleWorkerStatusesForUi(urls) {
  return urls.map(url => {
    try {
      const res = UrlFetchApp.fetch(url + '/techcrwler/status', {
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

function executeRunFromUi(startRow, workerUrl) {
  const url = workerUrl || findIdleWorker_();
  const worker = CONFIG.WORKER_URLS.find(w => w.url === url);
  
  PropertiesService.getDocumentProperties().setProperty(CONFIG.ACTIVE_WORKER_PROP, url);
  PropertiesService.getDocumentProperties().setProperty(CONFIG.ACTIVE_WORKER_PROP + '_NAME', worker ? worker.name : 'Unknown');

  return UrlFetchApp.fetch(url + '/techcrwler/start', {
    method: 'post',
    contentType: 'application/json',
    headers: { 'Authorization': 'Bearer ' + CONFIG.AUTH_TOKEN },
    payload: JSON.stringify({ 
      start_row: startRow, 
      mode: 'full', 
      sheet_id: SpreadsheetApp.getActiveSpreadsheet().getId()
    })
  });
}

function findIdleWorker_() {
  for (const w of CONFIG.WORKER_URLS) {
    try {
      const res = UrlFetchApp.fetch(w.url + '/techcrwler/status', {
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
    const res = UrlFetchApp.fetch(url + '/techcrwler/status', {
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
    return { active: true, status: 'running', progress_current: 0, progress_total: 0, progress_success: 0, workerName: "Network Blip - Waiting..." }; 
  }
}

function executeCancelFromUi() {
  const url = PropertiesService.getDocumentProperties().getProperty(CONFIG.ACTIVE_WORKER_PROP);
  if (url) {
    try {
      const res = UrlFetchApp.fetch(url + '/techcrwler/cancel', { 
        method: 'post', 
        headers: { 'Authorization': 'Bearer ' + CONFIG.AUTH_TOKEN },
        muteHttpExceptions: true
      });
      return JSON.parse(res.getContentText());
    } catch(e) {
      return { status: "error", message: e.toString() };
    }
  }
  return { status: "no_worker" };
}

function uiCheckStatus() { 
  uiStartRun(true); 
}

function uiCheckHealthServer() {
  return CONFIG.WORKER_URLS.map(w => {
    try {
      const res = UrlFetchApp.fetch(w.url + '/techcrwler/health', { 
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
