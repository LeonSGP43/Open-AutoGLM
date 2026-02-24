import http from "node:http";
import { execFile } from "node:child_process";
import { promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

const __filename = fileURLToPath(import.meta.url);
const ROOT_DIR = path.dirname(__filename);
const REPO_ROOT = path.resolve(ROOT_DIR, "..");
const PUBLIC_DIR = path.join(ROOT_DIR, "public");
const DATA_DIR = path.join(ROOT_DIR, "data");
const DASHBOARD_HTML = path.join(PUBLIC_DIR, "metrics_dashboard.html");
const TOOL_SCRIPT = path.join(ROOT_DIR, "tools", "metrics_dashboard.py");

const PYTHON_BIN = process.env.PYTHON_BIN || "python3";
const PORT = Number.parseInt(process.env.PORT || "5173", 10);
const REFRESH_INTERVAL_MS = Math.max(
  5000,
  Number.parseInt(process.env.REFRESH_INTERVAL_MS || "30000", 10)
);
const RUN_ONCE = process.argv.includes("--once");

const MIME_TYPES = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".txt": "text/plain; charset=utf-8"
};

let refreshInFlight = false;

async function ensureLayout() {
  await fs.mkdir(PUBLIC_DIR, { recursive: true });
  await fs.mkdir(DATA_DIR, { recursive: true });
}

async function ensurePlaceholderHtml() {
  try {
    await fs.access(DASHBOARD_HTML);
    return;
  } catch {
    // fall through
  }
  const placeholder = `<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Monitoring Dashboard</title></head>
<body style="font-family: monospace; padding: 24px;">
  <h1>Monitoring dashboard is initializing...</h1>
  <p>Run <code>npm run dev</code> and wait for refresh output in terminal.</p>
</body>
</html>
`;
  await fs.writeFile(DASHBOARD_HTML, placeholder, "utf-8");
}

function buildRefreshArgs() {
  return [
    TOOL_SCRIPT,
    "--refresh",
    "--reports-dir",
    DATA_DIR,
    "--out-file",
    DASHBOARD_HTML
  ];
}

async function refreshDashboard(trigger) {
  if (refreshInFlight) {
    console.log(`[refresh] skip (${trigger}), previous refresh still running`);
    return;
  }
  refreshInFlight = true;
  const startedAt = Date.now();
  console.log(`[refresh] start (${trigger})`);
  try {
    const { stdout, stderr } = await execFileAsync(PYTHON_BIN, buildRefreshArgs(), {
      cwd: REPO_ROOT,
      maxBuffer: 2 * 1024 * 1024
    });
    if (stdout.trim()) {
      console.log(stdout.trim());
    }
    if (stderr.trim()) {
      console.error(stderr.trim());
    }
    console.log(`[refresh] done in ${Date.now() - startedAt}ms`);
  } catch (error) {
    const stderr = typeof error?.stderr === "string" ? error.stderr.trim() : "";
    const stdout = typeof error?.stdout === "string" ? error.stdout.trim() : "";
    if (stdout) {
      console.log(stdout);
    }
    if (stderr) {
      console.error(stderr);
    }
    console.error(`[refresh] failed in ${Date.now() - startedAt}ms`);
  } finally {
    refreshInFlight = false;
  }
}

function resolvePublicPath(urlPath) {
  let pathname = "/";
  try {
    pathname = decodeURIComponent(String(urlPath || "/").split("?")[0] || "/");
  } catch {
    pathname = "/";
  }
  if (pathname === "/") {
    pathname = "/metrics_dashboard.html";
  }
  const candidate = path.resolve(PUBLIC_DIR, `.${pathname}`);
  const allowedPrefix = `${PUBLIC_DIR}${path.sep}`;
  if (candidate !== PUBLIC_DIR && !candidate.startsWith(allowedPrefix)) {
    return null;
  }
  return candidate;
}

function startHttpServer() {
  const server = http.createServer(async (req, res) => {
    const target = resolvePublicPath(req.url);
    if (!target) {
      res.writeHead(403, { "Content-Type": "text/plain; charset=utf-8" });
      res.end("Forbidden");
      return;
    }
    try {
      const data = await fs.readFile(target);
      const ext = path.extname(target).toLowerCase();
      const contentType = MIME_TYPES[ext] || "application/octet-stream";
      res.writeHead(200, { "Content-Type": contentType, "Cache-Control": "no-store" });
      res.end(data);
    } catch (error) {
      if (error && typeof error === "object" && "code" in error && error.code === "ENOENT") {
        res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
        res.end("Not Found");
        return;
      }
      res.writeHead(500, { "Content-Type": "text/plain; charset=utf-8" });
      res.end("Server Error");
    }
  });
  server.listen(PORT, "127.0.0.1", () => {
    console.log(`Dashboard: http://127.0.0.1:${PORT}`);
    console.log(`Auto refresh interval: ${REFRESH_INTERVAL_MS}ms`);
  });
  return server;
}

async function main() {
  await ensureLayout();
  await ensurePlaceholderHtml();
  await refreshDashboard("startup");

  if (RUN_ONCE) {
    return;
  }

  const server = startHttpServer();
  const timer = setInterval(() => {
    void refreshDashboard("interval");
  }, REFRESH_INTERVAL_MS);

  const shutdown = (signal) => {
    clearInterval(timer);
    server.close(() => {
      console.log(`Stopped on ${signal}`);
      process.exit(0);
    });
  };
  process.on("SIGINT", () => shutdown("SIGINT"));
  process.on("SIGTERM", () => shutdown("SIGTERM"));
}

await main();
