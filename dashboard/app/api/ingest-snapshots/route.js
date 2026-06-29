import { spawn } from "child_process";

export async function GET(request) {
  const { searchParams } = new URL(request.url);
  const pagesStr = searchParams.get("pages") || "";
  const pages = pagesStr.split(",").filter((p) => p.trim());

  const args = ["-u", "-m", "compliance_agent.ingest_snapshots", "--verbose"];
  if (pages.length > 0) {
    args.push("--pages");
    pages.forEach((p) => args.push(p));
  }

  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      const child = spawn("python", args, {
        cwd: "../",
        env: { ...process.env, PYTHONUNBUFFERED: "1", PYTHONPATH: "src" },
      });

      child.stdout.on("data", (data) => {
        const lines = data.toString().split("\n");
        lines.forEach((line) => {
          if (line.trim()) {
            controller.enqueue(
              encoder.encode(`data: ${JSON.stringify({ type: "log", data: line })} \n\n`)
            );
          }
        });
      });

      child.stderr.on("data", (data) => {
        const lines = data.toString().split("\n");
        lines.forEach((line) => {
          if (line.trim()) {
            controller.enqueue(
              encoder.encode(`data: ${JSON.stringify({ type: "log", data: line })} \n\n`)
            );
          }
        });
      });

      child.on("close", (code) => {
        controller.enqueue(
          encoder.encode(`data: ${JSON.stringify({ type: "done", code })} \n\n`)
        );
        controller.close();
      });

      child.on("error", (err) => {
        controller.enqueue(
          encoder.encode(`data: ${JSON.stringify({ type: "error", error: err.message })} \n\n`)
        );
        controller.close();
      });
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      "Connection": "keep-alive",
    },
  });
}
