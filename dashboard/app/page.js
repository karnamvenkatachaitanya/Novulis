"use client";

import React, { useState, useEffect, useRef } from "react";

const DEFAULT_PATHS = [
  "/",
  "/login",
  "/dashboard/my-applications",
  "/dashboard/facilities",
  "/dashboard/action-items",
  "/dashboard/user-management",
  "/dashboard/announcements",
  "/dashboard/settings",
  "/dashboard/faqs",
  "/dashboard/tickets",
  "/dashboard/contact",
  "/privacy",
  "/terms",
];

const MarkdownRenderer = ({ content, theme }) => {
  if (!content) return null;

  const lines = content.split("\n");
  const renderedElements = [];
  let currentListItems = [];

  const flushList = (key) => {
    if (currentListItems.length > 0) {
      renderedElements.push(
        <ul key={`list-${key}`} className="list-disc pl-5 mb-2 flex flex-col gap-1">
          {currentListItems.map((item, idx) => (
            <li key={idx} className="text-xs text-slate-700 dark:text-slate-300 leading-relaxed">{item}</li>
          ))}
        </ul>
      );
      currentListItems = [];
    }
  };

  const parseInlineMarkdown = (text) => {
    const boldRegex = /\*\*([^*]+)\*\*/g;
    const parts = [];
    let lastIndex = 0;
    let match;

    while ((match = boldRegex.exec(text)) !== null) {
      if (match.index > lastIndex) {
        parts.push(text.substring(lastIndex, match.index));
      }
      parts.push(
        <strong key={match.index} className="font-bold text-indigo-700 dark:text-indigo-400">
          {match[1]}
        </strong>
      );
      lastIndex = boldRegex.lastIndex;
    }

    if (lastIndex < text.length) {
      parts.push(text.substring(lastIndex));
    }

    return parts.length > 0 ? parts : text;
  };

  lines.forEach((line, idx) => {
    const trimmed = line.trim();

    if (trimmed.startsWith("#")) {
      flushList(idx);
      const level = trimmed.match(/^#+/)[0].length;
      const headerText = trimmed.replace(/^#+\s*/, "");
      const parsedText = parseInlineMarkdown(headerText);

      if (level === 1) {
        renderedElements.push(
          <h1 key={idx} className="text-base font-extrabold mb-2 mt-3 text-slate-800 dark:text-slate-100">
            {parsedText}
          </h1>
        );
      } else if (level === 2) {
        renderedElements.push(
          <h2 key={idx} className="text-sm font-bold mb-2 mt-3 text-indigo-700 dark:text-indigo-400">
            {parsedText}
          </h2>
        );
      } else {
        renderedElements.push(
          <h3 key={idx} className="text-xs font-bold mb-1 mt-2 text-slate-800 dark:text-slate-200">
            {parsedText}
          </h3>
        );
      }
    } else if (trimmed.startsWith("- ") || trimmed.startsWith("* ") || (trimmed.startsWith("+ ") && !trimmed.startsWith("+ New"))) {
      const itemText = trimmed.replace(/^[-*+]\s*/, "");
      currentListItems.push(parseInlineMarkdown(itemText));
    } else if (trimmed === "") {
      flushList(idx);
      renderedElements.push(<div key={`spacer-${idx}`} className="h-2" />);
    } else {
      flushList(idx);
      renderedElements.push(
        <p key={idx} className="text-xs mb-2 text-slate-700 dark:text-slate-300 leading-relaxed">
          {parseInlineMarkdown(line)}
        </p>
      );
    }
  });

  flushList(lines.length);

  return <div className="markdown-body">{renderedElements}</div>;
};

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
  const [recipientEmail, setRecipientEmail] = useState("");
  const terminalEndRef = useRef(null);
  const terminalContainerRef = useRef(null);

  // UI Improvements: Dark mode & Workflow step states
  const [theme, setTheme] = useState("dark");
  const [workflowStep, setWorkflowStep] = useState(0);

  // Chat state
  const [chatOpen, setChatOpen] = useState(false);
  const [chatMessages, setChatMessages] = useState([]);
  const [chatInput, setChatInput] = useState("");
  const [chatLoading, setChatLoading] = useState(false);
  const chatMessagesRef = useRef(null);

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
    
    let active = true;
    let currentEmail = "";
    let currentPass = "";
    
    const runTyping = async () => {
      // Type email
      for (let i = 0; i < email.length; i++) {
        if (!active) return;
        currentEmail += email[i];
        setSimEmail(currentEmail);
        await new Promise((r) => setTimeout(r, 80));
      }
      
      if (!active) return;
      setSimStep("typing-pass");
      
      // Type password
      for (let i = 0; i < pass.length; i++) {
        if (!active) return;
        currentPass += pass[i];
        setSimPassword(currentPass);
        await new Promise((r) => setTimeout(r, 100));
      }
      
      if (!active) return;
      setSimStep("submitting");
      
      await new Promise((r) => setTimeout(r, 1500));
      if (!active) return;
      setSimStep("dashboard");
    };

    runTyping();

    return () => {
      active = false;
    };
  }, [status]);

  // Parse logs to detect active scraping route and screenshot changes
  useEffect(() => {
    if (logs.length === 0) return;
    const lastLog = logs[logs.length - 1];

    // Detect active scraping target
    // Format: 2026-06-25 12:57:02,657 INFO main - Starting audit for /dashboard/faqs
    const routeMatch = lastLog.match(/Starting audit for\s+(\/[a-zA-Z0-9_/-]*)/);
    if (routeMatch) {
      setActiveRoute(routeMatch[1]);
      setActiveScreenshot(""); // clear previous until new is saved
    }

    // Detect saved page capture or captured page screenshot
    // Format: 2026-06-25 12:57:09,315 INFO scraper - Saved page capture: C:\...\dashboard_faqs-20260625-125709.json
    // Or: 2026-06-29 15:20:43,947 INFO scraper - Captured page screenshot: C:\...\dashboard_my-applications-20260629-152032.png
    const screenshotMatch = lastLog.match(/(?:Saved page capture|Captured page screenshot):.*[\\/]([a-zA-Z0-9_-]+-\d{8}-\d{6})\.(?:json|png)/i);
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
        body: JSON.stringify({ runId: latestReport.runId, email: recipientEmail }),
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

  // ── RAG Chatbot Handler ──
  const handleSendChat = async (messageText) => {
    const msg = (messageText || chatInput).trim();
    if (!msg || chatLoading) return;

    // Add user message
    setChatMessages((prev) => [...prev, { role: "user", content: msg }]);
    setChatInput("");
    setChatLoading(true);

    let botText = "";
    let source = "";

    try {
      const res = await fetch(`/api/chat?message=${encodeURIComponent(msg)}`);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const text = decoder.decode(value, { stream: true });
        const lines = text.split("\n");

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const jsonStr = line.slice(6).trim();
          if (!jsonStr) continue;

          try {
            const event = JSON.parse(jsonStr);

            if (event.type === "intent") {
              // We got the intent classification
              continue;
            }

            if (event.type === "status") {
              // Status update — show as temporary text
              if (!botText) {
                setChatMessages((prev) => {
                  const updated = [...prev];
                  const lastMsg = updated[updated.length - 1];
                  if (lastMsg && lastMsg.role === "bot") {
                    lastMsg.content = event.data;
                  } else {
                    updated.push({ role: "bot", content: event.data, source: "" });
                  }
                  return updated;
                });
              }
              continue;
            }

            if (event.type === "token") {
              botText += event.data;
              setChatMessages((prev) => {
                const updated = [...prev];
                const lastMsg = updated[updated.length - 1];
                if (lastMsg && lastMsg.role === "bot") {
                  lastMsg.content = botText;
                } else {
                  updated.push({ role: "bot", content: botText, source: "" });
                }
                return updated;
              });
              // Auto-scroll chat
              if (chatMessagesRef.current) {
                chatMessagesRef.current.scrollTop = chatMessagesRef.current.scrollHeight;
              }
            }

            if (event.type === "done") {
              source = event.source || "";
              // Update the source badge on the final message
              setChatMessages((prev) => {
                const updated = [...prev];
                const lastMsg = updated[updated.length - 1];
                if (lastMsg && lastMsg.role === "bot") {
                  lastMsg.source = source;
                }
                return updated;
              });
            }

            if (event.type === "error") {
              const errorText = event.error || "The chatbot could not finish the request.";
              setChatMessages((prev) => {
                const updated = [...prev];
                const lastMsg = updated[updated.length - 1];
                if (lastMsg && lastMsg.role === "bot") {
                  lastMsg.content = errorText;
                  lastMsg.source = "general";
                } else {
                  updated.push({ role: "bot", content: errorText, source: "general" });
                }
                return updated;
              });
              botText = errorText;
            }
          } catch {
            // Skip unparseable lines
          }
        }
      }
    } catch (err) {
      setChatMessages((prev) => [
        ...prev,
        { role: "bot", content: `Error: ${err.message}`, source: "general" },
      ]);
    } finally {
      setChatLoading(false);
      if (chatMessagesRef.current) {
        chatMessagesRef.current.scrollTop = chatMessagesRef.current.scrollHeight;
      }
    }
  };

  return (
    <div className="min-h-screen p-4 md:p-6 flex flex-col justify-between text-slate-800 dark:text-slate-100 transition-colors duration-300">
      {/* 1. Header */}
      <header className="flex flex-col sm:flex-row justify-between items-start sm:items-center mb-6 border-b border-slate-300 dark:border-slate-800 pb-4 gap-4 w-full">
        <div>
          <h1 className="text-2xl sm:text-3xl font-bold tracking-tight bg-gradient-to-r from-indigo-700 to-cyan-600 dark:from-indigo-400 dark:to-cyan-400 bg-clip-text text-transparent">
            WaiverPro Compliance Dashboard
          </h1>
          <p className="text-sm text-slate-700 dark:text-slate-400 mt-1 font-medium">Real-Time Autonomous QA Agent Controller</p>
        </div>
        <div className="flex flex-row gap-3 items-center w-full sm:w-auto justify-between sm:justify-end">
          {/* Light/Dark Toggle Button */}
          <button
            onClick={toggleTheme}
            className="p-2 rounded-lg bg-slate-200/90 hover:bg-slate-300 dark:bg-slate-800/80 dark:hover:bg-slate-700/80 border border-slate-350 dark:border-slate-700 transition-all cursor-pointer shadow-sm text-xs sm:text-sm font-semibold"
            title="Toggle theme mode"
          >
            {theme === "dark" ? "☀️ Light Mode" : "🌙 Dark Mode"}
          </button>
          <div className="flex items-center gap-2">
            <span className="text-xs text-slate-700 dark:text-gray-500 font-mono font-bold">Status:</span>
            {status === "idle" && <span className="badge badge-low">Idle</span>}
            {status === "running" && <span className="badge badge-high animate-pulse">Running</span>}
            {status === "success" && <span className="badge badge-low">Success</span>}
            {status === "error" && <span className="badge badge-critical">Error</span>}
          </div>
        </div>
      </header>
 
      {/* 2. Visual Workflow Progress Indicator */}
      <section className="glass-panel p-4 sm:p-6 mb-6">
        <h3 className="text-xs font-bold tracking-wider text-slate-800 dark:text-gray-400 font-mono uppercase mb-4">
          Agent Operational Workflow Sequence
        </h3>
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-6 gap-3">
          {WORKFLOW_STEPS.map((step) => {
            const isCompleted = workflowStep > step.id;
            const isActive = workflowStep === step.id;
            return (
              <div 
                key={step.id} 
                className={`p-3 rounded-lg border transition-all duration-300 ${
                  isActive 
                    ? "bg-indigo-600/10 dark:bg-indigo-500/10 border-indigo-600 dark:border-indigo-500 shadow-md shadow-indigo-500/10 scale-[1.03]" 
                    : isCompleted 
                      ? "bg-emerald-500/5 dark:bg-emerald-500/5 border-emerald-500/40 dark:border-emerald-500/30 opacity-95" 
                      : "bg-white/60 dark:bg-slate-900/40 border-slate-300 dark:border-slate-800 opacity-70"
                }`}
              >
                <div className="flex items-center gap-2 mb-1">
                  <span className={`w-5 h-5 rounded-full text-[10px] font-bold flex items-center justify-center border ${
                    isActive 
                      ? "bg-indigo-600 dark:bg-indigo-600 text-white border-indigo-500 animate-pulse" 
                      : isCompleted 
                        ? "bg-emerald-600 dark:bg-emerald-600 text-white border-emerald-500" 
                        : "bg-slate-300 dark:bg-slate-800 text-slate-800 dark:text-slate-400 border-slate-400 dark:border-slate-700"
                  }`}>
                    {isCompleted ? "✓" : step.id}
                  </span>
                  <span className={`text-xs font-bold ${isActive ? "text-indigo-800 dark:text-indigo-300" : isCompleted ? "text-emerald-700 dark:text-emerald-400" : "text-slate-800 dark:text-slate-300"}`}>
                    {step.name}
                  </span>
                </div>
                <p className="text-[10px] text-slate-700 dark:text-gray-400 leading-tight font-medium">
                  {step.desc}
                </p>
              </div>
            );
          })}
        </div>
      </section>
      <main className="flex-1 grid grid-cols-1 lg:grid-cols-3 gap-6 mb-8">
        
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
        <section className="lg:col-span-2 flex flex-col gap-6">
            {/* Browser Simulator */}
          <div className="glass-panel overflow-hidden flex flex-col relative flex-1 min-h-[500px]">
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
            <div className="flex-1 bg-slate-100 dark:bg-[#10131E] flex flex-col items-center justify-center relative transition-colors duration-300 w-full overflow-hidden">
              {/* Scan Beam animation — sits above the screenshot */}
              {status === "running" && simStep === "dashboard" && activeScreenshot && <div className="scan-overlay" style={{zIndex: 10}} />}
 
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
                <div className="absolute inset-0 flex flex-col">
                  {activeScreenshot ? (
                    <div className="relative w-full h-full overflow-y-auto scrollbar-thin">
                      <div className="sticky top-2 left-2 bg-black/80 px-2 py-1 text-[10px] text-emerald-400 font-mono rounded inline-block m-2" style={{zIndex: 11}}>
                        LIVE SCREENSHOT CAPTURED
                      </div>
                      <img
                        src={`/api/serve-file?path=${encodeURIComponent(activeScreenshot)}`}
                        alt="Auditing View"
                        className="w-full h-auto object-contain object-top -mt-8"
                        style={{position: 'relative', zIndex: 1}}
                      />
                    </div>
                  ) : (
                    <div className="flex-1 flex flex-col items-center justify-center gap-3">
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
                  <div className="flex flex-col sm:flex-row gap-2 justify-between sm:items-center">
                    <span className="text-xs text-slate-500 dark:text-gray-400 font-mono">Attachment: <code>{latestReport.pdfPath.split(/[\\/]/).pop()}</code></span>
                    <div className="flex gap-2">
                      <input
                        type="email"
                        id="email-input"
                        placeholder="Recipient Email (optional)"
                        value={recipientEmail}
                        onChange={(e) => setRecipientEmail(e.target.value)}
                        className="text-xs px-3 py-1.5 rounded border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-950 text-slate-900 dark:text-slate-100 placeholder-slate-400 dark:placeholder-gray-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 w-full sm:w-48"
                      />
                      <button
                        onClick={triggerEmailAlert}
                        disabled={emailStatus === "sending"}
                        className="btn-primary text-xs py-2 px-4 whitespace-nowrap"
                      >
                        {emailStatus === "sending" ? "Sending..." : "Send Email"}
                      </button>
                    </div>
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

      {/* 6. Chat Toggle Button */}
      <button
        onClick={() => setChatOpen(!chatOpen)}
        title="Open RAG Chatbot"
        style={{
          position: 'fixed', bottom: 20, right: 24, zIndex: 51,
          width: 52, height: 52, borderRadius: '50%',
          background: 'linear-gradient(135deg, #6366F1, #06B6D4)',
          color: 'white', border: 'none', cursor: 'pointer',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: 22, boxShadow: '0 6px 24px rgba(99,102,241,0.4)',
          transition: 'all 0.2s ease',
        }}
      >
        {chatOpen ? "✕" : "💬"}
      </button>

      {/* 7. Chat Panel */}
      <div style={{
        position: 'fixed', bottom: 85, right: 24, zIndex: 50,
        width: 380, maxWidth: '95vw', height: 540, maxHeight: '75vh',
        display: 'flex', flexDirection: 'column',
        background: theme === 'dark' ? 'rgba(31,41,55,0.95)' : 'rgba(255,255,255,0.97)',
        backdropFilter: 'blur(16px)', WebkitBackdropFilter: 'blur(16px)',
        border: `1px solid ${theme === 'dark' ? 'rgba(55,65,81,0.5)' : 'rgba(148,163,184,0.45)'}`,
        borderRadius: '16px',
        boxShadow: theme === 'dark' ? '0 12px 40px rgba(0,0,0,0.5)' : '0 12px 40px rgba(0,0,0,0.12)',
        transform: chatOpen ? 'translateY(0)' : 'translateY(120%)',
        opacity: chatOpen ? 1 : 0,
        pointerEvents: chatOpen ? 'auto' : 'none',
        transition: 'transform 0.3s cubic-bezier(0.4,0,0.2,1), opacity 0.3s ease',
      }}>
        {/* Chat Header */}
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '14px 16px',
          borderBottom: `1px solid ${theme === 'dark' ? 'rgba(55,65,81,0.5)' : 'rgba(148,163,184,0.35)'}`,
          background: 'linear-gradient(135deg, rgba(99,102,241,0.08), rgba(6,182,212,0.08))',
          borderRadius: '16px 16px 0 0',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <span style={{ fontSize: 20 }}>🤖</span>
            <div>
              <div style={{ fontSize: 14, fontWeight: 700, color: theme === 'dark' ? '#818cf8' : '#4338ca' }}>WaiverPro Assistant</div>
              <div style={{ fontSize: 10, color: theme === 'dark' ? '#9ca3af' : '#64748b' }}>RAG-powered compliance chatbot</div>
            </div>
          </div>
          <button
            onClick={() => setChatOpen(false)}
            style={{
              background: 'none', border: 'none', cursor: 'pointer',
              fontSize: 18, color: theme === 'dark' ? '#9ca3af' : '#94a3b8',
              padding: 4, lineHeight: 1,
            }}
          >✕</button>
        </div>

        {/* Chat Messages */}
        <div ref={chatMessagesRef} style={{
          flex: 1, overflowY: 'auto', padding: '14px 16px',
          display: 'flex', flexDirection: 'column', gap: 10,
        }}>
          {chatMessages.length === 0 && (
            <div style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 12, padding: '32px 0' }}>
              <span style={{ fontSize: 32 }}>💡</span>
              <p style={{ fontSize: 12, color: theme === 'dark' ? '#9ca3af' : '#64748b', textAlign: 'center', lineHeight: 1.6, maxWidth: 260 }}>
                Ask me about the current dashboard state, compliance guidelines, or tell me to scrape specific pages.
              </p>
            </div>
          )}
          {chatMessages.map((msg, idx) => (
            <div key={idx} style={{ display: 'flex', flexDirection: 'column' }}>
              {msg.role === "user" ? (
                <div style={{
                  alignSelf: 'flex-end', maxWidth: '82%',
                  padding: '10px 14px', borderRadius: '14px 14px 4px 14px',
                  background: 'linear-gradient(135deg, #6366F1, #4F46E5)',
                  color: 'white', fontSize: 13, lineHeight: 1.5, wordBreak: 'break-word',
                }}>{msg.content}</div>
              ) : (
                <div style={{
                  alignSelf: 'flex-start', maxWidth: '82%',
                  padding: '10px 14px', borderRadius: '14px 14px 14px 4px',
                  background: theme === 'dark' ? '#111827' : '#ffffff',
                  border: `1px solid ${theme === 'dark' ? 'rgba(55,65,81,0.5)' : 'rgba(148,163,184,0.35)'}`,
                  color: theme === 'dark' ? '#f9fafb' : '#0f172a',
                  fontSize: 13, lineHeight: 1.5, wordBreak: 'break-word',
                }}>
                  {msg.source && (
                    <div style={{
                      display: 'inline-block', padding: '2px 8px', borderRadius: 9999,
                      fontSize: 10, fontWeight: 600, marginBottom: 6,
                      background: msg.source === 'live_data' ? 'rgba(16,185,129,0.15)' :
                                  msg.source === 'guidelines' ? 'rgba(99,102,241,0.15)' :
                                  msg.source === 'action' ? 'rgba(245,158,11,0.15)' : 'rgba(148,163,184,0.15)',
                      color: msg.source === 'live_data' ? '#10B981' :
                             msg.source === 'guidelines' ? '#6366F1' :
                             msg.source === 'action' ? '#F59E0B' : '#94A3B8',
                      border: `1px solid ${
                        msg.source === 'live_data' ? 'rgba(16,185,129,0.3)' :
                        msg.source === 'guidelines' ? 'rgba(99,102,241,0.3)' :
                        msg.source === 'action' ? 'rgba(245,158,11,0.3)' : 'rgba(148,163,184,0.3)'
                      }`,
                    }}>
                      {msg.source === 'live_data' ? '📊 Live Data' :
                       msg.source === 'guidelines' ? '📖 Guidelines' :
                       msg.source === 'action' ? '⚡ Action' : '💬 General'}
                    </div>
                  )}
                  <MarkdownRenderer content={msg.content} theme={theme} />
                </div>
              )}
            </div>
          ))}
          {chatLoading && (
            <div style={{
              alignSelf: 'flex-start', padding: '12px 16px', borderRadius: '14px 14px 14px 4px',
              background: theme === 'dark' ? '#111827' : '#ffffff',
              border: `1px solid ${theme === 'dark' ? 'rgba(55,65,81,0.5)' : 'rgba(148,163,184,0.35)'}`,
            }}>
              <div className="typing-dots"><span></span><span></span><span></span></div>
            </div>
          )}
        </div>

        {/* Quick Suggestion Chips */}
        {chatMessages.length === 0 && !chatLoading && (
          <div style={{
            display: 'flex', flexWrap: 'wrap', gap: 6, padding: '8px 16px',
            borderTop: `1px solid ${theme === 'dark' ? 'rgba(55,65,81,0.5)' : 'rgba(148,163,184,0.35)'}`,
          }}>
            {[
              "What is WaiverPro?",
              "What services do you provide?",
              "What should /login display?",
              "What tickets are open?",
            ].map((suggestion) => (
              <button
                key={suggestion}
                onClick={() => { setChatInput(suggestion); handleSendChat(suggestion); }}
                style={{
                  padding: '5px 12px', borderRadius: 9999, fontSize: 11, fontWeight: 500,
                  cursor: 'pointer',
                  border: `1px solid ${theme === 'dark' ? 'rgba(55,65,81,0.5)' : 'rgba(148,163,184,0.4)'}`,
                  background: theme === 'dark' ? '#111827' : '#ffffff',
                  color: theme === 'dark' ? '#9ca3af' : '#334155',
                  transition: 'all 0.15s ease',
                }}
              >
                {suggestion}
              </button>
            ))}
          </div>
        )}

        {/* Chat Input Bar */}
        <div style={{
          display: 'flex', gap: 8, padding: '12px 16px',
          borderTop: `1px solid ${theme === 'dark' ? 'rgba(55,65,81,0.5)' : 'rgba(148,163,184,0.35)'}`,
        }}>
          <input
            type="text"
            placeholder="Ask about the dashboard..."
            value={chatInput}
            onChange={(e) => setChatInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !chatLoading && chatInput.trim()) {
                handleSendChat(chatInput);
              }
            }}
            disabled={chatLoading}
            style={{
              flex: 1, padding: '10px 14px', borderRadius: 10,
              border: `1px solid ${theme === 'dark' ? 'rgba(55,65,81,0.5)' : 'rgba(148,163,184,0.4)'}`,
              background: theme === 'dark' ? '#1f2937' : '#ffffff',
              color: theme === 'dark' ? '#f9fafb' : '#0f172a',
              fontSize: 13, outline: 'none',
            }}
          />
          <button
            onClick={() => handleSendChat(chatInput)}
            disabled={chatLoading || !chatInput.trim()}
            style={{
              padding: '10px 16px', borderRadius: 10,
              background: 'linear-gradient(135deg, #6366F1, #4F46E5)',
              color: 'white', border: 'none', fontWeight: 600, fontSize: 13,
              cursor: chatLoading || !chatInput.trim() ? 'not-allowed' : 'pointer',
              opacity: chatLoading || !chatInput.trim() ? 0.5 : 1,
              whiteSpace: 'nowrap', transition: 'all 0.2s ease',
            }}
          >
            Send
          </button>
        </div>
      </div>
    </div>
  );
}
