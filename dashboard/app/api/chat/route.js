import { spawn } from "child_process";
import { existsSync, readdirSync, readFileSync, statSync } from "fs";
import path from "path";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const CACHE_TTL_MS = Number(process.env.CHAT_CACHE_TTL_MS || 30000);
const CHATBOT_TIMEOUT_MS = Number(process.env.CHATBOT_TIMEOUT_MS || 30000);
const chatCache = globalThis.__waiverproChatCache || new Map();
const snapshotCache = globalThis.__waiverproSnapshotCache || new Map();
globalThis.__waiverproChatCache = chatCache;
globalThis.__waiverproSnapshotCache = snapshotCache;

const encoder = new TextEncoder();

function normalizeMessage(message) {
  return message.trim().toLowerCase().replace(/\s+/g, " ");
}

function sseFromEvents(events) {
  return new Response(
    new ReadableStream({
      start(controller) {
        events.forEach((event) => {
          controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`));
        });
        controller.close();
      },
    }),
    sseHeaders()
  );
}

function sseHeaders() {
  return {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
    },
  };
}

function getCachedEvents(cacheKey) {
  const cached = chatCache.get(cacheKey);
  if (!cached) return null;
  if (Date.now() - cached.createdAt > CACHE_TTL_MS) {
    chatCache.delete(cacheKey);
    return null;
  }
  return cached.events;
}

function setCachedEvents(cacheKey, events) {
  const hasAction = events.some((event) => event.intent === "ACTION_SCRAPE" || event.source === "action");
  const hasError = events.some((event) => event.type === "error");
  if (!hasAction && !hasError) {
    chatCache.set(cacheKey, { createdAt: Date.now(), events });
  }
}

function getInstantEvents(message) {
  const normalized = normalizeMessage(message);
  // Match more greetings, thanks, farewells, and variations like "hello there"
  const greetingPattern = /^(hi|hello|helo|hey|hay|hy|greetings|good morning|good afternoon|good evening|thanks|thank you|ty)[!. a-z]*$/;

  if (greetingPattern.test(normalized)) {
    const isThanks = /(thanks|thank you|ty)/.test(normalized);
    const replyText = isThanks
      ? "You're welcome! Let me know if you need anything else."
      : "Hi! I can help with WaiverPro dashboard data. Ask me things like `what tickets are open`, `explain ticket 17`, or `show current facilities`.";
    return [
      { type: "intent", intent: "GENERAL", page_path: null, latency_mode: "instant" },
      {
        type: "token",
        data: replyText,
      },
      { type: "done", source: "general", chunks_used: 0, latency_mode: "instant" },
      { type: "close", code: 0 },
    ];
  }

  const conclusionPattern = /^(nothing|no thanks|no|that is all|bye|goodbye|quit|exit)[!. ]*$/;
  if (conclusionPattern.test(normalized)) {
    return [
      { type: "intent", intent: "GENERAL", page_path: null, latency_mode: "instant" },
      { type: "token", data: "You're welcome! Feel free to ask if you need help with WaiverPro compliance in the future. Goodbye!" },
      { type: "done", source: "general", chunks_used: 0, latency_mode: "instant" },
      { type: "close", code: 0 },
    ];
  }

  const introPattern = /(introduce|intraduse|who are you|what is your name|your identity|who made you|who created you|your creator)/;
  if (introPattern.test(normalized)) {
    return [
      { type: "intent", intent: "GENERAL", page_path: null, latency_mode: "instant" },
      {
        type: "token",
        data: "I am the WaiverPro Compliance Assistant. I can check visual guidelines, list dashboard statistics, find layout errors, and trigger new web page scans. How can I help you today?",
      },
      { type: "done", source: "general", chunks_used: 0, latency_mode: "instant" },
      { type: "close", code: 0 },
    ];
  }

  const capabilityPattern = /(capability|capabilities|what can you do|what can u do|what features|features do you have|how can you help|help)/;
  if (capabilityPattern.test(normalized)) {
    return [
      { type: "intent", intent: "GENERAL", page_path: null, latency_mode: "instant" },
      {
        type: "token",
        data: "Here is what I can do:\n\n1. **Check Live Data**: Ask me about active tickets (`what tickets are open`), applications (`how many applications are there`), or page summaries.\n2. **Look Up Guidelines**: Ask about design rules (`what should the login page show`).\n3. **Scan Pages**: Ask me to scrape routes (`scrape /dashboard/facilities`) to pull fresh details dynamically.\n4. **Check Update Times**: Ask me when pages were last crawled (`when was the last update`).",
      },
      { type: "done", source: "general", chunks_used: 0, latency_mode: "instant" },
      { type: "close", code: 0 },
    ];
  }

  const dashboardTerms = /(waiverpro|dashboard|compliance|scrape|guideline|ticket|tickets|application|applications|facility|facilities|faq|faqs|login|contact|support|action|settings|announcement|user)/;
  const offTopicTerms = /(poem|prime number|recipe|weather|stock price|write code|write a code|joke)/;
  if (offTopicTerms.test(normalized) && !dashboardTerms.test(normalized)) {
    return [
      { type: "intent", intent: "OFF_TOPIC", page_path: null, latency_mode: "instant" },
      {
        type: "token",
        data: "I am sorry, but I can only assist with questions regarding the WaiverPro Compliance Dashboard, scraping operations, and compliance guidelines. How can I help you with WaiverPro compliance today?",
      },
      { type: "done", source: "general", chunks_used: 0, latency_mode: "instant" },
      { type: "close", code: 0 },
    ];
  }

  if (normalized.length <= 12 && !dashboardTerms.test(normalized)) {
    return [
      { type: "intent", intent: "GENERAL", page_path: null, latency_mode: "instant" },
      {
        type: "token",
        data: "I am here. Ask me about a WaiverPro page, ticket, application, or facility and I will answer from the latest dashboard data.",
      },
      { type: "done", source: "general", chunks_used: 0, latency_mode: "instant" },
      { type: "close", code: 0 },
    ];
  }

  return null;
}

const PAGE_KEYWORDS = [
  ["/dashboard/tickets", ["ticket", "tickets", "tick", "ticks", "tkt", "tkts"]],
  ["/dashboard/facilities", ["facility", "facilities", "fac", "facs"]],
  ["/dashboard/my-applications", ["application", "applications", "my applications", "app", "apps", "appication", "appications"]],
  ["/dashboard/action-items", ["action item", "action items", "task", "tasks", "action", "actions"]],
  ["/dashboard/user-management", ["user", "users", "user management", "usr", "usrs"]],
  ["/dashboard/announcements", ["announcement", "announcements", "announce"]],
  ["/dashboard/settings", ["setting", "settings", "config"]],
  ["/dashboard/faqs", ["faq", "faqs"]],
  ["/dashboard/contact", ["contact", "support contact"]],
  ["/login", ["login", "sign in"]],
];

const STOP_WORDS = new Set([
  "a", "an", "and", "about", "are", "as", "at", "be", "by", "can", "do", "does",
  "for", "from", "how", "i", "in", "is", "it", "me", "of", "on", "or", "page",
  "please", "show", "tell", "the", "this", "to", "waiverpro", "what", "when",
  "where", "which", "who", "why", "with",
]);

const PATH_LABELS = {
  "/": "Home",
  "/login": "Login",
  "/dashboard/my-applications": "My Applications",
  "/dashboard/facilities": "Facilities",
  "/dashboard/action-items": "Action Items",
  "/dashboard/user-management": "User Management",
  "/dashboard/announcements": "Announcements",
  "/dashboard/settings": "Settings",
  "/dashboard/faqs": "FAQs",
  "/dashboard/tickets": "Tickets",
  "/dashboard/contact": "Contact",
  "/privacy": "Privacy",
  "/terms": "Terms",
};

function detectCurrentPage(message) {
  const normalized = normalizeMessage(message);
  if (/\b(?:tick|ticket)[- ]?\d{1,3}\b/.test(normalized)) {
    return "/dashboard/tickets";
  }

  const guidelineTerms = /(should|expected|guideline|guidelines|policy|policies|documentation|docs|requirement|requirements|according to|supposed to)/;
  if (guidelineTerms.test(normalized)) return null;

  for (const [pagePath, keywords] of PAGE_KEYWORDS) {
    if (keywords.some((keyword) => {
      // Use boundary check to match words correctly (e.g. "app" should not match "happy")
      const regex = new RegExp(`\\b${keyword.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`);
      return regex.test(normalized);
    })) {
      return pagePath;
    }
  }

  return null;
}

function extractTicketId(message) {
  const normalized = normalizeMessage(message);
  const match = normalized.match(/\b(?:tick|ticket|tkt)[- #]*0*(\d{1,3})\b/);
  if (!match) return null;
  return `TICK-${match[1].padStart(3, "0")}`;
}

function snapshotFreshness(snapshot) {
  if (snapshot?.captured_at_unix) {
    return new Date(snapshot.captured_at_unix * 1000).toLocaleString("en-US", {
      dateStyle: "medium",
      timeStyle: "short",
    });
  }
  return null;
}

function capturePrefixForPage(pagePath) {
  if (!pagePath) return null;
  if (pagePath === "/") return "home";
  return pagePath.replace(/^\//, "").replace(/\//g, "_");
}

function latestCaptureForPage(pagePath) {
  const prefix = capturePrefixForPage(pagePath);
  if (!prefix) return null;

  const capturesDir = path.resolve(process.cwd(), "..", "captured_states");
  if (!existsSync(capturesDir)) return null;

  const files = readdirSync(capturesDir)
    .filter((name) => name.startsWith(`${prefix}-`) && name.endsWith(".json"))
    .sort()
    .reverse();

  if (files.length === 0) return null;

  const filePath = path.join(capturesDir, files[0]);
  const stats = statSync(filePath);
  const cacheKey = `${pagePath}:${filePath}`;
  const cached = snapshotCache.get(cacheKey);
  if (cached && cached.mtimeMs === stats.mtimeMs) {
    return cached.snapshot;
  }

  const snapshot = JSON.parse(readFileSync(filePath, "utf8"));
  snapshotCache.set(cacheKey, { mtimeMs: stats.mtimeMs, snapshot });
  return snapshot;
}

function pagePathFromCaptureName(fileName) {
  const baseName = fileName.replace(/-\d{8}-\d{6}\.json$/, "");
  if (baseName === "home") return "/";
  if (baseName === "login") return "/login";
  if (baseName === "privacy") return "/privacy";
  if (baseName === "terms") return "/terms";
  if (baseName.startsWith("dashboard_")) {
    return `/dashboard/${baseName.replace("dashboard_", "").replace(/_/g, "-")}`;
  }
  return `/${baseName.replace(/_/g, "/")}`;
}

function latestCaptureFiles() {
  const capturesDir = path.resolve(process.cwd(), "..", "captured_states");
  if (!existsSync(capturesDir)) return [];

  const latestByPage = new Map();
  readdirSync(capturesDir)
    .filter((name) => name.endsWith(".json"))
    .sort()
    .forEach((name) => {
      latestByPage.set(pagePathFromCaptureName(name), path.join(capturesDir, name));
    });

  return [...latestByPage.entries()].map(([pagePath, filePath]) => ({ pagePath, filePath }));
}

function readSnapshotFile(pagePath, filePath) {
  const stats = statSync(filePath);
  const cacheKey = `${pagePath}:${filePath}`;
  const cached = snapshotCache.get(cacheKey);
  if (cached && cached.mtimeMs === stats.mtimeMs) {
    return cached.snapshot;
  }

  const snapshot = JSON.parse(readFileSync(filePath, "utf8"));
  snapshotCache.set(cacheKey, { mtimeMs: stats.mtimeMs, snapshot });
  return snapshot;
}

function queryTerms(message) {
  return normalizeMessage(message)
    .replace(/[^a-z0-9 -]/g, " ")
    .split(/\s+/)
    .map((term) => term.trim())
    .filter((term) => term.length >= 3 && !STOP_WORDS.has(term));
}

function scoreText(text, terms) {
  const lower = text.toLowerCase();
  return terms.reduce((score, term) => {
    const matches = lower.match(new RegExp(`\\b${term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`, "g"));
    return score + (matches ? matches.length : 0);
  }, 0);
}

function bestLines(text, terms, limit = 8) {
  const seen = new Set();
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length >= 3 && line.length <= 180)
    .filter((line) => {
      const key = line.toLowerCase();
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .map((line, index) => ({ line, index, score: scoreText(line, terms) }))
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score || a.index - b.index)
    .slice(0, limit)
    .map((item) => item.line);
}

function summarizeTickets(snapshot) {
  const text = snapshot.inner_text || "";
  const matches = [...text.matchAll(/(TICK-\d{3})\n([a-z-]+)\n([a-z]+)\n([^\n]+)/g)];
  const unique = [];
  const seen = new Set();

  for (const match of matches) {
    const ticket = {
      id: match[1],
      status: match[2],
      priority: match[3],
      title: match[4],
    };
    if (!seen.has(ticket.id)) {
      seen.add(ticket.id);
      unique.push(ticket);
    }
  }

  if (unique.length === 0) {
    return "I found the latest tickets capture, but could not parse individual ticket cards from it.";
  }

  const openCount = unique.filter((ticket) => ticket.status === "open").length;
  const inProgressCount = unique.filter((ticket) => ticket.status === "in-progress").length;
  const topTickets = unique
    .slice(0, 8)
    .map((ticket) => `- ${ticket.id}: ${ticket.title} (${ticket.status}, ${ticket.priority})`)
    .join("\n");

  const freshness = snapshotFreshness(snapshot);
  const sourceLine = freshness ? `Source: latest Tickets snapshot captured ${freshness}.\n\n` : "";
  return `${sourceLine}I found ${unique.length} visible tickets: ${openCount} open and ${inProgressCount} in progress.\n\nRecent tickets:\n${topTickets}`;
}

function summarizeTicket(snapshot, message) {
  const ticketId = extractTicketId(message);
  if (!ticketId) return null;

  const text = snapshot.inner_text || "";
  const pattern = new RegExp(`${ticketId}\\n([a-z-]+)\\n([a-z]+)\\n([^\\n]+)([\\s\\S]*?)(?=\\nTICK-\\d{3}|\\nCreate New Support Ticket|$)`);
  const match = text.match(pattern);
  if (!match) {
    return `I could not find ${ticketId} in the latest captured Tickets page.`;
  }

  const details = match[4]
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .filter((line) => line !== "View Details");
  const description = details.find((line) => !/^Created |^\d+ replies$|^Updated /.test(line));
  const created = details.find((line) => line.startsWith("Created "));
  const replies = details.find((line) => /^\d+ replies$/.test(line));
  const updated = details.find((line) => line.startsWith("Updated "));

  return [
    snapshotFreshness(snapshot) ? `Source: latest Tickets snapshot captured ${snapshotFreshness(snapshot)}.` : null,
    `${ticketId}: ${match[3]}`,
    `- Status: ${match[1]}`,
    `- Priority: ${match[2]}`,
    description ? `- Details: ${description}` : null,
    created ? `- ${created}` : null,
    replies ? `- ${replies}` : null,
    updated ? `- ${updated}` : null,
  ].filter(Boolean).join("\n");
}

function summarizeFaqs(snapshot) {
  const lines = (snapshot.inner_text || "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  const questions = lines.filter((line) => line.endsWith("?"));

  if (questions.length === 0) {
    return summarizeGenericSnapshot(snapshot, "/dashboard/faqs");
  }

  const freshness = snapshotFreshness(snapshot);
  const sourceLine = freshness ? `Source: latest FAQs snapshot captured ${freshness}.\n\n` : "";
  return `${sourceLine}FAQ questions currently shown:\n${questions.map((question) => `- ${question}`).join("\n")}`;
}

function summarizeGenericSnapshot(snapshot, pagePath) {
  const text = (snapshot.inner_text || "").split("\n").filter(Boolean);
  const title = text.find((line) => line.length > 3 && !line.includes("WaiverPro")) || pagePath;
  const sample = text.slice(0, 18).join("\n");
  return `From the latest captured ${pagePath} page, I found this current page content:\n\n${title}\n\n${sample}`;
}

function summarizeApplications(snapshot) {
  const text = snapshot.inner_text || "";
  const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
  const apps = [];

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const match = line.match(/^([A-Z]{3}-\d{5}-[A-Z0-9]{6})/);
    if (match) {
      const id = match[1];
      const name = lines[i + 1] || "";
      const line3 = lines[i + 2] || "";
      const parts = line3.split("\t").map((p) => p.trim()).filter(Boolean);
      const facility = parts[0] || "";
      const type = parts[1] || "";
      const status = lines[i + 3] || "";
      const date = lines[i + 4] || "";

      apps.push({ id, name, facility, type, status, date });
    }
  }

  if (apps.length === 0) {
    return "I found the latest Applications capture, but could not parse individual application rows.";
  }

  const counts = {};
  apps.forEach((app) => {
    counts[app.status] = (counts[app.status] || 0) + 1;
  });

  const statusSummary = Object.entries(counts)
    .map(([status, count]) => `${count} ${status}`)
    .join(", ");

  const topApps = apps
    .slice(0, 8)
    .map((app) => `- ${app.id}: ${app.name} (${app.status}, Created: ${app.date})`)
    .join("\n");

  const freshness = snapshotFreshness(snapshot);
  const sourceLine = freshness ? `Source: latest Applications snapshot captured ${freshness}.\n\n` : "";
  return `${sourceLine}I found ${apps.length} total applications: ${statusSummary}.\n\nRecent applications:\n${topApps}`;
}

async function getLatestSnapshotTimeFromSupabase(pagePath) {
  const url = process.env.SUPABASE_URL || "";
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY || process.env.SUPABASE_KEY || "";
  if (!url || !key) return null;

  try {
    const queryUrl = `${url}/rest/v1/dashboard_snapshots?select=scraped_at&page_path=eq.${encodeURIComponent(pagePath)}&order=scraped_at.desc&limit=1`;
    const res = await fetch(queryUrl, {
      headers: {
        "apikey": key,
        "Authorization": `Bearer ${key}`
      }
    });
    if (!res.ok) return null;
    const data = await res.json();
    if (data && data.length > 0) {
      return data[0].scraped_at;
    }
  } catch (err) {
    console.error("Error fetching snapshot time from Supabase:", err);
  }
  return null;
}

async function getAllLatestSnapshotTimesFromSupabase() {
  const url = process.env.SUPABASE_URL || "";
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY || process.env.SUPABASE_KEY || "";
  if (!url || !key) return [];

  try {
    const queryUrl = `${url}/rest/v1/dashboard_snapshots?select=page_path,scraped_at&order=scraped_at.desc`;
    const res = await fetch(queryUrl, {
      headers: {
        "apikey": key,
        "Authorization": `Bearer ${key}`
      }
    });
    if (!res.ok) return [];
    const data = await res.json();
    if (data && data.length > 0) {
      const seen = new Set();
      const results = [];
      for (const item of data) {
        if (!seen.has(item.page_path)) {
          seen.add(item.page_path);
          results.push({ pagePath: item.page_path, scrapedAt: item.scraped_at });
        }
      }
      return results;
    }
  } catch (err) {
    console.error("Error fetching all snapshot times from Supabase:", err);
  }
  return [];
}

function formatScrapedAt(scrapedAtStr) {
  if (!scrapedAtStr) return null;
  const date = new Date(scrapedAtStr);
  return date.toLocaleString("en-US", {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

async function getUpdateTimeEvents(message) {
  const normalized = normalizeMessage(message);
  const isUpdateQuery = /(when|last updated|last update|updated|freshness|scraped|captured|latest data)/.test(normalized);
  if (!isUpdateQuery) return null;

  // Let's check if they mentioned a specific page
  let detectedPath = null;
  for (const [pagePath, keywords] of PAGE_KEYWORDS) {
    if (keywords.some((kw) => normalized.includes(kw))) {
      detectedPath = pagePath;
      break;
    }
  }

  let answer = "";
  let latencyMode = "supabase_update_time";

  if (detectedPath) {
    // Try Supabase first
    const scrapedAt = await getLatestSnapshotTimeFromSupabase(detectedPath);
    if (scrapedAt) {
      const freshness = formatScrapedAt(scrapedAt);
      const label = PATH_LABELS[detectedPath] || detectedPath;
      answer = `The **${label}** page was last updated on **${freshness}** (fetched dynamically from Supabase).`;
    } else {
      // Fallback to local files
      const snapshot = latestCaptureForPage(detectedPath);
      if (snapshot) {
        const freshness = snapshotFreshness(snapshot);
        const label = PATH_LABELS[detectedPath] || detectedPath;
        answer = `The **${label}** page was last updated on **${freshness}** (fetched from local cache).`;
        latencyMode = "local_update_time";
      } else {
        answer = `No snapshot record was found for the **${detectedPath}** page in Supabase or local cache. It has not been scraped yet.`;
      }
    }
  } else {
    // Try Supabase first
    const times = await getAllLatestSnapshotTimesFromSupabase();
    if (times.length > 0) {
      const list = times
        .map((t) => {
          const label = PATH_LABELS[t.pagePath] || t.pagePath;
          const freshness = formatScrapedAt(t.scrapedAt);
          return `- **${label}**: ${freshness}`;
        })
        .join("\n");
      answer = `Here are the latest update times for each page from Supabase:\n\n${list}`;
    } else {
      // Fallback to local files
      const files = latestCaptureFiles();
      if (files.length === 0) {
        answer = "No page snapshots have been captured yet. Please run a compliance sweep to fetch live data.";
      } else {
        const summaries = [];
        files.forEach(({ pagePath, filePath }) => {
          try {
            const snapshot = readSnapshotFile(pagePath, filePath);
            const freshness = snapshotFreshness(snapshot);
            if (freshness) {
              const label = PATH_LABELS[pagePath] || pagePath;
              summaries.push({ label, time: freshness, dateObj: new Date(snapshot.captured_at_unix * 1000) });
            }
          } catch (_) {}
        });

        if (summaries.length === 0) {
          answer = "Could not determine the update times of captured snapshots.";
        } else {
          summaries.sort((a, b) => b.dateObj - a.dateObj);
          const list = summaries.map(s => `- **${s.label}**: ${s.time}`).join("\n");
          answer = `Here are the latest update times for each page (fetched from local cache):\n\n${list}`;
          latencyMode = "local_update_time";
        }
      }
    }
  }

  return [
    { type: "intent", intent: "QUERY_CURRENT", page_path: detectedPath, latency_mode: latencyMode },
    { type: "token", data: answer },
    { type: "done", source: "live_data", chunks_used: 1, latency_mode: latencyMode },
    { type: "close", code: 0 },
  ];
}

function getLocalCurrentEvents(message) {
  const pagePath = detectCurrentPage(message);
  if (!pagePath) return null;

  try {
    const snapshot = latestCaptureForPage(pagePath);
    if (!snapshot) return null;

    const answer =
      pagePath === "/dashboard/tickets"
        ? (summarizeTicket(snapshot, message) || summarizeTickets(snapshot))
        : pagePath === "/dashboard/faqs"
          ? summarizeFaqs(snapshot)
          : pagePath === "/dashboard/my-applications"
            ? summarizeApplications(snapshot)
            : summarizeGenericSnapshot(snapshot, pagePath);

    return [
      { type: "intent", intent: "QUERY_CURRENT", page_path: pagePath, latency_mode: "local_snapshot" },
      { type: "token", data: answer },
      { type: "done", source: "live_data", chunks_used: 1, latency_mode: "local_snapshot" },
      { type: "close", code: 0 },
    ];
  } catch (err) {
    return [
      { type: "intent", intent: "QUERY_CURRENT", page_path: pagePath, latency_mode: "local_snapshot" },
      { type: "token", data: `I found a local snapshot for ${pagePath}, but could not read it: ${err.message}` },
      { type: "done", source: "live_data", chunks_used: 0, latency_mode: "local_snapshot" },
      { type: "close", code: 0 },
    ];
  }
}

function getWaiverProSearchEvents(message) {
  const normalized = normalizeMessage(message);
  const waiverProTerms = /(waiverpro|waiver pro|dashboard|ticket|tickets|application|applications|facility|facilities|action|items|user|users|announcement|announcements|settings|faq|faqs|contact|login|privacy|terms|support|profile|notification|notifications|waiver|healthcare)/;
  if (!waiverProTerms.test(normalized)) return null;

  const terms = queryTerms(message);
  if (/(who can help|contact support|support contact|help with support|contact)/.test(normalized) && !/support ticket|tickets/.test(normalized)) {
    try {
      const snapshot = latestCaptureForPage("/dashboard/contact");
      const lines = bestLines(snapshot.inner_text || "", [...terms, "support", "contact"], 8);
      const snippets = lines.length
        ? lines.map((line) => `- ${line}`).join("\n")
        : "- Use the Contact page for WaiverPro support information.";
      return [
        { type: "intent", intent: "QUERY_CURRENT", page_path: "/dashboard/contact", latency_mode: "local_search" },
        {
          type: "token",
          data: `For WaiverPro support/contact help, I found this on Contact (/dashboard/contact):\n\n${snippets}`,
        },
        { type: "done", source: "live_data", chunks_used: 1, latency_mode: "local_search" },
        { type: "close", code: 0 },
      ];
    } catch {
      return null;
    }
  }

  if (terms.length === 0) {
    return [
      { type: "intent", intent: "QUERY_CURRENT", page_path: "/", latency_mode: "local_overview" },
      {
        type: "token",
        data: "WaiverPro is a healthcare waiver management dashboard. From the latest captured pages, it includes My Applications, Facilities, Action Items, User Management, Announcements, Settings, FAQs, Tickets, Contact, Privacy, and Terms areas.",
      },
      { type: "done", source: "live_data", chunks_used: 1, latency_mode: "local_overview" },
      { type: "close", code: 0 },
    ];
  }

  const matches = latestCaptureFiles()
    .map(({ pagePath, filePath }) => {
      try {
        const snapshot = readSnapshotFile(pagePath, filePath);
        const text = snapshot.inner_text || "";
        const pageBoost = scoreText(`${pagePath} ${PATH_LABELS[pagePath] || ""}`, terms) * 2;
        const intentBoost =
          normalized.includes("support") && pagePath === "/dashboard/contact" ? 5 :
          normalized.includes("contact") && pagePath === "/dashboard/contact" ? 5 :
          normalized.includes("notification") && pagePath === "/dashboard/settings" ? 5 :
          0;
        const lines = bestLines(text, terms, 6);
        return {
          pagePath,
          score: intentBoost + pageBoost + scoreText(text, terms),
          lines,
        };
      } catch {
        return null;
      }
    })
    .filter(Boolean)
    .filter((match) => match.score > 0)
    .sort((a, b) => b.score - a.score)
    .slice(0, 3);

  if (matches.length === 0) return null;

  const primary = matches[0];
  const snippets = primary.lines.length > 0
    ? primary.lines.map((line) => `- ${line}`).join("\n")
    : "- I found this topic in the latest page capture, but the matching text was too broad to summarize cleanly.";
  const otherPages = matches.slice(1).map((match) => `${PATH_LABELS[match.pagePath] || match.pagePath} (${match.pagePath})`);
  const also = otherPages.length ? `\n\nAlso related: ${otherPages.join(", ")}` : "";

  return [
    { type: "intent", intent: "QUERY_CURRENT", page_path: primary.pagePath, latency_mode: "local_search" },
    {
      type: "token",
      data: `I found this in the latest WaiverPro snapshot on ${PATH_LABELS[primary.pagePath] || primary.pagePath} (${primary.pagePath}):\n\n${snippets}${also}`,
    },
    { type: "done", source: "live_data", chunks_used: matches.length, latency_mode: "local_search" },
    { type: "close", code: 0 },
  ];
}

function fallbackAnswer(message) {
  const normalized = normalizeMessage(message);
  if (/(should|expected|guideline|guidelines|policy|policies|documentation|docs|requirement|requirements)/.test(normalized)) {
    return "I could not finish the deeper guideline search within the response time limit. I can answer fastest from the latest dashboard scrape, or you can ask a specific current page question like `what tickets are open` or `explain ticket 17`.";
  }

  if (/(ticket|tickets|application|applications|facility|facilities|login|dashboard|user|announcement|settings|faq|contact)/.test(normalized)) {
    return "I do not have an exact local match for that yet. Ask a specific current-data question like `what tickets are open`, `explain ticket 17`, or run a fresh scrape so I can answer from updated dashboard data.";
  }

  return "I can help with WaiverPro dashboard data, compliance guidelines, and scraping tasks. Ask about a specific page, ticket, application, or facility.";
}

export async function GET(request) {
  const { searchParams } = new URL(request.url);
  const message = searchParams.get("message") || "";

  if (!message.trim()) {
    return new Response(JSON.stringify({ error: "No message provided" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  const cacheKey = normalizeMessage(message);

  // If the user wants to trigger a scrape, run it directly in JS and stream logs
  const isScrape = /(scrape|crawl|refresh|rescrape|re-scrape|fetch|pull|sync|update)\b/.test(cacheKey) && 
                   !/(when|last|time|date|how|why|who|what)/.test(cacheKey);
  if (isScrape) {
    let detectedPath = null;
    let isInvalidPath = false;

    // Check if they specified a path explicitly (e.g. /dashboard/facilities or dashboard/facilities)
    const pathMatch = cacheKey.match(/\/dashboard\/[\w-]+/);
    if (pathMatch) {
      const matchedPath = pathMatch[0];
      if (ALL_PAGE_PATHS.includes(matchedPath)) {
        detectedPath = matchedPath;
      } else {
        isInvalidPath = true;
      }
    } else {
      // Otherwise match page keywords
      for (const [pagePath, keywords] of PAGE_KEYWORDS) {
        if (keywords.some((kw) => cacheKey.includes(kw))) {
          detectedPath = pagePath;
          break;
        }
      }
    }

    if (isInvalidPath) {
      return new Response(
        sseFromEvents([
          { type: "intent", intent: "ACTION_SCRAPE", page_path: null, latency_mode: "local_refusal" },
          { type: "token", data: "I can only scrape official WaiverPro dashboard routes. Please specify a valid page, such as `/dashboard/facilities`, `/dashboard/tickets`, or `/dashboard/my-applications`." },
          { type: "done", source: "action", chunks_used: 0, latency_mode: "local_refusal" },
          { type: "close", code: 1 }
        ]),
        sseHeaders()
      );
    }

    const args = ["main.py", "--no-email", "--no-github-issues", "--verbose"];
    if (detectedPath) {
      args.push("--target-path");
      args.push(detectedPath);
    } else {
      // Default to scraping the primary dynamic dashboard paths to save time
      args.push("--target-path"); args.push("/dashboard/my-applications");
      args.push("--target-path"); args.push("/dashboard/facilities");
      args.push("--target-path"); args.push("/dashboard/tickets");
    }

    return new Response(
      new ReadableStream({
        start(controller) {
          let closed = false;
          const pythonCmd = process.platform === "win32" ? "python" : "python3";
          
          controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: "intent", intent: "ACTION_SCRAPE", page_path: detectedPath, latency_mode: "live_scrape" })}\n\n`));
          controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: "token", data: `Starting compliance scraper sweep for ${detectedPath || "all key pages (My Applications, Facilities, Tickets)"}...\n\n` })}\n\n`));

          const child = spawn(pythonCmd, args, {
            cwd: "../",
            env: { ...process.env, PYTHONUNBUFFERED: "1" },
          });

          child.stdout.on("data", (data) => {
            if (closed) return;
            const lines = data.toString().split("\n");
            lines.forEach((line) => {
              if (line.trim()) {
                // Strip timestamps or logging boilerplate for cleaner chatbot printing
                const clean = line.replace(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} (INFO|WARNING|ERROR|DEBUG) \w+ - /, "").trim();
                if (clean) {
                  controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: "token", data: `> _${clean}_\n` })}\n\n`));
                }
              }
            });
          });

          child.stderr.on("data", (data) => {
            if (closed) return;
            const lines = data.toString().split("\n");
            lines.forEach((line) => {
              if (line.trim()) {
                const clean = line.replace(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} (INFO|WARNING|ERROR|DEBUG) \w+ - /, "").trim();
                if (clean) {
                  controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: "token", data: `> _${clean}_\n` })}\n\n`));
                }
              }
            });
          });

          child.on("close", (code) => {
            if (closed) return;
            closed = true;
            const success = code === 0 || code === 2;
            const statusMsg = success 
              ? `\n🎉 **Compliance sweep completed successfully!** Page snapshots have been scraped and indexed into Supabase.`
              : `\n❌ **Scraper failed with exit code ${code}.** Please check the console logs or verify connection credentials.`;
            
            controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: "token", data: statusMsg })}\n\n`));
            controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: "done", source: "action", chunks_used: 1, latency_mode: "live_scrape" })}\n\n`));
            controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: "close", code })}\n\n`));
            controller.close();
          });
        },
      }),
      sseHeaders()
    );
  }

  const cachedEvents = getCachedEvents(cacheKey);
  if (cachedEvents) {
    return sseFromEvents(cachedEvents);
  }

  // Fast-path: Check local JS handlers to resolve queries instantly and bypass Hugging Face LLM latency/timeouts
  const instantEvents = getInstantEvents(message) || await getUpdateTimeEvents(message) || getLocalCurrentEvents(message) || getWaiverProSearchEvents(message);
  if (instantEvents) {
    setCachedEvents(cacheKey, instantEvents);
    return sseFromEvents(instantEvents);
  }

  // Build inline Python script that streams chatbot events as JSON lines
  const pyScript = `
import sys, os, json
sys.path.insert(0, os.path.abspath('src'))
from dotenv import load_dotenv
load_dotenv()
from compliance_agent.chatbot import chat_stream
message = json.loads(sys.argv[1])
for event in chat_stream(message):
    print(json.dumps(event), flush=True)
`;

  const stream = new ReadableStream({
    start(controller) {
      const capturedEvents = [];
      let closed = false;
      const pythonCmd = process.platform === "win32" ? "python" : "python3";
      const child = spawn(
        pythonCmd,
        ["-u", "-c", pyScript, JSON.stringify(message)],
        {
          cwd: "../",
          env: { ...process.env, PYTHONUNBUFFERED: "1" },
        }
      );

      child.stdout.on("data", (data) => {
        if (closed) return;
        const lines = data.toString().split("\n");
        lines.forEach((line) => {
          if (line.trim()) {
            try {
              capturedEvents.push(JSON.parse(line.trim()));
            } catch {
              // Keep streaming even if a line is malformed.
            }
            try {
              controller.enqueue(encoder.encode(`data: ${line.trim()}\n\n`));
            } catch (e) {
              // Stream already closed or aborted
            }
          }
        });
      });

      const timeout = setTimeout(() => {
        if (closed) return;
        closed = true;
        try {
          child.kill("SIGTERM");
        } catch (e) {}

        const fallbackEvents = [
          { type: "token", data: fallbackAnswer(message) },
          { type: "done", source: "general", chunks_used: 0, latency_mode: "timeout_fallback" },
        ];
        
        try {
          fallbackEvents.forEach((event) => {
            controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`));
          });
          controller.close();
        } catch (e) {
          // Stream already closed or aborted by client
        }
      }, CHATBOT_TIMEOUT_MS);

      child.stderr.on("data", (data) => {
        // Log stderr but don't send to client
        const lines = data.toString().split("\n");
        lines.forEach((line) => {
          if (line.trim()) {
            console.log("[chatbot stderr]", line);
          }
        });
      });

      child.on("close", (code) => {
        if (closed) return;
        closed = true;
        clearTimeout(timeout);
        const closeEvent = { type: "close", code };
        if (code === 0) {
          setCachedEvents(cacheKey, [...capturedEvents, closeEvent]);
        }
        try {
          controller.enqueue(
            encoder.encode(
              `data: ${JSON.stringify(closeEvent)}\n\n`
            )
          );
          controller.close();
        } catch (e) {
          // Stream already closed
        }
      });

      child.on("error", (err) => {
        if (closed) return;
        closed = true;
        clearTimeout(timeout);
        try {
          controller.enqueue(
            encoder.encode(
              `data: ${JSON.stringify({ type: "error", error: err.message })}\n\n`
            )
          );
          controller.close();
        } catch (e) {
          // Stream already closed
        }
      });
    },
  });

  return new Response(stream, sseHeaders());
}

// EOF
