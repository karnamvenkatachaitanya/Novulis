"use client";

import React, { useState, useEffect, useRef } from "react";

const DEFAULT_PATHS = [
  "/dashboard/my-applications",
  "/dashboard/facilities",
  "/dashboard/action-items",
  "/dashboard/user-management",
  "/dashboard/announcements",
  "/dashboard/settings",
  "/dashboard/faqs",
  "/dashboard/tickets",
  "/dashboard/contact",
];

export default function Home() {
  const [selectedPaths, setSelectedPaths] = useState(DEFAULT_PATHS);
  const [status, setStatus] = useState("idle"); // idle | running | success | error
  const [logs, setLogs] = useState([]);
  const [activeRoute, setActiveRoute] = useState("");
  const [activeScreenshot, setActiveScreenshot] = useState("");
  const [latestReport, setLatestReport] = useState(null);
  const [autoScroll, setAutoScroll] = useState(true);
  
  // Login simulator states
  const [simEmail, setSimEmail] = useState("");
  const [simPassword, setSimPassword] = useState("");
  const [simStep, setSimStep] = useState("idle"); // idle | typing-email | typing-pass | submitting | dashboard

  // Email states
  const [emailStatus, setEmailStatus] = useState("idle"); // idle | sending | success | error
  const [emailMessage, setEmailMessage] = useState("");
  const terminalEndRef = useRef(null);
  const terminalContainerRef = useRef(null);

  // UI Improvements: Dark mode & Workflow step states
  const [theme, setTheme] = useState("dark");
  const [workflowStep, setWorkflowStep] = useState(0);

  const WORKFLOW_STEPS = [
    { id: 1, name: "Initialize Sweep", desc: "Validate target scopes" },
    { id: 2, name: "Playwright Crawler", desc: "Simulate Login & Scrape" },
    { id: 3, name: "pgvector Retrieval", desc: "Query Supabase RPC matches" },
    { id: 4, name: "LLM Compliance Judge", desc: "Verify layout elements" },
    { id: 5, name: "PDF Summary Compiler", desc: "Render ReportLab PDF" },
    { id: 6, name: "SMTP Email Alert", desc: "Dispatch unified PDF" },
  ];

  // Theme configuration on mount
  useEffect(() => {
    const savedTheme = localStorage.getItem("theme") || "dark";
    setTheme(savedTheme);
    if (savedTheme === "dark") {
      document.documentElement.classList.add("dark");
    } else {
      document.documentElement.classList.remove("dark");
    }
  }, []);

  const toggleTheme = () => {
    const nextTheme = theme === "dark" ? "light" : "dark";
    setTheme(nextTheme);
    localStorage.setItem("theme", nextTheme);
    if (nextTheme === "dark") {
      document.documentElement.classList.add("dark");
    } else {
      document.documentElement.classList.remove("dark");
    }
  };

  // Determine operational workflow steps dynamically from logs
  useEffect(() => {
    if (status !== "running") {
      if (status === "success") {
        setWorkflowStep(6);
      } else {
        setWorkflowStep(0);
      }
      return;
    }
    if (logs.length === 0) return;
    const lastLog = logs[logs.length - 1];

    if (lastLog.includes("Initializing compliance agent") || lastLog.includes("Connecting to local workspace")) {
      setWorkflowStep(1);
    } else if (lastLog.includes("Scraping target page") || lastLog.includes("Playwright") || lastLog.includes("login")) {
      setWorkflowStep(2);
    } else if (lastLog.includes("Retrieved") || lastLog.includes("guidelines") || lastLog.includes("match_guidelines")) {
      setWorkflowStep(3);
    } else if (lastLog.includes("LLM compliance judge") || lastLog.includes("Routing audit") || lastLog.includes("run_compliance_check")) {
      setWorkflowStep(4);
    } else if (lastLog.includes("Compiled single unified PDF report") || lastLog.includes("coverage-report")) {
      setWorkflowStep(5);
    } else if (lastLog.includes("SMTP alert") || lastLog.includes("email")) {
      setWorkflowStep(6);
    }
  }, [logs, status]);

  // Handle manual terminal scrolling
  const handleTerminalScroll = (e) => {
    const container = e.currentTarget;
    const isAtBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 15;
    if (!isAtBottom) {
      setAutoScroll(false);
    } else {
      setAutoScroll(true);
    }
  };

  // Fetch latest report from filesystem on load
  const loadLatestReport = async () => {
    try {
      const res = await fetch("/api/latest-report");
      const data = await res.json();
      if (data.success) {
        setLatestReport(data);
      }
    } catch (err) {
      console.error("Failed to load latest report:", err);
    }
  };

  useEffect(() => {
    loadLatestReport();
  }, []);

  // Auto-scroll logs
  useEffect(() => {
    if (autoScroll && terminalContainerRef.current) {
      terminalContainerRef.current.scrollTop = terminalContainerRef.current.scrollHeight;
    }
  }, [logs, autoScroll]);

  // Login Typing Simulator
  useEffect(() => {
    if (status !== "running") {
      setSimStep("idle");
      setSimEmail("");
      setSimPassword("");
      return;
    }

    setSimStep("typing-email");
    const email = "admin@gmail.com";
    const pass = "password";
    let emailIdx = 0;
    let passIdx = 0;

    // Type email
    const emailInterval = setInterval(() => {
      if (emailIdx < email.length) {
        setSimEmail((prev) => prev + email[emailIdx]);
        emailIdx++;
      } else {
        clearInterval(emailInterval);
        setSimStep("typing-pass");
        
        // Type password
        const passInterval = setInterval(() => {
          if (passIdx < pass.length) {
            setSimPassword((prev) => prev + pass[passIdx]);
            passIdx++;
          } else {
            clearInterval(passInterval);
            setSimStep("submitting");
            
            // Redirect to dashboard
            setTimeout(() => {
              setSimStep("dashboard");
            }, 1500);
          }
        }, 100);
      }
    }, 80);

    return () => {
      clearInterval(emailInterval);
    };
  }, [status]);

  // Parse logs to detect active scraping route and screenshot changes
  useEffect(() => {
    if (logs.length === 0) return;
    const lastLog = logs[logs.length - 1];

    // Detect active scraping target
    // Format: 2026-06-25 12:57:02,657 INFO main - Starting audit for /dashboard/faqs
    const routeMatch = lastLog.match(/Starting audit for\s+(\/dashboard\/[\w-]+)/);
    if (routeMatch) {
      setActiveRoute(routeMatch[1]);
      setActiveScreenshot(""); // clear previous until new is saved
    }

    // Detect saved page capture screenshot
    // Format: 2026-06-25 12:57:09,315 INFO scraper - Saved page capture: C:\...\dashboard_faqs-20260625-125709.json
    const screenshotMatch = lastLog.match(/Saved page capture:.*\\(dashboard_[\w-]+-[\d-]+-[\d-]+)\.json/);
    if (screenshotMatch) {
      const baseName = screenshotMatch[1]; // e.g. dashboard_faqs-20260625-125709
      setActiveScreenshot(`captured_states/${baseName}.png`);
    }
  }, [logs]);

  // Execute Sweep command
  const startSweep = () => {
    setStatus("running");
    setLogs(["[SYSTEM] Initializing compliance agent orchestrator...", "[SYSTEM] Connecting to local workspace child process..."]);
    setActiveRoute("");
    setActiveScreenshot("");

    const pathsParam = selectedPaths.join(",");
    const eventSource = new EventSource(`/api/run-agent?paths=${encodeURIComponent(pathsParam)}`);

    eventSource.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      if (msg.type === "log") {
        setLogs((prev) => [...prev, msg.data]);
      } else if (msg.type === "done") {
        eventSource.close();
        setStatus(msg.code === 0 || msg.code === 2 ? "success" : "error");
        setLogs((prev) => [...prev, `[SYSTEM] Process finished with exit code: ${msg.code}`]);
        loadLatestReport();
      } else if (msg.type === "error") {
        eventSource.close();
        setStatus("error");
        setLogs((prev) => [...prev, `[SYSTEM] Process error: ${msg.error}`]);
      }
    };

    eventSource.onerror = (err) => {
      console.error("SSE Connection Failed:", err);
      eventSource.close();
      setStatus("error");
      setLogs((prev) => [...prev, "[SYSTEM] Connection to log server failed unexpectedly."]);
    };
  };

  const handlePathToggle = (path) => {
    if (selectedPaths.includes(path)) {
      setSelectedPaths(selectedPaths.filter((p) => p !== path));
    } else {
      setSelectedPaths([...selectedPaths, path]);
    }
  };

  const triggerEmailAlert = async () => {
    if (!latestReport) return;
    setEmailStatus("sending");
    setEmailMessage("");

    try {
      const res = await fetch("/api/send-email", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ runId: latestReport.runId }),
      });
      const data = await res.json();
      if (data.success) {
        setEmailStatus("success");
        setEmailMessage(`Email sent successfully to ${data.recipient || "stakeholders"}!`);
      } else {
        setEmailStatus("error");
        setEmailMessage(data.error || "Failed to dispatch email.");
      }
    } catch (err) {
      setEmailStatus("error");
      setEmailMessage(err.message || "Network error sending email.");
    }
  };

  return (
    <div className="min-h-screen p-6 flex flex-col justify-between bg-slate-50 dark:bg-[#0B0F19] text-slate-800 dark:text-slate-100 transition-colors duration-300">
      {/* 1. Header */}
      <header className="flex justify-between items-center mb-6 border-b border-slate-200 dark:border-slate-800 pb-4">
        <div>
          <h1 className="text-3xl font-bold tracking-tight bg-gradient-to-r from-indigo-500 to-cyan-500 dark:from-indigo-400 dark:to-cyan-400 bg-clip-text text-transparent">
            WaiverPro Compliance Dashboard
          </h1>
          <p className="text-sm text-slate-500 dark:text-gray-400 mt-1">Real-Time Autonomous QA Agent Controller</p>
        </div>
        <div className="flex gap-4 items-center">
          {/* Light/Dark Toggle Button */}
          <button
            onClick={toggleTheme}
            className="p-2 rounded-lg bg-slate-200/80 hover:bg-slate-300/80 dark:bg-slate-800/80 dark:hover:bg-slate-700/80 border border-slate-300 dark:border-slate-700 transition-all cursor-pointer shadow-sm text-sm"
            title="Toggle theme mode"
          >
            {theme === "dark" ? "☀️ Light Mode" : "🌙 Dark Mode"}
          </button>
          <span className="text-xs text-slate-500 dark:text-gray-500 font-mono">Agent Status:</span>
          {status === "idle" && <span className="badge badge-low">Idle</span>}
          {status === "running" && <span className="badge badge-high animate-pulse">Running Sweeps</span>}
          {status === "success" && <span className="badge badge-low">Audit Complete</span>}
          {status === "error" && <span className="badge badge-critical">Sweep Error</span>}
        </div>
      </header>

      {/* 2. Visual Workflow Progress Indicator */}
      <section className="glass-panel p-6 mb-6">
        <h3 className="text-xs font-bold tracking-wider text-slate-500 dark:text-gray-400 font-mono uppercase mb-4">
          Agent Operational Workflow Sequence
        </h3>
        <div className="grid grid-cols-1 md:grid-cols-6 gap-4">
          {WORKFLOW_STEPS.map((step) => {
            const isCompleted = workflowStep > step.id;
            const isActive = workflowStep === step.id;
            return (
              <div 
                key={step.id} 
                className={`p-3 rounded-lg border transition-all duration-300 ${
                  isActive 
                    ? "bg-indigo-500/10 border-indigo-500 shadow-md shadow-indigo-500/10 scale-[1.03]" 
                    : isCompleted 
                      ? "bg-emerald-500/5 dark:bg-emerald-500/5 border-emerald-500/30 opacity-90" 
                      : "bg-slate-100/50 dark:bg-slate-900/40 border-slate-200 dark:border-slate-800 opacity-60"
                }`}
              >
                <div className="flex items-center gap-2 mb-1">
                  <span className={`w-5 h-5 rounded-full text-[10px] font-bold flex items-center justify-center border ${
                    isActive 
                      ? "bg-indigo-600 text-white border-indigo-500 animate-pulse" 
                      : isCompleted 
                        ? "bg-emerald-600 text-white border-emerald-500" 
                        : "bg-slate-200 dark:bg-slate-800 text-slate-600 dark:text-slate-400 border-slate-350 dark:border-slate-700"
                  }`}>
                    {isCompleted ? "✓" : step.id}
                  </span>
                  <span className={`text-xs font-semibold ${isActive ? "text-indigo-600 dark:text-indigo-400" : isCompleted ? "text-emerald-600 dark:text-emerald-400" : "text-slate-700 dark:text-slate-300"}`}>
                    {step.name}
                  </span>
                </div>
                <p className="text-[10px] text-slate-500 dark:text-gray-400 leading-tight">
                  {step.desc}
                </p>
              </div>
            );
          })}
        </div>
      </section>
      <main className="flex-1 grid grid-cols-1 xl:grid-cols-3 gap-6 mb-8">
        
        {/* Left Column: Controls & Active Logs */}
        <section className="flex flex-col gap-6">
          {/* Action Control Panel */}
          <div className="glass-panel p-6 flex flex-col gap-4">
            <h2 className="text-lg font-semibold text-indigo-600 dark:text-indigo-400">Agent Sweeper Configuration</h2>
            
            <div className="flex flex-col gap-2">
              <span className="text-xs font-mono text-slate-500 dark:text-gray-400">Target Scopes:</span>
              <div className="grid grid-cols-1 gap-2 bg-slate-100/80 dark:bg-slate-900/60 p-3 rounded-lg border border-slate-200 dark:border-slate-800/80">
                {DEFAULT_PATHS.map((p) => (
                  <label key={p} className="flex items-center gap-2 text-sm text-slate-700 dark:text-gray-300 cursor-pointer select-none">
                    <input
                      type="checkbox"
                      checked={selectedPaths.includes(p)}
                      disabled={status === "running"}
                      onChange={() => handlePathToggle(p)}
                      className="accent-indigo-500 rounded"
                    />
                    <code>{p}</code>
                  </label>
                ))}
              </div>
            </div>
 
            <button
              onClick={startSweep}
              disabled={status === "running" || selectedPaths.length === 0}
              className="btn-primary"
            >
              {status === "running" ? "Sweeping Live Pages..." : "Run Compliance Sweep"}
            </button>
          </div>
 
          {/* Logs Terminal */}
          <div className="glass-panel p-4 flex-1 flex flex-col min-h-[300px]">
            <div className="flex justify-between items-center mb-2 border-b border-slate-200 dark:border-slate-800 pb-2">
              <h2 className="text-sm font-bold text-cyan-600 dark:text-cyan-400 font-mono">Terminal Outputs</h2>
              <div className="flex items-center gap-2">
                <label className="text-[10px] font-mono text-slate-500 dark:text-gray-400 cursor-pointer flex items-center gap-1 select-none">
                  <input
                    type="checkbox"
                    checked={autoScroll}
                    onChange={(e) => setAutoScroll(e.target.checked)}
                    className="accent-indigo-500 rounded"
                  />
                  Auto-scroll
                </label>
                <span className="text-xs font-mono text-slate-400 dark:text-gray-500">| python stdout</span>
              </div>
            </div>
            <div 
              ref={terminalContainerRef}
              onScroll={handleTerminalScroll}
              className="flex-1 overflow-y-auto p-3 rounded-lg terminal-scroll font-mono text-xs max-h-[450px]"
            >
              {logs.length === 0 && <p className="text-slate-500 dark:text-gray-650 italic">Terminal awaiting agent launch...</p>}
              {logs.map((log, idx) => (
                <div key={idx} className="mb-1 whitespace-pre-wrap leading-relaxed">
                  {log}
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* Center & Right Column: Browser Simulator & Results */}
        <section className="xl:col-span-2 flex flex-col gap-6">
            {/* Browser Simulator */}
          <div className="glass-panel overflow-hidden flex flex-col relative min-h-[400px]">
            {/* Mac Browser Header */}
            <div className="bg-slate-200/90 dark:bg-gray-900/90 px-4 py-2 flex items-center gap-3 border-b border-slate-300 dark:border-gray-800">
              <div className="flex gap-1.5">
                <span className="w-3 h-3 rounded-full bg-rose-500 inline-block"></span>
                <span className="w-3 h-3 rounded-full bg-amber-500 inline-block"></span>
                <span className="w-3 h-3 rounded-full bg-emerald-500 inline-block"></span>
              </div>
              <div className="flex-1 bg-slate-100/90 dark:bg-gray-950/80 rounded-md py-1 px-4 border border-slate-300 dark:border-gray-800 text-xs text-slate-600 dark:text-gray-400 text-center font-mono select-none overflow-hidden text-ellipsis">
                https://white-cliff-0bca3ed00.1.azurestaticapps.net
                {activeRoute || (simStep === "dashboard" ? "/dashboard/my-applications" : "")}
              </div>
            </div>
 
            {/* Simulated Frame Content */}
            <div className="flex-1 bg-slate-100 dark:bg-[#10131E] flex flex-col items-center justify-center p-6 min-h-[350px] relative transition-colors duration-300">
              {/* Scan Beam animation */}
              {status === "running" && simStep === "dashboard" && <div className="scan-overlay" />}
 
              {/* Login Simulator view */}
              {simStep !== "dashboard" && simStep !== "idle" && (
                <div className="w-full max-w-sm glass-panel p-6 bg-white/90 dark:bg-gray-950/80 border border-slate-200 dark:border-gray-800 flex flex-col gap-4">
                  <h3 className="text-sm font-bold text-center text-indigo-600 dark:text-indigo-400 uppercase tracking-widest font-mono">WaiverPro Login</h3>
                  <div className="flex flex-col gap-1 text-xs">
                    <label className="text-slate-500 dark:text-gray-400">Email Address:</label>
                    <input
                      type="text"
                      readOnly
                      value={simEmail}
                      className="bg-slate-50 dark:bg-gray-900 border border-slate-300 dark:border-gray-750 rounded p-2 text-slate-800 dark:text-white font-mono typing-cursor"
                    />
                  </div>
                  <div className="flex flex-col gap-1 text-xs">
                    <label className="text-slate-500 dark:text-gray-400">Password:</label>
                    <input
                      type="password"
                      readOnly
                      value={simPassword}
                      className="bg-slate-50 dark:bg-gray-900 border border-slate-300 dark:border-gray-750 rounded p-2 text-slate-800 dark:text-white font-mono typing-cursor"
                    />
                  </div>
                  <button
                    className={`text-xs py-2 rounded text-white font-semibold font-mono transition-all ${
                      simStep === "submitting" ? "bg-emerald-600 animate-pulse" : "bg-indigo-600 hover:bg-indigo-750 shadow-sm"
                    }`}
                  >
                    {simStep === "submitting" ? "Connecting Session..." : "Submit Authentication"}
                  </button>
                  <p className="text-[10px] text-center text-slate-500 font-mono">
                    Status: {simStep.replace("-", " ")}
                  </p>
                </div>
              )}
 
              {/* Idle View */}
              {simStep === "idle" && status !== "success" && (
                <div className="text-center p-8 max-w-md flex flex-col items-center gap-4">
                  <div className="w-12 h-12 rounded-full bg-indigo-500/10 flex items-center justify-center border border-indigo-500/30 text-indigo-600 dark:text-indigo-400 text-xl font-bold font-mono">
                    P
                  </div>
                  <div>
                    <h3 className="text-md font-semibold text-slate-700 dark:text-gray-200">Playwright Frame Idle</h3>
                    <p className="text-xs text-slate-500 dark:text-gray-400 mt-1 leading-relaxed">
                      Click the "Run Compliance Sweep" button to launch Playwright Chromium headfully and visualize DOM extraction.
                    </p>
                  </div>
                </div>
              )}
 
              {/* Scraping live views with screenshot */}
              {simStep === "dashboard" && (
                <div className="w-full h-full flex flex-col items-center justify-center gap-4">
                  {activeScreenshot ? (
                    <div className="relative border border-slate-350 dark:border-gray-800 rounded-lg overflow-hidden max-w-full max-h-[350px]">
                      <img
                        src={`/api/serve-file?path=${encodeURIComponent(activeScreenshot)}`}
                        alt="Auditing View"
                        className="object-contain max-h-[320px]"
                      />
                      <div className="absolute top-2 left-2 bg-black/80 px-2 py-1 text-[10px] text-emerald-400 font-mono rounded">
                        LIVE SCREENSHOT CAPTURED
                      </div>
                    </div>
                  ) : (
                    <div className="text-center flex flex-col items-center gap-3">
                      <div className="w-8 h-8 rounded-full border-2 border-indigo-500 border-t-transparent animate-spin"></div>
                      <p className="text-xs text-slate-500 dark:text-gray-400 font-mono">
                        Navigated to <code>{activeRoute}</code>. Extracting styling layout matrices...
                      </p>
                    </div>
                  )}
                </div>
              )}
 
              {/* Done success state */}
              {status === "success" && simStep === "idle" && (
                <div className="text-center p-8 max-w-md flex flex-col items-center gap-3">
                  <div className="w-12 h-12 rounded-full bg-emerald-500/10 flex items-center justify-center border border-emerald-500/30 text-emerald-600 dark:text-emerald-400 text-lg">
                    ✓
                  </div>
                  <h3 className="text-md font-semibold text-emerald-600 dark:text-emerald-400">Sweep Completed</h3>
                  <p className="text-xs text-slate-500 dark:text-gray-400 leading-relaxed">
                    Agent audited successfully. Detailed discrepancy lists and ReportLab PDF report are rendered below.
                  </p>
                </div>
              )}
            </div>
          </div>
        </section>
      </main>

      {/* 4. Detailed Results Pane (RAG & Judgement findings) */}
      {latestReport && (
        <section className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
          
          {/* Compliance & Visual Regressions table */}
          <div className="glass-panel p-6 flex flex-col gap-4">
            <div className="flex justify-between items-center border-b border-slate-200 dark:border-slate-800 pb-3">
              <h2 className="text-lg font-bold text-indigo-650 dark:text-indigo-400">LLM Audited Findings</h2>
              <span className="text-xs font-mono text-slate-500 dark:text-gray-500">Run ID: {latestReport.runId}</span>
            </div>
 
            <div className="flex-1 overflow-y-auto max-h-[400px] flex flex-col gap-4">
              {latestReport.reports.map((report) => (
                <div key={report.target_path} className="border border-slate-200 dark:border-slate-800 rounded-lg p-4 bg-slate-50/50 dark:bg-gray-900/40">
                  <div className="flex justify-between items-center mb-3">
                    <span className="text-xs font-bold text-cyan-600 dark:text-cyan-400 font-mono">
                      Path: <code>{report.target_path}</code>
                    </span>
                    <span className={`text-[10px] uppercase font-mono font-bold px-2 py-0.5 rounded-full ${
                      report.findings.length > 0 ? "bg-rose-500/10 text-rose-500 dark:text-rose-400 border border-rose-500/20" : "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border border-emerald-500/20"
                    }`}>
                      {report.findings.length} issue(s)
                    </span>
                  </div>
 
                  {report.findings.length === 0 ? (
                    <p className="text-xs text-slate-500 italic">No discrepancies found on this page.</p>
                  ) : (
                    <div className="overflow-x-auto">
                      <table className="w-full text-left text-xs border-collapse">
                        <thead>
                          <tr className="border-b border-slate-200 dark:border-slate-800 text-slate-550 dark:text-gray-400 font-mono">
                            <th className="py-2 pr-2">Selector</th>
                            <th className="py-2 px-2">Discrepancy Description</th>
                            <th className="py-2 pl-2">Severity</th>
                          </tr>
                        </thead>
                        <tbody>
                          {report.findings.map((f, idx) => (
                            <tr key={idx} className="border-b border-slate-250 dark:border-slate-800/40 hover:bg-slate-100/50 dark:hover:bg-gray-800/20">
                              <td className="py-2 pr-2 font-mono text-[10px] text-slate-500 dark:text-gray-400 max-w-[100px] overflow-hidden text-ellipsis">
                                <code>{f.element_selector}</code>
                              </td>
                              <td className="py-2 px-2 text-slate-700 dark:text-gray-300">
                                <strong>Expected:</strong> {f.expected_behavior} <br />
                                <strong>Observed:</strong> {f.observed_behavior}
                              </td>
                              <td className="py-2 pl-2">
                                <span className={`badge badge-${f.severity || "medium"}`}>
                                  {f.severity || "medium"}
                                </span>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
 
          {/* PDF Viewer & Email Dispatcher */}
          <div className="glass-panel p-6 flex flex-col gap-4">
            <div className="flex justify-between items-center border-b border-slate-200 dark:border-slate-800 pb-3">
              <h2 className="text-lg font-bold text-indigo-650 dark:text-indigo-400">PDF Report & Alerting</h2>
              <span className="text-xs font-mono text-slate-500 dark:text-gray-500">ReportLab Document</span>
            </div>
 
            {latestReport.pdfPath ? (
              <div className="flex-1 flex flex-col gap-4">
                <div className="border border-slate-200 dark:border-slate-800 rounded-lg overflow-hidden flex-1 h-[250px] bg-slate-100 dark:bg-slate-950 relative">
                  <iframe
                    src={`/api/serve-file?path=${encodeURIComponent(latestReport.pdfPath)}`}
                    className="w-full h-full"
                    title="PDF Compliance Report"
                  />
                </div>
 
                <div className="flex flex-col gap-3 p-3 bg-slate-50 dark:bg-gray-900/60 rounded-lg border border-slate-200 dark:border-slate-800">
                  <div className="flex justify-between items-center">
                    <span className="text-xs text-slate-500 dark:text-gray-400 font-mono">Attachment: <code>{latestReport.pdfPath.split(/[\\/]/).pop()}</code></span>
                    <button
                      onClick={triggerEmailAlert}
                      disabled={emailStatus === "sending"}
                      className="btn-primary text-xs py-2 px-4"
                    >
                      {emailStatus === "sending" ? "Sending Email..." : "Send Report via Email"}
                    </button>
                  </div>
 
                  {emailStatus !== "idle" && (
                    <div className={`text-xs p-2 rounded border font-mono ${
                      emailStatus === "success" 
                        ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border-emerald-500/25" 
                        : emailStatus === "error" 
                          ? "bg-rose-500/10 text-rose-650 dark:text-rose-400 border-rose-500/25" 
                          : "bg-indigo-500/10 text-indigo-650 dark:text-indigo-400 border-indigo-500/25 animate-pulse"
                    }`}>
                      {emailStatus === "sending" && "Connecting to SMTP server and dispatching visual PDF alert..."}
                      {emailStatus === "success" && emailMessage}
                      {emailStatus === "error" && emailMessage}
                    </div>
                  )}
                </div>
              </div>
            ) : (
              <div className="flex-1 flex items-center justify-center italic text-xs text-slate-500 dark:text-gray-500">
                PDF report not compiled for this run yet. Run a compliance sweep first.
              </div>
            )}
          </div>
 
        </section>
      )}
 
      {/* 5. Footer */}
      <footer className="text-center text-xs text-slate-500 dark:text-gray-500 border-t border-slate-200 dark:border-slate-800 pt-4">
        WaiverPro Compliance Operations Center &copy; 2026. Made with Next.js & ReportLab.
      </footer>
    </div>
  );
}
